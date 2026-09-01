from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.identity.directory import PostgresIdentityDirectory
from backend.app.identity.microsoft_graph import (
    MicrosoftGraphConfig,
    MicrosoftGraphSyncService,
)
from backend.app.identity.provisioning import PostgresIdentityProvisioningStore


class FakeDeltaClient:
    def __init__(self, user_id: str, group_id: str) -> None:
        self.user_id = user_id
        self.group_id = group_id

    def collect_delta(
        self, resource: str, cursor_url: str | None = None
    ) -> tuple[list[dict[str, object]], str, int]:
        if resource == "users":
            items = [
                {
                    "id": self.user_id,
                    "displayName": "Graph Smoke User",
                    "userPrincipalName": f"{self.user_id}@example.test",
                    "accountEnabled": True,
                }
            ]
        else:
            items = [
                {
                    "id": self.group_id,
                    "displayName": "Graph Smoke Group",
                    "members@delta": [
                        {
                            "id": self.user_id,
                            "@odata.type": "#microsoft.graph.user",
                        }
                    ],
                }
            ]
        mode = "incremental" if cursor_url else "initial"
        return items, f"https://graph.microsoft.com/v1.0/{resource}/delta?token={mode}", 1


def main() -> int:
    load_dotenv(override=False)
    load_dotenv(".env.knowledge", override=False)
    dsn = os.environ["KNOWLEDGE_DATABASE_URL"]
    suffix = uuid.uuid4().hex[:10]
    tenant_id = f"smoke-tenant-{suffix}"
    source = f"microsoft-graph-smoke-{suffix}"
    issuer = f"https://login.microsoftonline.com/{tenant_id}/v2.0"
    user_id = f"user-{suffix}"
    group_id = f"group-{suffix}"
    config = MicrosoftGraphConfig(
        enabled=True,
        tenant_id=tenant_id,
        client_id="smoke-client",
        client_secret="smoke-secret",
        source=source,
        issuer=issuer,
        graph_base_url="https://graph.microsoft.com/v1.0",
        token_url=f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
        webhook_url="https://kb.example.test/webhooks/microsoft-graph",
        lifecycle_webhook_url=None,
        client_state="graph-smoke-client-state",
        group_id_map={group_id: "graph-smoke-department"},
        admin_group_ids=frozenset(),
        subscription_minutes=60,
    )
    store = PostgresIdentityProvisioningStore(dsn)
    service = MicrosoftGraphSyncService(
        config,
        store,
        client=FakeDeltaClient(user_id, group_id),  # type: ignore[arg-type]
    )
    try:
        first = service.sync()
        second = service.sync()
        identity = PostgresIdentityDirectory(dsn).resolve_user(issuer, user_id)
        if identity is None or identity.department_ids != ("graph-smoke-department",):
            raise AssertionError(f"Unexpected Graph directory identity: {identity}")
        status = service.status()
        if len(status["cursors"]) != 2:
            raise AssertionError(f"Expected two delta cursors: {status}")
        print(
            json.dumps(
                {
                    "status": "ok",
                    "initial": first,
                    "incremental": second,
                    "resolved_department_ids": list(identity.department_ids),
                    "cursor_resources": [
                        item["resource"] for item in status["cursors"]
                    ],
                },
                indent=2,
                default=str,
            )
        )
        return 0
    finally:
        with psycopg.connect(dsn) as connection:
            connection.execute(
                """
                DELETE FROM directory_user_departments
                WHERE user_id IN (SELECT user_id FROM directory_users WHERE source = %s)
                """,
                (source,),
            )
            connection.execute(
                """
                DELETE FROM directory_user_roles
                WHERE user_id IN (SELECT user_id FROM directory_users WHERE source = %s)
                """,
                (source,),
            )
            connection.execute("DELETE FROM directory_departments WHERE source = %s", (source,))
            connection.execute("DELETE FROM directory_roles WHERE source = %s", (source,))
            connection.execute("DELETE FROM directory_users WHERE source = %s", (source,))
            connection.execute(
                "DELETE FROM identity_sync_cursors WHERE tenant_id = %s",
                (tenant_id,),
            )


if __name__ == "__main__":
    sys.exit(main())
