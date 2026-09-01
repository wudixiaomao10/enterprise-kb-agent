from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Literal

from backend.app.models.knowledge import utc_now


UnitKind = Literal["department", "role"]


class PostgresIdentityProvisioningStore:
    def __init__(self, dsn: str, initialize_schema: bool = False) -> None:
        self.dsn = dsn
        if initialize_schema:
            self._init_schema()

    def get_cursor(self, provider: str, tenant_id: str, resource: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT cursor_url FROM identity_sync_cursors
                WHERE provider = %s AND tenant_id = %s AND resource = %s
                """,
                (provider, tenant_id, resource),
            ).fetchone()
        return row["cursor_url"] if row else None

    def reset_cursors(
        self, provider: str, tenant_id: str, resources: set[str]
    ) -> int:
        if not resources:
            return 0
        with self._connect() as connection:
            return connection.execute(
                """
                DELETE FROM identity_sync_cursors
                WHERE provider = %s AND tenant_id = %s AND resource = ANY(%s)
                """,
                (provider, tenant_id, sorted(resources)),
            ).rowcount

    @contextmanager
    def sync_lock(self, provider: str, tenant_id: str, resource: str):
        lock_key = int.from_bytes(
            hashlib.sha256(f"{provider}:{tenant_id}:{resource}".encode()).digest()[:8],
            byteorder="big",
            signed=True,
        )
        with self._connect() as connection:
            acquired = connection.execute(
                "SELECT pg_try_advisory_lock(%s) AS acquired", (lock_key,)
            ).fetchone()["acquired"]
            try:
                yield bool(acquired)
            finally:
                if acquired:
                    connection.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))

    def apply_graph_user_delta(
        self,
        *,
        source: str,
        tenant_id: str,
        issuer: str,
        users: list[dict[str, Any]],
        delta_link: str,
    ) -> dict[str, int]:
        upserted = 0
        deactivated = 0
        with self._connect() as connection:
            for item in users:
                external_id = required_text(item, "id")
                if "@removed" in item:
                    deactivated += connection.execute(
                        """
                        UPDATE directory_users SET active = false, synced_at = now()
                        WHERE source = %s AND external_id = %s AND active = true
                        """,
                        (source, external_id),
                    ).rowcount
                    continue
                user_id = stable_internal_id("aad", tenant_id, external_id)
                email = optional_text(item.get("mail")) or optional_text(
                    item.get("userPrincipalName")
                )
                connection.execute(
                    """
                    INSERT INTO directory_users (
                        user_id, source, external_id, issuer, subject, email,
                        display_name, active, attributes_json, synced_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, now())
                    ON CONFLICT (source, external_id) DO UPDATE SET
                        issuer = EXCLUDED.issuer,
                        subject = EXCLUDED.subject,
                        email = EXCLUDED.email,
                        display_name = EXCLUDED.display_name,
                        active = EXCLUDED.active,
                        attributes_json = EXCLUDED.attributes_json,
                        synced_at = now()
                    """,
                    (
                        user_id,
                        source,
                        external_id,
                        issuer,
                        external_id,
                        email,
                        optional_text(item.get("displayName")),
                        bool(item.get("accountEnabled", True)),
                        json.dumps(item, ensure_ascii=False),
                    ),
                )
                upserted += 1
            self._save_cursor(
                connection, "microsoft-graph", tenant_id, "users", delta_link
            )
        return {"upserted": upserted, "deactivated": deactivated}

    def apply_graph_group_delta(
        self,
        *,
        source: str,
        tenant_id: str,
        groups: list[dict[str, Any]],
        delta_link: str,
        group_id_map: dict[str, str] | None = None,
        admin_group_ids: set[str] | None = None,
    ) -> dict[str, int]:
        group_id_map = group_id_map or {}
        admin_group_ids = admin_group_ids or set()
        upserted = 0
        deactivated = 0
        memberships_added = 0
        memberships_removed = 0
        with self._connect() as connection:
            for item in groups:
                external_id = required_text(item, "id")
                kind: UnitKind = "role" if external_id in admin_group_ids else "department"
                table, id_column, membership_table, membership_column = unit_tables(kind)
                if "@removed" in item:
                    deactivated += connection.execute(
                        f"""
                        UPDATE {table} SET active = false, synced_at = now()
                        WHERE source = %s AND external_id = %s AND active = true
                        """,
                        (source, external_id),
                    ).rowcount
                    continue
                unit_id = group_id_map.get(external_id)
                if not unit_id:
                    unit_id = "admin" if kind == "role" else stable_internal_id(
                        "aad_group", tenant_id, external_id
                    )
                name = optional_text(item.get("displayName")) or external_id
                connection.execute(
                    f"""
                    INSERT INTO {table} (
                        {id_column}, source, external_id, name, active,
                        attributes_json, synced_at
                    ) VALUES (%s, %s, %s, %s, true, %s::jsonb, now())
                    ON CONFLICT (source, external_id) DO UPDATE SET
                        name = EXCLUDED.name,
                        active = true,
                        attributes_json = EXCLUDED.attributes_json,
                        synced_at = now()
                    """,
                    (
                        unit_id,
                        source,
                        external_id,
                        name,
                        json.dumps(item, ensure_ascii=False),
                    ),
                )
                unit_row = connection.execute(
                    f"SELECT {id_column} FROM {table} WHERE source = %s AND external_id = %s",
                    (source, external_id),
                ).fetchone()
                persisted_unit_id = unit_row[id_column]
                for member in item.get("members@delta", []):
                    member_type = str(member.get("@odata.type", "")).lower()
                    if member_type and not member_type.endswith("user"):
                        continue
                    member_id = optional_text(member.get("id"))
                    if not member_id:
                        continue
                    user = connection.execute(
                        """
                        SELECT user_id FROM directory_users
                        WHERE source = %s AND external_id = %s
                        """,
                        (source, member_id),
                    ).fetchone()
                    if user is None:
                        continue
                    if "@removed" in member:
                        memberships_removed += connection.execute(
                            f"""
                            DELETE FROM {membership_table}
                            WHERE user_id = %s AND {membership_column} = %s
                            """,
                            (user["user_id"], persisted_unit_id),
                        ).rowcount
                    else:
                        memberships_added += connection.execute(
                            f"""
                            INSERT INTO {membership_table} (
                                user_id, {membership_column}, synced_at
                            ) VALUES (%s, %s, now()) ON CONFLICT DO NOTHING
                            """,
                            (user["user_id"], persisted_unit_id),
                        ).rowcount
                upserted += 1
            self._save_cursor(
                connection, "microsoft-graph", tenant_id, "groups", delta_link
            )
        return {
            "upserted": upserted,
            "deactivated": deactivated,
            "memberships_added": memberships_added,
            "memberships_removed": memberships_removed,
        }

    def record_webhook_notification(
        self,
        notification: dict[str, Any],
        *,
        client_state_valid: bool,
    ) -> bool:
        canonical = json.dumps(notification, sort_keys=True, separators=(",", ":"))
        event_id = (
            optional_text(notification.get("id"))
            if client_state_valid
            else f"rejected:{hashlib.sha256(canonical.encode()).hexdigest()}"
        ) or hashlib.sha256(canonical.encode()).hexdigest()
        resource_data = notification.get("resourceData") or {}
        lifecycle_event = optional_text(notification.get("lifecycleEvent"))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO identity_webhook_events (
                    event_id, provider, subscription_id, tenant_id, resource,
                    change_type, lifecycle_event, resource_id, client_state_valid, status,
                    payload_json, received_at
                ) VALUES (%s, 'microsoft-graph', %s, %s, %s, %s, %s, %s, %s, %s,
                          %s::jsonb, now())
                ON CONFLICT (event_id) DO NOTHING
                """,
                (
                    event_id,
                    optional_text(notification.get("subscriptionId")),
                    optional_text(notification.get("tenantId")),
                    optional_text(notification.get("resource")),
                    optional_text(notification.get("changeType")),
                    lifecycle_event,
                    optional_text(resource_data.get("id")),
                    client_state_valid,
                    "queued" if client_state_valid else "rejected",
                    canonical,
                ),
            )
            row = connection.execute(
                "SELECT status FROM identity_webhook_events WHERE event_id = %s",
                (event_id,),
            ).fetchone()
        return bool(row and row["status"] == "queued")

    def record_provider_webhook_event(
        self,
        *,
        provider: str,
        event_id: str,
        tenant_id: str | None,
        resource: str | None,
        payload: dict[str, Any],
    ) -> bool:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            row = connection.execute(
                """
                INSERT INTO identity_webhook_events (
                    event_id, provider, tenant_id, resource, client_state_valid,
                    status, payload_json, received_at
                ) VALUES (%s, %s, %s, %s, true, 'queued', %s::jsonb, now())
                ON CONFLICT (event_id) DO NOTHING
                RETURNING event_id
                """,
                (event_id, provider, tenant_id, resource, canonical),
            ).fetchone()
        return row is not None

    def mark_provider_webhook_event_processed(
        self, provider: str, event_id: str
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE identity_webhook_events
                SET status = 'processed', processed_at = now(), error_message = NULL
                WHERE provider = %s AND event_id = %s AND status = 'queued'
                """,
                (provider, event_id),
            )

    def provider_webhook_status(self, provider: str) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT status, count(*) AS count
                FROM identity_webhook_events
                WHERE provider = %s
                GROUP BY status
                """,
                (provider,),
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def mark_webhook_events_processed(self, resources: set[str]) -> None:
        if not resources:
            return
        patterns = [pattern for resource in resources for pattern in (f"{resource}%", f"/{resource}%")]
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE identity_webhook_events
                SET status = 'processed', processed_at = now()
                WHERE provider = 'microsoft-graph' AND status = 'queued'
                  AND lifecycle_event IS NULL
                  AND (
                      resource LIKE ANY(%s)
                      OR (%s AND resource IS NULL)
                  )
                """,
                (patterns, resources == {"users", "groups"}),
            )

    def mark_lifecycle_events_processed(self, tenant_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE identity_webhook_events
                SET status = 'processed', processed_at = now()
                WHERE provider = 'microsoft-graph' AND tenant_id = %s
                  AND status = 'queued' AND lifecycle_event IS NOT NULL
                """,
                (tenant_id,),
            )

    def list_pending_lifecycle_events(self, tenant_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM identity_webhook_events
                WHERE provider = 'microsoft-graph' AND tenant_id = %s
                  AND status = 'queued' AND lifecycle_event IS NOT NULL
                ORDER BY received_at
                """,
                (tenant_id,),
            ).fetchall()
        return [
            row["payload_json"]
            for row in rows
            if isinstance(row["payload_json"], dict)
        ]

    def save_subscription(
        self, subscription: dict[str, Any], *, tenant_id: str | None = None
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO identity_graph_subscriptions (
                    subscription_id, tenant_id, resource, change_type, expiration_at,
                    payload_json, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, now())
                ON CONFLICT (subscription_id) DO UPDATE SET
                    tenant_id = EXCLUDED.tenant_id,
                    resource = EXCLUDED.resource,
                    change_type = EXCLUDED.change_type,
                    expiration_at = EXCLUDED.expiration_at,
                    payload_json = EXCLUDED.payload_json,
                    updated_at = now()
                """,
                (
                    required_text(subscription, "id"),
                    tenant_id,
                    optional_text(subscription.get("resource")),
                    optional_text(subscription.get("changeType")),
                    subscription.get("expirationDateTime"),
                    json.dumps(subscription, ensure_ascii=False),
                ),
            )

    def list_graph_subscriptions(self, tenant_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT subscription_id, tenant_id, resource, change_type,
                       expiration_at, payload_json, updated_at
                FROM identity_graph_subscriptions
                WHERE tenant_id = %s OR tenant_id IS NULL
                ORDER BY resource, updated_at DESC
                """,
                (tenant_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_graph_subscription(self, subscription_id: str, tenant_id: str) -> bool:
        with self._connect() as connection:
            return (
                connection.execute(
                    """
                    DELETE FROM identity_graph_subscriptions
                    WHERE subscription_id = %s AND (tenant_id = %s OR tenant_id IS NULL)
                    """,
                    (subscription_id, tenant_id),
                ).rowcount
                > 0
            )

    def graph_status(self, tenant_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            cursors = connection.execute(
                """
                SELECT resource, updated_at FROM identity_sync_cursors
                WHERE provider = 'microsoft-graph' AND tenant_id = %s
                ORDER BY resource
                """,
                (tenant_id,),
            ).fetchall()
            events = connection.execute(
                """
                SELECT status, count(*) AS count FROM identity_webhook_events
                WHERE provider = 'microsoft-graph' AND tenant_id = %s GROUP BY status
                """,
                (tenant_id,),
            ).fetchall()
            subscriptions = connection.execute(
                """
                SELECT subscription_id, tenant_id, resource, change_type, expiration_at
                FROM identity_graph_subscriptions
                WHERE tenant_id = %s OR tenant_id IS NULL
                ORDER BY resource, updated_at DESC
                """,
                (tenant_id,),
            ).fetchall()
        return {
            "cursors": [dict(row) for row in cursors],
            "webhook_events": {row["status"]: row["count"] for row in events},
            "subscriptions": [dict(row) for row in subscriptions],
        }

    def scim_upsert_user(
        self, *, source: str, issuer: str, resource: dict[str, Any]
    ) -> dict[str, Any]:
        scim_id = optional_text(resource.get("id")) or uuid.uuid4().hex
        user_name = required_text(resource, "userName")
        subject = optional_text(resource.get("externalId")) or user_name
        email = primary_scim_email(resource)
        now = utc_now()
        normalized = dict(resource)
        normalized.update(
            {
                "id": scim_id,
                "userName": user_name,
                "active": bool(resource.get("active", True)),
            }
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO directory_users (
                    user_id, source, external_id, issuer, subject, email,
                    display_name, active, attributes_json, synced_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, now())
                ON CONFLICT (source, external_id) DO UPDATE SET
                    issuer = EXCLUDED.issuer,
                    subject = EXCLUDED.subject,
                    email = EXCLUDED.email,
                    display_name = EXCLUDED.display_name,
                    active = EXCLUDED.active,
                    attributes_json = EXCLUDED.attributes_json,
                    synced_at = now()
                """,
                (
                    stable_internal_id("scim", source, scim_id),
                    source,
                    scim_id,
                    issuer,
                    subject,
                    email,
                    optional_text(resource.get("displayName")) or user_name,
                    normalized["active"],
                    json.dumps(normalized, ensure_ascii=False),
                ),
            )
        return with_scim_meta(normalized, "User", now)

    def get_scim_user(self, source: str, scim_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT attributes_json, active, synced_at FROM directory_users
                WHERE source = %s AND external_id = %s
                  AND COALESCE(attributes_json->>'_scimDeleted', 'false') <> 'true'
                """,
                (source, scim_id),
            ).fetchone()
        if row is None:
            return None
        resource = decode_json(row["attributes_json"])
        resource["active"] = row["active"]
        return with_scim_meta(resource, "User", row["synced_at"])

    def list_scim_users(self, source: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT attributes_json, active, synced_at FROM directory_users
                WHERE source = %s
                  AND COALESCE(attributes_json->>'_scimDeleted', 'false') <> 'true'
                ORDER BY external_id
                """,
                (source,),
            ).fetchall()
        result = []
        for row in rows:
            resource = decode_json(row["attributes_json"])
            resource["active"] = row["active"]
            result.append(with_scim_meta(resource, "User", row["synced_at"]))
        return result

    def deactivate_scim_user(self, source: str, scim_id: str) -> bool:
        with self._connect() as connection:
            return (
                connection.execute(
                    """
                    UPDATE directory_users SET
                        active = false,
                        attributes_json = jsonb_set(
                            attributes_json, '{_scimDeleted}', 'true'::jsonb, true
                        ),
                        synced_at = now()
                    WHERE source = %s AND external_id = %s
                    """,
                    (source, scim_id),
                ).rowcount
                == 1
            )

    def scim_upsert_group(
        self,
        *,
        source: str,
        resource: dict[str, Any],
        kind: UnitKind = "department",
        mapped_unit_id: str | None = None,
    ) -> dict[str, Any]:
        scim_id = optional_text(resource.get("id")) or uuid.uuid4().hex
        display_name = required_text(resource, "displayName")
        normalized = dict(resource)
        normalized.update({"id": scim_id, "displayName": display_name})
        normalized["_knowledgeKind"] = kind
        now = utc_now()
        table, id_column, membership_table, membership_column = unit_tables(kind)
        opposite_kind: UnitKind = "department" if kind == "role" else "role"
        opposite_table, _, _, _ = unit_tables(opposite_kind)
        unit_id = mapped_unit_id or stable_internal_id(
            f"scim_{kind}", source, scim_id
        )
        with self._connect() as connection:
            connection.execute(
                f"""
                UPDATE {opposite_table} SET active = false, synced_at = now()
                WHERE source = %s AND external_id = %s AND active = true
                """,
                (source, scim_id),
            )
            connection.execute(
                f"""
                INSERT INTO {table} (
                    {id_column}, source, external_id, name, active,
                    attributes_json, synced_at
                ) VALUES (%s, %s, %s, %s, true, %s::jsonb, now())
                ON CONFLICT (source, external_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    active = true,
                    attributes_json = EXCLUDED.attributes_json,
                    synced_at = now()
                """,
                (
                    unit_id,
                    source,
                    scim_id,
                    display_name,
                    json.dumps(normalized, ensure_ascii=False),
                ),
            )
            persisted = connection.execute(
                f"SELECT {id_column} FROM {table} WHERE source = %s AND external_id = %s",
                (source, scim_id),
            ).fetchone()[id_column]
            connection.execute(
                f"DELETE FROM {membership_table} WHERE {membership_column} = %s",
                (persisted,),
            )
            for member in normalized.get("members", []):
                member_id = optional_text(member.get("value"))
                if not member_id:
                    continue
                user = connection.execute(
                    """
                    SELECT user_id FROM directory_users
                    WHERE source = %s AND external_id = %s
                    """,
                    (source, member_id),
                ).fetchone()
                if user:
                    connection.execute(
                        f"""
                        INSERT INTO {membership_table} (
                            user_id, {membership_column}, synced_at
                        ) VALUES (%s, %s, now()) ON CONFLICT DO NOTHING
                        """,
                        (user["user_id"], persisted),
                    )
        return with_scim_meta(normalized, "Group", now)

    def get_scim_group(self, source: str, scim_id: str) -> dict[str, Any] | None:
        for table in ("directory_departments", "directory_roles"):
            with self._connect() as connection:
                row = connection.execute(
                    f"""
                    SELECT attributes_json, active, synced_at FROM {table}
                    WHERE source = %s AND external_id = %s
                      AND COALESCE(attributes_json->>'_scimDeleted', 'false') <> 'true'
                    """,
                    (source, scim_id),
                ).fetchone()
            if row:
                resource = decode_json(row["attributes_json"])
                return with_scim_meta(resource, "Group", row["synced_at"])
        return None

    def list_scim_groups(self, source: str) -> list[dict[str, Any]]:
        resources: list[dict[str, Any]] = []
        with self._connect() as connection:
            for table in ("directory_departments", "directory_roles"):
                rows = connection.execute(
                    f"""
                    SELECT attributes_json, active, synced_at FROM {table}
                    WHERE source = %s
                      AND COALESCE(attributes_json->>'_scimDeleted', 'false') <> 'true'
                    ORDER BY external_id
                    """,
                    (source,),
                ).fetchall()
                for row in rows:
                    resource = decode_json(row["attributes_json"])
                    resources.append(with_scim_meta(resource, "Group", row["synced_at"]))
        return resources

    def deactivate_scim_group(self, source: str, scim_id: str) -> bool:
        updated = 0
        with self._connect() as connection:
            for table in ("directory_departments", "directory_roles"):
                updated += connection.execute(
                    f"""
                    UPDATE {table} SET
                        active = false,
                        attributes_json = jsonb_set(
                            attributes_json, '{{_scimDeleted}}', 'true'::jsonb, true
                        ),
                        synced_at = now()
                    WHERE source = %s AND external_id = %s
                    """,
                    (source, scim_id),
                ).rowcount
        return updated > 0

    def _connect(self):
        from backend.app.database import get_postgres_pool

        return get_postgres_pool(self.dsn).connection()

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(build_provisioning_schema_sql())

    @staticmethod
    def _save_cursor(
        connection,
        provider: str,
        tenant_id: str,
        resource: str,
        cursor_url: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO identity_sync_cursors (
                provider, tenant_id, resource, cursor_url, updated_at
            ) VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (provider, tenant_id, resource) DO UPDATE SET
                cursor_url = EXCLUDED.cursor_url,
                updated_at = now()
            """,
            (provider, tenant_id, resource, cursor_url),
        )


def build_provisioning_schema_sql() -> str:
    return """
    CREATE TABLE IF NOT EXISTS identity_sync_cursors (
        provider TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        resource TEXT NOT NULL,
        cursor_url TEXT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (provider, tenant_id, resource)
    );
    CREATE TABLE IF NOT EXISTS identity_webhook_events (
        event_id TEXT PRIMARY KEY,
        provider TEXT NOT NULL,
        subscription_id TEXT,
        tenant_id TEXT,
        resource TEXT,
        change_type TEXT,
        lifecycle_event TEXT,
        resource_id TEXT,
        client_state_valid BOOLEAN NOT NULL,
        status TEXT NOT NULL,
        payload_json JSONB NOT NULL,
        received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        processed_at TIMESTAMPTZ,
        error_message TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_identity_webhook_status
        ON identity_webhook_events(provider, status, received_at);
    CREATE TABLE IF NOT EXISTS identity_graph_subscriptions (
        subscription_id TEXT PRIMARY KEY,
        tenant_id TEXT,
        resource TEXT,
        change_type TEXT,
        expiration_at TIMESTAMPTZ,
        payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    ALTER TABLE identity_webhook_events
        ADD COLUMN IF NOT EXISTS lifecycle_event TEXT;
    ALTER TABLE identity_graph_subscriptions
        ADD COLUMN IF NOT EXISTS tenant_id TEXT;
    CREATE INDEX IF NOT EXISTS idx_identity_graph_subscriptions_tenant
        ON identity_graph_subscriptions(tenant_id, resource);
    """


def stable_internal_id(prefix: str, namespace: str, external_id: str) -> str:
    digest = hashlib.sha256(f"{namespace}:{external_id}".encode()).hexdigest()[:24]
    return f"{prefix}_{digest}"


def unit_tables(kind: UnitKind) -> tuple[str, str, str, str]:
    if kind == "role":
        return (
            "directory_roles",
            "role_id",
            "directory_user_roles",
            "role_id",
        )
    return (
        "directory_departments",
        "department_id",
        "directory_user_departments",
        "department_id",
    )


def required_text(value: dict[str, Any], key: str) -> str:
    result = optional_text(value.get(key))
    if not result:
        raise ValueError(f"Missing required identity field: {key}")
    return result


def optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def primary_scim_email(resource: dict[str, Any]) -> str | None:
    emails = resource.get("emails") or []
    primary = next((item for item in emails if item.get("primary")), None)
    selected = primary or (emails[0] if emails else None)
    return optional_text(selected.get("value")) if selected else None


def with_scim_meta(
    resource: dict[str, Any], resource_type: str, modified_at: datetime
) -> dict[str, Any]:
    normalized = {
        key: value for key, value in resource.items() if not key.startswith("_")
    }
    schema = f"urn:ietf:params:scim:schemas:core:2.0:{resource_type}"
    normalized["schemas"] = list(dict.fromkeys([schema, *normalized.get("schemas", [])]))
    normalized["meta"] = {
        "resourceType": resource_type,
        "lastModified": modified_at.isoformat(),
        "version": f'W/"{int(modified_at.timestamp())}"',
    }
    return normalized


def decode_json(value: Any) -> dict[str, Any]:
    return json.loads(value) if isinstance(value, str) else dict(value or {})
