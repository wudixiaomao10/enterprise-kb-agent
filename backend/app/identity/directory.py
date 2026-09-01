from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from backend.app.models.knowledge import utc_now


@dataclass(frozen=True)
class DirectoryUser:
    external_id: str
    user_id: str
    subject: str
    issuer: str
    email: str | None = None
    display_name: str | None = None
    active: bool = True
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DirectoryUnit:
    external_id: str
    unit_id: str
    name: str
    active: bool = True
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DirectoryMembership:
    user_external_id: str
    unit_external_id: str


@dataclass(frozen=True)
class DirectorySyncSnapshot:
    source: str
    users: tuple[DirectoryUser, ...]
    departments: tuple[DirectoryUnit, ...] = ()
    roles: tuple[DirectoryUnit, ...] = ()
    user_departments: tuple[DirectoryMembership, ...] = ()
    user_roles: tuple[DirectoryMembership, ...] = ()
    deactivate_missing: bool = True


@dataclass(frozen=True)
class DirectoryIdentity:
    user_id: str
    subject: str
    issuer: str
    email: str | None
    display_name: str | None
    department_ids: tuple[str, ...]
    role_ids: tuple[str, ...]
    source: str


@dataclass(frozen=True)
class DirectorySyncResult:
    run_id: str
    source: str
    user_count: int
    department_count: int
    role_count: int
    user_department_count: int
    user_role_count: int
    deactivated_users: int
    deactivated_departments: int
    deactivated_roles: int
    completed_at: datetime = field(default_factory=utc_now)


class IdentityDirectory(Protocol):
    name: str

    def resolve_user(self, issuer: str, subject: str) -> DirectoryIdentity | None:
        ...

    def sync(self, snapshot: DirectorySyncSnapshot) -> DirectorySyncResult:
        ...

    def status(self) -> dict[str, Any]:
        ...


class InMemoryIdentityDirectory:
    name = "memory-directory"

    def __init__(self) -> None:
        self.users: dict[tuple[str, str], DirectoryUser] = {}
        self.departments: dict[tuple[str, str], DirectoryUnit] = {}
        self.roles: dict[tuple[str, str], DirectoryUnit] = {}
        self.user_departments: dict[str, set[str]] = {}
        self.user_roles: dict[str, set[str]] = {}
        self.last_sync: DirectorySyncResult | None = None

    def resolve_user(self, issuer: str, subject: str) -> DirectoryIdentity | None:
        user = next(
            (
                item
                for item in self.users.values()
                if item.issuer == issuer and item.subject == subject and item.active
            ),
            None,
        )
        if user is None:
            return None
        active_departments = {
            unit.unit_id
            for unit in self.departments.values()
            if unit.active
        }
        active_roles = {unit.unit_id for unit in self.roles.values() if unit.active}
        return DirectoryIdentity(
            user_id=user.user_id,
            subject=user.subject,
            issuer=user.issuer,
            email=user.email,
            display_name=user.display_name,
            department_ids=tuple(
                sorted(self.user_departments.get(user.user_id, set()) & active_departments)
            ),
            role_ids=tuple(sorted(self.user_roles.get(user.user_id, set()) & active_roles)),
            source=next(
                source
                for (source, external_id), item in self.users.items()
                if external_id == user.external_id and item is user
            ),
        )

    def sync(self, snapshot: DirectorySyncSnapshot) -> DirectorySyncResult:
        validate_snapshot(snapshot)
        user_keys = {user.external_id for user in snapshot.users}
        department_keys = {unit.external_id for unit in snapshot.departments}
        role_keys = {unit.external_id for unit in snapshot.roles}
        deactivated_users = self._deactivate_missing_users(
            snapshot.source, user_keys, snapshot.deactivate_missing
        )
        deactivated_departments = self._deactivate_missing_units(
            self.departments,
            snapshot.source,
            department_keys,
            snapshot.deactivate_missing,
        )
        deactivated_roles = self._deactivate_missing_units(
            self.roles,
            snapshot.source,
            role_keys,
            snapshot.deactivate_missing,
        )
        for user in snapshot.users:
            self.users[(snapshot.source, user.external_id)] = user
        for unit in snapshot.departments:
            self.departments[(snapshot.source, unit.external_id)] = unit
        for unit in snapshot.roles:
            self.roles[(snapshot.source, unit.external_id)] = unit

        source_user_ids = (
            {
                user.user_id
                for (source, _), user in self.users.items()
                if source == snapshot.source
            }
            if snapshot.deactivate_missing
            else {user.user_id for user in snapshot.users}
        )
        for user_id in source_user_ids:
            self.user_departments[user_id] = set()
            self.user_roles[user_id] = set()
        users_by_external = {user.external_id: user for user in snapshot.users}
        departments_by_external = {
            unit.external_id: unit for unit in snapshot.departments
        }
        roles_by_external = {unit.external_id: unit for unit in snapshot.roles}
        for membership in snapshot.user_departments:
            user = users_by_external[membership.user_external_id]
            unit = departments_by_external[membership.unit_external_id]
            self.user_departments[user.user_id].add(unit.unit_id)
        for membership in snapshot.user_roles:
            user = users_by_external[membership.user_external_id]
            unit = roles_by_external[membership.unit_external_id]
            self.user_roles[user.user_id].add(unit.unit_id)

        self.last_sync = build_sync_result(
            snapshot,
            deactivated_users,
            deactivated_departments,
            deactivated_roles,
        )
        return self.last_sync

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "active_users": sum(user.active for user in self.users.values()),
            "active_departments": sum(unit.active for unit in self.departments.values()),
            "active_roles": sum(unit.active for unit in self.roles.values()),
            "last_sync": self.last_sync.completed_at.isoformat()
            if self.last_sync
            else None,
        }

    def _deactivate_missing_users(
        self, source: str, external_ids: set[str], enabled: bool
    ) -> int:
        if not enabled:
            return 0
        count = 0
        for key, user in list(self.users.items()):
            if key[0] == source and key[1] not in external_ids and user.active:
                self.users[key] = DirectoryUser(
                    **{**user.__dict__, "active": False}
                )
                count += 1
        return count

    @staticmethod
    def _deactivate_missing_units(
        target: dict[tuple[str, str], DirectoryUnit],
        source: str,
        external_ids: set[str],
        enabled: bool,
    ) -> int:
        if not enabled:
            return 0
        count = 0
        for key, unit in list(target.items()):
            if key[0] == source and key[1] not in external_ids and unit.active:
                target[key] = DirectoryUnit(**{**unit.__dict__, "active": False})
                count += 1
        return count


