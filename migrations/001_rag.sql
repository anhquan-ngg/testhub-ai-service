-- Apply as the dedicated testhub_ai role after the DBA bootstrap.
-- Rollback before use only: drop schema rag cascade; keep public.vector intact.
BEGIN;
CREATE TABLE IF NOT EXISTS rag.index_profiles (
  id char(64) PRIMARY KEY,
  config jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS rag.revoked_indexes (
  index_id uuid PRIMARY KEY,
  owner_id uuid NOT NULL,
  profile_id char(64) NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS rag.document_indexes (
  id uuid PRIMARY KEY,
  profile_id char(64) NOT NULL REFERENCES rag.index_profiles(id),
  owner_id uuid NOT NULL,
  topic_id uuid NOT NULL,
  chapter_id uuid NOT NULL,
  document_id uuid NOT NULL,
  version_id uuid NOT NULL,
  source_sha256 char(64) NOT NULL,
  request_hash char(64) NOT NULL,
  status text NOT NULL CHECK (status IN ('BUILDING','READY','REVOKED')),
  chunk_count integer NOT NULL CHECK (chunk_count BETWEEN 1 AND 5000),
  completed_chunks integer NOT NULL DEFAULT 0,
  attempt_count integer NOT NULL DEFAULT 0,
  lease_token uuid,
  lease_expires_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (completed_chunks BETWEEN 0 AND chunk_count)
);
CREATE INDEX IF NOT EXISTS rag_index_scope ON rag.document_indexes(owner_id,profile_id,status);
CREATE TABLE IF NOT EXISTS rag.chunks (
  id uuid PRIMARY KEY,
  index_id uuid NOT NULL REFERENCES rag.document_indexes(id) ON DELETE CASCADE,
  ordinal integer NOT NULL CHECK (ordinal BETWEEN 0 AND 4999),
  kind text NOT NULL CHECK (kind IN ('PAGE','SLIDE','NOTES')),
  position integer NOT NULL CHECK (position BETWEEN 1 AND 500),
  start_offset integer NOT NULL CHECK (start_offset >= 0),
  end_offset integer NOT NULL CHECK (end_offset > start_offset),
  text text NOT NULL,
  text_sha256 char(64) NOT NULL,
  embedding public.vector(768),
  UNIQUE(index_id,ordinal)
);
CREATE INDEX IF NOT EXISTS rag_pending_chunks ON rag.chunks(index_id,ordinal) WHERE embedding IS NULL;
CREATE TABLE IF NOT EXISTS rag.schema_migrations (version integer PRIMARY KEY);
INSERT INTO rag.schema_migrations(version) VALUES(1) ON CONFLICT DO NOTHING;
COMMIT;
