# Releasing Astrocyte

The installable Python packages live in three places:

| Package | Location | Description |
|---|---|---|
| `astrocyte` | `astrocyte-py/` | Core framework (open source) |
| `astrocyte-stack` | `astrocyte-services-py/astrocyte-stack/` | Convenience meta-package: `astrocyte[default]` |
| `astrocyte-gateway-py` | `astrocyte-services-py/astrocyte-gateway-py/` | Standalone FastAPI gateway |
| `astrocyte-postgres` | `adapters-storage-py/astrocyte-postgres/` | Postgres + pgvector storage adapter |
| `astrocyte-qdrant`, `-neo4j`, `-elasticsearch` | `adapters-storage-py/` | Other storage adapters |
| `astrocyte-ingestion-*` | `adapters-ingestion-py/` | Kafka, Redis, GitHub ingestion connectors |
| `astrocyte-integration-tavus` | `adapters-integration-py/` | Tavus video integration |

All use **Hatch VCS** versioning from Git tags at the **repository root** (one level above each package dir).

## Tags and the release fan-out

Every annotated `v*` tag at the repo root triggers `.github/workflows/release.yml`, which fans out via `workflow_call` to the per-project publish workflows. Each per-project workflow has its own file (required for PyPI Trusted Publishing OIDC subject matching).

Current fan-out order (sequential dependencies; siblings can run in parallel):

```
publish-astrocyte                  (PyPI: astrocyte)
   ├── publish-postgres            (PyPI: astrocyte-postgres)
   │      └── publish-stack        (PyPI: astrocyte-stack)
   │      └── publish-postgres-image (GHCR: astrocyte-postgres image)
   │             └── publish-gateway-image (GHCR: astrocyte-gateway-py image)
   └── (other adapter publish workflows triggered out-of-band per the adapters-storage-ci / adapters-ingestion-ci workflows)
```

The fan-out enforces dependency order so that, e.g., `astrocyte-stack` (which pins `astrocyte[default]>=0.13.1,<2`) publishes only after both `astrocyte` and `astrocyte-postgres` are on PyPI.

Per-project workflows currently in `.github/workflows/`:

- `publish.yml` — `astrocyte` (core)
- `publish-astrocyte-postgres.yml`
- `publish-astrocyte-stack.yml`
- `publish-astrocyte-postgres-image.yml` (Docker → GHCR)
- `publish-astrocyte-gateway-py-image.yml` (Docker → GHCR)
- `publish-astrocyte-{qdrant,neo4j,elasticsearch,integration-tavus,ingestion-{github,kafka,redis}}.yml`

OIDC Trusted Publishing is configured per project on PyPI; no API tokens are stored in repo secrets for these workflows.

## Cutting a release

1. **Update `CHANGELOG.md`** at the repo root. PyPI description for `astrocyte` reads `astrocyte-py/README.md`; GitHub Release notes can mirror the changelog entry.
2. **Create an annotated tag**:
   ```bash
   git tag -a v0.14.0 -m "Release v0.14.0: M9 section-grain recall + R2 bench archive"
   git push origin v0.14.0
   ```
3. The push triggers `release.yml`. Watch the run under **Actions → Release**. Each per-project workflow is gated on PyPI version-already-exists checks, so re-runs are idempotent.

## Local resolution before a tag exists

Hatch VCS reports `0.<next>.<dev>+g<sha>` on `main` until a `v*` tag exists. Adapter `pyproject.toml` files use ranges like `astrocyte>=0.13,<2` so `uv sync` stays consistent across the workspace. To simulate the tagged version locally:

```bash
export SETUPTOOLS_SCM_PRETEND_VERSION=0.14.0
cd astrocyte-py && uv build
```

## Building wheels locally

```bash
cd astrocyte-py && uv build                                            # core
cd astrocyte-services-py/astrocyte-stack && uv build                   # meta-package
cd astrocyte-services-py/astrocyte-gateway-py && uv build              # gateway
cd adapters-storage-py/astrocyte-postgres && uv build                  # adapter (repeat per adapter)
```

## Install shapes — quick reference

The default install shapes shipped from PyPI:

