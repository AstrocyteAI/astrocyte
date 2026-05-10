# PostgreSQL 16 with pgvector, pgvectorscale (DiskANN), and VectorChord (vchordrq).
#
# Default ANN backend: pgvectorscale (DiskANN). Switched from pgvector
# (HNSW) on 2026-05-06 after the LongMemEval bench observed HNSW
# per-page write-lock drift under concurrent retain (~1.0s → 2.0s per
# session as the index grew). DiskANN's graph layout serializes less
# aggressively under concurrent inserts.
#
# Base: postgres:16-trixie. (Previously apache/age:release_PG16_1.6.0;
# Apache AGE was removed in M9 — see ADR-008. Tier-2 graph operations
# now run on flat tables + SQL CTEs in the same Postgres image, which
# eliminates the AGE extension dependency and shrinks the deployment
# surface.)
#
# Added:
#   - postgresql-16-pgvector       — pgvector (apt; trixie-pgdg)
#   - pgvectorscale (.deb from GitHub releases)
#   - vchord (.deb from GitHub releases)
#
# Install paths note: Tiger Data's packagecloud apt repo does not ship
# pgvectorscale for Debian Trixie at this time (only bookworm/jammy/
# focal), so we fetch the release artifact from GitHub directly. Same
# pattern as VectorChord. Both projects ship .deb files in their
# release ZIPs for amd64 + arm64; the build is reproducible by pinning
# both versions via build args.
#
# Resulting extensions available via CREATE EXTENSION:
#   - vector       (pgvector — always created)
#   - vectorscale  (pgvectorscale DiskANN — created when
#                   VECTOR_EXTENSION=pgvectorscale, the default)
#   - vchord       (VectorChord — created when VECTOR_EXTENSION=vchord)
#
# Note on shared_preload_libraries: vchord needs to be preloaded.
# pgvectorscale itself does NOT require shared_preload_libraries.
#
# Usage:
#   docker build -t astrocyte-astrocyte-postgres docker/astrocyte-postgres/

ARG PGVECTORSCALE_VERSION=0.9.0
ARG VCHORD_VERSION=1.1.1

FROM postgres:16-trixie

ARG PGVECTORSCALE_VERSION
ARG VCHORD_VERSION

# pgvector via apt (postgresql-16-pgvector); pgvectorscale and vchord
# from GitHub release artefacts (.deb in a .zip for pgvectorscale, raw
# .deb for vchord). Both ship per-arch builds so we resolve via dpkg's
# --print-architecture.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       postgresql-16-pgvector \
       ca-certificates \
       curl \
       unzip \
    && ARCH="$(dpkg --print-architecture)" \
    # pgvectorscale (DiskANN) — release ZIP contains two .deb files
    # (the main package and a -dbgsym debug-symbols package). We extract
    # both and install only the main package; rejecting the dbgsym
    # would force a more complex glob and the dbgsym is small enough
    # that we tolerate the extra ~60 KB on the install path.
    && curl -fsSL -o /tmp/pgvectorscale.zip \
       "https://github.com/timescale/pgvectorscale/releases/download/${PGVECTORSCALE_VERSION}/pgvectorscale-${PGVECTORSCALE_VERSION}-pg16-${ARCH}.zip" \
    && mkdir -p /tmp/pgvectorscale \
    && unzip -j /tmp/pgvectorscale.zip -d /tmp/pgvectorscale \
    && dpkg -i $(find /tmp/pgvectorscale -name 'pgvectorscale-postgresql-16_*.deb' ! -name '*-dbgsym_*') \
    && rm -rf /tmp/pgvectorscale.zip /tmp/pgvectorscale \
    # VectorChord (vchordrq) — direct .deb from GitHub.
    && curl -fsSL -o /tmp/vchord.deb \
       "https://github.com/tensorchord/VectorChord/releases/download/${VCHORD_VERSION}/postgresql-16-vchord_${VCHORD_VERSION}-1_${ARCH}.deb" \
    && dpkg -i /tmp/vchord.deb \
    && rm -f /tmp/vchord.deb \
    && apt-get purge -y --auto-remove curl unzip \
    && rm -rf /var/lib/apt/lists/*

# Pre-load vchord (pgvectorscale does NOT require preloading).
CMD ["postgres", "-c", "shared_preload_libraries=vchord"]