class PostgresIdentityDirectory:
    name = "postgres-directory"

    def __init__(self, dsn: str, initialize_schema: bool = False) -> None:
        self.dsn = dsn
        if initialize_schema:
            self._init_schema()

    def resolve_user(self, issuer: str, subject: str) -> DirectoryIdentity | None:
        with self._connect() as connection:
            user = connection.execute(
                """
                SELECT user_id, subject, issuer, email, display_name, source
                FROM directory_users
                WHERE issuer = %s AND subject = %s AND active = true
                """,
                (issuer, subject),
            ).fetchone()
            if user is None:
                return None
            departments = connection.execute(
                """
                SELECT d.department_id
                FROM directory_user_departments m
                JOIN directory_departments d ON d.department_id = m.department_id
                WHERE m.user_id = %s AND d.active = true
                ORDER BY d.department_id
                """,
                (user["user_id"],),
            ).fetchall()
            roles = connection.execute(
                """
                SELECT r.role_id
                FROM directory_user_roles m
                JOIN directory_roles r ON r.role_id = m.role_id
                WHERE m.user_id = %s AND r.active = true
                ORDER BY r.role_id
                """,
                (user["user_id"],),
            ).fetchall()
        return DirectoryIdentity(
            user_id=user["user_id"],
            subject=user["subject"],
            issuer=user["issuer"],
            email=user["email"],
            display_name=user["display_name"],
            department_ids=tuple(row["department_id"] for row in departments),
            role_ids=tuple(row["role_id"] for row in roles),
            source=user["source"],
        )

    def sync(self, snapshot: DirectorySyncSnapshot) -> DirectorySyncResult:
        validate_snapshot(snapshot)
        with self._connect() as connection:
            deactivated_users = self._deactivate_missing(
                connection,
                "directory_users",
                snapshot.source,
                [user.external_id for user in snapshot.users],
                snapshot.deactivate_missing,
            )
            deactivated_departments = self._deactivate_missing(
                connection,
                "directory_departments",
                snapshot.source,
                [unit.external_id for unit in snapshot.departments],
                snapshot.deactivate_missing,
            )
            deactivated_roles = self._deactivate_missing(
                connection,
                "directory_roles",
                snapshot.source,
                [unit.external_id for unit in snapshot.roles],
                snapshot.deactivate_missing,
            )
            for user in snapshot.users:
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
                        user.user_id,
                        snapshot.source,
                        user.external_id,
                        user.issuer,
                        user.subject,
                        user.email,
                        user.display_name,
                        user.active,
                        json.dumps(user.attributes, ensure_ascii=False),
                    ),
                )
            for unit in snapshot.departments:
                self._upsert_unit(connection, "department", snapshot.source, unit)
            for unit in snapshot.roles:
                self._upsert_unit(connection, "role", snapshot.source, unit)

            if snapshot.deactivate_missing:
                source_user_ids = connection.execute(
                    "SELECT user_id FROM directory_users WHERE source = %s",
                    (snapshot.source,),
                ).fetchall()
            else:
                source_user_ids = connection.execute(
                    """
                    SELECT user_id FROM directory_users
                    WHERE source = %s AND external_id = ANY(%s)
                    """,
                    (
                        snapshot.source,
                        [user.external_id for user in snapshot.users],
                    ),
                ).fetchall()
            user_ids = [row["user_id"] for row in source_user_ids]
            if user_ids:
                connection.execute(
                    "DELETE FROM directory_user_departments WHERE user_id = ANY(%s)",
                    (user_ids,),
                )
                connection.execute(
                    "DELETE FROM directory_user_roles WHERE user_id = ANY(%s)",
                    (user_ids,),
                )
            users_by_external = self._external_map(
                connection, "directory_users", "user_id", snapshot.source
            )
            departments_by_external = self._external_map(
                connection,
                "directory_departments",
                "department_id",
                snapshot.source,
            )
            roles_by_external = self._external_map(
                connection, "directory_roles", "role_id", snapshot.source
            )
            for membership in snapshot.user_departments:
                connection.execute(
                    """
                    INSERT INTO directory_user_departments (user_id, department_id, synced_at)
                    VALUES (%s, %s, now()) ON CONFLICT DO NOTHING
                    """,
                    (
                        users_by_external[membership.user_external_id],
                        departments_by_external[membership.unit_external_id],
                    ),
                )
            for membership in snapshot.user_roles:
                connection.execute(
                    """
                    INSERT INTO directory_user_roles (user_id, role_id, synced_at)
                    VALUES (%s, %s, now()) ON CONFLICT DO NOTHING
                    """,
                    (
                        users_by_external[membership.user_external_id],
                        roles_by_external[membership.unit_external_id],
                    ),
                )
            result = build_sync_result(
                snapshot,
                deactivated_users,
                deactivated_departments,
                deactivated_roles,
            )
            connection.execute(
                """
                INSERT INTO directory_sync_runs (
                    run_id, source, status, user_count, department_count,
                    role_count, membership_count, completed_at
                ) VALUES (%s, %s, 'completed', %s, %s, %s, %s, %s)
                """,
                (
                    result.run_id,
                    result.source,
                    result.user_count,
                    result.department_count,
                    result.role_count,
                    result.user_department_count + result.user_role_count,
                    result.completed_at,
                ),
            )
        return result

    def status(self) -> dict[str, Any]:
        with self._connect() as connection:
            counts = connection.execute(
                """
                SELECT
                    (SELECT count(*) FROM directory_users WHERE active) AS active_users,
                    (SELECT count(*) FROM directory_departments WHERE active) AS active_departments,
                    (SELECT count(*) FROM directory_roles WHERE active) AS active_roles
                """
            ).fetchone()
            latest = connection.execute(
                """
                SELECT run_id, source, status, completed_at
                FROM directory_sync_runs
                ORDER BY completed_at DESC NULLS LAST, started_at DESC
                LIMIT 1
                """
            ).fetchone()
        return {
            "provider": self.name,
            **dict(counts),
            "last_sync": dict(latest) if latest else None,
        }

    def _connect(self):
        from backend.app.database import get_postgres_pool

        return get_postgres_pool(self.dsn).connection()

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(build_directory_schema_sql())

    @staticmethod
    def _deactivate_missing(
        connection,
        table: str,
        source: str,
        external_ids: list[str],
        enabled: bool,
    ) -> int:
        if not enabled:
            return 0
        cursor = connection.execute(
            f"""
            UPDATE {table}
            SET active = false, synced_at = now()
            WHERE source = %s AND active = true
              AND NOT (external_id = ANY(%s))
            """,
            (source, external_ids),
        )
        return cursor.rowcount

    @staticmethod
    def _upsert_unit(connection, kind: str, source: str, unit: DirectoryUnit) -> None:
        table = f"directory_{kind}s"
        id_column = f"{kind}_id"
        connection.execute(
            f"""
            INSERT INTO {table} (
                {id_column}, source, external_id, name, active,
                attributes_json, synced_at
            ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, now())
            ON CONFLICT (source, external_id) DO UPDATE SET
                name = EXCLUDED.name,
                active = EXCLUDED.active,
                attributes_json = EXCLUDED.attributes_json,
                synced_at = now()
            """,
            (
                unit.unit_id,
                source,
                unit.external_id,
                unit.name,
                unit.active,
                json.dumps(unit.attributes, ensure_ascii=False),
            ),
        )

    @staticmethod
    def _external_map(
        connection, table: str, id_column: str, source: str
    ) -> dict[str, str]:
        rows = connection.execute(
            f"SELECT external_id, {id_column} FROM {table} WHERE source = %s",
            (source,),
        ).fetchall()
        return {row["external_id"]: row[id_column] for row in rows}


