from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.app.models.knowledge import SubjectScope
from backend.app.retrieval.providers import load_dotenv_if_available
from backend.app.identity.directory import DirectoryIdentity, IdentityDirectory


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: str
    subject: str
    issuer: str
    email: str | None = None
    display_name: str | None = None
    department_ids: tuple[str, ...] = ()
    role_ids: tuple[str, ...] = ()
    identity_source: str = "claims"

    @property
    def scope(self) -> SubjectScope:
        return SubjectScope(
            user_id=self.user_id,
            department_ids=self.department_ids,
            role_ids=self.role_ids,
        )

    @property
    def is_admin(self) -> bool:
        return "admin" in self.role_ids


class JWTAuthenticator:
    def __init__(self, identity_directory: IdentityDirectory | None = None) -> None:
        load_dotenv_if_available()
        self.mode = os.getenv("KNOWLEDGE_AUTH_MODE", "local").strip().lower()
        if self.mode not in {"local", "oidc"}:
            raise RuntimeError("KNOWLEDGE_AUTH_MODE must be local or oidc")
        self.issuer = os.getenv("KNOWLEDGE_JWT_ISSUER", "enterprise-kb-agent")
        self.audience = os.getenv("KNOWLEDGE_JWT_AUDIENCE", "knowledge-api")
        self.leeway_seconds = int(os.getenv("KNOWLEDGE_JWT_LEEWAY_SECONDS", "30"))
        self.department_claim = os.getenv(
            "KNOWLEDGE_OIDC_DEPARTMENT_CLAIM", "department_ids"
        )
        self.role_claim = os.getenv("KNOWLEDGE_OIDC_ROLE_CLAIM", "role_ids")
        self.user_id_claim = os.getenv("KNOWLEDGE_OIDC_USER_ID_CLAIM", "sub")
        self.subject_claim = os.getenv("KNOWLEDGE_OIDC_SUBJECT_CLAIM", "sub")
        self.email_claim = os.getenv("KNOWLEDGE_OIDC_EMAIL_CLAIM", "email")
        self.name_claim = os.getenv("KNOWLEDGE_OIDC_NAME_CLAIM", "name")
        self.required_scope = os.getenv(
            "KNOWLEDGE_OIDC_REQUIRED_SCOPE", "access_as_user"
        ).strip()
        self.trusted_admin_app_role = os.getenv(
            "KNOWLEDGE_OIDC_TRUSTED_ADMIN_APP_ROLE", ""
        ).strip()
        self.allow_dev_tokens = env_bool("KNOWLEDGE_AUTH_ALLOW_DEV_TOKEN", False)
        self.identity_mode = os.getenv(
            "KNOWLEDGE_IDENTITY_MODE", "claims"
        ).strip().lower()
        if self.identity_mode not in {"claims", "directory"}:
            raise RuntimeError("KNOWLEDGE_IDENTITY_MODE must be claims or directory")
        if self.identity_mode == "directory" and identity_directory is None:
            raise RuntimeError("Directory identity mode requires an IdentityDirectory")
        self.identity_directory = identity_directory
        self._jwks_client = None

        if self.mode == "local":
            self.secret = require_setting("KNOWLEDGE_JWT_SECRET")
            if len(self.secret) < 32:
                raise RuntimeError("KNOWLEDGE_JWT_SECRET must be at least 32 characters")
            self.algorithms = ["HS256"]
        else:
            self.secret = ""
            self.issuer = require_setting("KNOWLEDGE_OIDC_ISSUER")
            self.audience = require_setting("KNOWLEDGE_OIDC_AUDIENCE")
            self.jwks_url = require_setting("KNOWLEDGE_OIDC_JWKS_URL")
            self.algorithms = split_claim(
                os.getenv("KNOWLEDGE_OIDC_ALGORITHMS", "RS256")
            )

    def issue_local_token(
        self,
        *,
        user_id: str,
        department_ids: list[str],
        role_ids: list[str],
        email: str | None = None,
        display_name: str | None = None,
    ) -> tuple[str, int]:
        if self.mode != "local" or not self.allow_dev_tokens:
            raise PermissionError("Local token issuing is disabled")
        now = datetime.now(timezone.utc)
        ttl_seconds = int(os.getenv("KNOWLEDGE_JWT_TTL_SECONDS", "3600"))
        payload = {
            "sub": user_id,
            "iss": self.issuer,
            "aud": self.audience,
            "iat": now,
            "nbf": now,
            "exp": now + timedelta(seconds=ttl_seconds),
            "department_ids": sorted(set(department_ids)),
            "role_ids": sorted(set(role_ids)),
        }
        if email:
            payload["email"] = email
        if display_name:
            payload["name"] = display_name
        return jwt.encode(payload, self.secret, algorithm="HS256"), ttl_seconds

    def authenticate(self, token: str) -> AuthenticatedUser:
        if self.mode == "local":
            claims = jwt.decode(
                token,
                self.secret,
                algorithms=self.algorithms,
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.leeway_seconds,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            )
        else:
            signing_key = self.jwks_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=self.algorithms,
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.leeway_seconds,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            )
            self.validate_oidc_claims(claims)
        claims_user = self.user_from_claims(claims)
        if self.identity_mode == "claims":
            return claims_user
        assert self.identity_directory is not None
        directory_user = self.identity_directory.resolve_user(
            issuer=claims_user.issuer,
            subject=claims_user.subject,
        )
        if directory_user is None:
            raise jwt.InvalidTokenError(
                "Token subject is not active in the identity directory"
            )
        return self.apply_trusted_app_roles(
            self.user_from_directory(directory_user), claims
        )

    def validate_oidc_claims(self, claims: dict[str, Any]) -> None:
        if not self.required_scope:
            return
        scopes = set(split_claim(claims.get("scp", "")))
        if self.required_scope not in scopes:
            raise jwt.InvalidTokenError(
                f"Token is missing required scope: {self.required_scope}"
            )

    def apply_trusted_app_roles(
        self, user: AuthenticatedUser, claims: dict[str, Any]
    ) -> AuthenticatedUser:
        if not self.trusted_admin_app_role:
            return user
        app_roles = set(split_claim(claims.get("roles", [])))
        if self.trusted_admin_app_role not in app_roles:
            return user
        return AuthenticatedUser(
            user_id=user.user_id,
            subject=user.subject,
            issuer=user.issuer,
            email=user.email,
            display_name=user.display_name,
            department_ids=user.department_ids,
            role_ids=tuple(sorted(set(user.role_ids) | {"admin"})),
            identity_source=user.identity_source,
        )

    @property
    def jwks_client(self):
        if self._jwks_client is None:
            self._jwks_client = jwt.PyJWKClient(
                self.jwks_url,
                cache_keys=True,
                lifespan=300,
            )
        return self._jwks_client

    def user_from_claims(self, claims: dict[str, Any]) -> AuthenticatedUser:
        standard_subject = str(claims.get("sub", "")).strip()
        subject = str(claims.get(self.subject_claim, standard_subject)).strip()
        user_id = str(claims.get(self.user_id_claim, subject)).strip()
        if not standard_subject or not subject or not user_id:
            raise jwt.InvalidTokenError("Token is missing a usable subject")
        return AuthenticatedUser(
            user_id=user_id,
            subject=subject,
            issuer=str(claims.get("iss", self.issuer)),
            email=optional_string(claims.get(self.email_claim)),
            display_name=optional_string(claims.get(self.name_claim)),
            department_ids=tuple(split_claim(claims.get(self.department_claim, []))),
            role_ids=tuple(split_claim(claims.get(self.role_claim, []))),
            identity_source="claims",
        )

    @staticmethod
    def user_from_directory(identity: DirectoryIdentity) -> AuthenticatedUser:
        return AuthenticatedUser(
            user_id=identity.user_id,
            subject=identity.subject,
            issuer=identity.issuer,
            email=identity.email,
            display_name=identity.display_name,
            department_ids=identity.department_ids,
            role_ids=identity.role_ids,
            identity_source=identity.source,
        )


bearer_scheme = HTTPBearer(auto_error=False)


_authenticator: JWTAuthenticator | None = None


def configure_authenticator(authenticator: JWTAuthenticator) -> None:
    global _authenticator
    _authenticator = authenticator


def get_authenticator() -> JWTAuthenticator:
    global _authenticator
    if _authenticator is None:
        _authenticator = JWTAuthenticator()
    return _authenticator


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> AuthenticatedUser:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise auth_error()
    try:
        return get_authenticator().authenticate(credentials.credentials)
    except jwt.PyJWTError as error:
        raise auth_error() from error


def require_admin(
    user: AuthenticatedUser = Depends(get_current_user),
) -> AuthenticatedUser:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")
    return user


def auth_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "code": "authentication_required",
            "message": "Authentication required",
        },
        headers={"WWW-Authenticate": "Bearer"},
    )


def split_claim(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = value.replace(",", " ").split()
    elif isinstance(value, (list, tuple, set)):
        raw = [str(item) for item in value]
    else:
        raw = []
    return list(dict.fromkeys(item.strip() for item in raw if item.strip()))


def optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def require_setting(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
