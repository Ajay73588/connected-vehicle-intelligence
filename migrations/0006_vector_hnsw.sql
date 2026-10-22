-- 0006: approximate-nearest-neighbour indexes (HNSW, cosine). Kept in its own migration so environments whose
-- pgvector build cannot run SIMD distance code (e.g. the Windows dev wheel) can skip it with SKIP_VECTOR_INDEX=true.
CREATE INDEX knowledge_docs_embedding_idx ON knowledge_docs USING hnsw (embedding vector_cosine_ops);
CREATE INDEX incident_embeddings_embedding_idx ON incident_embeddings USING hnsw (embedding vector_cosine_ops);