def validate_snapshot(snapshot: DirectorySyncSnapshot) -> None:
    if not snapshot.source.strip():
        raise ValueError("Directory source cannot be empty")
    ensure_unique("user external_id", [item.external_id for item in snapshot.users])
    ensure_unique("user_id", [item.user_id for item in snapshot.users])
    ensure_unique("issuer/subject", [(item.issuer, item.subject) for item in snapshot.users])
    ensure_unique(
        "department external_id", [item.external_id for item in snapshot.departments]
    )
    ensure_unique("department_id", [item.unit_id for item in snapshot.departments])
    ensure_unique("role external_id", [item.external_id for item in snapshot.roles])
    ensure_unique("role_id", [item.unit_id for item in snapshot.roles])
    user_ids = {item.external_id for item in snapshot.users}
    department_ids = {item.external_id for item in snapshot.departments}
    role_ids = {item.external_id for item in snapshot.roles}
    for membership in snapshot.user_departments:
        if membership.user_external_id not in user_ids:
            raise ValueError(
                f"Unknown user external_id: {membership.user_external_id}"
            )
        if membership.unit_external_id not in department_ids:
            raise ValueError(
                f"Unknown department external_id: {membership.unit_external_id}"
            )
    for membership in snapshot.user_roles:
        if membership.user_external_id not in user_ids:
            raise ValueError(
                f"Unknown user external_id: {membership.user_external_id}"
            )
        if membership.unit_external_id not in role_ids:
            raise ValueError(f"Unknown role external_id: {membership.unit_external_id}")


