-- DBA-only setup, run explicitly against the chosen LOCAL database.
-- Never run automatically on service startup. Set the login password separately
-- (psql: \password testhub_ai); do not put real credentials in this file.
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'testhub_ai') THEN
    CREATE ROLE testhub_ai LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
  END IF;
END $$;
DO $$ BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname='testhub_ai' AND
      (rolsuper OR rolcreatedb OR rolcreaterole OR rolinherit OR rolreplication OR rolbypassrls))
    OR EXISTS (SELECT FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member WHERE r.rolname='testhub_ai') THEN
    RAISE EXCEPTION 'Review privileges/memberships of existing testhub_ai role';
  END IF;
  IF EXISTS (SELECT FROM pg_namespace n JOIN pg_roles r ON r.oid=n.nspowner
      WHERE n.nspname='rag' AND r.rolname<>'testhub_ai') THEN
    RAISE EXCEPTION 'Review owner of existing rag schema';
  END IF;
END $$;
CREATE SCHEMA IF NOT EXISTS rag AUTHORIZATION testhub_ai;
REVOKE ALL ON SCHEMA rag FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA rag TO testhub_ai;
GRANT USAGE ON SCHEMA public TO testhub_ai;
-- This grants NO permissions on business tables. Audit existing PUBLIC grants
-- on your database separately. Existing PostgreSQL images may lack pgvector.
