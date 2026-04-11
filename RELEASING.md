# Releasing Astrocyte

The installable Python package lives under **`astrocyte-py/`** and uses **Hatch VCS** versioning from Git tags at the **repository root** (one level above `astrocyte-py/`).

## Tags and milestones

| Tag     | Milestone (see `docs/_design/product-roadmap-v1.md`) |
|---------|------------------------------------------------------|
| `v0.6.0` | M3 — Extraction pipeline                             |
| `v0.7.0` | M4 — External data sources (library ingest + optional FastAPI helper) |

Create an annotated tag from the repo root:

```bash
git tag -a v0.7.0 -m "Release v0.7.0: M4 ingest"
git push origin v0.7.0
```

## Changelog

Update **`CHANGELOG.md`** at the repo root before tagging. PyPI description uses `astrocyte-py/README.md`; release notes on GitHub can mirror `CHANGELOG.md`.

## Building the wheel

```bash
cd astrocyte-py && uv build
```

## Optional extras

- **`astrocyte[gateway]`** — FastAPI (pulls Starlette) + uvicorn for `create_ingest_webhook_app` (`POST /v1/ingest/webhook/{source_id}`; Starlette ASGI app).
