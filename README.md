# Enterprise Knowledge Base Agent

企业知识库 Agent：文档解析与切片、ACL 检索、引用校验、异步任务和身份目录。

## Setup

Install Python dependencies:

```powershell
pip install -r requirements.txt
```

Create the dedicated conda environment:

```powershell
conda env create -f environment.yml
conda activate enterprise-kb-agent
```

Or, if the environment already exists:

```powershell
conda activate enterprise-kb-agent
python -m pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in your key (program loads `.env` automatically):

```powershell
copy .env.example .env
```

Or set environment variables in the current shell:

```powershell
$env:OPENAI_API_KEY="your_api_key_here"
$env:OPENAI_MODEL="gpt-5.6"
$env:OPENAI_BASE_URL="https://your-provider.example.com/v1"
```

## Architecture

```text
backend/app/ingestion      document parsing and chunking
backend/app/security       ACL checks before retrieval
backend/app/retrieval      hybrid keyword/vector retrieval
backend/app/llm            structured JSON model provider
backend/app/agent          claims, Citation Binder, and verification
backend/app/api            FastAPI endpoints
backend/app/sql            PostgreSQL/pgvector schema draft
```

Run tests:

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

Run the API:

```powershell
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8010
```

Build and test the browser frontend after changing files under
`backend/app/static`:

```powershell
npm install
npm test
npm run build
```

The checked-in `app.bundle.js` contains fixed versions of MSAL Browser and
Lucide. Production OIDC keeps the MSAL cache in per-tab `sessionStorage`,
renews access tokens silently before expiry, retries one 401 after a forced
renewal, and coordinates sign-in/sign-out between tabs without sharing access
tokens.

The checked-in `.env.knowledge` profile uses PostgreSQL, real OpenAI-compatible
embeddings, LLM reranking, and structured claims. Secrets remain in `.env`.
Override `KNOWLEDGE_STORE=sqlite` or `memory` for offline development.

Run PostgreSQL + pgvector, Redis, and MinIO locally:

```powershell
docker compose -f docker-compose.postgres.yml up -d
$env:KNOWLEDGE_STORE="postgres"
$env:KNOWLEDGE_DATABASE_URL="postgresql://knowledge:knowledge@127.0.0.1:5432/knowledge"
$env:KNOWLEDGE_REDIS_URL="redis://127.0.0.1:6380/0"
$env:KNOWLEDGE_OBJECT_STORAGE="minio"
$env:KNOWLEDGE_S3_ENDPOINT="http://127.0.0.1:9000"
D:\Anaconda\envs\enterprise-kb-agent\python.exe -m dramatiq backend.app.jobs.tasks --processes 1 --threads 2
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8010
```

MinIO exposes its S3 endpoint on `9000` and development console on `9001`.
New uploads use immutable `s3://bucket/key` references. Migrate existing local
versions with read-back SHA-256 verification while retaining rollback copies:

```powershell
python scripts/migrate_objects_to_s3.py --confirm
```

The API validates JWT Bearer tokens, then resolves the verified issuer/subject
through the PostgreSQL identity directory. ACL departments and roles come from
the directory, not self-reported token claims. Local development can use
`POST /auth/dev-token`; production switches to OIDC issuer/audience/JWKS
validation and disables local token issuing.

Validate or import a provider-neutral user/department/role snapshot before an
OIDC cutover:

```powershell
python scripts/sync_identity_directory.py docs/identity-directory.example.json --dry-run
python scripts/sync_identity_directory.py docs/identity-directory.example.json
```

Microsoft Entra ID can now provision the same directory through Microsoft
Graph `users/delta` and `groups/delta`. The final `@odata.deltaLink` is stored
in PostgreSQL only after the page set is applied. A Graph webhook validates
`clientState`, deduplicates notifications, and queues a Dramatiq delta sync.

