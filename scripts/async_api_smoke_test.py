from __future__ import annotations

import atexit
import base64
import json
import os
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pdf_ocr_smoke_test import build_scanned_pdf
from storage_directory_smoke_test import cleanup_directory_source
from backend.app.retrieval.providers import load_dotenv_if_available
from backend.app.storage.object_store import create_object_storage


BASE_URL = "http://127.0.0.1:8010"


def cleanup_document(document_id: str, object_storage=None) -> None:
    load_dotenv_if_available()
    if os.getenv("KNOWLEDGE_STORE", "").lower() != "postgres":
        return
    import psycopg

    dsn = os.environ["KNOWLEDGE_DATABASE_URL"]
    with psycopg.connect(dsn) as connection:
        storage_rows = connection.execute(
            "SELECT storage_uri FROM document_versions WHERE document_id = %s",
            (document_id,),
        ).fetchall()
        connection.execute("DELETE FROM citations WHERE document_id = %s", (document_id,))
        connection.execute(
            "DELETE FROM indexing_jobs WHERE document_id = %s", (document_id,)
        )
        connection.execute(
            "DELETE FROM document_chunks WHERE document_id = %s", (document_id,)
        )
        connection.execute(
            "DELETE FROM document_versions WHERE document_id = %s", (document_id,)
        )
        connection.execute("DELETE FROM documents WHERE document_id = %s", (document_id,))

    for (storage_uri,) in storage_rows:
        (object_storage or create_object_storage()).delete(storage_uri)


def cleanup_previous_smoke_documents() -> None:
    load_dotenv_if_available()
    if os.getenv("KNOWLEDGE_STORE", "").lower() != "postgres":
        return
    import psycopg

    with psycopg.connect(os.environ["KNOWLEDGE_DATABASE_URL"]) as connection:
        rows = connection.execute(
            """
            SELECT document_id
            FROM documents
            WHERE (owner_id LIKE 'smoke_sales_%' AND title LIKE 'OCR Policy %')
               OR (owner_id = 'u_sales' AND title LIKE 'SMOKE OCR Policy %')
            """
        ).fetchall()
    for (document_id,) in rows:
        cleanup_document(document_id)


