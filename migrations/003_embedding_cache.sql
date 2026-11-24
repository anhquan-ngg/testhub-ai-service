-- Optional durable cache for RETRIEVAL_QUERY embeddings (EMBEDDING_PERSISTENT_CACHE_ENABLED=true).
-- Apply as the dedicated testhub_ai role. Safe to apply before deploying the code; the service
-- does not require this version for readiness and fails open when the table is missing.
-- Stores only the sha256 cache key and the vector, never the raw query text.
-- Down: DROP TABLE rag.embedding_cache; DELETE FROM rag.schema_migrations WHERE version = 3;
BEGIN;
CREATE TABLE IF NOT EXISTS rag.embedding_cache (
  cache_key char(64) PRIMARY KEY,
  embedding public.vector(768) NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS embedding_cache_created_at_idx ON rag.embedding_cache (created_at);
INSERT INTO rag.schema_migrations(version) VALUES (3) ON CONFLICT DO NOTHING;
COMMIT;
