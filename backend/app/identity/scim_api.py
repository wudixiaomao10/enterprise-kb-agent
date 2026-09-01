from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse, Response
from psycopg.errors import UniqueViolation

from backend.app.identity.provisioning import PostgresIdentityProvisioningStore
from backend.app.retrieval.providers import load_dotenv_if_available


SCIM_CONTENT_TYPE = "application/scim+json"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
FILTER_PATTERN = re.compile(
    r'^\s*([A-Za-z][\w.-]*)\s+eq\s+("(?:[^"\\]|\\.)*")\s*$', re.I
)
MEMBER_FILTER_PATTERN = re.compile(
    r'^members\s*\[\s*value\s+eq\s+("(?:[^"\\]|\\.)*")\s*\]$', re.I
)


@dataclass(frozen=True)
class SCIMConfig:
    enabled: bool
    token: str
    source: str
    issuer: str
    group_id_map: dict[str, str]
    admin_group_ids: frozenset[str]

    @classmethod
    def from_env(cls) -> "SCIMConfig":
        load_dotenv_if_available()
        config = cls(
            enabled=env_bool("KNOWLEDGE_SCIM_ENABLED", False),
            token=os.getenv("KNOWLEDGE_SCIM_TOKEN", "").strip(),
            source=os.getenv("KNOWLEDGE_SCIM_SOURCE", "scim").strip(),
            issuer=os.getenv(
                "KNOWLEDGE_SCIM_OIDC_ISSUER",
                os.getenv(
                    "KNOWLEDGE_OIDC_ISSUER",
                    os.getenv("KNOWLEDGE_JWT_ISSUER", ""),
                ),
            ).strip(),
            group_id_map=parse_json_map("KNOWLEDGE_SCIM_GROUP_ID_MAP"),
            admin_group_ids=frozenset(
                split_csv(os.getenv("KNOWLEDGE_SCIM_ADMIN_GROUP_IDS", ""))
            ),
        )
        if config.enabled:
            if len(config.token) < 32:
                raise RuntimeError("KNOWLEDGE_SCIM_TOKEN must be at least 32 characters")
            if not config.issuer:
                raise RuntimeError("KNOWLEDGE_SCIM_OIDC_ISSUER is required")
        return config


class SCIMException(Exception):
    def __init__(self, status_code: int, detail: str, scim_type: str | None = None):
        self.status_code = status_code
        self.detail = detail
        self.scim_type = scim_type
        super().__init__(detail)


async def scim_exception_handler(_: Request, error: SCIMException) -> JSONResponse:
    payload: dict[str, Any] = {
        "schemas": [ERROR_SCHEMA],
        "status": str(error.status_code),
        "detail": error.detail,
    }
    if error.scim_type:
        payload["scimType"] = error.scim_type
    return scim_response(payload, status_code=error.status_code)