```text
GET  /admin/directory/graph
POST /admin/directory/graph/sync
POST /admin/directory/graph/subscriptions
POST /admin/directory/graph/subscriptions/reconcile
POST /admin/directory/graph/subscriptions/{subscription_id}/renew
POST /webhooks/microsoft-graph
POST /webhooks/microsoft-graph-lifecycle
```

Graph subscription maintenance is safe to run as a one-shot scheduler task. It
creates missing subscriptions, renews them before expiry, recreates subscriptions
that Graph reports as gone, and performs lifecycle-event recovery. Run it every
5-15 minutes after deploying both webhook URLs behind a stable public HTTPS domain:

```powershell
D:\Anaconda\envs\enterprise-kb-agent\python.exe scripts\maintain_graph_subscriptions.py
```

The SCIM 2.0 base URL is `http://127.0.0.1:8010/scim/v2` in local development.
It exposes service metadata plus `/Users` and `/Groups` CRUD/PATCH endpoints,
using `KNOWLEDGE_SCIM_TOKEN` instead of an employee JWT. Validate both
provisioning paths against the running API and PostgreSQL with:

```powershell
python scripts/scim_api_smoke_test.py
python scripts/graph_delta_postgres_smoke_test.py
```

Run the deterministic security gates before production changes:

```powershell
D:\Anaconda\envs\enterprise-kb-agent\python.exe scripts\run_security_eval.py
D:\Anaconda\envs\enterprise-kb-agent\python.exe scripts\run_security_eval.py --strict
```

The suite covers ACL isolation, deactivated identities, document Prompt Injection,
model output boundaries, spoofed file types, active PDF content, macro DOCX files,
and compressed archive abuse. `--strict` also requires
`KNOWLEDGE_VIRUS_SCANNER_COMMAND` to be configured; rejected or unavailable scans
are quarantined and never indexed.

Enable OpenTelemetry traces and token/cost/latency metrics in production:

```powershell
$env:KNOWLEDGE_OTEL_ENABLED="1"
$env:KNOWLEDGE_OTEL_EXPORTER="otlp"
$env:KNOWLEDGE_OTEL_ENDPOINT="http://127.0.0.1:4318"
$env:KNOWLEDGE_OTEL_SAMPLE_RATIO="1.0"
$env:KNOWLEDGE_LLM_INPUT_COST_PER_1K_USD="0"
$env:KNOWLEDGE_LLM_OUTPUT_COST_PER_1K_USD="0"
$env:KNOWLEDGE_EMBEDDING_INPUT_COST_PER_1K_USD="0"
```

The OTLP endpoint is a collector base URL; traces and metrics are sent to
`/v1/traces` and `/v1/metrics`. `KNOWLEDGE_MODEL_PRICING_JSON` can provide
model-specific USD-per-1,000-token prices. Every request returns an
`X-Request-ID`; chat and research requests also return `X-Query-ID` or
`X-Run-ID`. Admins can inspect process-local diagnostics at:

```text
GET  /admin/observability
```

The instrumentation records request, research, parsing, embedding, retrieval,
rerank, claim, verification, and LLM spans. Metric labels stay low-cardinality;
request IDs, access tokens, client secrets, prompts, and full document contents
are not emitted as metric attributes.

Database migrations, connection pooling, dead-letter handling, and backups are
included in the reliability baseline. Apply migrations explicitly in production
with the selected database URL:

```powershell
$env:KNOWLEDGE_DATABASE_URL="postgresql://knowledge:***@db.example.com:5432/knowledge"
D:\Anaconda\envs\enterprise-kb-agent\python.exe -m alembic -c alembic.ini upgrade head
$env:KNOWLEDGE_AUTO_MIGRATE="0"
```

The API uses one bounded `psycopg_pool` per process and DSN. Tune
`KNOWLEDGE_DB_POOL_MIN_SIZE`, `KNOWLEDGE_DB_POOL_MAX_SIZE`,
`KNOWLEDGE_DB_POOL_MAX_WAITING`, `KNOWLEDGE_DB_POOL_TIMEOUT_SECONDS`, and
`KNOWLEDGE_DB_POOL_WAIT_SECONDS` after measuring database capacity. Failed index
and research jobs enter the PostgreSQL DLQ after `KNOWLEDGE_JOB_MAX_ATTEMPTS`;
administrators can inspect and operate it through:

