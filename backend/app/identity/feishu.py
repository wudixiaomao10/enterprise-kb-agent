from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from urllib.parse import quote, urlparse

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from backend.app.identity.directory import (
    DirectoryMembership,
    DirectorySyncSnapshot,
    DirectoryUnit,
    DirectoryUser,
    IdentityDirectory,
)
from backend.app.identity.provisioning import (
    PostgresIdentityProvisioningStore,
    stable_internal_id,
)


FEISHU_PROVIDER = "feishu"
FEISHU_USER_EVENTS = {
    "contact.user.created_v3",
    "contact.user.updated_v3",
    "contact.user.deleted_v3",
}
FEISHU_DEPARTMENT_EVENTS = {
    "contact.department.created_v3",
    "contact.department.updated_v3",
    "contact.department.deleted_v3",
    "contact.scope.updated_v3",
}
FEISHU_CONTACT_EVENTS = FEISHU_USER_EVENTS | FEISHU_DEPARTMENT_EVENTS
TOKEN_ERROR_CODES = {99991661, 99991663, 99991664}


class FeishuError(RuntimeError):
    pass


class FeishuWebhookError(FeishuError):
    pass


@dataclass(frozen=True)
class FeishuConfig:
    enabled: bool
    app_id: str
    app_secret: str
    source: str
    issuer: str
    base_url: str
    root_department_id: str
    verification_token: str
    encrypt_key: str
    department_id_map: dict[str, str]
    admin_user_ids: tuple[str, ...]
    webhook_max_age_seconds: int = 300

    @classmethod
    def from_env(cls) -> "FeishuConfig":
        app_id = os.getenv("KNOWLEDGE_FEISHU_APP_ID", "").strip()
        return cls(
            enabled=env_bool("KNOWLEDGE_FEISHU_ENABLED", False),
            app_id=app_id,
            app_secret=os.getenv("KNOWLEDGE_FEISHU_APP_SECRET", "").strip(),
            source=os.getenv("KNOWLEDGE_FEISHU_SOURCE", "feishu-contact").strip(),
            issuer=os.getenv(
                "KNOWLEDGE_FEISHU_ISSUER", f"feishu:{app_id}" if app_id else "feishu"
            ).strip(),
            base_url=os.getenv(
                "KNOWLEDGE_FEISHU_BASE_URL", "https://open.feishu.cn"
            ).strip().rstrip("/"),
            root_department_id=os.getenv(
                "KNOWLEDGE_FEISHU_ROOT_DEPARTMENT_ID", "0"
            ).strip(),
            verification_token=os.getenv(
                "KNOWLEDGE_FEISHU_VERIFICATION_TOKEN", ""
            ).strip(),
            encrypt_key=os.getenv("KNOWLEDGE_FEISHU_ENCRYPT_KEY", "").strip(),
            department_id_map=parse_json_map("KNOWLEDGE_FEISHU_DEPARTMENT_ID_MAP"),
            admin_user_ids=tuple(
                split_csv(os.getenv("KNOWLEDGE_FEISHU_ADMIN_USER_IDS", ""))
            ),
            webhook_max_age_seconds=env_int(
                "KNOWLEDGE_FEISHU_WEBHOOK_MAX_AGE_SECONDS", 300
            ),
        )

    def validate_api(self) -> None:
        if not self.enabled:
            raise FeishuError("Feishu directory synchronization is disabled")
        if not self.app_id or not self.app_secret:
            raise FeishuError("Feishu App ID and App Secret are required")
        if not self.source or not self.issuer:
            raise FeishuError("Feishu source and issuer are required")
        if not self.root_department_id:
            raise FeishuError("Feishu root department ID is required")
        parsed = urlparse(self.base_url)
        if parsed.scheme.lower() != "https" or not parsed.netloc:
            raise FeishuError("Feishu base URL must be an absolute HTTPS URL")

    def validate_webhook(self) -> None:
        self.validate_api()
        if not self.verification_token:
            raise FeishuWebhookError("Feishu verification token is required")
        if self.webhook_max_age_seconds < 30:
            raise FeishuWebhookError(
                "Feishu webhook replay window must be at least 30 seconds"
            )


