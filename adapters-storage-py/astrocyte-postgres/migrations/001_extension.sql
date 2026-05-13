-- Vector extensions (idempotent).
--
-- pgvectorscale is the only supported vector backend (see ADR notes in
-- the postgres adapter README). pgvectorscale CASCADE-installs pgvector
-- automatically because it depends on pgvector's ``vector`` type and
-- operator class — we don't issue a separate CREATE EXTENSION for it.
--
-- If pgvectorscale isn't installed in the running Postgres, this fails
-- fast with a clear error rather than silently producing a broken
-- schema. Use the shipped ``ghcr.io/astrocyteai/astrocyte-postgres``
-- image, or install ``postgresql-16-pgvectorscale`` from Timescale's
-- apt repo (also available on Supabase, Neon, and Timescale Cloud).

CREATE EXTENSION IF NOT EXISTS vectorscale CASCADE;
