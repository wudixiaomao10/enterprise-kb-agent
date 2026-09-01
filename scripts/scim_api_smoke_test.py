from __future__ import annotations

import json
import os
import sys
import uuid
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import psycopg
from dotenv import load_dotenv


BASE_URL = os.getenv("KNOWLEDGE_API_URL", "http://127.0.0.1:8010").rstrip("/")


def api_request(
    method: str,
    path: str,
    *,
    token: str | None = None,
    payload: dict[str, object] | None = None,
    scim: bool = False,
) -> tuple[int, dict[str, object] | None]:
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/scim+json" if scim else "application/json"
    request = Request(f"{BASE_URL}{path}", data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=20) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise RuntimeError(f"{method} {path} failed: {error.code} {detail}") from error


def main() -> int:
    load_dotenv(override=False)
    load_dotenv(".env.knowledge", override=False)
    scim_token = os.getenv("KNOWLEDGE_SCIM_TOKEN", "")
    if not scim_token:
        raise RuntimeError("KNOWLEDGE_SCIM_TOKEN is required")

    suffix = uuid.uuid4().hex[:10]
    subject = f"scim-smoke-{suffix}"
    user_id = None
    group_id = None
    try:
        user_status, user = api_request(
            "POST",
            "/scim/v2/Users",
            token=scim_token,
            scim=True,
            payload={
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                "externalId": subject,
                "userName": f"{subject}@example.test",
                "displayName": "SCIM Smoke User",
                "active": True,
                "emails": [
                    {
                        "value": f"{subject}@example.test",
                        "type": "work",
                        "primary": True,
                    }
                ],
            },
        )
        assert user is not None
        user_id = str(user["id"])

        group_status, group = api_request(
            "POST",
            "/scim/v2/Groups",
            token=scim_token,
            scim=True,
            payload={
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
                "externalId": f"group-{suffix}",
                "displayName": "SCIM Smoke Department",
                "members": [{"value": user_id}],
            },
        )
        assert group is not None
        group_id = str(group["id"])

        patch_status, patched = api_request(
            "PATCH",
            f"/scim/v2/Groups/{group_id}",
            token=scim_token,
            scim=True,
            payload={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [
                    {
                        "op": "replace",
                        "path": "displayName",
                        "value": "SCIM Smoke Department Updated",
                    }
                ],
            },
        )
        assert patched is not None

        token_status, token_payload = api_request(
            "POST",
            "/auth/dev-token",
            payload={"user_id": subject, "department_ids": [], "role_ids": []},
        )
        assert token_payload is not None
        me_status, me = api_request(
            "GET", "/auth/me", token=str(token_payload["access_token"])
        )
        assert me is not None
        departments = list(me.get("department_ids", []))
        if len(departments) != 1 or me.get("identity_source") != "scim-local":
            raise AssertionError(f"Unexpected directory identity: {me}")

        print(
            json.dumps(
                {
                    "status": "ok",
                    "http_statuses": [
                        user_status,
                        group_status,
                        patch_status,
                        token_status,
                        me_status,
                    ],
                    "scim_user_id": user_id,
                    "scim_group_id": group_id,
                    "resolved_department_ids": departments,
                },
                indent=2,
            )
        )
        return 0
    finally:
        if group_id:
            api_request("DELETE", f"/scim/v2/Groups/{group_id}", token=scim_token)
        if user_id:
            api_request("DELETE", f"/scim/v2/Users/{user_id}", token=scim_token)
        cleanup_test_resources(user_id, group_id)


def cleanup_test_resources(user_scim_id: str | None, group_scim_id: str | None) -> None:
    dsn = os.getenv("KNOWLEDGE_DATABASE_URL", "")
    source = os.getenv("KNOWLEDGE_SCIM_SOURCE", "scim")
    if not dsn or not user_scim_id:
        return
    with psycopg.connect(dsn) as connection:
        connection.execute(
            """
            DELETE FROM directory_user_departments
            WHERE user_id IN (
                SELECT user_id FROM directory_users
                WHERE source = %s AND external_id = %s
            ) OR department_id IN (
                SELECT department_id FROM directory_departments
                WHERE source = %s AND external_id = %s
            )
            """,
            (source, user_scim_id, source, group_scim_id),
        )
        connection.execute(
            """
            DELETE FROM directory_user_roles
            WHERE user_id IN (
                SELECT user_id FROM directory_users
                WHERE source = %s AND external_id = %s
            ) OR role_id IN (
                SELECT role_id FROM directory_roles
                WHERE source = %s AND external_id = %s
            )
            """,
            (source, user_scim_id, source, group_scim_id),
        )
        connection.execute(
            "DELETE FROM directory_departments WHERE source = %s AND external_id = %s",
            (source, group_scim_id),
        )
        connection.execute(
            "DELETE FROM directory_roles WHERE source = %s AND external_id = %s",
            (source, group_scim_id),
        )
        connection.execute(
            "DELETE FROM directory_users WHERE source = %s AND external_id = %s",
            (source, user_scim_id),
        )


if __name__ == "__main__":
    sys.exit(main())
