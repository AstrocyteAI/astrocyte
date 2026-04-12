# Releasing Astrocyte

The installable Python package lives under **`astrocyte-py/`** and uses **Hatch VCS** versioning from Git tags at the **repository root** (one level above `astrocyte-py/`).

## Tags and milestones

| Tag      | Milestone (see `docs/_design/product-roadmap-v1.md`) |
|----------|-------------------------------------------------------|
| `v0.6.0` | M3 — Extraction pipeline                              |
| `v0.7.0` | M4 — External data sources (library ingest)         |
| `v0.8.0` | M5 + M6 + M7 — Production adapters, gateway, recall authority |

Create an **annotated** tag from the repo root **after** updating **`CHANGELOG.md`**:

```bash
git tag -a v0.8.0 -m "Release v0.8.0: M5 storage adapters, M6 gateway, M7 recall authority"
git push origin v0.8.0
```

The **`release.yml`** workflow (on `v*`) publishes **`astrocyte`** → **`astrocyte-pgvector`** → **`astrocyte-gateway-py`** image in order.

### Local resolution before a tag exists

Hatch VCS may still report **`0.7.x.dev…`** on `main` until **`v0.8.0`** exists. Adapter **`pyproject.toml`** files use **`astrocyte>=0.7.0,<0.9`** so **`uv sync`** stays consistent. To simulate the tagged version locally:

```bash
export SETUPTOOLS_SCM_PRETEND_VERSION=0.8.0
cd astrocyte-py && uv build
```

## Changelog

Update **`CHANGELOG.md`** at the repo root before tagging. PyPI description uses `astrocyte-py/README.md`; GitHub Release notes can mirror **`CHANGELOG.md`**.

## Building the wheel

```bash
cd astrocyte-py && uv build
```

## Optional extras

- **`astrocyte[gateway]`** — FastAPI (pulls Starlette) + uvicorn for `create_ingest_webhook_app` (`POST /v1/ingest/webhook/{source_id}`; Starlette ASGI app).
