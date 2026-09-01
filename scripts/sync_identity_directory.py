from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.identity.directory import (
    DirectoryMembership,
    DirectorySyncSnapshot,
    DirectoryUnit,
    DirectoryUser,
    PostgresIdentityDirectory,
    validate_snapshot,
)
from backend.app.retrieval.providers import load_dotenv_if_available


def snapshot_from_payload(payload: dict) -> DirectorySyncSnapshot:
    return DirectorySyncSnapshot(
        source=str(payload["source"]),
        users=tuple(
            DirectoryUser(
                external_id=str(item["external_id"]),
                user_id=str(item["user_id"]),
                subject=str(item["subject"]),
                issuer=str(item["issuer"]),
                email=item.get("email"),
                display_name=item.get("display_name"),
                active=bool(item.get("active", True)),
                attributes=dict(item.get("attributes", {})),
            )
            for item in payload.get("users", [])
        ),
        departments=tuple(
            DirectoryUnit(
                external_id=str(item["external_id"]),
                unit_id=str(item["unit_id"]),
                name=str(item["name"]),
                active=bool(item.get("active", True)),
                attributes=dict(item.get("attributes", {})),
            )
            for item in payload.get("departments", [])
        ),
        roles=tuple(
            DirectoryUnit(
                external_id=str(item["external_id"]),
                unit_id=str(item["unit_id"]),
                name=str(item["name"]),
                active=bool(item.get("active", True)),
                attributes=dict(item.get("attributes", {})),
            )
            for item in payload.get("roles", [])
        ),
        user_departments=tuple(
            DirectoryMembership(
                str(item["user_external_id"]), str(item["unit_external_id"])
            )
            for item in payload.get("user_departments", [])
        ),
        user_roles=tuple(
            DirectoryMembership(
                str(item["user_external_id"]), str(item["unit_external_id"])
            )
            for item in payload.get("user_roles", [])
        ),
        deactivate_missing=bool(payload.get("deactivate_missing", True)),
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate and sync a user/department/role directory snapshot"
    )
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    payload = json.loads(args.snapshot.read_text(encoding="utf-8"))
    snapshot = snapshot_from_payload(payload)
    validate_snapshot(snapshot)
    if args.dry_run:
        print(
            {
                "status": "valid",
                "source": snapshot.source,
                "users": len(snapshot.users),
                "departments": len(snapshot.departments),
                "roles": len(snapshot.roles),
            }
        )
        return

    load_dotenv_if_available()
    result = PostgresIdentityDirectory(
        os.environ["KNOWLEDGE_DATABASE_URL"]
    ).sync(snapshot)
    print(
        {
            "status": "ok",
            "run_id": result.run_id,
            "source": result.source,
            "users": result.user_count,
            "departments": result.department_count,
            "roles": result.role_count,
            "deactivated_users": result.deactivated_users,
        }
    )


if __name__ == "__main__":
    main()
