from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import jwt
from pypdf import PdfWriter

from backend.app.ingestion.parser import DocumentParseError, parse_document
from backend.app.ingestion.service import DocumentIngestionService
from backend.app.identity.directory import (
    DirectoryMembership,
    DirectorySyncSnapshot,
    DirectoryUnit,
    DirectoryUser,
    InMemoryIdentityDirectory,
)
from backend.app.jobs.models import IndexJobStatus
from backend.app.jobs.repository import InMemoryIndexJobRepository
from backend.app.jobs.service import DeferredInlineDispatcher, IndexJobService
from backend.app.models.knowledge import ACLEntry, Permission, SubjectType
from backend.app.preview.pdf import (
    PDFPreviewError,
    locate_pdf_chunk,
    render_pdf_page,
)
from backend.app.repositories.memory_store import InMemoryKnowledgeStore
from backend.app.security.auth import JWTAuthenticator, auth_error
from backend.app.security.headers import browser_security_headers, trusted_origin
from backend.app.storage.object_store import (
    LocalObjectStorage,
    ObjectStorageError,
    S3ObjectStorage,
)


AUTH_ENV = {
    "KNOWLEDGE_AUTH_MODE": "local",
    "KNOWLEDGE_AUTH_ALLOW_DEV_TOKEN": "1",
    "KNOWLEDGE_JWT_SECRET": "unit-test-secret-that-is-longer-than-32-characters",
    "KNOWLEDGE_JWT_ISSUER": "unit-test-issuer",
    "KNOWLEDGE_JWT_AUDIENCE": "unit-test-audience",
    "KNOWLEDGE_JWT_LEEWAY_SECONDS": "0",
    "KNOWLEDGE_IDENTITY_MODE": "claims",
}


class JWTAuthenticationTests(unittest.TestCase):
    def test_unauthorized_response_is_stable_and_does_not_leak_token_details(self) -> None:
        error = auth_error()

        self.assertEqual(error.status_code, 401)
        self.assertEqual(error.headers["WWW-Authenticate"], "Bearer")
        self.assertEqual(
            error.detail,
            {
                "code": "authentication_required",
                "message": "Authentication required",
            },
        )

    def test_browser_security_policy_allows_only_configured_oidc_origin(self) -> None:
        headers = browser_security_headers(
            "https://login.microsoftonline.com/tenant-id/v2.0"
        )

        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertIn(
            "connect-src 'self' https://login.microsoftonline.com",
            headers["Content-Security-Policy"],
        )
        self.assertIn("frame-ancestors 'self'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Frame-Options"], "SAMEORIGIN")
        self.assertEqual(trusted_origin("http://login.example.com/tenant"), "")

    def test_oidc_scope_is_required_before_directory_resolution(self) -> None:
        with patch.dict("os.environ", AUTH_ENV, clear=False):
            authenticator = JWTAuthenticator()

        authenticator.validate_oidc_claims({"scp": "openid access_as_user"})
        with self.assertRaises(jwt.InvalidTokenError):
            authenticator.validate_oidc_claims({"scp": "openid profile"})

    def test_only_configured_app_role_maps_to_internal_admin(self) -> None:
        env = {**AUTH_ENV, "KNOWLEDGE_OIDC_TRUSTED_ADMIN_APP_ROLE": "Knowledge.Admin"}
        with patch.dict("os.environ", env, clear=False):
            authenticator = JWTAuthenticator()
        user = authenticator.user_from_claims(
            {"sub": "user-1", "iss": "unit-test-issuer", "role_ids": []}
        )

        unchanged = authenticator.apply_trusted_app_roles(
            user, {"roles": ["Unrelated.Role"]}
        )
        promoted = authenticator.apply_trusted_app_roles(
            user, {"roles": ["Knowledge.Admin"]}
        )

        self.assertNotIn("admin", unchanged.role_ids)
        self.assertIn("admin", promoted.role_ids)

    def test_local_token_maps_verified_claims_to_acl_scope(self) -> None:
        with patch.dict("os.environ", AUTH_ENV, clear=False):
            authenticator = JWTAuthenticator()
            token, ttl = authenticator.issue_local_token(
                user_id="u_sales",
                department_ids=["sales", "sales"],
                role_ids=["manager"],
                email="sales@example.com",
            )
            user = authenticator.authenticate(token)

        self.assertEqual(ttl, 3600)
        self.assertEqual(user.user_id, "u_sales")
        self.assertEqual(user.scope.department_ids, ("sales",))
        self.assertEqual(user.scope.role_ids, ("manager",))

    def test_wrong_audience_and_expired_tokens_are_rejected(self) -> None:
        with patch.dict("os.environ", AUTH_ENV, clear=False):
            authenticator = JWTAuthenticator()
            now = datetime.now(timezone.utc)
            base_claims = {
                "sub": "u_sales",
                "iss": authenticator.issuer,
                "iat": now - timedelta(minutes=10),
                "exp": now + timedelta(minutes=10),
            }
            wrong_audience = jwt.encode(
                {**base_claims, "aud": "another-api"},
                authenticator.secret,
                algorithm="HS256",
            )
            expired = jwt.encode(
                {
                    **base_claims,
                    "aud": authenticator.audience,
                    "exp": now - timedelta(minutes=1),
                },
                authenticator.secret,
                algorithm="HS256",
            )

            with self.assertRaises(jwt.InvalidAudienceError):
                authenticator.authenticate(wrong_audience)
            with self.assertRaises(jwt.ExpiredSignatureError):
                authenticator.authenticate(expired)

    def test_directory_scope_replaces_untrusted_claim_scope(self) -> None:
        directory = InMemoryIdentityDirectory()
        directory.sync(
            DirectorySyncSnapshot(
                source="test-idp",
                users=(
                    DirectoryUser(
                        external_id="external-user",
                        user_id="internal-user",
                        subject="external-user",
                        issuer="unit-test-issuer",
                    ),
                ),
                departments=(
                    DirectoryUnit("hr-external", "hr", "Human Resources"),
                ),
                user_departments=(
                    DirectoryMembership("external-user", "hr-external"),
                ),
            )
        )
        env = {**AUTH_ENV, "KNOWLEDGE_IDENTITY_MODE": "directory"}
        with patch.dict("os.environ", env, clear=False):
            authenticator = JWTAuthenticator(directory)
            token, _ = authenticator.issue_local_token(
                user_id="external-user",
                department_ids=["sales"],
                role_ids=["admin"],
            )
            user = authenticator.authenticate(token)

        self.assertEqual(user.user_id, "internal-user")
        self.assertEqual(user.department_ids, ("hr",))
        self.assertEqual(user.role_ids, ())
        self.assertEqual(user.identity_source, "test-idp")