class FeishuContactClient:
    def __init__(
        self,
        config: FeishuConfig,
        *,
        client: httpx.Client | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self.config = config
        self.client = client or httpx.Client(timeout=30.0)
        self.sleep = sleep
        self._access_token: str | None = None
        self._token_expires_at = 0.0

    def collect_directory(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        departments = self.list_departments()
        department_ids = [
            item_id
            for item in departments
            if (item_id := department_external_id(item)) is not None
        ]
        users_by_id: dict[str, dict[str, Any]] = {}
        for department_id in [self.config.root_department_id, *department_ids]:
            for user in self.list_users_by_department(department_id):
                user_id = feishu_user_id(user)
                if not user_id:
                    continue
                current = users_by_id.get(user_id)
                if current is None:
                    current = dict(user)
                    current["department_ids"] = list(
                        dict.fromkeys(string_list(user.get("department_ids")))
                    )
                    users_by_id[user_id] = current
                direct = list(current.get("department_ids", []))
                if department_id != self.config.root_department_id:
                    direct.append(department_id)
                current["department_ids"] = list(dict.fromkeys(direct))
        return departments, list(users_by_id.values())

    def list_departments(self) -> list[dict[str, Any]]:
        path = (
            "/open-apis/contact/v3/departments/"
            f"{quote(self.config.root_department_id, safe='')}/children"
        )
        return self._collect_items(
            path,
            params={
                "department_id_type": "open_department_id",
                "fetch_child": "true",
                "page_size": 50,
            },
        )

    def list_users_by_department(self, department_id: str) -> list[dict[str, Any]]:
        return self._collect_items(
            "/open-apis/contact/v3/users/find_by_department",
            params={
                "department_id": department_id,
                "department_id_type": "open_department_id",
                "user_id_type": "open_id",
                "page_size": 50,
            },
        )

    def get_user(self, user_id: str) -> dict[str, Any]:
        payload = self._request_data(
            "GET",
            f"/open-apis/contact/v3/users/{quote(user_id, safe='')}",
            params={
                "user_id_type": "open_id",
                "department_id_type": "open_department_id",
            },
        )
        user = payload.get("user")
        if not isinstance(user, dict):
            raise FeishuError("Feishu get user response has no user object")
        return user

    def _collect_items(
        self, path: str, *, params: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        for _ in range(10_000):
            page_params = dict(params)
            if page_token:
                page_params["page_token"] = page_token
            data = self._request_data("GET", path, params=page_params)
            values = data.get("items", [])
            if not isinstance(values, list):
                raise FeishuError("Feishu paginated response items must be a list")
            items.extend(item for item in values if isinstance(item, dict))
            if not data.get("has_more"):
                return items
            page_token = optional_text(data.get("page_token"))
            if not page_token:
                raise FeishuError("Feishu response has_more without page_token")
        raise FeishuError("Feishu pagination exceeded the safety limit")

    def _request_data(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.config.validate_api()
        url = self._api_url(path)
        token_refreshed = False
        response: httpx.Response | None = None
        payload: dict[str, Any] | None = None
        for attempt in range(5):
            response = self.client.request(
                method,
                url,
                params=params,
                headers={"Authorization": f"Bearer {self.tenant_access_token()}"},
            )
            if response.status_code in {429, 500, 502, 503, 504}:
                if attempt < 4:
                    self.sleep(retry_delay_seconds(response, attempt))
                    continue
            try:
                parsed = response.json()
            except ValueError as error:
                raise FeishuError("Feishu returned invalid JSON") from error
            if not isinstance(parsed, dict):
                raise FeishuError("Feishu returned a non-object JSON response")
            payload = parsed
            code = int(payload.get("code", 0))
            if (
                (response.status_code == 401 or code in TOKEN_ERROR_CODES)
                and not token_refreshed
            ):
                self._access_token = None
                self._token_expires_at = 0.0
                token_refreshed = True
                continue
            break
        assert response is not None and payload is not None
        response.raise_for_status()
        code = int(payload.get("code", 0))
        if code != 0:
            raise FeishuError(
                f"Feishu API failed, code={code}, msg={payload.get('msg', '')}"
            )
        data = payload.get("data", {})
        if not isinstance(data, dict):
            raise FeishuError("Feishu response data must be an object")
        return data

    def tenant_access_token(self) -> str:
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token
        self.config.validate_api()
        response = self.client.post(
            self._api_url("/open-apis/auth/v3/tenant_access_token/internal"),
            json={"app_id": self.config.app_id, "app_secret": self.config.app_secret},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or int(payload.get("code", -1)) != 0:
            raise FeishuError(
                f"Feishu token request failed: {payload.get('msg', '') if isinstance(payload, dict) else ''}"
            )
        token = optional_text(payload.get("tenant_access_token"))
        if not token:
            raise FeishuError("Feishu token response has no tenant_access_token")
        self._access_token = token
        self._token_expires_at = time.time() + max(int(payload.get("expire", 7200)), 120)
        return token

    def _api_url(self, path: str) -> str:
        url = f"{self.config.base_url}/{path.lstrip('/')}"
        expected = urlparse(self.config.base_url)
        actual = urlparse(url)
        if (expected.scheme, expected.netloc) != (actual.scheme, actual.netloc):
            raise FeishuError("Rejected Feishu URL outside configured origin")
        return url


class FeishuSnapshotBuilder:
    def __init__(self, config: FeishuConfig) -> None:
        self.config = config

    def build(
        self,
        departments: Iterable[dict[str, Any]],
        users: Iterable[dict[str, Any]],
        *,
        deactivate_missing: bool,
    ) -> DirectorySyncSnapshot:
        department_items = [dict(item) for item in departments]
        parent_by_external: dict[str, str | None] = {}
        units: list[DirectoryUnit] = []
        unit_id_by_external: dict[str, str] = {}
        for item in department_items:
            external_id = department_external_id(item)
            if not external_id:
                continue
            unit_id = self.config.department_id_map.get(external_id) or stable_internal_id(
                "dept", self.config.source, external_id
            )
            parent_id = optional_text(item.get("parent_department_id"))
            if parent_id == self.config.root_department_id:
                parent_id = None
            parent_by_external[external_id] = parent_id
            unit_id_by_external[external_id] = unit_id
            units.append(
                DirectoryUnit(
                    external_id=external_id,
                    unit_id=unit_id,
                    name=optional_text(item.get("name")) or external_id,
                    active=bool(item.get("status", {}).get("is_deleted") is not True),
                    attributes={
                        "provider": FEISHU_PROVIDER,
                        "open_department_id": external_id,
                        "parent_open_department_id": parent_id,
                        "department_id": optional_text(item.get("department_id")),
                    },
                )
            )

        ancestor_cache: dict[str, tuple[str, ...]] = {}

        def ancestors(external_id: str) -> tuple[str, ...]:
            cached = ancestor_cache.get(external_id)
            if cached is not None:
                return cached
            chain: list[str] = []
            seen: set[str] = set()
            current: str | None = external_id
            while current and current in unit_id_by_external:
                if current in seen:
                    raise FeishuError(f"Feishu department hierarchy has a cycle at {current}")
                seen.add(current)
                chain.append(current)
                current = parent_by_external.get(current)
            result = tuple(chain)
            ancestor_cache[external_id] = result
            return result

        directory_users: list[DirectoryUser] = []
        memberships: list[DirectoryMembership] = []
        role_memberships: list[DirectoryMembership] = []
        admin_ids = set(self.config.admin_user_ids)
        for item in users:
            external_id = feishu_user_id(item)
            if not external_id:
                continue
            active = feishu_user_active(item)
            direct_departments = [
                value
                for value in string_list(item.get("department_ids"))
                if value in unit_id_by_external
            ]
            directory_users.append(
                DirectoryUser(
                    external_id=external_id,
                    user_id=stable_internal_id("usr", self.config.source, external_id),
                    subject=external_id,
                    issuer=self.config.issuer,
                    email=optional_text(item.get("email")),
                    display_name=optional_text(item.get("name")) or external_id,
                    active=active,
                    attributes={
                        "provider": FEISHU_PROVIDER,
                        "open_id": optional_text(item.get("open_id")),
                        "user_id": optional_text(item.get("user_id")),
                        "union_id": optional_text(item.get("union_id")),
                        "direct_department_ids": direct_departments,
                        "job_title": optional_text(item.get("job_title")),
                    },
                )
            )
            if active:
                accessible_departments = {
                    ancestor
                    for department_id in direct_departments
                    for ancestor in ancestors(department_id)
                }
                memberships.extend(
                    DirectoryMembership(external_id, department_id)
                    for department_id in sorted(accessible_departments)
                )
                if external_id in admin_ids:
                    role_memberships.append(DirectoryMembership(external_id, "admin"))

        roles = (
            (DirectoryUnit("admin", "admin", "admin"),)
            if admin_ids
            else ()
        )
        return DirectorySyncSnapshot(
            source=self.config.source,
            users=tuple(directory_users),
            departments=tuple(units),
            roles=roles,
            user_departments=tuple(memberships),
            user_roles=tuple(role_memberships),
            deactivate_missing=deactivate_missing,
        )


class FeishuWebhookCodec:
    def __init__(self, config: FeishuConfig) -> None:
        self.config = config

    def decode(self, raw_body: bytes, headers: Mapping[str, str]) -> dict[str, Any]:
        self.config.validate_webhook()
        try:
            outer = json.loads(raw_body)
        except (UnicodeDecodeError, ValueError) as error:
            raise FeishuWebhookError("Invalid Feishu webhook JSON") from error
        if not isinstance(outer, dict):
            raise FeishuWebhookError("Feishu webhook body must be an object")

        encrypted = optional_text(outer.get("encrypt"))
        signature_present = bool(headers.get("X-Lark-Signature"))
        if encrypted:
            if not self.config.encrypt_key:
                raise FeishuWebhookError("Encrypted Feishu webhook has no configured key")
            payload = self._decrypt(encrypted)
            if signature_present:
                self._verify_signature(raw_body, headers)
            elif payload.get("type") != "url_verification":
                raise FeishuWebhookError("Feishu webhook signature is required")
        else:
            payload = outer
            if self.config.encrypt_key and payload.get("type") != "url_verification":
                raise FeishuWebhookError("Unencrypted Feishu event rejected")
            if signature_present:
                self._verify_signature(raw_body, headers)

        token = optional_text(payload.get("token")) or optional_text(
            (payload.get("header") or {}).get("token")
            if isinstance(payload.get("header"), dict)
            else None
        )
        if not token or not hmac.compare_digest(token, self.config.verification_token):
            raise FeishuWebhookError("Invalid Feishu verification token")
        return payload

    def _verify_signature(
        self, raw_body: bytes, headers: Mapping[str, str]
    ) -> None:
        timestamp = optional_text(headers.get("X-Lark-Request-Timestamp"))
        nonce = optional_text(headers.get("X-Lark-Request-Nonce"))
        signature = optional_text(headers.get("X-Lark-Signature"))
        if not timestamp or not nonce or not signature:
            raise FeishuWebhookError("Incomplete Feishu signature headers")
        try:
            age = abs(int(time.time()) - int(timestamp))
        except ValueError as error:
            raise FeishuWebhookError("Invalid Feishu request timestamp") from error
        if age > self.config.webhook_max_age_seconds:
            raise FeishuWebhookError("Expired Feishu webhook request")
        expected = hashlib.sha256(
            timestamp.encode()
            + nonce.encode()
            + self.config.encrypt_key.encode()
            + raw_body
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise FeishuWebhookError("Invalid Feishu webhook signature")

    def _decrypt(self, encrypted: str) -> dict[str, Any]:
        try:
            raw = base64.b64decode(encrypted, validate=True)
            if len(raw) <= AES.block_size:
                raise ValueError("encrypted payload is too short")
            key = hashlib.sha256(self.config.encrypt_key.encode()).digest()
            plain = unpad(
                AES.new(key, AES.MODE_CBC, raw[: AES.block_size]).decrypt(
                    raw[AES.block_size :]
                ),
                AES.block_size,
            )
            payload = json.loads(plain.decode("utf-8"))
        except Exception as error:
            raise FeishuWebhookError("Unable to decrypt Feishu webhook") from error
        if not isinstance(payload, dict):
            raise FeishuWebhookError("Decrypted Feishu webhook must be an object")
        return payload


class FeishuSyncService:
    def __init__(
        self,
        config: FeishuConfig,
        directory: IdentityDirectory,
        *,
        provisioning_store: PostgresIdentityProvisioningStore | None = None,
        client: FeishuContactClient | None = None,
    ) -> None:
        self.config = config
        self.directory = directory
        self.provisioning_store = provisioning_store
        self.client = client or FeishuContactClient(config)
        self.builder = FeishuSnapshotBuilder(config)
        self.webhook = FeishuWebhookCodec(config)
        self._seen_events: set[str] = set()
        self._seen_lock = threading.Lock()

    def sync(
        self,
        user_id: str | None = None,
        *,
        force_inactive: bool = False,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        self.config.validate_api()
        if user_id:
            departments = self.client.list_departments()
            user = (
                {"open_id": user_id, "name": user_id, "active": False}
                if force_inactive
                else self.client.get_user(user_id)
            )
            snapshot = self.builder.build(
                departments,
                [user],
                deactivate_missing=False,
            )
            mode = "user"
        else:
            departments, users = self.client.collect_directory()
            snapshot = self.builder.build(
                departments,
                users,
                deactivate_missing=True,
            )
            mode = "full"
        result = self.directory.sync(snapshot)
        if event_id:
            self.mark_event_processed(event_id)
        return {
            "status": "completed",
            "mode": mode,
            "user_id": user_id,
            "run_id": result.run_id,
            "source": result.source,
            "user_count": result.user_count,
            "department_count": result.department_count,
            "role_count": result.role_count,
            "user_department_count": result.user_department_count,
            "user_role_count": result.user_role_count,
            "deactivated_users": result.deactivated_users,
            "deactivated_departments": result.deactivated_departments,
            "completed_at": result.completed_at.isoformat(),
        }

    def accept_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        header = payload.get("header") or {}
        if not isinstance(header, dict):
            raise FeishuWebhookError("Feishu event header must be an object")
        event_type = optional_text(header.get("event_type"))
        event_id = optional_text(header.get("event_id"))
        if not event_type or not event_id:
            raise FeishuWebhookError("Feishu event type and event ID are required")
        if len(event_id) > 256:
            raise FeishuWebhookError("Feishu event ID is too long")
        if event_type not in FEISHU_CONTACT_EVENTS:
            return {
                "status": "ignored",
                "event_type": event_type,
                "event_id": event_id,
            }
        if not self.record_event(event_id, event_type, payload):
            return {
                "status": "duplicate",
                "event_type": event_type,
                "event_id": event_id,
            }
        user_id = feishu_event_user_id(payload) if event_type in FEISHU_USER_EVENTS else None
        return {
            "status": "queued",
            "event_type": event_type,
            "event_id": event_id,
            "user_id": user_id,
            "force_inactive": event_type == "contact.user.deleted_v3" and bool(user_id),
        }

    def record_event(
        self, event_id: str, event_type: str, payload: dict[str, Any]
    ) -> bool:
        if self.provisioning_store is not None:
            return self.provisioning_store.record_provider_webhook_event(
                provider=FEISHU_PROVIDER,
                event_id=event_id,
                tenant_id=self.config.app_id,
                resource=event_type,
                payload=payload,
            )
        with self._seen_lock:
            if event_id in self._seen_events:
                return False
            self._seen_events.add(event_id)
            return True

    def mark_event_processed(self, event_id: str) -> None:
        if self.provisioning_store is not None:
            self.provisioning_store.mark_provider_webhook_event_processed(
                FEISHU_PROVIDER, event_id
            )

    def status(self) -> dict[str, Any]:
        event_status = (
            self.provisioning_store.provider_webhook_status(FEISHU_PROVIDER)
            if self.provisioning_store is not None
            else {"seen": len(self._seen_events)}
        )
        return {
            "enabled": self.config.enabled,
            "provider": FEISHU_PROVIDER,
            "source": self.config.source,
            "issuer": self.config.issuer,
            "app_id_configured": bool(self.config.app_id),
            "webhook_configured": bool(self.config.verification_token),
            "encrypted_webhook": bool(self.config.encrypt_key),
            "directory": self.directory.status(),
            "webhook_events": event_status,
        }


def feishu_event_user_id(payload: dict[str, Any]) -> str | None:
    event = payload.get("event") or {}
    if not isinstance(event, dict):
        return None
    candidates = [event]
    for key in ("object", "user"):
        value = event.get(key)
        if isinstance(value, dict):
            candidates.append(value)
    for candidate in candidates:
        if user_id := feishu_user_id(candidate):
            return user_id
    return None


def feishu_user_id(item: Mapping[str, Any]) -> str | None:
    for key in ("open_id", "user_id", "union_id"):
        value = item.get(key)
        if isinstance(value, Mapping):
            if nested := feishu_user_id(value):
                return nested
        elif result := optional_text(value):
            return result
    return None


def feishu_user_active(item: Mapping[str, Any]) -> bool:
    if item.get("active") is False:
        return False
    status = item.get("status") or {}
    if not isinstance(status, dict):
        return True
    if status.get("is_activated") is False:
        return False
    return not any(
        bool(status.get(name))
        for name in ("is_resigned", "is_frozen", "is_unjoin")
    )


def department_external_id(item: Mapping[str, Any]) -> str | None:
    return optional_text(item.get("open_department_id")) or optional_text(
        item.get("department_id")
    )


def string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def retry_delay_seconds(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After", "").strip()
    if retry_after.isdigit():
        return min(float(retry_after), 60.0)
    return min(2**attempt, 30.0)


def optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def split_csv(value: str) -> list[str]:
    return list(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))


def parse_json_map(name: str) -> dict[str, str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise FeishuError(f"{name} must be a JSON object")
    return {str(key): str(item) for key, item in value.items()}


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise FeishuError(f"{name} must be an integer") from error


__all__ = [
    "FEISHU_CONTACT_EVENTS",
    "FEISHU_DEPARTMENT_EVENTS",
    "FEISHU_PROVIDER",
    "FEISHU_USER_EVENTS",
    "FeishuConfig",
    "FeishuContactClient",
    "FeishuError",
    "FeishuSnapshotBuilder",
    "FeishuSyncService",
    "FeishuWebhookCodec",
    "FeishuWebhookError",
    "feishu_event_user_id",
]
