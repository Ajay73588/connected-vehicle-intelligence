-- 0002: pgvector for semantic retrieval (knowledge base + historical incident similarity).
-- Dimension 256 matches cvi.storage.vector.EMBED_DIM (feature-hashing embedder; swap for a model embedder via config).
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE knowledge_docs (
    id         BIGSERIAL PRIMARY KEY,
    tenant_id  BIGINT REFERENCES tenants(id) ON DELETE CASCADE,   -- NULL = global knowledge
    title      TEXT NOT NULL,
    category   TEXT NOT NULL,
    content    TEXT NOT NULL,
    embedding  vector(256) NOT NULL,
    UNIQUE NULLS NOT DISTINCT (tenant_id, title)
);

CREATE TABLE incident_embeddings (
    alert_id   BIGINT PRIMARY KEY REFERENCES alerts(id) ON DELETE CASCADE,
    tenant_id  BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    summary    TEXT NOT NULL,
    embedding  vector(256) NOT NULL
);
CREATE INDEX incident_embeddings_tenant_idx ON incident_embeddings (tenant_id);
