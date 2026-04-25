-- Enable Apache AGE extension.
-- Requires: CREATE EXTENSION age;  (superuser, once per database)
CREATE EXTENSION IF NOT EXISTS age;

-- Make AGE catalog accessible without fully-qualifying every call.
-- This is set per-connection at runtime too; the migration ensures it
-- applies to any psql session that runs subsequent migration scripts.
LOAD 'age';
SET search_path = ag_catalog, "$user", public;
