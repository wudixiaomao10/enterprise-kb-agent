from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx

from backend.app.identity.microsoft_graph import (
    MicrosoftGraphClient,
    MicrosoftGraphConfig,
    MicrosoftGraphSyncService,
)
from backend.app.identity.scim_api import (
    GROUP_SCHEMA,
    PATCH_SCHEMA,
    apply_group_patch,
    apply_user_patch,
)
from backend.app.security.auth import JWTAuthenticator


def graph_config() -> MicrosoftGraphConfig:
    return MicrosoftGraphConfig(
        enabled=True,
        tenant_id="tenant-id",
        client_id="client-id",
        client_secret="client-secret",
        source="microsoft-graph",
        issuer="https://login.microsoftonline.com/tenant-id/v2.0",
        graph_base_url="https://graph.microsoft.com/v1.0",
        token_url=(
            "https://login.microsoftonline.com/tenant-id/oauth2/v2.0/token"
        ),
        webhook_url="https://kb.example.test/webhooks/microsoft-graph",
        lifecycle_webhook_url=None,
        client_state="a-long-webhook-client-state",
        group_id_map={},
        admin_group_ids=frozenset(),
        subscription_minutes=60,
    )


class FakeSubscriptionStore:
    def __init__(self, subscriptions=(), lifecycle_events=()) -> None:
        self.subscriptions = list(subscriptions)
        self.lifecycle_events = list(lifecycle_events)
        self.lifecycle_events_processed = False

    def list_graph_subscriptions(self, tenant_id: str):
        return [
            item
            for item in self.subscriptions
            if item.get("tenant_id") in {None, tenant_id}
        ]

    def save_subscription(self, subscription, *, tenant_id=None) -> None:
        row = {
            "subscription_id": subscription["id"],
            "tenant_id": tenant_id,
            "resource": subscription["resource"],
            "expiration_at": subscription["expirationDateTime"],
        }
        self.subscriptions = [
            item
            for item in self.subscriptions
            if item["subscription_id"] != row["subscription_id"]
        ]
        self.subscriptions.append(row)

    def delete_graph_subscription(self, subscription_id: str, tenant_id: str) -> bool:
        before = len(self.subscriptions)
        self.subscriptions = [
            item
            for item in self.subscriptions
            if item["subscription_id"] != subscription_id
        ]
        return len(self.subscriptions) != before

    def list_pending_lifecycle_events(self, tenant_id: str):
        return list(self.lifecycle_events)

    def mark_lifecycle_events_processed(self, tenant_id: str) -> None:
        self.lifecycle_events_processed = True


class FakeSubscriptionClient:
    def __init__(self) -> None:
        self.created = []
        self.renewed = []
        self.resources = {}

    def create_subscription(self, resource: str):
        subscription = {
            "id": f"{resource}-subscription",
            "resource": f"/{resource}",
            "expirationDateTime": (
                datetime.now(timezone.utc) + timedelta(days=1)
            ).isoformat(),
        }
        self.created.append(resource)
        self.resources[subscription["id"]] = resource
        return subscription

    def renew_subscription(self, subscription_id: str):
        self.renewed.append(subscription_id)
        resource = self.resources[subscription_id]
        return {
            "id": subscription_id,
            "resource": f"/{resource}",
            "expirationDateTime": (
                datetime.now(timezone.utc) + timedelta(days=1)
            ).isoformat(),
        }

    def list_subscriptions(self):
        return []


def subscription_service(store=None, client=None) -> MicrosoftGraphSyncService:
    config = replace(
        graph_config(),
        lifecycle_webhook_url="https://kb.example.test/webhooks/microsoft-graph-lifecycle",
        subscription_renew_before_minutes=15,
    )
    return MicrosoftGraphSyncService(
        config,
        store or FakeSubscriptionStore(),
        client=client or FakeSubscriptionClient(),
    )


