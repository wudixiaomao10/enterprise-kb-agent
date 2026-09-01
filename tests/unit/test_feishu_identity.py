from __future__ import annotations

import base64
import hashlib
import json
import time
import unittest

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from backend.app.identity.directory import InMemoryIdentityDirectory
from backend.app.identity.feishu import (
    FeishuConfig,
    FeishuContactClient,
    FeishuError,
    FeishuSnapshotBuilder,
    FeishuSyncService,
    FeishuWebhookCodec,
    FeishuWebhookError,
    feishu_event_user_id,
)
from backend.app.models.knowledge import (
    ACLEntry,
    Document,
    SourceType,
    SubjectScope,
    SubjectType,
)
from backend.app.security.acl import can_access_document


def feishu_config(**overrides) -> FeishuConfig:
    values = {
        "enabled": True,
        "app_id": "cli_test",
        "app_secret": "secret",
        "source": "feishu-test",
        "issuer": "feishu:cli_test",
        "base_url": "https://open.feishu.cn",
        "root_department_id": "0",
        "verification_token": "verify-token",
        "encrypt_key": "encrypt-key",
        "department_id_map": {"od_sales": "sales"},
        "admin_user_ids": (),
        "webhook_max_age_seconds": 300,
    }
    values.update(overrides)
    return FeishuConfig(**values)


def department(
    external_id: str,
    parent_id: str,
    name: str,
) -> dict[str, object]:
    return {
        "open_department_id": external_id,
        "parent_department_id": parent_id,
        "name": name,
    }


class FakeFeishuClient:
    def __init__(self, departments, users) -> None:
        self.departments = list(departments)
        self.users = {item["open_id"]: dict(item) for item in users}

    def collect_directory(self):
        return self.departments, list(self.users.values())

    def list_departments(self):
        return self.departments

    def get_user(self, user_id: str):
        return dict(self.users[user_id])


class FeishuSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = feishu_config(admin_user_ids=("ou_admin",))
        self.departments = [
            department("od_sales", "0", "Sales"),
            department("od_east", "od_sales", "East"),
            department("od_hangzhou", "od_east", "Hangzhou"),
        ]

    def test_child_department_user_inherits_parent_acl(self) -> None:
        directory = InMemoryIdentityDirectory()
        snapshot = FeishuSnapshotBuilder(self.config).build(
            self.departments,
            [
                {
                    "open_id": "ou_user",
                    "name": "Alice",
                    "department_ids": ["od_hangzhou"],
                    "status": {"is_activated": True},
                }
            ],
            deactivate_missing=True,
        )
        directory.sync(snapshot)

        identity = directory.resolve_user(self.config.issuer, "ou_user")
        self.assertIsNotNone(identity)
        assert identity is not None
        self.assertIn("sales", identity.department_ids)
        self.assertEqual(len(identity.department_ids), 3)

        sales_document = Document(
            document_id="doc-sales",
            title="Sales playbook",
            source_type=SourceType.TEXT,
            owner_id="u_owner",
            department_id="sales",
            acl=[ACLEntry(SubjectType.DEPARTMENT, "sales")],
        )
        scope = SubjectScope(
            identity.user_id,
            identity.department_ids,
            identity.role_ids,
        )
        self.assertTrue(can_access_document(scope, sales_document))

    def test_user_without_department_does_not_receive_department_access(self) -> None:
        directory = InMemoryIdentityDirectory()
        directory.sync(
            FeishuSnapshotBuilder(self.config).build(
                self.departments,
                [{"open_id": "ou_no_department", "name": "Visitor"}],
                deactivate_missing=True,
            )
        )
        identity = directory.resolve_user(self.config.issuer, "ou_no_department")
        self.assertIsNotNone(identity)
        assert identity is not None
        self.assertEqual(identity.department_ids, ())

    def test_admin_mapping_assigns_admin_role(self) -> None:
        directory = InMemoryIdentityDirectory()
        directory.sync(
            FeishuSnapshotBuilder(self.config).build(
                self.departments,
                [{"open_id": "ou_admin", "name": "Admin"}],
                deactivate_missing=True,
            )
        )
        identity = directory.resolve_user(self.config.issuer, "ou_admin")
        self.assertIsNotNone(identity)
        assert identity is not None
        self.assertEqual(identity.role_ids, ("admin",))

    def test_department_cycle_is_rejected(self) -> None:
        cyclic = [
            department("od_a", "od_b", "A"),
            department("od_b", "od_a", "B"),
        ]
        with self.assertRaisesRegex(FeishuError, "cycle"):
            FeishuSnapshotBuilder(self.config).build(
                cyclic,
                [
                    {
                        "open_id": "ou_user",
                        "department_ids": ["od_a"],
                    }
                ],
                deactivate_missing=True,
            )


