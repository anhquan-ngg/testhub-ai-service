-- Topic-owned documents need not have a bank chapter before question commit.
-- Down: restore NOT NULL only after all chapter-less document indexes are retired.
BEGIN;
ALTER TABLE rag.document_indexes ALTER COLUMN chapter_id DROP NOT NULL;
INSERT INTO rag.schema_migrations(version) VALUES (2) ON CONFLICT DO NOTHING;
COMMIT;
