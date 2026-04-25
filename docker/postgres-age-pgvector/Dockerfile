# PostgreSQL 16 with both Apache AGE and pgvector extensions.
#
# Base: apache/age:v1.5.0-pg16 (AGE pre-installed for PG16).
# Added: postgresql-16-pgvector via the official PostgreSQL apt repository,
#        which is already configured in the base image.
#
# Resulting extensions available via CREATE EXTENSION:
#   - age      (Apache AGE graph engine)
#   - vector   (pgvector dense vector similarity)
#
# Usage:
#   docker build -t astrocyte-postgres-age-pgvector \
#       docker/postgres-age-pgvector/
#
# Or via docker-compose:
#   docker compose -f astrocyte-services-py/docker-compose-age.yml up --build

FROM apache/age:v1.5.0-pg16

# Install pgvector from the PostgreSQL apt repository (pre-configured in the
# base image).  The postgresql-16-pgvector package provides the .so shared
# library and the SQL extension files.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       postgresql-16-pgvector \
    && rm -rf /var/lib/apt/lists/*

# Pre-load AGE on every server start so sessions don't need LOAD 'age'.
# Appended to the default postgresql.conf supplied by the base image.
# Callers still need SET search_path = ag_catalog, ... per connection.
RUN echo "shared_preload_libraries = 'age'" >> /usr/share/postgresql/16/postgresql.conf.sample