class IndexJobTests(unittest.TestCase):
    def test_inline_job_indexes_registered_document(self) -> None:
        store = InMemoryKnowledgeStore()
        ingestion = DocumentIngestionService(
            store,
            Path(".codex-tmp/unit-tests/jobs"),
        )
        registration = ingestion.register_document(
            filename="async.md",
            raw_bytes="# 异步索引\n\n任务执行完成后生成可检索的 chunk。".encode(),
            title="异步索引",
            owner_id="u_platform",
            department_id="platform",
            acl=[ACLEntry(SubjectType.DEPARTMENT, "platform", Permission.READ)],
        )
        service = IndexJobService(
            InMemoryIndexJobRepository(),
            ingestion,
            DeferredInlineDispatcher(),
        )

        job = service.submit(
            document_id=str(registration["document_id"]),
            version_id=str(registration["version_id"]),
            requested_by="u_platform",
        )

        self.assertEqual(job.status, IndexJobStatus.COMPLETED)
        self.assertEqual(job.progress, 100)
        self.assertEqual(job.attempts, 1)
        self.assertGreater(job.result["chunk_count"], 0)


class IdentityDirectoryTests(unittest.TestCase):
    def test_incremental_sync_preserves_unmentioned_user_memberships(self) -> None:
        directory = InMemoryIdentityDirectory()
        department = DirectoryUnit("sales-ext", "sales", "Sales")
        users = (
            DirectoryUser("user-a", "u_a", "sub-a", "issuer"),
            DirectoryUser("user-b", "u_b", "sub-b", "issuer"),
        )
        directory.sync(
            DirectorySyncSnapshot(
                source="idp",
                users=users,
                departments=(department,),
                user_departments=(
                    DirectoryMembership("user-a", "sales-ext"),
                    DirectoryMembership("user-b", "sales-ext"),
                ),
            )
        )

        directory.sync(
            DirectorySyncSnapshot(
                source="idp",
                users=(users[0],),
                departments=(department,),
                user_departments=(
                    DirectoryMembership("user-a", "sales-ext"),
                ),
                deactivate_missing=False,
            )
        )

        user_b = directory.resolve_user("issuer", "sub-b")
        self.assertIsNotNone(user_b)
        assert user_b is not None
        self.assertEqual(user_b.department_ids, ("sales",))


