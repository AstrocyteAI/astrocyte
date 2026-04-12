# Build from repository root:
#   docker build -f astrocyte-services-py/astrocyte-gateway-py/Dockerfile -t astrocyte-gateway-py .
#
# Multi-stage: smaller runtime image (no build trees); deps in a venv copied to final stage.

FROM python:3.11-slim AS builder

WORKDIR /build
COPY astrocyte-py /build/astrocyte-py
COPY adapters-storage-py/astrocyte-pgvector /build/astrocyte-pgvector
COPY astrocyte-services-py/astrocyte-gateway-py /build/astrocyte-gateway-py

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# hatch-vcs / setuptools-scm: copied trees are not git checkouts — pin versions for image builds.
ARG SETUPTOOLS_SCM_PRETEND_VERSION=0.1.0
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${SETUPTOOLS_SCM_PRETEND_VERSION}

RUN python -m venv /venv \
    && /venv/bin/pip install --upgrade pip \
    && /venv/bin/pip install /build/astrocyte-py \
    && /venv/bin/pip install /build/astrocyte-pgvector \
    && /venv/bin/pip install /build/astrocyte-gateway-py \
    && rm -rf /build

FROM python:3.11-slim

COPY --from=builder /venv /venv
ENV PATH="/venv/bin:${PATH}" \
    ASTROCYTE_HOST=0.0.0.0 \
    ASTROCYTE_PORT=8080

EXPOSE 8080

CMD ["astrocyte-gateway-py"]