```text
GET  /admin/jobs/dead-letter
POST /admin/jobs/dead-letter/{dlq_id}/replay
POST /admin/jobs/dead-letter/{dlq_id}/discard
```

Backups use `pg_dump`/`pg_restore` for PostgreSQL and SHA-256 verified copies
for local or S3/MinIO objects. The restore command requires an explicit
`--confirm`:

```powershell
D:\Anaconda\envs\enterprise-kb-agent\python.exe scripts\backup_restore.py backup --output .codex-tmp/backups\knowledge-$(Get-Date -Format yyyyMMdd-HHmmss)
D:\Anaconda\envs\enterprise-kb-agent\python.exe scripts\backup_restore.py restore --backup-dir .codex-tmp/backups\knowledge-20260831-120000 --confirm
```

Run the backup and restore commands against an isolated database/storage target
as a production rehearsal; keep the generated `manifest.json` with the backup.
Redis is treated as the Dramatiq transport rather than the source of truth:
durable job state and the DLQ live in PostgreSQL. After a broker loss, recover
PostgreSQL and objects first, then replay pending work from the job/DLQ records;
managed Redis deployments should additionally enable their own snapshot and
failover policy.

PDF indexing uses Docling + RapidOCR and runs in the Dramatiq worker. Validate
the real OCR and authenticated async pipeline with:

```powershell
python scripts/pdf_ocr_smoke_test.py
python scripts/storage_directory_smoke_test.py
python scripts/async_api_smoke_test.py
```

Validate the configured remote embedding endpoint without exposing credentials:

```powershell
python scripts/embedding_smoke_test.py
```

When vector dimensions change, stop the API and run the controlled migration.
It clears only derived vectors and immediately rebuilds them from source files:

```powershell
python scripts/migrate_embedding_dimensions.py --confirm-clear-vectors
```

Reindex after changing an embedding model without changing dimensions. Admin
endpoints require an admin Bearer token and return queued jobs:

```powershell
Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8010/admin/reindex' -Method POST
```

Run the retrieval evaluation baseline:

```powershell
python scripts/run_retrieval_eval.py
```

Run an end-to-end remote pipeline smoke test in an isolated memory store:

```powershell
python scripts/pipeline_smoke_test.py
```

The knowledge backend supports local, OpenAI-compatible, and Azure OpenAI
embedding providers. Explicit remote configuration fails fast when credentials
are missing and validates every returned vector dimension.

```powershell
$env:KNOWLEDGE_EMBEDDING_PROVIDER="openai"
$env:KNOWLEDGE_EMBEDDING_MODEL="text-embedding-3-small"
$env:OPENAI_API_KEY="your_api_key_here"
$env:OPENAI_BASE_URL="https://your-provider.example.com/v1"
$env:KNOWLEDGE_LLM_PROVIDER="openai"
$env:KNOWLEDGE_RERANKER_PROVIDER="llm"
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8010
```

Runtime diagnostics:

```text
GET  /admin/embedding
POST /admin/embedding/probe
GET  /admin/pipeline
```

Long-running research uses a separate LangGraph and does not change the fast
`POST /chat/query` path. Submit a durable job, then poll its progress:

```text
POST /research/jobs
GET  /research/jobs/{job_id}
POST /research/jobs/{job_id}/cancel
```

The graph executes `plan -> retrieve -> assess -> expand/retrieve -> synthesize`.
Every retrieval round uses the requester's ACL scope before PostgreSQL hybrid
search; final output reuses the same Citation Binder and Evidence Verifier as
fast answers.

See `docs/企业知识库Agent一步步实现.md` for the step-by-step implementation path.
