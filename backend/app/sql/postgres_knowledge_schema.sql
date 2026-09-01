CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    source_type TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    department_id TEXT,
    acl_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS document_versions (
    version_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    version_number INTEGER NOT NULL,
    storage_uri TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    is_current BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, version_number)
);

CREATE TABLE IF NOT EXISTS document_chunks (
    chunk_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    version_id TEXT NOT NULL REFERENCES document_versions(version_id),
    page INTEGER NOT NULL,
    section_path TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    acl_json JSONB NOT NULL,
    embedding VECTOR(1536),
    search_vector TSVECTOR GENERATED ALWAYS AS (
        setweight(to_tsvector('simple', coalesce(section_path, '')), 'A') ||
        setweight(to_tsvector('simple', coalesce(content, '')), 'B')
    ) STORED,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS queries (
    query_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    department_ids TEXT[] NOT NULL DEFAULT '{}',
    question TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS answers (
    answer_id TEXT PRIMARY KEY,
    query_id TEXT NOT NULL REFERENCES queries(query_id),
    answer TEXT NOT NULL,
    verified BOOLEAN NOT NULL,
    refusal_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS citations (
    citation_id TEXT PRIMARY KEY,
    answer_id TEXT NOT NULL REFERENCES answers(answer_id),
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    version_id TEXT NOT NULL REFERENCES document_versions(version_id),
    chunk_id TEXT NOT NULL REFERENCES document_chunks(chunk_id),
    page INTEGER NOT NULL,
    section_path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS indexing_jobs (
    job_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    version_id TEXT NOT NULL REFERENCES document_versions(version_id),
    requested_by TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'running', 'completed', 'failed', 'cancelled')
    ),
    progress INTEGER NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
    attempts INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    result_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_documents_department
    ON documents(department_id);

CREATE INDEX IF NOT EXISTS idx_documents_acl
    ON documents USING gin(acl_json);

CREATE INDEX IF NOT EXISTS idx_chunks_document
    ON document_chunks(document_id);

CREATE INDEX IF NOT EXISTS idx_chunks_version
    ON document_chunks(version_id);

CREATE INDEX IF NOT EXISTS idx_chunks_acl
    ON document_chunks USING gin(acl_json);

CREATE INDEX IF NOT EXISTS idx_chunks_search_vector
    ON document_chunks USING gin(search_vector);

CREATE INDEX IF NOT EXISTS idx_chunks_embedding
    ON document_chunks USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

CREATE INDEX IF NOT EXISTS idx_indexing_jobs_status_created
    ON indexing_jobs(status, created_at);

CREATE INDEX IF NOT EXISTS idx_indexing_jobs_document
    ON indexing_jobs(document_id, created_at);

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
    resource TEXT,
    change_type TEXT,
    expiration_at TIMESTAMPTZ,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