def ensure_unique(label: str, values: list[Any]) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"Duplicate {label} in directory snapshot")


def build_sync_result(
    snapshot: DirectorySyncSnapshot,
    deactivated_users: int,
    deactivated_departments: int,
    deactivated_roles: int,
) -> DirectorySyncResult:
    return DirectorySyncResult(
        run_id=f"dirsync_{uuid.uuid4().hex[:16]}",
        source=snapshot.source,
        user_count=len(snapshot.users),
        department_count=len(snapshot.departments),
        role_count=len(snapshot.roles),
        user_department_count=len(snapshot.user_departments),
        user_role_count=len(snapshot.user_roles),
        deactivated_users=deactivated_users,
        deactivated_departments=deactivated_departments,
        deactivated_roles=deactivated_roles,
    )


def build_directory_schema_sql() -> str:
    return """
    CREATE TABLE IF NOT EXISTS directory_users (
        user_id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        external_id TEXT NOT NULL,
        issuer TEXT NOT NULL,
        subject TEXT NOT NULL,
        email TEXT,
        display_name TEXT,
        active BOOLEAN NOT NULL DEFAULT true,
        attributes_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (source, external_id),
        UNIQUE (issuer, subject)
    );
    CREATE TABLE IF NOT EXISTS directory_departments (
        department_id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        external_id TEXT NOT NULL,
        name TEXT NOT NULL,
        active BOOLEAN NOT NULL DEFAULT true,
        attributes_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (source, external_id)
    );
    CREATE TABLE IF NOT EXISTS directory_roles (
        role_id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        external_id TEXT NOT NULL,
        name TEXT NOT NULL,
        active BOOLEAN NOT NULL DEFAULT true,
        attributes_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (source, external_id)
    );
    CREATE TABLE IF NOT EXISTS directory_user_departments (
        user_id TEXT NOT NULL REFERENCES directory_users(user_id),
        department_id TEXT NOT NULL REFERENCES directory_departments(department_id),
        synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (user_id, department_id)
    );
    CREATE TABLE IF NOT EXISTS directory_user_roles (
        user_id TEXT NOT NULL REFERENCES directory_users(user_id),
        role_id TEXT NOT NULL REFERENCES directory_roles(role_id),
        synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (user_id, role_id)
    );
    CREATE TABLE IF NOT EXISTS directory_sync_runs (
        run_id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        status TEXT NOT NULL,
        user_count INTEGER NOT NULL DEFAULT 0,
        department_count INTEGER NOT NULL DEFAULT 0,
        role_count INTEGER NOT NULL DEFAULT 0,
        membership_count INTEGER NOT NULL DEFAULT 0,
        started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        completed_at TIMESTAMPTZ
    );
    CREATE INDEX IF NOT EXISTS idx_directory_users_subject
        ON directory_users(issuer, subject) WHERE active = true;
    CREATE INDEX IF NOT EXISTS idx_directory_users_source
        ON directory_users(source, active);
    """
