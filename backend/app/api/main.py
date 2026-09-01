from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import time
from pathlib import Path

import httpx

try:
    from fastapi import (
        BackgroundTasks,
        Depends,
        FastAPI,
        File,
        Form,
        HTTPException,
        Request,
        Response,
        UploadFile,
        status,
    )
    from fastapi.responses import HTMLResponse, PlainTextResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel, ConfigDict, Field
except ImportError as error:  # pragma: no cover
    raise RuntimeError("Install API dependencies: pip install -r requirements.txt") from error

from backend.app.bootstrap import (
    create_demo_services,
    create_dead_letter_queue,
    create_identity_directory,
    create_identity_provisioning_store,
    create_index_job_service,
    create_microsoft_graph_sync_service,
    create_research_job_service,
)
from backend.app.agent.serialization import serialize_knowledge_answer
from backend.app.identity.directory import (
    DirectoryMembership,
    DirectorySyncSnapshot,
    DirectoryUnit,
    DirectoryUser,
)
from backend.app.identity.microsoft_graph import (
    GRAPH_RESOURCES,
    normalize_graph_resources,
    resources_from_notifications,
)
from backend.app.security.content import upload_max_bytes
from backend.app.identity.scim_api import (
    SCIMConfig,
    SCIMException,
    create_scim_router,
    scim_exception_handler,
)
from backend.app.jobs.models import IndexJob
from backend.app.jobs.dlq import DeadLetterEntry
from backend.app.research.models import ResearchJob
from backend.app.observability import (
    configure_observability,
    correlation_context,
    current_context,
    metrics_snapshot,
    new_id,
    observability_status,
    observed_span,
    record_http_request,
    set_context_value,
)
from backend.app.models.knowledge import (
    ACLEntry,
    Document,
    DocumentChunk,
    DocumentVersion,
    Permission,
    SubjectType,
)
from backend.app.preview.pdf import PDFPreviewError, locate_pdf_chunk, render_pdf_page
from backend.app.security.auth import (
    AuthenticatedUser,
    JWTAuthenticator,
    configure_authenticator,
    get_authenticator,
    get_current_user,
    require_admin,
)
from backend.app.security.headers import browser_security_headers
from backend.app.storage.object_store import ObjectStorageError, ObjectStorageNotFound


configure_observability()
app = FastAPI(title="Enterprise Knowledge Base Agent", version="0.1.0")
frontend_dir = Path(__file__).resolve().parents[1] / "static"
app.mount("/assets", StaticFiles(directory=frontend_dir), name="assets")
store, ingestion_service, qa_service = create_demo_services()
dead_letter_queue = create_dead_letter_queue(store)
index_job_service = create_index_job_service(
    store,
    ingestion_service,
    dlq=dead_letter_queue,
)
identity_directory = create_identity_directory(store)
research_job_service = create_research_job_service(
    store,
    qa_service,
    identity_directory,
    dlq=dead_letter_queue,
)
identity_provisioning_store = create_identity_provisioning_store(store)
graph_sync_service = create_microsoft_graph_sync_service(identity_provisioning_store)
configure_authenticator(JWTAuthenticator(identity_directory))
authenticator = get_authenticator()
frontend_security_headers = browser_security_headers(
    authenticator.issuer if authenticator.mode == "oidc" else ""
)
if identity_provisioning_store is not None:
    app.add_exception_handler(SCIMException, scim_exception_handler)
    app.include_router(
        create_scim_router(identity_provisioning_store, SCIMConfig.from_env())
    )


