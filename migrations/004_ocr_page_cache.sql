-- Optional durable cache for vision-OCR transcripts (OCR_PERSISTENT_CACHE_ENABLED=true).
-- Apply as the dedicated testhub_ai role. Safe to apply before deploying the code; the service
-- does not require this version for readiness and fails open when the table is missing.
-- The key is sha256(config version | model | prompt version | image bytes); the transcript is
-- extracted document text, so keep it under the same retention as the document chunks.
-- Down: DROP TABLE rag.ocr_page_cache; DELETE FROM rag.schema_migrations WHERE version = 4;
BEGIN;
CREATE TABLE IF NOT EXISTS rag.ocr_page_cache (
  cache_key char(64) PRIMARY KEY,
  transcript text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ocr_page_cache_created_at_idx ON rag.ocr_page_cache (created_at);
INSERT INTO rag.schema_migrations(version) VALUES (4) ON CONFLICT DO NOTHING;
COMMIT;