class ProductionParserTests(unittest.TestCase):
    def test_plain_text_parser_preserves_parser_metadata(self) -> None:
        blocks = parse_document("policy.md", b"# Policy\n\nEmployees receive leave.")

        self.assertEqual(blocks[0].metadata["parser"], "plain-text")

    def test_pypdf_rejects_image_or_blank_pdf_without_ocr(self) -> None:
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        payload = BytesIO()
        writer.write(payload)

        with patch.dict(
            "os.environ",
            {
                "KNOWLEDGE_PDF_PARSER": "pypdf",
                "KNOWLEDGE_PDF_MIN_TEXT_CHARS": "20",
            },
            clear=False,
        ):
            with self.assertRaises(DocumentParseError):
                parse_document("scan.pdf", payload.getvalue())


class PDFPreviewTests(unittest.TestCase):
    def test_page_render_and_text_location(self) -> None:
        import pymupdf

        document = pymupdf.open()
        page = document.new_page(width=420, height=594)
        expected = "PDF citation preview supports exact page navigation."
        page.insert_text((48, 80), expected, fontsize=12)
        raw_bytes = document.tobytes()
        document.close()

        rendered = render_pdf_page(raw_bytes, 1)
        location = locate_pdf_chunk(raw_bytes, 1, expected)

        self.assertTrue(rendered.png_bytes.startswith(b"\x89PNG"))
        self.assertEqual(rendered.page_count, 1)
        self.assertEqual(location.match_method, "text-search")
        self.assertGreater(len(location.highlights), 0)
        self.assertTrue(all(0 <= item.x <= 1 for item in location.highlights))

    def test_docling_bbox_is_preferred_and_page_range_is_validated(self) -> None:
        import pymupdf

        document = pymupdf.open()
        document.new_page(width=200, height=300)
        raw_bytes = document.tobytes()
        document.close()

        location = locate_pdf_chunk(
            raw_bytes,
            1,
            "OCR text without a PDF text layer",
            {"bbox_norms": [[0.1, 0.2, 0.8, 0.3]]},
        )

        self.assertEqual(location.match_method, "docling-bbox")
        self.assertEqual(len(location.highlights), 1)
        self.assertAlmostEqual(location.highlights[0].width, 0.7)
        with self.assertRaises(PDFPreviewError):
            render_pdf_page(raw_bytes, 2)


class FakeS3Body(BytesIO):
    pass


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, **kwargs):
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = kwargs["Body"]
        return {"ETag": "test"}

    def get_object(self, **kwargs):
        return {"Body": FakeS3Body(self.objects[(kwargs["Bucket"], kwargs["Key"])])}

    def delete_object(self, **kwargs):
        self.objects.pop((kwargs["Bucket"], kwargs["Key"]), None)


class ObjectStorageTests(unittest.TestCase):
    def test_s3_round_trip_and_materialization(self) -> None:
        client = FakeS3Client()
        storage = S3ObjectStorage(
            bucket="knowledge",
            client=client,
            materialize_dir=Path(".codex-tmp/unit-tests/materialized"),
        )

        uri = storage.put("policy.pdf", b"pdf-content")

        self.assertTrue(uri.startswith("s3://knowledge/documents/"))
        self.assertEqual(storage.read(uri), b"pdf-content")
        with storage.materialize(uri) as path:
            self.assertEqual(path.suffix, ".pdf")
            self.assertEqual(path.read_bytes(), b"pdf-content")
        self.assertFalse(path.exists())
        storage.delete(uri)
        self.assertEqual(client.objects, {})

    def test_storage_rejects_wrong_bucket_and_local_path_escape(self) -> None:
        storage = S3ObjectStorage(bucket="knowledge", client=FakeS3Client())
        with self.assertRaises(ObjectStorageError):
            storage.read("s3://another-bucket/document.pdf")

        local = LocalObjectStorage(Path(".codex-tmp/unit-tests/storage-root"))
        with self.assertRaises(ObjectStorageError):
            local.read(str(Path("README.md").resolve()))


if __name__ == "__main__":
    unittest.main()