@app.middleware("http")
async def secure_browser_responses(request: Request, call_next):
    response = await call_next(request)
    for name, value in frontend_security_headers.items():
        if name == "Content-Security-Policy" and request.url.path != "/":
            continue
        response.headers.setdefault(name, value)
    if not request.url.path.startswith("/assets/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.middleware("http")
async def observe_requests(request: Request, call_next):
    candidate = request.headers.get("X-Request-ID", "").strip()
    request_id = candidate if candidate and len(candidate) <= 96 else new_id("req")
    query_id = new_id("query") if request.url.path == "/chat/query" else None
    started = time.perf_counter()
    response = None
    status_code = 500
    with correlation_context(request_id=request_id, query_id=query_id):
        try:
            with observed_span(
                "http.request",
                attributes={"http.method": request.method},
                stage="http",
            ) as active_span:
                response = await call_next(request)
                status_code = response.status_code
                route = getattr(request.scope.get("route"), "path", request.url.path)
                context = current_context()
                if active_span is not None:
                    active_span.set_attributes(
                        {
                            "http.route": route,
                            "http.status_code": status_code,
                            **{
                                f"correlation.{key}": value
                                for key, value in context.items()
                            },
                        }
                    )
        finally:
            if response is not None:
                response.headers["X-Request-ID"] = request_id
                if query_id:
                    response.headers["X-Query-ID"] = query_id
                run_id = getattr(request.state, "observation_run_id", None)
                if run_id:
                    response.headers["X-Run-ID"] = run_id
            record_http_request(
                method=request.method,
                route=getattr(request.scope.get("route"), "path", request.url.path),
                status_code=status_code,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
    return response


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str
    limit: int = 5


class ResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=2, max_length=4000)
    per_query_limit: int = Field(default=5, ge=2, le=10)
    max_rounds: int = Field(default=3, ge=1, le=5)


class UploadDocumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str
    title: str | None = None
    department_id: str | None = None
    acl_departments: list[str] = Field(default_factory=list)
    content_text: str | None = None
    content_base64: str | None = None


class DevTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    department_ids: list[str] = Field(default_factory=list)
    role_ids: list[str] = Field(default_factory=list)
    email: str | None = None
    display_name: str | None = None


class DirectoryUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_id: str
    user_id: str
    subject: str
    issuer: str
    email: str | None = None
    display_name: str | None = None
    active: bool = True
    attributes: dict[str, object] = Field(default_factory=dict)


class DirectoryUnitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_id: str
    unit_id: str
    name: str
    active: bool = True
    attributes: dict[str, object] = Field(default_factory=dict)


class DirectoryMembershipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_external_id: str
    unit_external_id: str


class DirectorySyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    users: list[DirectoryUserRequest]
    departments: list[DirectoryUnitRequest] = Field(default_factory=list)
    roles: list[DirectoryUnitRequest] = Field(default_factory=list)
    user_departments: list[DirectoryMembershipRequest] = Field(default_factory=list)
    user_roles: list[DirectoryMembershipRequest] = Field(default_factory=list)
    deactivate_missing: bool = True


class GraphResourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resources: list[str] = Field(default_factory=lambda: list(GRAPH_RESOURCES))
    reset_cursor: bool = False


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/admin/observability")
def observability_status_endpoint(
    _: AuthenticatedUser = Depends(require_admin),
) -> dict[str, object]:
    return {
        "status": observability_status(),
        "metrics": metrics_snapshot(),
    }


@app.get("/admin/embedding")
def embedding_status(_: AuthenticatedUser = Depends(require_admin)) -> dict[str, object]:
    provider = ingestion_service.embedding_provider
    result: dict[str, object] = {
        "provider": provider.name,
        "model": provider.model,
        "dimensions": provider.dimensions,
        "store": type(store).__name__,
    }
    get_dimensions = getattr(store, "get_embedding_dimensions", None)
    if callable(get_dimensions):
        result["database_dimensions"] = get_dimensions()
        result["dimensions_match"] = result["database_dimensions"] == provider.dimensions
    return result


@app.post("/admin/embedding/probe")
def probe_embedding(_: AuthenticatedUser = Depends(require_admin)) -> dict[str, object]:
    try:
        probe = ingestion_service.embedding_provider.probe()
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail={
                "status": "failed",
                "provider": ingestion_service.embedding_provider.name,
                "error_type": type(error).__name__,
                "message": str(error),
            },
        ) from error
    return {
        "status": "ok",
        "provider": probe.provider,
        "model": probe.model,
        "dimensions": probe.dimensions,
        "elapsed_ms": probe.elapsed_ms,
        "vector_norm": probe.vector_norm,
    }


@app.get("/admin/pipeline")
def pipeline_status(_: AuthenticatedUser = Depends(require_admin)) -> dict[str, object]:
    return {
        "store": type(store).__name__,
        "embedding": qa_service.embedding_provider.name,
        "embedding_dimensions": qa_service.embedding_provider.dimensions,
        "reranker": qa_service.reranker.name,
        "claim_generator": qa_service.claim_generator.name,
        "citation_binder": type(qa_service.binder).__name__,
        "evidence_verifier": type(qa_service.verifier).__name__,
        "pdf_parser": "docling",
        "object_storage": ingestion_service.object_storage.name,
        "job_dispatcher": index_job_service.dispatcher.name,
        "auth_mode": authenticator.mode,
        "identity_mode": authenticator.identity_mode,
        "identity_directory": identity_directory.name,
        "graph_provisioning": bool(
            graph_sync_service and graph_sync_service.config.enabled
        ),
        "scim_provisioning": SCIMConfig.from_env().enabled,
    }


@app.get("/", response_class=HTMLResponse)
def root() -> str:
    return (frontend_dir / "index.html").read_text(encoding="utf-8")


