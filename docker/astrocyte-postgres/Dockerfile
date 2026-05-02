# PostgreSQL 16 with both Apache AGE and pgvector extensions.
#
# Base: apache/age:release_PG16_1.6.0 (AGE 1.6.0 pre-installed for PG16).
# Added: postgresql-16-pgvector via the official PostgreSQL apt repository,
#        which is already configured in the base image.
#
# Resulting extensions available via CREATE EXTENSION:
#   - age      (Apache AGE graph engine)
#   - vector   (pgvector dense vector similarity)
#
# Usage:
#   docker build -t astrocyte-astrocyte-postgres \
#       docker/astrocyte-postgres/
#
# Or via docker-compose:
#   docker compose -f astrocyte-services-py/docker-compose-age.yml up --build

FROM apache/age:release_PG16_1.6.0

# Install pgvector from the PostgreSQL apt repository (pre-configured in the
# base image).  The postgresql-16-pgvector package provides the .so shared
# library and the SQL extension files.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       postgresql-16-pgvector \
    && rm -rf /var/lib/apt/lists/*

# Pre-load AGE by setting runtime config on first cluster initialization.
# This is applied to the actual data directory config via ALTER SYSTEM.
# Callers still need SET search_path = ag_catalog, ... per connection.
RUN mkdir -p /docker-entrypoint-initdb.d \
    && printf "%s\n" "ALTER SYSTEM SET shared_preload_libraries = 'age';" \
       > /docker-entrypoint-initdb.d/10-age-preload.sql