| Command | Use |
|---|---|
| `pip install astrocyte` | Framework only; in-memory backends; for tests, evals, embedded use. |
| `pip install astrocyte-stack` | Default production stack: `astrocyte` + `astrocyte-postgres`. |
| `pip install "astrocyte[postgres]"` | Same as `astrocyte-stack`, explicit form. |
| `pip install "astrocyte[qdrant]"` / `[neo4j]` / `[elasticsearch]` | Single-backend installs. |
| `pip install "astrocyte[mcp]"` | MCP server (`astrocyte-mcp` CLI). |
| `pip install "astrocyte[gateway]"` | Embed FastAPI gateway in-process. Most users want `astrocyte-gateway-py` instead. |

The full extras matrix lives in [`docs/_plugins/ecosystem-and-packaging.md`](docs/_plugins/ecosystem-and-packaging.md).

Developer / contributor extras (not shipped to end users):

| Extra | Purpose |
|---|---|
| `astrocyte[dev]` | Test + lint + built-in feature extras the unit-test suite imports. ~134 packages. |
| `astrocyte[bench]` | `dev` + the adapter stack the `make bench-*` flow runs against + `aiobotocore`. ~150 packages. |

## Known constraints

- **`astrocyte-llm-litellm`** is not in the extras matrix (no `astrocyte[litellm]`) due to upstream litellm pinning `python-dotenv==1.0.1` in the secure-`aiohttp` window, which conflicts with `fastmcp>=3.0`'s `python-dotenv>=1.1.0`. Users who want LiteLLM install it directly: `pip install astrocyte-stack astrocyte-llm-litellm`. Track the upstream fix at <https://github.com/BerriAI/litellm/issues/26190>; once resolved, re-add the `[litellm]` extra to `astrocyte-py/pyproject.toml`.

- **Apache AGE** was removed in M9 (ADR-008). The `astrocyte-age` package, its publish workflow, and the `[age]` extra are deleted. Operators with AGE-backed banks rebuild from raw `memory_units`; no migration tool ships.

## The MCP plugin (Claude Code / Cursor / Codex)

The `mcp-plugins/astrocyte/` directory is **not a PyPI package**. It ships as part of the main repo and is distributed via:

- **Claude Code:** the `.claude-plugin/marketplace.json` at the repo root makes this repo a discoverable plugin marketplace. Users install with `/plugin marketplace add AstrocyteAI/astrocyte && /plugin install astrocyte`.
- **Cursor / Codex:** users `git clone` the repo (or the plugin dir specifically) and drop it under `~/.cursor/plugins/` or `~/.codex/plugins/`.

To release a new plugin version:

1. Bump the `version` field in **all four** of these files (keep them in sync):
   - `.claude-plugin/marketplace.json` → `plugins[0].version`
   - `mcp-plugins/astrocyte/.claude-plugin/plugin.json`
   - `mcp-plugins/astrocyte/.cursor-plugin/plugin.json`
   - `mcp-plugins/astrocyte/.codex-plugin/plugin.json`
2. Update `mcp-plugins/astrocyte/README.md`'s "v0.1.0 scope / v0.2.0 path" section if scope changed.
3. Tag the next repo-level `v*` release. Marketplace clients pick up the bump on next `/plugin update astrocyte`.

The plugin's MCP server (`astrocyte-mcp`) is shipped via the regular PyPI fan-out (it's a console script in `astrocyte-py`); the plugin is just the editor-side wiring + the `astrocyte-memory` skill. No separate publish workflow is needed.

## Scaffolding a new adapter

Use the cookiecutter at `tooling/cookiecutter-astrocyte-adapter/` to generate the skeleton:

```bash
pip install cookiecutter
cd adapters-storage-py            # or adapters-llm-py / adapters-ingestion-py
cookiecutter ../tooling/cookiecutter-astrocyte-adapter
```

Prompts cover package slug, SPI family (`vector_stores` / `graph_stores` / `document_stores` / `pageindex_stores` / `wiki_stores` / `mental_model_stores` / `source_stores` / `llm_providers`), backend name + label. The generated package builds with `uv build` out of the box and is ready to wire into CI + `release.yml`. Full how-to in [`docs/_design/adapter-packages.md`](docs/_design/adapter-packages.md).

## Refreshing the lockfile

```bash
cd astrocyte-py && uv lock
```

Commit the resulting `uv.lock` alongside any `pyproject.toml` change that affects the dependency graph (new extras, new pins, source-path additions).