def create_scim_router(
    store: PostgresIdentityProvisioningStore,
    config: SCIMConfig | None = None,
) -> APIRouter:
    resolved = config or SCIMConfig.from_env()
    router = APIRouter(prefix="/scim/v2", tags=["SCIM 2.0"])

    def authorize(request: Request) -> None:
        if not resolved.enabled:
            raise SCIMException(404, "SCIM provisioning is disabled")
        authorization = request.headers.get("Authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(
            token, resolved.token
        ):
            raise SCIMException(401, "Invalid SCIM bearer token")

    auth_dependency = Depends(authorize)

    @router.get("/ServiceProviderConfig")
    def service_provider_config(_: None = auth_dependency) -> JSONResponse:
        return scim_response(
            {
                "schemas": [
                    "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"
                ],
                "patch": {"supported": True},
                "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
                "filter": {"supported": True, "maxResults": 200},
                "changePassword": {"supported": False},
                "sort": {"supported": False},
                "etag": {"supported": True},
                "authenticationSchemes": [
                    {
                        "type": "oauthbearertoken",
                        "name": "Bearer Token",
                        "description": "Static bearer token for IdP provisioning",
                        "specUri": "https://www.rfc-editor.org/rfc/rfc6750",
                        "primary": True,
                    }
                ],
            }
        )

    @router.get("/ResourceTypes")
    def resource_types(_: None = auth_dependency) -> JSONResponse:
        return scim_response(
            list_response(
                [
                    {
                        "schemas": [
                            "urn:ietf:params:scim:schemas:core:2.0:ResourceType"
                        ],
                        "id": "User",
                        "name": "User",
                        "endpoint": "/Users",
                        "schema": USER_SCHEMA,
                    },
                    {
                        "schemas": [
                            "urn:ietf:params:scim:schemas:core:2.0:ResourceType"
                        ],
                        "id": "Group",
                        "name": "Group",
                        "endpoint": "/Groups",
                        "schema": GROUP_SCHEMA,
                    },
                ]
            )
        )

    @router.get("/Schemas")
    def schemas(_: None = auth_dependency) -> JSONResponse:
        return scim_response(
            list_response(
                [
                    schema_resource(
                        "User",
                        USER_SCHEMA,
                        [
                            ("userName", "string", True, "server"),
                            ("externalId", "string", False, "none"),
                            ("displayName", "string", False, "none"),
                            ("active", "boolean", False, "none"),
                            ("emails", "complex", False, "none"),
                        ],
                    ),
                    schema_resource(
                        "Group",
                        GROUP_SCHEMA,
                        [
                            ("displayName", "string", True, "none"),
                            ("externalId", "string", False, "none"),
                            ("members", "complex", False, "none"),
                        ],
                    ),
                ]
            )
        )

    @router.get("/Users")
    def list_users(
        request: Request,
        filter: str | None = None,
        startIndex: int = 1,
        count: int = 100,
        _: None = auth_dependency,
    ) -> JSONResponse:
        resources = filter_resources(
            store.list_scim_users(resolved.source),
            filter,
            {"id", "userName", "externalId"},
        )
        page, start = paginate(resources, startIndex, count)
        return scim_response(
            list_response(
                [with_location(item, request, "Users") for item in page],
                total=len(resources),
                start_index=start,
            )
        )

    @router.post("/Users")
    def create_user(
        request: Request,
        resource: dict[str, Any] = Body(...),
        _: None = auth_dependency,
    ) -> JSONResponse:
        candidate = clean_resource(resource)
        candidate.pop("id", None)
        created = upsert_user(store, resolved, candidate)
        result = with_location(created, request, "Users")
        return scim_response(
            result,
            status_code=201,
            headers=resource_headers(result),
        )

    @router.get("/Users/{scim_id}")
    def get_user(
        request: Request, scim_id: str, _: None = auth_dependency
    ) -> JSONResponse:
        resource = require_resource(store.get_scim_user(resolved.source, scim_id), "User")
        result = with_location(resource, request, "Users")
        return scim_response(result, headers=resource_headers(result))

    @router.put("/Users/{scim_id}")
    def replace_user(
        request: Request,
        scim_id: str,
        resource: dict[str, Any] = Body(...),
        _: None = auth_dependency,
    ) -> JSONResponse:
        require_resource(store.get_scim_user(resolved.source, scim_id), "User")
        candidate = clean_resource(resource)
        candidate["id"] = scim_id
        updated = upsert_user(store, resolved, candidate)
        result = with_location(updated, request, "Users")
        return scim_response(result, headers=resource_headers(result))

    @router.patch("/Users/{scim_id}")
    def patch_user(
        request: Request,
        scim_id: str,
        patch: dict[str, Any] = Body(...),
        _: None = auth_dependency,
    ) -> JSONResponse:
        current = require_resource(
            store.get_scim_user(resolved.source, scim_id), "User"
        )
        updated_resource = apply_user_patch(clean_resource(current), patch)
        updated_resource["id"] = scim_id
        updated = upsert_user(store, resolved, updated_resource)
        result = with_location(updated, request, "Users")
        return scim_response(result, headers=resource_headers(result))

    @router.delete("/Users/{scim_id}")
    def delete_user(scim_id: str, _: None = auth_dependency) -> Response:
        if not store.deactivate_scim_user(resolved.source, scim_id):
            raise SCIMException(404, "User not found")
        return Response(status_code=204)

    @router.get("/Groups")
    def list_groups(
        request: Request,
        filter: str | None = None,
        startIndex: int = 1,
        count: int = 100,
        _: None = auth_dependency,
    ) -> JSONResponse:
        resources = filter_resources(
            store.list_scim_groups(resolved.source),
            filter,
            {"id", "displayName", "externalId"},
        )
        page, start = paginate(resources, startIndex, count)
        return scim_response(
            list_response(
                [with_location(item, request, "Groups") for item in page],
                total=len(resources),
                start_index=start,
            )
        )

    @router.post("/Groups")
    def create_group(
        request: Request,
        resource: dict[str, Any] = Body(...),
        _: None = auth_dependency,
    ) -> JSONResponse:
        candidate = clean_resource(resource)
        candidate.pop("id", None)
        created = upsert_group(store, resolved, candidate)
        result = with_location(created, request, "Groups")
        return scim_response(
            result,
            status_code=201,
            headers=resource_headers(result),
        )

    @router.get("/Groups/{scim_id}")
    def get_group(
        request: Request, scim_id: str, _: None = auth_dependency
    ) -> JSONResponse:
        resource = require_resource(
            store.get_scim_group(resolved.source, scim_id), "Group"
        )
        result = with_location(resource, request, "Groups")
        return scim_response(result, headers=resource_headers(result))

    @router.put("/Groups/{scim_id}")
    def replace_group(
        request: Request,
        scim_id: str,
        resource: dict[str, Any] = Body(...),
        _: None = auth_dependency,
    ) -> JSONResponse:
        current = require_resource(
            store.get_scim_group(resolved.source, scim_id), "Group"
        )
        candidate = clean_resource(resource)
        candidate["id"] = scim_id
        candidate.setdefault("knowledgeKind", current.get("knowledgeKind", "department"))
        updated = upsert_group(store, resolved, candidate)
        result = with_location(updated, request, "Groups")
        return scim_response(result, headers=resource_headers(result))

    @router.patch("/Groups/{scim_id}")
    def patch_group(
        request: Request,
        scim_id: str,
        patch: dict[str, Any] = Body(...),
        _: None = auth_dependency,
    ) -> JSONResponse:
        current = require_resource(
            store.get_scim_group(resolved.source, scim_id), "Group"
        )
        updated_resource = apply_group_patch(clean_resource(current), patch)
        updated_resource["id"] = scim_id
        updated = upsert_group(store, resolved, updated_resource)
        result = with_location(updated, request, "Groups")
        return scim_response(result, headers=resource_headers(result))

    @router.delete("/Groups/{scim_id}")
    def delete_group(scim_id: str, _: None = auth_dependency) -> Response:
        if not store.deactivate_scim_group(resolved.source, scim_id):
            raise SCIMException(404, "Group not found")
        return Response(status_code=204)

    return router


