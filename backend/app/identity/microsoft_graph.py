from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable
from urllib.parse import quote, urlparse

import httpx

from backend.app.identity.provisioning import PostgresIdentityProvisioningStore
from backend.app.retrieval.providers import load_dotenv_if_available


GRAPH_PROVIDER = "microsoft-graph"
GRAPH_RESOURCES = ("users", "groups")


@dataclass(frozen=True)
class MicrosoftGraphConfig:
    enabled: bool
    tenant_id: str
    client_id: str
    client_secret: str
    source: str
    issuer: str
    graph_base_url: str
    token_url: str
    webhook_url: str | None
    lifecycle_webhook_url: str | None
    client_state: str
    group_id_map: dict[str, str]
    admin_group_ids: frozenset[str]
    subscription_minutes: int
    subscription_renew_before_minutes: int = 15

    @classmethod
    def from_env(cls) -> "MicrosoftGraphConfig":
        load_dotenv_if_available()
        tenant_id = os.getenv("KNOWLEDGE_GRAPH_TENANT_ID", "").strip()
        enabled = env_bool("KNOWLEDGE_GRAPH_ENABLED", False)
        graph_base_url = os.getenv(
            "KNOWLEDGE_GRAPH_BASE_URL", "https://graph.microsoft.com/v1.0"
        ).strip().rstrip("/")
        issuer = os.getenv("KNOWLEDGE_GRAPH_OIDC_ISSUER", "").strip()
        if not issuer and tenant_id:
            issuer = f"https://login.microsoftonline.com/{tenant_id}/v2.0"
        token_url = os.getenv("KNOWLEDGE_GRAPH_TOKEN_URL", "").strip()
        if not token_url and tenant_id:
            token_url = (
                f"https://login.microsoftonline.com/{tenant_id}"
                "/oauth2/v2.0/token"
            )
        config = cls(
            enabled=enabled,
            tenant_id=tenant_id,
            client_id=os.getenv("KNOWLEDGE_GRAPH_CLIENT_ID", "").strip(),
            client_secret=os.getenv("KNOWLEDGE_GRAPH_CLIENT_SECRET", "").strip(),
            source=os.getenv(
                "KNOWLEDGE_GRAPH_SOURCE", "microsoft-graph"
            ).strip(),
            issuer=issuer,
            graph_base_url=graph_base_url,
            token_url=token_url,
            webhook_url=optional_env("KNOWLEDGE_GRAPH_WEBHOOK_URL"),
            lifecycle_webhook_url=optional_env(
                "KNOWLEDGE_GRAPH_LIFECYCLE_WEBHOOK_URL"
            ),
            client_state=os.getenv("KNOWLEDGE_GRAPH_CLIENT_STATE", "").strip(),
            group_id_map=parse_json_map("KNOWLEDGE_GRAPH_GROUP_ID_MAP"),
            admin_group_ids=frozenset(
                split_csv(os.getenv("KNOWLEDGE_GRAPH_ADMIN_GROUP_IDS", ""))
            ),
            subscription_minutes=int(
                os.getenv("KNOWLEDGE_GRAPH_SUBSCRIPTION_MINUTES", "1440")
            ),
            subscription_renew_before_minutes=int(
                os.getenv(
                    "KNOWLEDGE_GRAPH_SUBSCRIPTION_RENEW_BEFORE_MINUTES", "15"
                )
            ),
        )
        if config.enabled:
            config.validate()
        return config

    def validate(self) -> None:
        missing = [
            name
            for name, value in {
                "KNOWLEDGE_GRAPH_TENANT_ID": self.tenant_id,
                "KNOWLEDGE_GRAPH_CLIENT_ID": self.client_id,
                "KNOWLEDGE_GRAPH_CLIENT_SECRET": self.client_secret,
                "KNOWLEDGE_GRAPH_OIDC_ISSUER": self.issuer,
                "KNOWLEDGE_GRAPH_CLIENT_STATE": self.client_state,
            }.items()
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing Microsoft Graph settings: {', '.join(missing)}")
        if len(self.client_state) < 16:
            raise RuntimeError("KNOWLEDGE_GRAPH_CLIENT_STATE must be at least 16 characters")
        if len(self.client_state) > 128:
            raise RuntimeError("KNOWLEDGE_GRAPH_CLIENT_STATE must not exceed 128 characters")
        if self.subscription_minutes < 45:
            raise RuntimeError("KNOWLEDGE_GRAPH_SUBSCRIPTION_MINUTES must be at least 45")
        if self.subscription_minutes > 41_760:
            raise RuntimeError(
                "KNOWLEDGE_GRAPH_SUBSCRIPTION_MINUTES must not exceed 41760"
            )
        if self.subscription_renew_before_minutes < 5:
            raise RuntimeError(
                "KNOWLEDGE_GRAPH_SUBSCRIPTION_RENEW_BEFORE_MINUTES must be at least 5"
            )
        if self.subscription_renew_before_minutes >= self.subscription_minutes:
            raise RuntimeError(
                "KNOWLEDGE_GRAPH_SUBSCRIPTION_RENEW_BEFORE_MINUTES must be less than subscription lifetime"
            )
        validate_https_url(self.graph_base_url, "KNOWLEDGE_GRAPH_BASE_URL")
        validate_https_url(self.token_url, "KNOWLEDGE_GRAPH_TOKEN_URL")

    def validate_webhook_endpoints(self) -> None:
        if not self.webhook_url:
            raise RuntimeError("KNOWLEDGE_GRAPH_WEBHOOK_URL is required for subscriptions")
        if not self.lifecycle_webhook_url:
            raise RuntimeError(
                "KNOWLEDGE_GRAPH_LIFECYCLE_WEBHOOK_URL is required for subscriptions"
            )
        validate_https_url(self.webhook_url, "KNOWLEDGE_GRAPH_WEBHOOK_URL")
        validate_https_url(
            self.lifecycle_webhook_url,
            "KNOWLEDGE_GRAPH_LIFECYCLE_WEBHOOK_URL",
        )

    def webhook_client_state_matches(self, value: Any) -> bool:
        candidate = str(value or "")
        return bool(self.client_state) and secrets.compare_digest(
            candidate, self.client_state
        )


class MicrosoftGraphClient:
    def __init__(
        self,
        config: MicrosoftGraphConfig,
        *,
        client: httpx.Client | None = None,
        token_provider: Callable[[], str] | None = None,
    ) -> None:
        self.config = config
        self.client = client or httpx.Client(timeout=httpx.Timeout(30.0))
        self.token_provider = token_provider or self._client_credentials_token
        self._access_token: str | None = None
        self._token_expires_at = 0.0
        parsed = urlparse(config.graph_base_url)
        self._graph_origin = (parsed.scheme.lower(), parsed.netloc.lower())

    def collect_delta(
        self, resource: str, cursor_url: str | None = None
    ) -> tuple[list[dict[str, Any]], str, int]:
        resource = normalize_graph_resource(resource)
        url = cursor_url or self.initial_delta_url(resource)
        items: list[dict[str, Any]] = []
        page_count = 0
        while url:
            self._validate_graph_url(url)
            page = self._request_json("GET", url)
            page_count += 1
            if page_count > 10_000:
                raise RuntimeError("Microsoft Graph delta pagination exceeded 10000 pages")
            values = page.get("value", [])
            if not isinstance(values, list):
                raise RuntimeError("Microsoft Graph delta response has invalid value")
            items.extend(item for item in values if isinstance(item, dict))
            next_link = page.get("@odata.nextLink")
            delta_link = page.get("@odata.deltaLink")
            if next_link:
                url = str(next_link)
                continue
            if not delta_link:
                raise RuntimeError("Microsoft Graph delta response has no final deltaLink")
            self._validate_graph_url(str(delta_link))
            return items, str(delta_link), page_count
        raise RuntimeError("Microsoft Graph delta pagination ended unexpectedly")

    def initial_delta_url(self, resource: str) -> str:
        if resource == "users":
            fields = "id,userPrincipalName,displayName,mail,accountEnabled"
        elif resource == "groups":
            fields = "id,displayName,description,securityEnabled,members"
        else:  # pragma: no cover - normalize_graph_resource guards this
            raise ValueError(f"Unsupported Microsoft Graph resource: {resource}")
        return f"{self.config.graph_base_url}/{resource}/delta?$select={fields}"

    def create_subscription(self, resource: str) -> dict[str, Any]:
        resource = normalize_graph_resource(resource)
        self.config.validate_webhook_endpoints()
        expiration = datetime.now(timezone.utc) + timedelta(
            minutes=self.config.subscription_minutes
        )
        payload: dict[str, Any] = {
            "changeType": "updated,deleted",
            "notificationUrl": self.config.webhook_url,
            "resource": f"/{resource}",
            "expirationDateTime": expiration.isoformat(),
            "clientState": self.config.client_state,
        }
        if self.config.lifecycle_webhook_url:
            validate_https_url(
                self.config.lifecycle_webhook_url,
                "KNOWLEDGE_GRAPH_LIFECYCLE_WEBHOOK_URL",
            )
            payload["lifecycleNotificationUrl"] = self.config.lifecycle_webhook_url
        return self._request_json(
            "POST", f"{self.config.graph_base_url}/subscriptions", json=payload
        )

    def renew_subscription(self, subscription_id: str) -> dict[str, Any]:
        validate_subscription_id(subscription_id)
        expiration = datetime.now(timezone.utc) + timedelta(
            minutes=self.config.subscription_minutes
        )
        url = (
            f"{self.config.graph_base_url}/subscriptions/"
            f"{quote(subscription_id, safe='')}"
        )
        return self._request_json(
            "PATCH", url, json={"expirationDateTime": expiration.isoformat()}
        )

    def list_subscriptions(self) -> list[dict[str, Any]]:
        payload = self._request_json(
            "GET", f"{self.config.graph_base_url}/subscriptions"
        )
        values = payload.get("value", [])
        if not isinstance(values, list):
            raise RuntimeError("Microsoft Graph subscriptions response has invalid value")
        return [item for item in values if isinstance(item, dict)]

    def _request_json(
        self, method: str, url: str, *, json: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self._validate_graph_url(url)
        response: httpx.Response | None = None
        for attempt in range(5):
            response = self.client.request(
                method,
                url,
                headers={"Authorization": f"Bearer {self.token_provider()}"},
                json=json,
            )
            if response.status_code not in {429, 500, 502, 503, 504}:
                break
            if attempt < 4:
                time.sleep(retry_delay_seconds(response, attempt))
        assert response is not None
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Microsoft Graph returned a non-object JSON response")
        return payload

    def _client_credentials_token(self) -> str:
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token
        response = self.client.post(
            self.config.token_url,
            data={
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            },
        )
        response.raise_for_status()
        payload = response.json()
        token = str(payload.get("access_token", "")).strip()
        if not token:
            raise RuntimeError("Microsoft identity platform returned no access token")
        self._access_token = token
        self._token_expires_at = time.time() + int(payload.get("expires_in", 3600))
        return token

    def _validate_graph_url(self, url: str) -> None:
        parsed = urlparse(url)
        origin = (parsed.scheme.lower(), parsed.netloc.lower())
        if origin != self._graph_origin:
            raise ValueError("Rejected Microsoft Graph pagination URL outside Graph origin")


class MicrosoftGraphSyncService:
    def __init__(
        self,
        config: MicrosoftGraphConfig,
        store: PostgresIdentityProvisioningStore,
        client: MicrosoftGraphClient | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.client = client or MicrosoftGraphClient(config)

    def sync(self, resources: Iterable[str] | None = None) -> dict[str, Any]:
        self.require_enabled()
        selected = normalize_graph_resources(resources)
        results: dict[str, Any] = {}
        processed: set[str] = set()
        for resource in GRAPH_RESOURCES:
            if resource not in selected:
                continue
            with self.store.sync_lock(
                GRAPH_PROVIDER, self.config.tenant_id, resource
            ) as acquired:
                if not acquired:
                    results[resource] = {"status": "already_running"}
                    continue
                cursor = self.store.get_cursor(
                    GRAPH_PROVIDER, self.config.tenant_id, resource
                )
                items, delta_link, page_count = self.client.collect_delta(
                    resource, cursor
                )
                if resource == "users":
                    applied = self.store.apply_graph_user_delta(
                        source=self.config.source,
                        tenant_id=self.config.tenant_id,
                        issuer=self.config.issuer,
                        users=items,
                        delta_link=delta_link,
                    )
                else:
                    applied = self.store.apply_graph_group_delta(
                        source=self.config.source,
                        tenant_id=self.config.tenant_id,
                        groups=items,
                        delta_link=delta_link,
                        group_id_map=self.config.group_id_map,
                        admin_group_ids=set(self.config.admin_group_ids),
                    )
                results[resource] = {
                    "status": "completed",
                    "received": len(items),
                    "pages": page_count,
                    "incremental": cursor is not None,
                    **applied,
                }
                processed.add(resource)
        self.store.mark_webhook_events_processed(processed)
        status = "completed" if processed == selected else "partially_completed"
        return {"status": status, "resources": results}

    def create_subscriptions(
        self, resources: Iterable[str] | None = None
    ) -> list[dict[str, Any]]:
        self.require_enabled()
        self.config.validate_webhook_endpoints()
        subscriptions = []
        for resource in normalize_graph_resources(resources):
            subscription = self._create_or_recover_subscription(resource)
            subscriptions.append(subscription)
        return subscriptions

    def reconcile_subscriptions(
        self, resources: Iterable[str] | None = None
    ) -> dict[str, Any]:
        self.require_enabled()
        self.config.validate_webhook_endpoints()
        selected = normalize_graph_resources(resources)
        existing = {
            normalized_resource: item
            for item in self.store.list_graph_subscriptions(self.config.tenant_id)
            if (normalized_resource := safe_graph_resource(item.get("resource")))
        }
        results: dict[str, Any] = {}
        errors: dict[str, str] = {}
        threshold = datetime.now(timezone.utc) + timedelta(
            minutes=self.config.subscription_renew_before_minutes
        )
        for resource in sorted(selected):
            subscription = existing.get(resource)
            try:
                if subscription is None:
                    subscription = self._create_or_recover_subscription(resource)
                    action = "created"
                else:
                    expiration = parse_datetime(subscription.get("expiration_at"))
                    if expiration is None or expiration <= threshold:
                        try:
                            subscription = self.renew_subscription(
                                str(subscription["subscription_id"])
                            )
                            action = "renewed"
                        except httpx.HTTPStatusError as error:
                            if error.response.status_code not in {404, 410}:
                                raise
                            self.store.delete_graph_subscription(
                                str(subscription["subscription_id"]),
                                self.config.tenant_id,
                            )
                            subscription = self._create_or_recover_subscription(resource)
                            action = "recreated"
                    else:
                        action = "healthy"
                results[resource] = {
                    "action": action,
                    "subscription_id": subscription.get("id", subscription.get("subscription_id")),
                    "expirationDateTime": subscription.get(
                        "expirationDateTime", subscription.get("expiration_at")
                    ),
                }
            except Exception as error:
                errors[resource] = f"{type(error).__name__}: {error}"
        return {
            "status": "completed" if not errors else "partially_completed",
            "resources": results,
            "errors": errors,
        }

    def handle_lifecycle_notifications(
        self, notifications: Iterable[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        self.require_enabled()
        events = (
            list(notifications)
            if notifications is not None
            else self.store.list_pending_lifecycle_events(self.config.tenant_id)
        )
        resync = any(
            str(item.get("lifecycleEvent", "")) in {"missed", "subscriptionRemoved"}
            for item in events
        )
        renewals = 0
        for item in events:
            if item.get("lifecycleEvent") == "subscriptionRemoved":
                subscription_id = str(item.get("subscriptionId", "")).strip()
                if subscription_id:
                    self.store.delete_graph_subscription(
                        subscription_id, self.config.tenant_id
                    )
                continue
            if item.get("lifecycleEvent") != "reauthorizationRequired":
                continue
            subscription_id = str(item.get("subscriptionId", "")).strip()
            if not subscription_id:
                continue
            try:
                self.renew_subscription(subscription_id)
                renewals += 1
            except httpx.HTTPStatusError as error:
                if error.response.status_code not in {404, 410}:
                    raise
                self.store.delete_graph_subscription(
                    subscription_id, self.config.tenant_id
                )
        reconciliation = self.reconcile_subscriptions()
        sync_result: dict[str, Any] | None = None
        if resync:
            sync_result = self.sync(GRAPH_RESOURCES)
        if reconciliation["status"] == "completed" and (
            sync_result is None or sync_result["status"] == "completed"
        ):
            self.store.mark_lifecycle_events_processed(self.config.tenant_id)
        return {
            "status": reconciliation["status"],
            "renewed": renewals,
            "resynced": resync,
            "sync": sync_result,
            "reconciliation": reconciliation,
        }

    def _create_or_recover_subscription(self, resource: str) -> dict[str, Any]:
        try:
            subscription = self.client.create_subscription(resource)
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 409:
                raise
            remote = None
            for item in self.client.list_subscriptions():
                if (
                    safe_graph_resource(item.get("resource")) == resource
                    and item.get("notificationUrl") == self.config.webhook_url
                    and item.get("lifecycleNotificationUrl")
                    == self.config.lifecycle_webhook_url
                    and item.get("clientState") == self.config.client_state
                ):
                    remote = item
                    break
            if remote is None:
                raise
            subscription = remote
        self.store.save_subscription(subscription, tenant_id=self.config.tenant_id)
        return subscription

    def reset_cursors(self, resources: Iterable[str] | None = None) -> int:
        self.require_enabled()
        selected = normalize_graph_resources(resources)
        return self.store.reset_cursors(
            GRAPH_PROVIDER, self.config.tenant_id, selected
        )

    def renew_subscription(self, subscription_id: str) -> dict[str, Any]:
        self.require_enabled()
        subscription = self.client.renew_subscription(subscription_id)
        self.store.save_subscription(subscription, tenant_id=self.config.tenant_id)
        return subscription

    def status(self) -> dict[str, Any]:
        base = {
            "enabled": self.config.enabled,
            "tenant_id": self.config.tenant_id or None,
            "source": self.config.source,
            "issuer": self.config.issuer or None,
            "webhook_configured": bool(self.config.webhook_url),
            "lifecycle_webhook_configured": bool(self.config.lifecycle_webhook_url),
            "subscription_renew_before_minutes": self.config.subscription_renew_before_minutes,
        }
        if not self.config.tenant_id:
            return {**base, "cursors": [], "webhook_events": {}, "subscriptions": []}
        return {**base, **self.store.graph_status(self.config.tenant_id)}

    def require_enabled(self) -> None:
        if not self.config.enabled:
            raise RuntimeError("Microsoft Graph provisioning is disabled")


def normalize_graph_resource(resource: str) -> str:
    normalized = resource.strip().lower().strip("/").split("/", 1)[0]
    if normalized not in GRAPH_RESOURCES:
        raise ValueError(f"Unsupported Microsoft Graph resource: {resource}")
    return normalized


def normalize_graph_resources(resources: Iterable[str] | None) -> set[str]:
    if resources is None:
        return set(GRAPH_RESOURCES)
    selected = {normalize_graph_resource(resource) for resource in resources}
    if not selected:
        raise ValueError("At least one Microsoft Graph resource is required")
    return selected


def safe_graph_resource(value: Any) -> str | None:
    try:
        return normalize_graph_resource(str(value or ""))
    except ValueError:
        return None


def resources_from_notifications(notifications: Iterable[dict[str, Any]]) -> set[str]:
    resources: set[str] = set()
    for notification in notifications:
        raw = str(notification.get("resource", "")).strip()
        try:
            resources.add(normalize_graph_resource(raw))
        except ValueError:
            continue
    return resources


def retry_delay_seconds(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After", "").strip()
    if retry_after.isdigit():
        return min(float(retry_after), 60.0)
    return min(2**attempt, 30)


def validate_https_url(value: str, setting_name: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise RuntimeError(f"{setting_name} must be an absolute HTTPS URL")


def validate_subscription_id(value: str) -> None:
    candidate = str(value).strip()
    if not candidate or len(candidate) > 256 or any(
        character in candidate for character in "/\\?#"
    ):
        raise ValueError("Invalid Microsoft Graph subscription id")


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


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


def optional_env(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
