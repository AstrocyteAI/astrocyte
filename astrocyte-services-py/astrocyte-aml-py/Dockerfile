# AML submission image — the documented entrypoint maintainers build and run.
#
# BUILD CONTEXT IS THE REPOSITORY ROOT, not this directory:
#
#   docker build -f astrocyte-services-py/astrocyte-aml-py/Dockerfile -t astrocyte-aml .
#
# The adapter resolves `astrocyte` from a sibling path (`[tool.uv.sources]` ->
# ../../astrocyte-py), so a context rooted at this directory cannot see the
# library and the build fails at install time. docker-compose.yml sets this.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Source for the three first-party distributions the service needs. Copied
# before install so the layer caches on dependency changes rather than on
# every source edit.
COPY astrocyte-py/ /src/astrocyte-py/
COPY adapters-storage-py/astrocyte-postgres/ /src/astrocyte-postgres/
COPY astrocyte-services-py/astrocyte-aml-py/ /src/astrocyte-aml-py/

# astrocyte and astrocyte-postgres derive their version from git (hatch-vcs).
# The build context carries source without `.git`, so version detection would
# fail; copying `.git` in to satisfy it would bloat the image the evaluator has
# to build. Pinning explicitly is also better provenance: the version this
# image reports is a declared input, not a side effect of how it was cloned.
# It surfaces in OKF exports as `generated.by: astrocyte/<version>`.
ARG ASTROCYTE_VERSION=0.15.2
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${ASTROCYTE_VERSION} \
    SETUPTOOLS_SCM_PRETEND_VERSION_FOR_ASTROCYTE=${ASTROCYTE_VERSION} \
    SETUPTOOLS_SCM_PRETEND_VERSION_FOR_ASTROCYTE_POSTGRES=${ASTROCYTE_VERSION}

# Installed from local source, not PyPI: the released wheel predates the
# adapter, and pinning to it is what silently broke provider resolution once
# before (see astrocyte-aml-py/pyproject.toml [tool.uv.sources]).
# The [openai] extra is REQUIRED, not optional: AML mandates gpt-4o-mini for
# Add and Search, and OpenAIProvider raises ImportError without it — which
# surfaces only when the brain is built, i.e. on the evaluator's first call.
RUN pip install --no-cache-dir \
        "/src/astrocyte-py[openai]" \
        /src/astrocyte-postgres \
        /src/astrocyte-aml-py \
        "uvicorn[standard]>=0.30"

# Default config; override by mounting your own and setting ASTROCYTE_CONFIG_PATH.
COPY astrocyte-services-py/astrocyte-aml-py/deploy/astrocyte.aml.yaml /app/astrocyte.yaml
ENV ASTROCYTE_CONFIG_PATH=/app/astrocyte.yaml

# Non-root: the evaluator runs this image, and nothing here needs privileges.
RUN useradd --create-home --uid 10001 astrocyte \
    && chown -R astrocyte:astrocyte /app
USER astrocyte

EXPOSE 8080

# /health builds the brain, so an unhealthy container means "would fail on the
# first /add" rather than "process is dead" — which is the distinction that
# matters to an evaluator.
HEALTHCHECK --interval=15s --timeout=10s --start-period=30s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=8).status==200 else 1)"

CMD ["uvicorn", "astrocyte_aml.app:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