def upsert_group(
    store: PostgresIdentityProvisioningStore,
    config: SCIMConfig,
    resource: dict[str, Any],
) -> dict[str, Any]:
    for member in resource.get("members") or []:
        member_id = str(member.get("value", "")).strip()
        if member_id and store.get_scim_user(config.source, member_id) is None:
            raise SCIMException(
                400,
                f"Group member does not reference an existing User: {member_id}",
                "invalidValue",
            )
    identifiers = {
        str(resource.get("id", "")),
        str(resource.get("externalId", "")),
    }
    kind = (
        "role"
        if identifiers & set(config.admin_group_ids)
        or resource.get("_knowledgeKind") == "role"
        else "department"
    )
    mapped_id = next(
        (config.group_id_map[item] for item in identifiers if item in config.group_id_map),
        None,
    )
    return store.scim_upsert_group(
        source=config.source,
        resource=resource,
        kind=kind,
        mapped_unit_id=mapped_id,
    )


def upsert_user(
    store: PostgresIdentityProvisioningStore,
    config: SCIMConfig,
    resource: dict[str, Any],
) -> dict[str, Any]:
    try:
        return store.scim_upsert_user(
            source=config.source,
            issuer=config.issuer,
            resource=resource,
        )
    except UniqueViolation as error:
        raise SCIMException(
            409, "A User with the same externalId already exists", "uniqueness"
        ) from error


