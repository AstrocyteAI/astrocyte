# cookiecutter-astrocyte-adapter

Cookiecutter template for new Astrocyte adapter packages. Produces a working skeleton that:

- Implements one of the SPI Protocols (`VectorStore`, `GraphStore`, `DocumentStore`, `PageIndexStore`, `WikiStore`, `MentalModelStore`, `SourceStore`, `LLMProvider`)
- Registers the right entry-point group so `astrocyte._discovery` picks it up
- Includes a `conftest.py` with a `require_<backend>` skip fixture
- Includes a smoke test that runs against the in-memory reference fixture
- Builds with `uv build` out of the box

## Usage

```bash
pip install cookiecutter

# From the repo root — output goes to the parent of the chosen package_slug
cd adapters-storage-py     # or adapters-llm-py / adapters-ingestion-py / adapters-integration-py
cookiecutter ../tooling/cookiecutter-astrocyte-adapter
```

The prompts:

| Variable | Default | Notes |
|---|---|---|
| `package_slug` | `astrocyte-mybackend` | PyPI package name; kebab-case. Conventionally `astrocyte-<backend>` for storage, `astrocyte-llm-<name>` for LLM, `astrocyte-ingestion-<source>` for ingestion. |
| `module_slug` | auto-derived | Python module; `astrocyte-pinecone` → `astrocyte_pinecone`. |
| `backend_name` | `MyBackend` | Display name (e.g. `Pinecone`). Used in class name + docstrings. |
| `backend_label` | auto-derived | Short key for `astrocyte.yaml` (e.g. `pinecone`). |
| `spi_family` | `vector_stores` | One of the SPI families. Drives entry-point group + base class import + method skeleton. |
| `astrocyte_min_version` | `0.13` | Minimum `astrocyte` SPI version. |
| `author_name` / `author_email` | placeholders | Filled into `pyproject.toml` metadata. |
| `license` | `Apache-2.0` | License identifier. |

## What gets generated

```
{{package_slug}}/
├── pyproject.toml                   # name, deps, entry-point, hatch build
├── README.md                        # install snippet + config snippet
├── {{module_slug}}/
│   ├── __init__.py                  # re-exports the adapter class
│   └── store.py                     # adapter implementation skeleton (method stubs)
└── tests/
    ├── conftest.py                  # require_<backend> skip fixture
    └── test_smoke.py                # SPI Protocol-conformance assertion + ping test
```

Note: in the source template tree these four files have a ``.py.jinja`` suffix so static analyzers don't try to parse Jinja placeholders as Python. The ``hooks/post_gen_project.py`` post-generation hook strips the ``.jinja`` suffix after rendering, so the generated project has plain ``.py`` files as expected.

## After scaffolding

1. **Fill in the method bodies** in `<module_slug>/store.py`. The skeleton has the full SPI method signatures; each body is a `raise NotImplementedError`.
2. **Update `pyproject.toml`** with your backend's actual SDK pin (`...-sdk>=X.Y,<Z`).
3. **Run the smoke test** — `uv sync --extra dev && uv run pytest`. The conformance check should pass once the Protocol's required methods are implemented.
4. **Add CI coverage** — copy a service block from `.github/workflows/adapters-storage-ci.yml` (or the analogous ingestion / LLM workflow) and a `pytest` step for your adapter.
5. **Add publish workflow** — copy `.github/workflows/publish-astrocyte-qdrant.yml`, rename, and register the project on PyPI Trusted Publishing.
6. **Wire into `release.yml`** as a `publish-<slug>` job, depending on `publish-astrocyte`.
7. **Add a narrow extra in `astrocyte-py/pyproject.toml`**: `<label> = ["astrocyte-<slug>>=0.1.0,<1"]`.

The full how-to is in [`docs/_design/adapter-packages.md`](../../docs/_design/adapter-packages.md).

## Why a template (vs copy-from-qdrant)

Copying an existing adapter and renaming works, but you end up with:
- Wrong entry-point group if the SPI family differs.
- Stale references to the source adapter's backend name in tests / docstrings / classifiers.
- Pre-existing `uv.lock` from the source package that doesn't match your deps.

The template parameterizes all of that. For one-off rename-and-edit jobs, copy. For greenfield adapters, scaffold.

## SPI family → entry-point group mapping

The template wires this up automatically based on `spi_family`. For reference:

| `spi_family` value | Entry-point group | Adapter base class |
|---|---|---|
| `vector_stores` | `astrocyte.vector_stores` | implements `VectorStore` Protocol |
| `graph_stores` | `astrocyte.graph_stores` | implements `GraphStore` Protocol |
| `document_stores` | `astrocyte.document_stores` | implements `DocumentStore` Protocol |
| `pageindex_stores` | `astrocyte.pageindex_stores` | implements `PageIndexStore` Protocol |
| `wiki_stores` | `astrocyte.wiki_stores` | implements `WikiStore` Protocol |
| `mental_model_stores` | `astrocyte.mental_model_stores` | implements `MentalModelStore` Protocol |
| `source_stores` | `astrocyte.source_stores` | implements `SourceStore` Protocol |
| `llm_providers` | `astrocyte.llm_providers` | implements `LLMProvider` Protocol |

The Protocol method signatures live in [`astrocyte-py/astrocyte/provider.py`](../../astrocyte-py/astrocyte/provider.py) — the ground truth.