@app.get("/auth/config")
def auth_config() -> dict[str, object]:
    if authenticator.mode != "oidc":
        return {
            "mode": "local",
            "dev_token_enabled": authenticator.allow_dev_tokens,
        }
    issuer = authenticator.issuer.rstrip("/")
    authority = issuer.removesuffix("/v2.0")
    return {
        "mode": "oidc",
        "client_id": os.getenv("KNOWLEDGE_OIDC_CLIENT_ID", "").strip(),
        "authority": authority,
        "authorization_endpoint": f"{authority}/oauth2/v2.0/authorize",
        "token_endpoint": f"{authority}/oauth2/v2.0/token",
        "scope": os.getenv("KNOWLEDGE_OIDC_SCOPE", "").strip(),
        "redirect_uri": os.getenv("KNOWLEDGE_OIDC_REDIRECT_URI", "").strip(),
        "post_logout_redirect_uri": os.getenv(
            "KNOWLEDGE_OIDC_POST_LOGOUT_REDIRECT_URI", ""
        ).strip(),
        "token_refresh_skew_seconds": max(
            30,
            int(os.getenv("KNOWLEDGE_OIDC_TOKEN_REFRESH_SKEW_SECONDS", "120")),
        ),
    }


@app.post("/auth/dev-token")
def issue_dev_token(request: DevTokenRequest) -> dict[str, object]:
    try:
        token, expires_in = authenticator.issue_local_token(
            user_id=request.user_id,
            department_ids=request.department_ids,
            role_ids=request.role_ids,
            email=request.email,
            display_name=request.display_name,
        )
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": expires_in,
    }


@app.get("/auth/me")
def auth_me(user: AuthenticatedUser = Depends(get_current_user)) -> dict[str, object]:
    return {
        "user_id": user.user_id,
        "email": user.email,
        "display_name": user.display_name,
        "department_ids": list(user.department_ids),
        "role_ids": list(user.role_ids),
        "issuer": user.issuer,
        "identity_source": user.identity_source,
    }


@app.get("/admin/directory")
def directory_status(
    _: AuthenticatedUser = Depends(require_admin),
) -> dict[str, object]:
    return {
        "identity_mode": authenticator.identity_mode,
        **identity_directory.status(),
    }


@app.post("/admin/directory/sync")
def sync_directory(
    request: DirectorySyncRequest,
    _: AuthenticatedUser = Depends(require_admin),
) -> dict[str, object]:
    if request.source == "local-dev":
        raise HTTPException(status_code=400, detail="local-dev is a reserved source")
    snapshot = DirectorySyncSnapshot(
        source=request.source,
        users=tuple(
            DirectoryUser(
                external_id=item.external_id,
                user_id=item.user_id,
                subject=item.subject,
                issuer=item.issuer,
                email=item.email,
                display_name=item.display_name,
                active=item.active,
                attributes=item.attributes,
            )
            for item in request.users
        ),
        departments=tuple(
            DirectoryUnit(
                external_id=item.external_id,
                unit_id=item.unit_id,
                name=item.name,
                active=item.active,
                attributes=item.attributes,
            )
            for item in request.departments
        ),
        roles=tuple(
            DirectoryUnit(
                external_id=item.external_id,
                unit_id=item.unit_id,
                name=item.name,
                active=item.active,
                attributes=item.attributes,
            )
            for item in request.roles
        ),
        user_departments=tuple(
            DirectoryMembership(item.user_external_id, item.unit_external_id)
            for item in request.user_departments
        ),
        user_roles=tuple(
            DirectoryMembership(item.user_external_id, item.unit_external_id)
            for item in request.user_roles
        ),
        deactivate_missing=request.deactivate_missing,
    )
    try:
        result = identity_directory.sync(snapshot)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {
        "run_id": result.run_id,
        "source": result.source,
        "user_count": result.user_count,
        "department_count": result.department_count,
        "role_count": result.role_count,
        "user_department_count": result.user_department_count,
        "user_role_count": result.user_role_count,
        "deactivated_users": result.deactivated_users,
        "deactivated_departments": result.deactivated_departments,
        "deactivated_roles": result.deactivated_roles,
        "completed_at": result.completed_at.isoformat(),
    }


@app.get("/admin/directory/graph")
def graph_directory_status(
    _: AuthenticatedUser = Depends(require_admin),
) -> dict[str, object]:
    service = require_graph_service()
    return service.status()