class MicrosoftGraphClientTests(unittest.TestCase):
    def test_delta_follows_pages_and_returns_only_final_cursor(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.params.get("page") == "2":
                return httpx.Response(
                    200,
                    json={
                        "value": [{"id": "group-1", "displayName": "Sales"}],
                        "@odata.deltaLink": (
                            "https://graph.microsoft.com/v1.0/groups/delta?$deltatoken=done"
                        ),
                    },
                )
            return httpx.Response(
                200,
                json={
                    "value": [{"id": "group-1", "members@delta": []}],
                    "@odata.nextLink": (
                        "https://graph.microsoft.com/v1.0/groups/delta?page=2"
                    ),
                },
            )

        http_client = httpx.Client(transport=httpx.MockTransport(handler))
        client = MicrosoftGraphClient(
            graph_config(), client=http_client, token_provider=lambda: "token"
        )
        items, cursor, pages = client.collect_delta("groups")

        self.assertEqual(pages, 2)
        self.assertEqual([item["id"] for item in items], ["group-1", "group-1"])
        self.assertIn("$deltatoken=done", cursor)

    def test_delta_rejects_cross_origin_pagination_link(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "value": [],
                    "@odata.nextLink": "https://attacker.example/delta?page=2",
                },
            )

        client = MicrosoftGraphClient(
            graph_config(),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            token_provider=lambda: "token",
        )
        with self.assertRaisesRegex(ValueError, "outside Graph origin"):
            client.collect_delta("users")

    def test_webhook_client_state_uses_exact_match(self) -> None:
        config = graph_config()
        self.assertTrue(
            config.webhook_client_state_matches("a-long-webhook-client-state")
        )
        self.assertFalse(config.webhook_client_state_matches("wrong"))

    def test_subscription_creation_uses_formal_resource_and_lifecycle_url(self) -> None:
        config = replace(
            graph_config(),
            lifecycle_webhook_url="https://kb.example.test/webhooks/microsoft-graph-lifecycle",
        )

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            self.assertEqual(payload["resource"], "/users")
            self.assertEqual(
                payload["lifecycleNotificationUrl"],
                config.lifecycle_webhook_url,
            )
            return httpx.Response(
                201,
                json={
                    "id": "subscription-1",
                    "resource": "/users",
                    "expirationDateTime": "2026-08-31T12:00:00Z",
                },
            )

        client = MicrosoftGraphClient(
            config,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            token_provider=lambda: "token",
        )
        subscription = client.create_subscription("users")

        self.assertEqual(subscription["id"], "subscription-1")

    def test_enabled_config_requires_https_webhook_endpoints(self) -> None:
        config = replace(graph_config(), lifecycle_webhook_url=None)
        with self.assertRaisesRegex(RuntimeError, "LIFECYCLE_WEBHOOK_URL"):
            config.validate_webhook_endpoints()

        invalid = replace(
            graph_config(),
            webhook_url="http://kb.example.test/webhooks/microsoft-graph",
            lifecycle_webhook_url="https://kb.example.test/webhooks/microsoft-graph-lifecycle",
        )
        with self.assertRaisesRegex(RuntimeError, "WEBHOOK_URL must be"):
            invalid.validate_webhook_endpoints()