def apply_user_patch(resource: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    validate_patch(patch)
    result = dict(resource)
    for operation in patch["Operations"]:
        op = str(operation.get("op", "")).lower()
        path = operation.get("path")
        value = operation.get("value")
        if not path:
            if op in {"add", "replace"} and isinstance(value, dict):
                result.update(value)
                continue
            raise SCIMException(400, "User PATCH without path requires an object value", "invalidValue")
        if op in {"add", "replace"}:
            set_path(result, canonicalize_path(str(path)), value)
        elif op == "remove":
            remove_path(result, canonicalize_path(str(path)))
        else:
            raise SCIMException(400, f"Unsupported PATCH operation: {op}", "invalidSyntax")
    return result


def apply_group_patch(resource: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    validate_patch(patch)
    result = dict(resource)
    result["members"] = list(resource.get("members") or [])
    for operation in patch["Operations"]:
        op = str(operation.get("op", "")).lower()
        path = str(operation.get("path", "")).strip()
        value = operation.get("value")
        if not path:
            if op in {"add", "replace"} and isinstance(value, dict):
                result.update(value)
                continue
            raise SCIMException(400, "Group PATCH without path requires an object value", "invalidValue")
        member_match = MEMBER_FILTER_PATTERN.match(path)
        if member_match:
            if op != "remove":
                raise SCIMException(400, "Filtered members path only supports remove", "invalidPath")
            remove_id = json.loads(member_match.group(1))
            result["members"] = [
                member for member in result["members"]
                if str(member.get("value", "")) != remove_id
            ]
            continue
        if path.lower() == "members":
            values = value if isinstance(value, list) else ([value] if value else [])
            if op == "add":
                result["members"] = dedupe_members([*result["members"], *values])
            elif op == "replace":
                result["members"] = dedupe_members(values)
            elif op == "remove":
                if values:
                    remove_ids = {str(item.get("value", "")) for item in values}
                    result["members"] = [
                        item for item in result["members"]
                        if str(item.get("value", "")) not in remove_ids
                    ]
                else:
                    result["members"] = []
            else:
                raise SCIMException(400, f"Unsupported PATCH operation: {op}", "invalidSyntax")
            continue
        if op in {"add", "replace"}:
            set_path(result, canonicalize_path(path), value)
        elif op == "remove":
            remove_path(result, canonicalize_path(path))
        else:
            raise SCIMException(400, f"Unsupported PATCH operation: {op}", "invalidSyntax")
    return result


def validate_patch(patch: dict[str, Any]) -> None:
    schemas = patch.get("schemas") or []
    operations = patch.get("Operations")
    if PATCH_SCHEMA not in schemas or not isinstance(operations, list):
        raise SCIMException(400, "Invalid SCIM PatchOp payload", "invalidSyntax")


def set_path(resource: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    target = resource
    for part in parts[:-1]:
        child = target.setdefault(part, {})
        if not isinstance(child, dict):
            raise SCIMException(400, f"Invalid PATCH path: {path}", "invalidPath")
        target = child
    target[parts[-1]] = value


def remove_path(resource: dict[str, Any], path: str) -> None:
    parts = path.split(".")
    target: Any = resource
    for part in parts[:-1]:
        if not isinstance(target, dict) or part not in target:
            return
        target = target[part]
    if isinstance(target, dict):
        target.pop(parts[-1], None)


def filter_resources(
    resources: list[dict[str, Any]],
    expression: str | None,
    allowed_attributes: set[str],
) -> list[dict[str, Any]]:
    if not expression:
        return resources
    match = FILTER_PATTERN.match(expression)
    allowed_by_lower = {item.lower(): item for item in allowed_attributes}
    if not match or match.group(1).lower() not in allowed_by_lower:
        raise SCIMException(400, "Only supported eq filters are allowed", "invalidFilter")
    attribute = allowed_by_lower[match.group(1).lower()]
    expected = str(json.loads(match.group(2)))
    return [
        resource
        for resource in resources
        if str(resource.get(attribute, "")) == expected
    ]


def paginate(
    resources: list[dict[str, Any]], start_index: int, count: int
) -> tuple[list[dict[str, Any]], int]:
    start = max(start_index, 1)
    size = min(max(count, 0), 200)
    offset = start - 1
    return resources[offset : offset + size], start


def list_response(
    resources: list[dict[str, Any]],
    *,
    total: int | None = None,
    start_index: int = 1,
) -> dict[str, Any]:
    return {
        "schemas": [LIST_SCHEMA],
        "totalResults": len(resources) if total is None else total,
        "startIndex": start_index,
        "itemsPerPage": len(resources),
        "Resources": resources,
    }


def schema_resource(
    name: str,
    schema_id: str,
    attributes: list[tuple[str, str, bool, str]],
) -> dict[str, Any]:
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Schema"],
        "id": schema_id,
        "name": name,
        "description": f"SCIM 2.0 {name}",
        "attributes": [
            {
                "name": item_name,
                "type": item_type,
                "multiValued": item_name in {"emails", "members"},
                "required": required,
                "mutability": "readWrite",
                "returned": "default",
                "uniqueness": uniqueness,
            }
            for item_name, item_type, required, uniqueness in attributes
        ],
    }


def clean_resource(resource: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(resource)
    cleaned.pop("meta", None)
    return cleaned


def with_location(
    resource: dict[str, Any], request: Request, collection: str
) -> dict[str, Any]:
    result = dict(resource)
    meta = dict(result.get("meta") or {})
    meta["location"] = (
        f"{str(request.base_url).rstrip('/')}/scim/v2/{collection}/{result['id']}"
    )
    result["meta"] = meta
    return result


def resource_headers(resource: dict[str, Any]) -> dict[str, str]:
    return {
        "Location": str(resource.get("meta", {}).get("location", "")),
        "ETag": str(resource.get("meta", {}).get("version", "")),
    }


def require_resource(
    resource: dict[str, Any] | None, resource_type: str
) -> dict[str, Any]:
    if resource is None:
        raise SCIMException(404, f"{resource_type} not found")
    return resource


def dedupe_members(members: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for member in members:
        if not isinstance(member, dict):
            raise SCIMException(400, "Group members must be objects", "invalidValue")
        value = str(member.get("value", "")).strip()
        if value and value not in seen:
            seen.add(value)
            result.append(dict(member))
    return result


def canonicalize_path(path: str) -> str:
    names = {
        "username": "userName",
        "externalid": "externalId",
        "displayname": "displayName",
        "active": "active",
        "emails": "emails",
        "name": "name",
        "givenname": "givenName",
        "familyname": "familyName",
        "members": "members",
    }
    return ".".join(names.get(part.lower(), part) for part in path.split("."))


def scim_response(
    content: dict[str, Any],
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        content=content,
        status_code=status_code,
        headers=headers,
        media_type=SCIM_CONTENT_TYPE,
    )


def parse_json_map(name: str) -> dict[str, str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError(f"{name} must be a JSON object")
    return {str(key): str(item) for key, item in value.items()}


def split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