class FeishuContactClientTests(unittest.TestCase):
    def test_collect_directory_paginates_and_merges_memberships(self) -> None:
        calls: list[tuple[str, str, dict[str, str]]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            calls.append((request.method, request.url.path, params))
            if request.url.path.endswith("/tenant_access_token/internal"):
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "tenant_access_token": "tenant-token",
                        "expire": 7200,
                    },
                )
            self.assertEqual(request.headers["Authorization"], "Bearer tenant-token")
            if request.url.path.endswith("/departments/0/children"):
                if params.get("page_token") == "next":
                    return httpx.Response(
                        200,
                        json={
                            "code": 0,
                            "data": {
                                "items": [department("od_east", "od_sales", "East")],
                                "has_more": False,
                            },
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "items": [department("od_sales", "0", "Sales")],
                            "has_more": True,
                            "page_token": "next",
                        },
                    },
                )
            if request.url.path.endswith("/users/find_by_department"):
                department_id = params["department_id"]
                users = {
                    "0": [],
                    "od_sales": [
                        {
                            "open_id": "ou_alice",
                            "name": "Alice",
                            "department_ids": ["od_sales"],
                        }
                    ],
                    "od_east": [
                        {
                            "open_id": "ou_alice",
                            "name": "Alice",
                            "department_ids": ["od_east"],
                        }
                    ],
                }[department_id]
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"items": users, "has_more": False}},
                )
            raise AssertionError(f"Unexpected request: {request.url}")

        client = FeishuContactClient(
            feishu_config(),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            sleep=lambda _: None,
        )
        departments, users = client.collect_directory()

        self.assertEqual(len(departments), 2)
        self.assertEqual(len(users), 1)
        self.assertEqual(
            set(users[0]["department_ids"]),
            {"od_sales", "od_east"},
        )
        token_calls = [path for _, path, _ in calls if "access_token" in path]
        self.assertEqual(len(token_calls), 1)


class FeishuSyncServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = feishu_config()
        self.departments = [department("od_sales", "0", "Sales")]
        self.users = [
            {
                "open_id": "ou_alice",
                "name": "Alice",
                "department_ids": ["od_sales"],
            },
            {
                "open_id": "ou_bob",
                "name": "Bob",
                "department_ids": ["od_sales"],
            },
        ]

    def test_user_sync_preserves_other_users(self) -> None:
        directory = InMemoryIdentityDirectory()
        client = FakeFeishuClient(self.departments, self.users)
        service = FeishuSyncService(
            self.config,
            directory,
            client=client,  # type: ignore[arg-type]
        )
        service.sync()
        client.users["ou_alice"]["name"] = "Alice Updated"

        result = service.sync("ou_alice")

        self.assertEqual(result["mode"], "user")
        self.assertIsNotNone(directory.resolve_user(self.config.issuer, "ou_bob"))
        alice = directory.resolve_user(self.config.issuer, "ou_alice")
        self.assertIsNotNone(alice)
        assert alice is not None
        self.assertEqual(alice.display_name, "Alice Updated")

    def test_deleted_user_event_deactivates_user(self) -> None:
        directory = InMemoryIdentityDirectory()
        service = FeishuSyncService(
            self.config,
            directory,
            client=FakeFeishuClient(self.departments, self.users),  # type: ignore[arg-type]
        )
        service.sync()

        result = service.sync("ou_alice", force_inactive=True)

        self.assertEqual(result["mode"], "user")
        self.assertIsNone(directory.resolve_user(self.config.issuer, "ou_alice"))
        self.assertIsNotNone(directory.resolve_user(self.config.issuer, "ou_bob"))

    def test_contact_events_are_deduplicated(self) -> None:
        service = FeishuSyncService(
            self.config,
            InMemoryIdentityDirectory(),
            client=FakeFeishuClient([], []),  # type: ignore[arg-type]
        )
        payload = {
            "header": {
                "event_id": "evt_1",
                "event_type": "contact.user.updated_v3",
            },
            "event": {"object": {"open_id": "ou_alice"}},
        }

        accepted = service.accept_event(payload)
        duplicate = service.accept_event(payload)

        self.assertEqual(accepted["status"], "queued")
        self.assertEqual(accepted["user_id"], "ou_alice")
        self.assertEqual(duplicate["status"], "duplicate")

    def test_department_event_queues_full_sync(self) -> None:
        service = FeishuSyncService(
            self.config,
            InMemoryIdentityDirectory(),
            client=FakeFeishuClient([], []),  # type: ignore[arg-type]
        )
        accepted = service.accept_event(
            {
                "header": {
                    "event_id": "evt_department",
                    "event_type": "contact.department.updated_v3",
                },
                "event": {},
            }
        )
        self.assertEqual(accepted["status"], "queued")
        self.assertIsNone(accepted["user_id"])

    def test_event_user_id_supports_nested_user(self) -> None:
        payload = {
            "event": {
                "object": {
                    "user_id": {
                        "open_id": "ou_nested",
                        "user_id": "user_123",
                        "union_id": "on_nested",
                    }
                }
            }
        }
        self.assertEqual(feishu_event_user_id(payload), "ou_nested")


class FeishuWebhookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = feishu_config()
        self.codec = FeishuWebhookCodec(self.config)

    def test_url_verification_checks_verification_token(self) -> None:
        payload = {
            "type": "url_verification",
            "token": self.config.verification_token,
            "challenge": "challenge-value",
        }
        raw = json.dumps(payload).encode()

        decoded = self.codec.decode(raw, {})

        self.assertEqual(decoded["challenge"], "challenge-value")

    def test_wrong_verification_token_is_rejected(self) -> None:
        raw = json.dumps(
            {
                "type": "url_verification",
                "token": "wrong-token",
                "challenge": "challenge-value",
            }
        ).encode()
        with self.assertRaisesRegex(FeishuWebhookError, "verification token"):
            self.codec.decode(raw, {})

    def test_encrypted_signed_event_is_decrypted(self) -> None:
        payload = {
            "schema": "2.0",
            "header": {
                "event_id": "evt_encrypted",
                "event_type": "contact.user.created_v3",
                "token": self.config.verification_token,
            },
            "event": {"object": {"open_id": "ou_new"}},
        }
        encrypted = encrypt_payload(payload, self.config.encrypt_key)
        raw = json.dumps({"encrypt": encrypted}, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        nonce = "nonce-value"
        signature = sign_request(
            timestamp,
            nonce,
            self.config.encrypt_key,
            raw,
        )

        decoded = self.codec.decode(
            raw,
            {
                "X-Lark-Request-Timestamp": timestamp,
                "X-Lark-Request-Nonce": nonce,
                "X-Lark-Signature": signature,
            },
        )

        self.assertEqual(decoded["header"]["event_id"], "evt_encrypted")

    def test_invalid_signature_is_rejected(self) -> None:
        payload = {
            "header": {
                "event_id": "evt_invalid",
                "event_type": "contact.user.updated_v3",
                "token": self.config.verification_token,
            },
            "event": {},
        }
        raw = json.dumps({"encrypt": encrypt_payload(payload, self.config.encrypt_key)}).encode()
        with self.assertRaisesRegex(FeishuWebhookError, "signature"):
            self.codec.decode(
                raw,
                {
                    "X-Lark-Request-Timestamp": str(int(time.time())),
                    "X-Lark-Request-Nonce": "nonce",
                    "X-Lark-Signature": "invalid",
                },
            )

    def test_stale_request_is_rejected(self) -> None:
        payload = {
            "header": {
                "event_id": "evt_stale",
                "event_type": "contact.user.updated_v3",
                "token": self.config.verification_token,
            },
            "event": {},
        }
        raw = json.dumps({"encrypt": encrypt_payload(payload, self.config.encrypt_key)}).encode()
        timestamp = str(int(time.time()) - self.config.webhook_max_age_seconds - 1)
        with self.assertRaisesRegex(FeishuWebhookError, "Expired"):
            self.codec.decode(
                raw,
                {
                    "X-Lark-Request-Timestamp": timestamp,
                    "X-Lark-Request-Nonce": "nonce",
                    "X-Lark-Signature": sign_request(
                        timestamp,
                        "nonce",
                        self.config.encrypt_key,
                        raw,
                    ),
                },
            )


def encrypt_payload(payload: dict[str, object], encrypt_key: str) -> str:
    iv = bytes(range(AES.block_size))
    key = hashlib.sha256(encrypt_key.encode()).digest()
    plain = json.dumps(payload, separators=(",", ":")).encode()
    ciphertext = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plain, AES.block_size))
    return base64.b64encode(iv + ciphertext).decode()


def sign_request(timestamp: str, nonce: str, encrypt_key: str, body: bytes) -> str:
    return hashlib.sha256(
        timestamp.encode() + nonce.encode() + encrypt_key.encode() + body
    ).hexdigest()


if __name__ == "__main__":
    unittest.main()