class MicrosoftGraphSubscriptionTests(unittest.TestCase):
    def test_reconcile_creates_missing_subscriptions_and_is_idempotent(self) -> None:
        store = FakeSubscriptionStore()
        client = FakeSubscriptionClient()
        service = subscription_service(store, client)

        first = service.reconcile_subscriptions()
        second = service.reconcile_subscriptions()

        self.assertEqual(first["status"], "completed")
        self.assertEqual(set(client.created), {"users", "groups"})
        self.assertEqual(second["resources"]["users"]["action"], "healthy")
        self.assertEqual(len(client.created), 2)

    def test_reconcile_renews_subscription_near_expiry(self) -> None:
        store = FakeSubscriptionStore(
            [
                {
                    "subscription_id": "users-subscription",
                    "tenant_id": "tenant-id",
                    "resource": "/users",
                    "expiration_at": (
                        datetime.now(timezone.utc) + timedelta(minutes=5)
                    ).isoformat(),
                }
            ]
        )
        client = FakeSubscriptionClient()
        client.resources["users-subscription"] = "users"
        service = subscription_service(store, client)

        result = service.reconcile_subscriptions(["users"])

        self.assertEqual(result["resources"]["users"]["action"], "renewed")
        self.assertEqual(client.renewed, ["users-subscription"])

    def test_subscription_removed_lifecycle_event_recreates_and_resyncs(self) -> None:
        store = FakeSubscriptionStore(
            [
                {
                    "subscription_id": "users-subscription",
                    "tenant_id": "tenant-id",
                    "resource": "/users",
                    "expiration_at": (
                        datetime.now(timezone.utc) + timedelta(days=1)
                    ).isoformat(),
                }
            ],
            lifecycle_events=[
                {
                    "subscriptionId": "users-subscription",
                    "lifecycleEvent": "subscriptionRemoved",
                }
            ],
        )
        client = FakeSubscriptionClient()
        service = subscription_service(store, client)
        service.sync = lambda resources: {"status": "completed"}  # type: ignore[method-assign]

        result = service.handle_lifecycle_notifications()

        self.assertTrue(result["resynced"])
        self.assertTrue(store.lifecycle_events_processed)
        self.assertEqual(set(client.created), {"users", "groups"})


class SCIMPatchTests(unittest.TestCase):
    def test_group_patch_applies_member_operations_in_order(self) -> None:
        resource = {
            "schemas": [GROUP_SCHEMA],
            "id": "group-1",
            "displayName": "Sales",
            "members": [{"value": "user-1"}],
        }
        patch_document = {
            "schemas": [PATCH_SCHEMA],
            "Operations": [
                {"op": "add", "path": "members", "value": [{"value": "user-2"}]},
                {"op": "remove", "path": 'members[value eq "user-1"]'},
                {"op": "replace", "path": "displayName", "value": "Revenue"},
            ],
        }

        updated = apply_group_patch(resource, patch_document)

        self.assertEqual(updated["displayName"], "Revenue")
        self.assertEqual(updated["members"], [{"value": "user-2"}])

    def test_user_patch_supports_nested_attributes(self) -> None:
        updated = apply_user_patch(
            {"id": "user-1", "userName": "alex@example.com", "active": True},
            {
                "schemas": [PATCH_SCHEMA],
                "Operations": [
                    {"op": "replace", "path": "active", "value": False},
                    {"op": "add", "path": "name.givenName", "value": "Alex"},
                ],
            },
        )
        self.assertFalse(updated["active"])
        self.assertEqual(updated["name"]["givenName"], "Alex")


class OIDCSubjectMappingTests(unittest.TestCase):
    def test_oid_can_be_used_as_directory_subject(self) -> None:
        environment = {
            "KNOWLEDGE_AUTH_MODE": "local",
            "KNOWLEDGE_IDENTITY_MODE": "claims",
            "KNOWLEDGE_AUTH_ALLOW_DEV_TOKEN": "1",
            "KNOWLEDGE_JWT_SECRET": "unit-test-secret-that-is-longer-than-32-characters",
            "KNOWLEDGE_JWT_ISSUER": "issuer",
            "KNOWLEDGE_JWT_AUDIENCE": "audience",
            "KNOWLEDGE_OIDC_SUBJECT_CLAIM": "oid",
        }
        with patch.dict("os.environ", environment, clear=False):
            authenticator = JWTAuthenticator()
            user = authenticator.user_from_claims(
                {"sub": "pairwise-subject", "oid": "graph-object-id", "iss": "issuer"}
            )
        self.assertEqual(user.subject, "graph-object-id")


if __name__ == "__main__":
    unittest.main()
