-- Vector extensions (idempotent).
--
-- ``pgvector`` (extension name: ``vector``) is always created —
-- pgvector, pgvectorscale, and vchord all reuse pgvector's ``vector``
-- type and operators.
--
-- ``pgvectorscale`` (extension name: ``vectorscale``) is created only
-- when migrate.sh sets the ``:install_vectorscale`` psql variable
-- (driven by ``VECTOR_EXTENSION=pgvectorscale``).
--
-- ``vchord`` (extension name: ``vchord``) is created only when
-- migrate.sh sets ``:install_vchord`` (driven by
-- ``VECTOR_EXTENSION=vchord``). vchord requires ``vchord.so`` to be
-- in ``shared_preload_libraries`` — see the shipped Dockerfile.
--
-- Either binary must be present in the Postgres install. If it isn't,
-- this ``CREATE EXTENSION`` fails fast with a clear error rather than
-- silently producing a broken schema.

CREATE EXTENSION IF NOT EXISTS vector;

\if :{?install_vectorscale}
CREATE EXTENSION IF NOT EXISTS vectorscale CASCADE;
\endif

\if :{?install_vchord}
CREATE EXTENSION IF NOT EXISTS vchord CASCADE;
\endif