@app.post("/admin/directory/graph/sync", status_code=status.HTTP_202_ACCEPTED)
def sync_graph_directory(
    request: GraphResourceRequest,
    background_tasks: BackgroundTasks,
    _: AuthenticatedUser = Depends(require_admin),
) -> dict[str, object]:
    service = require_graph_service(enabled=True)
    try:
        reset_count = (
            service.reset_cursors(request.resources) if request.reset_cursor else 0
        )
        dispatcher = dispatch_graph_sync(request.resources, background_tasks)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {
        "status": "queued",
        "resources": request.resources,
        "dispatcher": dispatcher,
        "tenant_id": service.config.tenant_id,
        "reset_cursors": reset_count,
    }


@app.post("/admin/directory/graph/subscriptions")
def create_graph_subscriptions(
    request: GraphResourceRequest,
    _: AuthenticatedUser = Depends(require_admin),
) -> dict[str, object]:
    service = require_graph_service(enabled=True)
    try:
        subscriptions = service.create_subscriptions(request.resources)
    except httpx.HTTPStatusError as error:
        raise HTTPException(
            status_code=502,
            detail="Microsoft Graph subscription creation failed",
        ) from error
    except (RuntimeError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"subscriptions": subscriptions}


@app.post("/admin/directory/graph/subscriptions/reconcile")
def reconcile_graph_subscriptions(
    request: GraphResourceRequest,
    _: AuthenticatedUser = Depends(require_admin),
) -> dict[str, object]:
    service = require_graph_service(enabled=True)
    try:
        return service.reconcile_subscriptions(request.resources)
    except (RuntimeError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/admin/directory/graph/subscriptions/{subscription_id}/renew")
def renew_graph_subscription(
    subscription_id: str,
    _: AuthenticatedUser = Depends(require_admin),
) -> dict[str, object]:
    service = require_graph_service(enabled=True)
    try:
        return service.renew_subscription(subscription_id)
    except httpx.HTTPStatusError as error:
        raise HTTPException(
            status_code=502,
            detail="Microsoft Graph subscription renewal failed",
        ) from error
    except (RuntimeError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post(
    "/webhooks/microsoft-graph-lifecycle",
    status_code=status.HTTP_202_ACCEPTED,
)
@app.post("/webhooks/microsoft-graph", status_code=status.HTTP_202_ACCEPTED)
async def microsoft_graph_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
):
    validation_token = request.query_params.get("validationToken")
    if validation_token is not None:
        return PlainTextResponse(validation_token, status_code=200)

    service = require_graph_service(enabled=True)
    assert identity_provisioning_store is not None
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > 1_048_576:
        raise HTTPException(status_code=413, detail="Webhook payload too large")
    try:
        raw_body = await request.body()
        if len(raw_body) > 1_048_576:
            raise HTTPException(status_code=413, detail="Webhook payload too large")
        payload = json.loads(raw_body)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from error
    notifications = payload.get("value", []) if isinstance(payload, dict) else []
    if not isinstance(notifications, list):
        raise HTTPException(status_code=400, detail="Webhook value must be a list")

    accepted: list[dict[str, object]] = []
    lifecycle_notifications: list[dict[str, object]] = []
    rejected = 0
    for item in notifications:
        if not isinstance(item, dict):
            rejected += 1
            continue
        tenant_valid = not service.config.tenant_id or str(
            item.get("tenantId", "")
        ) == service.config.tenant_id
        client_state_valid = (
            tenant_valid
            and service.config.webhook_client_state_matches(item.get("clientState"))
        )
        inserted = identity_provisioning_store.record_webhook_notification(
            item, client_state_valid=client_state_valid
        )
        if client_state_valid and inserted:
            accepted.append(item)
            if item.get("lifecycleEvent"):
                lifecycle_notifications.append(item)
        elif not client_state_valid:
            rejected += 1

    ordinary_notifications = [
        item for item in accepted if not item.get("lifecycleEvent")
    ]
    resources = resources_from_notifications(ordinary_notifications)
    if ordinary_notifications and not resources:
        resources = set(GRAPH_RESOURCES)
    dispatcher = None
    if resources:
        dispatcher = dispatch_graph_sync(sorted(resources), background_tasks)
    lifecycle_dispatcher = None
    if lifecycle_notifications:
        lifecycle_dispatcher = dispatch_graph_subscription_maintenance(background_tasks)
    return {
        "status": "accepted",
        "queued_notifications": len(accepted),
        "rejected_notifications": rejected,
        "resources": sorted(resources),
        "dispatcher": dispatcher,
        "lifecycle_notifications": len(lifecycle_notifications),
        "lifecycle_dispatcher": lifecycle_dispatcher,
    }


def require_graph_service(*, enabled: bool = False):
    if graph_sync_service is None:
        raise HTTPException(
            status_code=503,
            detail="Microsoft Graph provisioning requires PostgreSQL",
        )
    if enabled and not graph_sync_service.config.enabled:
        raise HTTPException(
            status_code=503, detail="Microsoft Graph provisioning is disabled"
        )
    return graph_sync_service


def dispatch_graph_sync(
    resources: list[str], background_tasks: BackgroundTasks
) -> str:
    service = require_graph_service(enabled=True)
    normalized = sorted(normalize_graph_resources(resources))
    if os.getenv("KNOWLEDGE_JOB_MODE", "inline").strip().lower() == "dramatiq":
        from backend.app.jobs.tasks import process_graph_directory_sync

        process_graph_directory_sync.send(normalized)
        return "dramatiq"
    background_tasks.add_task(service.sync, normalized)
    return "background-task"


def dispatch_graph_subscription_maintenance(background_tasks: BackgroundTasks) -> str:
    require_graph_service(enabled=True)
    if os.getenv("KNOWLEDGE_JOB_MODE", "inline").strip().lower() == "dramatiq":
        from backend.app.jobs.tasks import process_graph_subscription_maintenance

        process_graph_subscription_maintenance.send()
        return "dramatiq"
    background_tasks.add_task(
        process_graph_subscription_maintenance_inline,
    )
    return "background-task"


def process_graph_subscription_maintenance_inline() -> None:
    service = require_graph_service(enabled=True)
    service.handle_lifecycle_notifications()


@app.get("/documents")
def list_documents(
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[dict[str, object]]:
    return [
        {
            "document_id": document.document_id,
            "title": document.title,
            "source_type": document.source_type.value,
            "owner_id": document.owner_id,
            "department_id": document.department_id,
            "created_at": document.created_at.isoformat(),
            "filename": document.metadata.get("filename"),
        }
        for document in store.list_accessible_documents(user.scope)
    ]


@app.get("/documents/{document_id}")
def get_document_detail(
    document_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    document = store.get_accessible_document(document_id, user.scope)
    if document is None:
        raise HTTPException(status_code=404, detail="document not found")
    versions = store.list_accessible_document_versions(document_id, user.scope)
    current_version = next((item for item in versions if item.is_current), None)
    return serialize_document(document, current_version=current_version)


@app.get("/documents/{document_id}/versions")
def list_document_versions(
    document_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[dict[str, object]]:
    document = store.get_accessible_document(document_id, user.scope)
    if document is None:
        raise HTTPException(status_code=404, detail="document not found")
    return [
        serialize_document_version(version)
        for version in store.list_accessible_document_versions(document_id, user.scope)
    ]


@app.get("/documents/{document_id}/versions/{version_id}/pages/{page_number}")
def get_pdf_page(
    document_id: str,
    version_id: str,
    page_number: int,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    document, version = require_accessible_version(document_id, version_id, user)
    if document.source_type.value != "pdf":
        raise HTTPException(status_code=415, detail="document is not a PDF")
    raw_bytes = read_verified_version_source(version)
    try:
        rendered = render_pdf_page(raw_bytes, page_number)
    except PDFPreviewError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return Response(
        content=rendered.png_bytes,
        media_type="image/png",
        headers={
            "Cache-Control": "private, max-age=60",
            "ETag": f'"{version.content_hash}-page-{page_number}"',
            "X-PDF-Page-Count": str(rendered.page_count),
            "X-Image-Width": str(rendered.width),
            "X-Image-Height": str(rendered.height),
        },
    )


@app.get("/chunks/{chunk_id}")
def get_chunk_detail(
    chunk_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    chunk = store.get_accessible_chunk(chunk_id, user.scope)
    if chunk is None:
        raise HTTPException(status_code=404, detail="chunk not found")
    document = store.get_accessible_document(chunk.document_id, user.scope)
    if document is None:
        raise HTTPException(status_code=404, detail="chunk not found")
    versions = store.list_accessible_document_versions(document.document_id, user.scope)
    version = next(
        (item for item in versions if item.version_id == chunk.version_id),
        None,
    )
    return serialize_chunk(chunk, document=document, version=version)


@app.get("/chunks/{chunk_id}/preview")
def get_chunk_preview(
    chunk_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    chunk = store.get_accessible_chunk(chunk_id, user.scope)
    if chunk is None:
        raise HTTPException(status_code=404, detail="chunk not found")
    document, version = require_accessible_version(
        chunk.document_id,
        chunk.version_id,
        user,
    )
    if document.source_type.value != "pdf":
        raise HTTPException(status_code=415, detail="chunk source is not a PDF")
    raw_bytes = read_verified_version_source(version)
    try:
        location = locate_pdf_chunk(
            raw_bytes,
            chunk.page,
            chunk.content,
            chunk.metadata,
        )
    except PDFPreviewError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {
        "preview_type": "pdf-page",
        "document_id": document.document_id,
        "version_id": version.version_id,
        "chunk_id": chunk.chunk_id,
        "page": location.page_number,
        "page_count": location.page_count,
        "page_width": location.page_width,
        "page_height": location.page_height,
        "match_method": location.match_method,
        "highlights": [
            {
                "x": item.x,
                "y": item.y,
                "width": item.width,
                "height": item.height,
            }
            for item in location.highlights
        ],
        "page_image_url": (
            f"/documents/{document.document_id}/versions/{version.version_id}"
            f"/pages/{location.page_number}"
        ),
    }


@app.post("/documents/upload", status_code=status.HTTP_202_ACCEPTED)
def upload_document(
    request: UploadDocumentRequest,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    if request.content_base64:
        try:
            raw = base64.b64decode(request.content_base64, validate=True)
        except (binascii.Error, ValueError) as error:
            raise HTTPException(status_code=400, detail="Invalid base64 content") from error
    elif request.content_text is not None:
        raw = request.content_text.encode("utf-8")
    else:
        raise HTTPException(
            status_code=400,
            detail="content_text or content_base64 is required",
        )

    return register_and_enqueue_document(
        filename=request.filename,
        raw_bytes=raw,
        title=request.title,
        department_id=request.department_id,
        acl_departments=request.acl_departments,
        user=user,
    )


@app.post("/documents/upload-file", status_code=status.HTTP_202_ACCEPTED)
async def upload_document_file(
    file: UploadFile = File(...),
    title: str | None = Form(None),
    department_id: str | None = Form(None),
    acl_departments: str = Form(""),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    max_bytes = upload_max_bytes()
    raw = await file.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise HTTPException(status_code=413, detail="Document exceeds upload limit")
    return register_and_enqueue_document(
        filename=file.filename or "document.bin",
        raw_bytes=raw,
        title=title,
        department_id=department_id,
        acl_departments=[item.strip() for item in acl_departments.split(",") if item.strip()],
        user=user,
    )


@app.post("/documents/{document_id}/reindex", status_code=status.HTTP_202_ACCEPTED)
def reindex_document(
    document_id: str,
    user: AuthenticatedUser = Depends(require_admin),
) -> dict[str, object]:
    version = store.get_current_version(document_id)
    if version is None:
        raise HTTPException(status_code=404, detail="document not found")
    job = index_job_service.submit(
        document_id=document_id,
        version_id=version.version_id,
        requested_by=user.user_id,
    )
    return serialize_job(job)


@app.post("/admin/reindex", status_code=status.HTTP_202_ACCEPTED)
def reindex_all(
    user: AuthenticatedUser = Depends(require_admin),
) -> dict[str, object]:
    jobs = []
    for document in store.list_documents():
        version = store.get_current_version(document.document_id)
        if version is None:
            continue
        jobs.append(
            index_job_service.submit(
                document_id=document.document_id,
                version_id=version.version_id,
                requested_by=user.user_id,
            )
        )
    return {"status": "queued", "jobs": [serialize_job(job) for job in jobs]}


@app.get("/jobs")
def list_jobs(
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[dict[str, object]]:
    jobs = index_job_service.repository.list_jobs()
    if not user.is_admin:
        jobs = [job for job in jobs if job.requested_by == user.user_id]
    return [serialize_job(job) for job in jobs]


@app.get("/admin/jobs/dead-letter")
def list_dead_letter_jobs(
    status: str = "pending",
    limit: int = 100,
    _: AuthenticatedUser = Depends(require_admin),
) -> list[dict[str, object]]:
    try:
        entries = dead_letter_queue.list(status=status, limit=limit)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return [serialize_dead_letter(item) for item in entries]


@app.post("/admin/jobs/dead-letter/{dlq_id}/replay")
def replay_dead_letter_job(
    dlq_id: str,
    _: AuthenticatedUser = Depends(require_admin),
) -> dict[str, object]:
    entry = dead_letter_queue.get(dlq_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="dead-letter entry not found")
    try:
        if entry.job_type == "indexing":
            job = index_job_service.replay_dead_letter(dlq_id)
            serialized_job = serialize_job(job) if job else None
        else:
            job = research_job_service.replay_dead_letter(dlq_id)
            serialized_job = serialize_research_job(job) if job else None
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=503, detail="dead-letter replay unavailable") from error
    return {"dead_letter": serialize_dead_letter(dead_letter_queue.get(dlq_id)), "job": serialized_job}


@app.post("/admin/jobs/dead-letter/{dlq_id}/discard")
def discard_dead_letter_job(
    dlq_id: str,
    _: AuthenticatedUser = Depends(require_admin),
) -> dict[str, object]:
    entry = dead_letter_queue.get(dlq_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="dead-letter entry not found")
    try:
        discarded = (
            index_job_service.discard_dead_letter(dlq_id)
            if entry.job_type == "indexing"
            else research_job_service.discard_dead_letter(dlq_id)
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"dead_letter": serialize_dead_letter(discarded)}


@app.get("/jobs/{job_id}")
def get_job(
    job_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    job = require_visible_job(job_id, user)
    return serialize_job(job)


@app.post("/jobs/{job_id}/retry", status_code=status.HTTP_202_ACCEPTED)
def retry_job(
    job_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    require_visible_job(job_id, user)
    try:
        job = index_job_service.retry(job_id)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return serialize_job(job)


@app.post("/jobs/{job_id}/cancel")
def cancel_job(
    job_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    require_visible_job(job_id, user)
    job = index_job_service.cancel(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return serialize_job(job)


@app.post("/chat/query")
def query(
    request: QueryRequest,
    http_request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    try:
        answer = qa_service.answer(request.question, user.scope, limit=request.limit)
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail="模型或检索服务暂时不可用，请稍后重试",
        ) from error
    return serialize_knowledge_answer(answer)


@app.post("/research/jobs", status_code=status.HTTP_202_ACCEPTED)
def create_research_job(
    request: ResearchRequest,
    http_request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    try:
        job = research_job_service.submit(
            question=request.question.strip(),
            requested_by=user.user_id,
            subject=user.scope,
            identity_issuer=user.issuer,
            identity_subject=user.subject,
            max_rounds=request.max_rounds,
            per_query_limit=request.per_query_limit,
        )
    except Exception as error:
        raise HTTPException(status_code=503, detail=f"Research queue unavailable: {error}") from error
    http_request.state.observation_run_id = job.job_id
    set_context_value("run_id", job.job_id)
    return serialize_research_job(job)


@app.get("/research/jobs")
def list_research_jobs(
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[dict[str, object]]:
    jobs = research_job_service.repository.list_jobs()
    if not user.is_admin:
        jobs = [
            job
            for job in jobs
            if job.requested_by == user.user_id
            and research_result_is_still_accessible(job, user)
        ]
    return [serialize_research_job(job) for job in jobs]


@app.get("/research/jobs/{job_id}")
def get_research_job(
    job_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    return serialize_research_job(require_visible_research_job(job_id, user))


@app.post("/research/jobs/{job_id}/cancel")
def cancel_research_job(
    job_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    require_visible_research_job(job_id, user)
    job = research_job_service.cancel(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="research job not found")
    return serialize_research_job(job)


def register_and_enqueue_document(
    *,
    filename: str,
    raw_bytes: bytes,
    title: str | None,
    department_id: str | None,
    acl_departments: list[str],
    user: AuthenticatedUser,
) -> dict[str, object]:
    departments = acl_departments or ([department_id] if department_id else [])
    departments = list(dict.fromkeys(item for item in departments if item))
    if not user.is_admin:
        forbidden = set(departments) - set(user.department_ids)
        if forbidden:
            raise HTTPException(
                status_code=403,
                detail=f"Cannot grant ACL for departments: {sorted(forbidden)}",
            )
    acl = (
        [ACLEntry(SubjectType.DEPARTMENT, item, Permission.READ) for item in departments]
        if departments
        else [ACLEntry(SubjectType.USER, user.user_id, Permission.READ)]
    )
    try:
        registration = ingestion_service.register_document(
            filename=filename,
            raw_bytes=raw_bytes,
            title=title,
            owner_id=user.user_id,
            department_id=department_id,
            acl=acl,
        )
        job = index_job_service.submit(
            document_id=str(registration["document_id"]),
            version_id=str(registration["version_id"]),
            requested_by=user.user_id,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=503, detail=f"Index queue unavailable: {error}") from error
    return {**registration, "job": serialize_job(job)}


def require_accessible_version(
    document_id: str,
    version_id: str,
    user: AuthenticatedUser,
) -> tuple[Document, DocumentVersion]:
    document = store.get_accessible_document(document_id, user.scope)
    if document is None:
        raise HTTPException(status_code=404, detail="document not found")
    version = next(
        (
            item
            for item in store.list_accessible_document_versions(
                document_id,
                user.scope,
            )
            if item.version_id == version_id
        ),
        None,
    )
    if version is None:
        raise HTTPException(status_code=404, detail="document version not found")
    return document, version


def read_verified_version_source(version: DocumentVersion) -> bytes:
    try:
        raw_bytes = ingestion_service.object_storage.read(version.storage_uri)
    except ObjectStorageNotFound as error:
        raise HTTPException(status_code=404, detail="document source not found") from error
    except ObjectStorageError as error:
        raise HTTPException(
            status_code=503,
            detail="document storage is temporarily unavailable",
        ) from error
    actual_hash = hashlib.sha256(raw_bytes).hexdigest()
    if actual_hash != version.content_hash:
        raise HTTPException(
            status_code=409,
            detail="document source integrity check failed",
        )
    return raw_bytes


def require_visible_job(job_id: str, user: AuthenticatedUser) -> IndexJob:
    job = index_job_service.repository.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if not user.is_admin and job.requested_by != user.user_id:
        raise HTTPException(status_code=403, detail="job is not visible to this user")
    return job


def serialize_job(job: IndexJob) -> dict[str, object]:
    return {
        "job_id": job.job_id,
        "document_id": job.document_id,
        "version_id": job.version_id,
        "requested_by": job.requested_by,
        "status": job.status.value,
        "progress": job.progress,
        "attempts": job.attempts,
        "error_message": job.error_message,
        "result": job.result,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
    }


def serialize_dead_letter(entry: DeadLetterEntry | None) -> dict[str, object]:
    if entry is None:
        return {}
    return {
        "dlq_id": entry.dlq_id,
        "job_type": entry.job_type,
        "job_id": entry.job_id,
        "payload": entry.payload,
        "error_type": entry.error_type,
        "error_message": entry.error_message,
        "attempts": entry.attempts,
        "status": entry.status,
        "created_at": entry.created_at.isoformat(),
        "replayed_at": entry.replayed_at.isoformat() if entry.replayed_at else None,
        "updated_at": entry.updated_at.isoformat(),
    }


def require_visible_research_job(
    job_id: str,
    user: AuthenticatedUser,
) -> ResearchJob:
    job = research_job_service.repository.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="research job not found")
    if not user.is_admin and job.requested_by != user.user_id:
        raise HTTPException(status_code=403, detail="research job is not visible to this user")
    if not user.is_admin and not research_result_is_still_accessible(job, user):
        raise HTTPException(
            status_code=403,
            detail="research result citations are no longer accessible to this user",
        )
    return job


def research_result_is_still_accessible(
    job: ResearchJob,
    user: AuthenticatedUser,
) -> bool:
    answer = job.result.get("answer") if isinstance(job.result, dict) else None
    if not isinstance(answer, dict):
        return True
    citations = answer.get("citations", [])
    if not citations:
        return True
    for citation in citations:
        if not isinstance(citation, dict):
            return False
        chunk_id = str(citation.get("chunk_id", "")).strip()
        version_id = str(citation.get("version_id", "")).strip()
        if not chunk_id or not version_id:
            return False
        chunk = store.get_accessible_chunk(chunk_id, user.scope)
        if chunk is None or chunk.version_id != version_id:
            return False
        current_version = store.get_current_version(chunk.document_id)
        if current_version is None or current_version.version_id != version_id:
            return False
    return True


def serialize_research_job(job: ResearchJob) -> dict[str, object]:
    return {
        "job_id": job.job_id,
        "question": job.question,
        "requested_by": job.requested_by,
        "status": job.status.value,
        "stage": job.stage,
        "progress": job.progress,
        "attempts": job.attempts,
        "error_message": job.error_message,
        "result": job.result,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
    }


def serialize_document(
    document: Document,
    *,
    current_version: DocumentVersion | None,
) -> dict[str, object]:
    return {
        "document_id": document.document_id,
        "title": document.title,
        "source_type": document.source_type.value,
        "owner_id": document.owner_id,
        "department_id": document.department_id,
        "created_at": document.created_at.isoformat(),
        "metadata": document.metadata,
        "current_version": (
            serialize_document_version(current_version)
            if current_version is not None
            else None
        ),
    }


def serialize_document_version(version: DocumentVersion) -> dict[str, object]:
    return {
        "version_id": version.version_id,
        "document_id": version.document_id,
        "version_number": version.version_number,
        "content_hash": version.content_hash,
        "is_current": version.is_current,
        "created_at": version.created_at.isoformat(),
    }


def serialize_chunk(
    chunk: DocumentChunk,
    *,
    document: Document,
    version: DocumentVersion | None,
) -> dict[str, object]:
    return {
        "chunk_id": chunk.chunk_id,
        "document_id": chunk.document_id,
        "version_id": chunk.version_id,
        "page": chunk.page,
        "section_path": chunk.section_path,
        "content": chunk.content,
        "content_hash": chunk.content_hash,
        "metadata": chunk.metadata,
        "document": {
            "document_id": document.document_id,
            "title": document.title,
            "source_type": document.source_type.value,
            "owner_id": document.owner_id,
            "department_id": document.department_id,
        },
        "version": serialize_document_version(version) if version is not None else None,
    }