def request_json(
    method: str,
    path: str,
    payload: dict | None = None,
    token: str | None = None,
) -> tuple[int, dict | list]:
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(BASE_URL + path, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        response_body = json.loads(error.read())
        return error.code, response_body


def issue_token(
    user_id: str,
    department: str,
    role_ids: list[str] | None = None,
) -> str:
    status, response = request_json(
        "POST",
        "/auth/dev-token",
        {
            "user_id": user_id,
            "department_ids": [department],
            "role_ids": role_ids or [],
        },
    )
    if status != 200:
        raise RuntimeError(f"Token issue failed: {status} {response}")
    return str(response["access_token"])


def wait_for_job(job_id: str, token: str, timeout_seconds: int = 240) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status, job = request_json("GET", f"/jobs/{job_id}", token=token)
        if status != 200:
            raise RuntimeError(f"Job status failed: {status} {job}")
        if job["status"] == "completed":
            return job
        if job["status"] in {"failed", "cancelled"}:
            raise RuntimeError(f"Indexing job did not complete: {job}")
        time.sleep(2)
    raise TimeoutError(f"Indexing job {job_id} did not finish in time")


def main() -> None:
    cleanup_previous_smoke_documents()
    suffix = uuid.uuid4().hex[:8]
    sales_user = "u_sales"
    hr_user = "u_hr"
    sales_token = issue_token(sales_user, "sales")
    hr_token = issue_token(hr_user, "hr")
    admin_token = issue_token("u_admin", "platform", ["admin"])

    me_status, me = request_json("GET", "/auth/me", token=sales_token)
    if (
        me_status != 200
        or me["department_ids"] != ["sales"]
        or me.get("identity_source") != "local-dev"
    ):
        raise RuntimeError(f"JWT claims were not mapped correctly: {me}")
    unauthenticated_status, _ = request_json("GET", "/documents")
    non_admin_status, _ = request_json("GET", "/admin/pipeline", token=sales_token)
    admin_status, pipeline = request_json("GET", "/admin/pipeline", token=admin_token)
    if unauthenticated_status != 401 or non_admin_status != 403:
        raise RuntimeError("JWT authentication or admin authorization boundary failed")
    if admin_status != 200 or pipeline.get("auth_mode") != "local":
        raise RuntimeError(f"Admin pipeline status failed: {admin_status} {pipeline}")
    if (
        pipeline.get("object_storage") != "s3"
        or pipeline.get("identity_mode") != "directory"
    ):
        raise RuntimeError(f"Production storage/directory mode is not active: {pipeline}")

    directory_source = f"smoke-api-directory-{suffix}"
    load_dotenv_if_available()
    atexit.register(
        cleanup_directory_source,
        os.environ["KNOWLEDGE_DATABASE_URL"],
        directory_source,
    )
    external_subject = f"smoke-api-subject-{suffix}"
    sync_status, sync_result = request_json(
        "POST",
        "/admin/directory/sync",
        {
            "source": directory_source,
            "users": [
                {
                    "external_id": "external-user",
                    "user_id": f"mapped-user-{suffix}",
                    "subject": external_subject,
                    "issuer": "enterprise-kb-agent",
                }
            ],
            "departments": [
                {
                    "external_id": "external-department",
                    "unit_id": f"mapped-department-{suffix}",
                    "name": "Mapped Department",
                }
            ],
            "user_departments": [
                {
                    "user_external_id": "external-user",
                    "unit_external_id": "external-department",
                }
            ],
        },
        admin_token,
    )
    if sync_status != 200 or sync_result.get("user_count") != 1:
        raise RuntimeError(f"Directory API sync failed: {sync_status} {sync_result}")
    mapped_token = issue_token(external_subject, "finance", ["admin"])
    mapped_status, mapped_user = request_json("GET", "/auth/me", token=mapped_token)
    if (
        mapped_status != 200
        or mapped_user["department_ids"] != [f"mapped-department-{suffix}"]
        or mapped_user["role_ids"]
        or mapped_user["identity_source"] != directory_source
    ):
        raise RuntimeError(f"Directory did not replace JWT claims: {mapped_user}")

    pdf_path = Path(".codex-tmp/smoke/scanned-policy.pdf").resolve()
    if not pdf_path.exists():
        build_scanned_pdf(pdf_path)
    upload_status, upload = request_json(
        "POST",
        "/documents/upload",
        {
            "filename": f"smoke-scanned-policy-{suffix}.pdf",
            "title": f"SMOKE OCR Policy {suffix}",
            "department_id": "sales",
            "acl_departments": ["sales"],
            "content_base64": base64.b64encode(pdf_path.read_bytes()).decode(),
        },
        sales_token,
    )
    if upload_status != 202:
        raise RuntimeError(f"Upload failed: {upload_status} {upload}")

    document_id = str(upload["document_id"])
    cleanup_storage = create_object_storage()
    if os.getenv("KNOWLEDGE_SMOKE_KEEP_ARTIFACTS", "0") != "1":
        atexit.register(cleanup_document, document_id, cleanup_storage)
    job = wait_for_job(str(upload["job"]["job_id"]), sales_token)
    if job["result"].get("parser") != "docling":
        raise RuntimeError(f"Async job did not use Docling: {job}")

    query_payload = {"question": "How many days of annual leave are in the OCR policy?"}
    sales_status, sales_answer = request_json(
        "POST", "/chat/query", query_payload, sales_token
    )
    if sales_status != 200 or not any(
        citation["document_id"] == document_id
        for citation in sales_answer.get("citations", [])
    ):
        raise RuntimeError(f"Sales answer did not cite the uploaded PDF: {sales_answer}")

    hr_status, hr_answer = request_json("POST", "/chat/query", query_payload, hr_token)
    if hr_status != 200 or any(
        citation["document_id"] == document_id
        for citation in hr_answer.get("citations", [])
    ):
        raise RuntimeError(f"ACL leakage detected for HR user: {hr_answer}")

    injection_status, _ = request_json(
        "POST",
        "/chat/query",
        {**query_payload, "user_id": sales_user, "department_ids": ["sales"]},
        hr_token,
    )
    if injection_status != 422:
        raise RuntimeError(
            f"Identity fields in request body were not rejected: {injection_status}"
        )

    if os.getenv("KNOWLEDGE_SMOKE_KEEP_ARTIFACTS", "0") != "1":
        cleanup_document(document_id, cleanup_storage)
        atexit.unregister(cleanup_document)

    print(
        {
            "status": "ok",
            "document_id": document_id,
            "job_id": job["job_id"],
            "job_attempts": job["attempts"],
            "parser": job["result"]["parser"],
            "sales_verified": sales_answer["verified"],
            "sales_citation_count": len(sales_answer["citations"]),
            "permission_leak": False,
            "identity_override_rejected": True,
            "unauthenticated_rejected": True,
            "admin_boundary_verified": True,
            "directory_scope_verified": True,
            "object_storage": pipeline["object_storage"],
        }
    )


if __name__ == "__main__":
    main()
