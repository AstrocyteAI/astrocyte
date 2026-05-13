# PostgreSQL 16 with pgvectorscale (DiskANN). pgvector is pulled in as a
# transitive dependency of pgvectorscale, so application code using
# pgvector's ``vector`` type and ``<=>`` operator works unchanged.
#
# Standardized on pgvectorscale on 2026-05-06 after the LongMemEval bench
# surfaced HNSW per-page write-lock drift under concurrent retain
# (~1.0s → 2.0s per session as the index grew). DiskANN's graph layout
# serializes less aggressively. pgvectorscale is OSS under the
# PostgreSQL License.
#
# Base: postgres:16-trixie. (Previously apache/age:release_PG16_1.6.0;
# Apache AGE was removed in M9 — see ADR-008. Tier-2 graph operations
# now run on flat tables + SQL CTEs in the same Postgres image.)
#
# Install paths note: Tiger Data's packagecloud apt repo does not ship
# pgvectorscale for Debian Trixie at this time (only bookworm/jammy/
# focal), so we fetch the release artifact from GitHub directly. The
# release ZIP ships .deb files for amd64 + arm64; the build is
# reproducible by pinning the version via the build arg below.
#
# Resulting extensions available via CREATE EXTENSION:
#   - vector       (pgvector — pulled in transitively)
#   - vectorscale  (pgvectorscale DiskANN — created by migration 001)
#
# pgvectorscale does NOT require shared_preload_libraries.
#
# Usage:
#   docker build -t astrocyte-astrocyte-postgres docker/astrocyte-postgres/

ARG PGVECTORSCALE_VERSION=0.9.0

FROM postgres:16-trixie

ARG PGVECTORSCALE_VERSION

# pgvector via apt (postgresql-16-pgvector); pgvectorscale from the
# GitHub release ZIP (.deb inside). pgvectorscale ships per-arch builds
# so we resolve via dpkg's --print-architecture.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       postgresql-16-pgvector \
       ca-certificates \
       curl \
       unzip \
    && ARCH="$(dpkg --print-architecture)" \
    # The release ZIP contains two .deb files (the main package and a
    # -dbgsym debug-symbols package). We install only the main package;
    # rejecting the dbgsym would force a more complex glob and the
    # dbgsym is small enough that we tolerate the extra ~60 KB.
    && curl -fsSL -o /tmp/pgvectorscale.zip \
       "https://github.com/timescale/pgvectorscale/releases/download/${PGVECTORSCALE_VERSION}/pgvectorscale-${PGVECTORSCALE_VERSION}-pg16-${ARCH}.zip" \
    && mkdir -p /tmp/pgvectorscale \
    && unzip -j /tmp/pgvectorscale.zip -d /tmp/pgvectorscale \
    && dpkg -i $(find /tmp/pgvectorscale -name 'pgvectorscale-postgresql-16_*.deb' ! -name '*-dbgsym_*') \
    && rm -rf /tmp/pgvectorscale.zip /tmp/pgvectorscale \
    && apt-get purge -y --auto-remove curl unzip \
    && rm -rf /var/lib/apt/lists/*
