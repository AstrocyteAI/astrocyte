# Adding an Astrocyte adapter

Astrocyte's storage, LLM, and ingestion backends live in **separate PyPI packages**. The framework ships only the SPI Protocols and in-memory reference implementations; everything that touches real infrastructure is an adapter.

This page is the how-to-guide for shipping a new adapter package. The SPI contracts themselves are spec'd in [`provider-spi.md`](/plugins/provider-spi/); the distribution model is in [`ecosystem-and-packaging.md`](/plugins/ecosystem-and-packaging/).

---

## When to write an adapter

| You want to… | Write a… |
|---|---|
| Make a vector database (Pinecone, Weaviate, Milvus, Chroma…) usable | `VectorStore` adapter |
| Make a graph database (Memgraph, ArangoDB, AGE-like…) usable | `GraphStore` adapter |
| Make a full-text store (Tantivy, Vespa, Marqo…) usable | `DocumentStore` adapter |
| Make a PageIndex-shaped tree store usable | `PageIndexStore` adapter |
| Make a wiki / compiled-knowledge store usable | `WikiStore` adapter |
| Make a mental-model store usable | `MentalModelStore` adapter |
| Make a new LLM provider available (Cohere, Together, …) | `LLMProvider` adapter |
| Wire a streaming or batch source as memory input | `astrocyte-ingestion-*` package |
| Add a third-party integration (Tavus, LiveKit, …) | `astrocyte-integration-*` package |

The shape is the same for all of them: a thin `pyproject.toml`, a single Python module implementing the Protocol, an entry-point declaration, contract tests, and a publish workflow.

---

## Anatomy of a minimal adapter

A working adapter is 5 files. Using `astrocyte-qdrant` as the canonical reference:

```
adapters-storage-py/astrocyte-qdrant/
├── pyproject.toml                   # name, deps, entry-point
├── README.md                        # what it does + install snippet
├── astrocyte_qdrant/
│   ├── __init__.py                  # re-exports the class
│   └── store.py                     # the actual implementation
└── tests/
    ├── conftest.py                  # `require_<backend>` fixture + skip when unreachable
    └── test_<backend>_roundtrip.py  # contract tests
```

That's it. ~300 lines of Python total for a typical vector adapter.

### `pyproject.toml`

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "astrocyte-myvector"
version = "0.1.0"
description = "MyVector VectorStore adapter for Astrocyte"
readme = "README.md"
requires-python = ">=3.11"
license = "Apache-2.0"
dependencies = [
    "astrocyte>=0.13,<2",          # pin the SPI minor
    "myvector-sdk>=2.0,<3",        # the backend client
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-asyncio>=0.23"]

[project.entry-points."astrocyte.vector_stores"]
myvector = "astrocyte_myvector.store:MyVectorStore"

[tool.hatch.build.targets.wheel]
packages = ["astrocyte_myvector"]

# In-repo dev resolution — drops out of the published wheel.
[tool.uv.sources]
astrocyte = { path = "../../astrocyte-py", editable = true }
```

The **entry-point family** depends on which SPI you implement:

| SPI Protocol | Entry-point group | Module reference |
|---|---|---|
| `VectorStore` | `astrocyte.vector_stores` | `your_pkg.store:YourVectorStore` |
| `GraphStore` | `astrocyte.graph_stores` | `your_pkg.store:YourGraphStore` |
| `DocumentStore` | `astrocyte.document_stores` | `your_pkg.store:YourDocumentStore` |
| `PageIndexStore` | `astrocyte.pageindex_stores` | `your_pkg.store:YourPageIndexStore` |
| `WikiStore` | `astrocyte.wiki_stores` | `your_pkg.store:YourWikiStore` |
| `MentalModelStore` | `astrocyte.mental_model_stores` | `your_pkg.store:YourMentalModelStore` |
| `SourceStore` | `astrocyte.source_stores` | `your_pkg.store:YourSourceStore` |
| `LLMProvider` | `astrocyte.llm_providers` | `your_pkg.provider:YourLLMProvider` |

The key on the left of `=` (e.g. `myvector`) is what users put in `astrocyte.yaml`'s `vector_store:` field. Make it short and lowercase.

### `astrocyte_myvector/__init__.py`

```python
"""MyVector VectorStore adapter for Astrocyte."""

from astrocyte_myvector.store import MyVectorStore

__all__ = ["MyVectorStore"]
```

### `astrocyte_myvector/store.py`

```python
"""Async MyVector implementation of :class:`~astrocyte.provider.VectorStore`."""

from __future__ import annotations

from typing import ClassVar

from astrocyte.types import HealthStatus, VectorFilters, VectorHit, VectorItem


class MyVectorStore:
    """Vector store backed by MyVector (bank isolation via payload filter)."""

    SPI_VERSION: ClassVar[int] = 1

    def __init__(self, url: str, *, api_key: str | None = None, ...) -> None:
        ...

    async def store_vectors(self, items: list[VectorItem]) -> list[str]:
        ...

    async def search_similar(
        self,
        query_vector: list[float],
        bank_id: str,
        limit: int = 10,
        filters: VectorFilters | None = None,
    ) -> list[VectorHit]:
        ...

    async def delete(self, ids: list[str], bank_id: str) -> int:
        ...

    async def list_vectors(self, bank_id: str, offset: int = 0, limit: int = 100, ...) -> list[VectorItem]:
        ...

    async def health(self) -> HealthStatus:
        ...
```

The full Protocol method signatures are in [`provider-spi.md`](/plugins/provider-spi/) §1.2 (VectorStore), §1.3 (GraphStore), §1.4 (DocumentStore). The Python `Protocol` definitions themselves are in [`astrocyte-py/astrocyte/provider.py`](https://github.com/AstrocyteAI/astrocyte/blob/main/astrocyte-py/astrocyte/provider.py) — they're the ground truth.

`SPI_VERSION: ClassVar[int]` is read by `astrocyte` at adapter-load time to enforce SPI compatibility. Bump it when you implement a higher SPI minor.

---

## Conformance: reusing the contract tests

Every SPI ships a contract test that any adapter can run against itself. The pattern:

| SPI | Contract test in core | Imports the SPI's reference fixtures |
|---|---|---|
| `VectorStore` | `astrocyte-py/tests/test_spi_vector_store_contract.py` | `InMemoryVectorStore` (fixture) |
| `PageIndexStore` | `astrocyte-py/tests/test_pageindex_store.py` | `InMemoryPageIndexStore` (fixture) |
| `GraphStore` | _(mirror the same pattern — exercise every Protocol method against a disposable instance)_ | — |

Reuse them from your adapter's tests by importing the test module and parametrizing the fixture:

```python
# adapters-storage-py/astrocyte-myvector/tests/test_contract.py
"""Run the canonical VectorStore contract against MyVectorStore."""
import pytest
from astrocyte.types import VectorItem
from astrocyte_myvector import MyVectorStore


@pytest.fixture
async def store(require_myvector):  # skips when backend unreachable
    s = MyVectorStore(url="http://localhost:9090")
    yield s
    # Tear down any test state here


# Import each contract scenario you want to run.
from tests_core.test_spi_vector_store_contract import (  # type: ignore
    test_store_and_search_roundtrip,
    test_delete_removes_vectors,
    test_bank_isolation,
    # ...
)
```

This is the same shape `astrocyte-postgres`, `astrocyte-qdrant`, `astrocyte-neo4j`, and `astrocyte-elasticsearch` use. If the framework adds a new contract test, every adapter picks it up on its next run.

### Skip-when-unreachable

The convention is a `conftest.py` fixture that probes the backend's health endpoint and skips the test session if unreachable. Cribbed from `astrocyte-qdrant`:

```python
# tests/conftest.py
import os
import pytest

URL = os.environ.get("ASTROCYTE_MYVECTOR_URL", "http://127.0.0.1:9090")


@pytest.fixture(scope="session")
def myvector_available() -> bool:
    try:
        from myvector_sdk import Client
        Client(url=URL, timeout=2.0).ping()
        return True
    except Exception:
        return False


@pytest.fixture
def require_myvector(myvector_available: bool) -> None:
    if not myvector_available:
        pytest.skip(f"MyVector not reachable at {URL}")
```

Contributors running tests on laptops without Docker get clean skips; CI runs the integration matrix with the actual service container.

---

## Discovery: how Astrocyte picks the adapter at runtime

Once published with the entry-point declared, users select your adapter via config:

```yaml
# astrocyte.yaml
vector_store: myvector              # ← matches the entry-point key
vector_store_config:
  url: https://my-instance.example.com
  api_key: ${MYVECTOR_API_KEY}      # env var substitution
```

`astrocyte._discovery` walks the entry-point group at startup, instantiates the matched class with `**vector_store_config`, and asserts the result satisfies the Protocol. No code change in `astrocyte` itself.

---

## CI integration

Two workflows per adapter:

### 1. Pre-merge: integration tests on every push

These run in the **shared adapter CI workflow** (`.github/workflows/adapters-storage-ci.yml` for storage adapters; equivalents for ingestion/LLM). It spins up the backend services (Qdrant, Neo4j, Elasticsearch, …) and runs each adapter's pytest suite against them.

Add a service block + a job step for your backend:

```yaml
services:
  myvector:
    image: myvector/myvector:v2.5.0
    ports:
      - 9090:9090
    options: >-
      --health-cmd "curl -fs http://localhost:9090/health"
      --health-interval 5s

# … then in the matrix:
- name: pytest (astrocyte-myvector)
  working-directory: adapters-storage-py/astrocyte-myvector
  env:
    ASTROCYTE_MYVECTOR_URL: http://localhost:9090
  run: |
    uv sync --extra dev
    uv run python -m pytest
```

### 2. Publish: tag-driven release to PyPI

Per-project publish workflow with OIDC Trusted Publishing. Copy `.github/workflows/publish-astrocyte-qdrant.yml` and rename. The skeleton is:

```yaml
name: Publish astrocyte-myvector to PyPI

on:
  workflow_call:
  workflow_dispatch:

permissions:
  id-token: write
  contents: read
  attestations: write

jobs:
  test:
    # … same services + pytest as above
  build:
    needs: test
    # … uv build, upload artifact
  attest:
    needs: build
    # … actions/attest-build-provenance@v2
  publish:
    needs: attest
    environment: release
    # … pypa/gh-action-pypi-publish@release/v1
```

**One workflow file per PyPI project** — required for PyPI Trusted Publishing OIDC subject matching. Don't merge into a matrix.

Wire it into the release fan-out by adding a job in `release.yml`:

```yaml
publish-myvector:
  needs: publish-astrocyte
  permissions:
    contents: read
    id-token: write
    attestations: write
  uses: ./.github/workflows/publish-astrocyte-myvector.yml
  secrets: inherit
```

---

## Versioning

| Change in your adapter | Bump |
|---|---|
| Bug fix, doc update | patch (0.1.0 → 0.1.1) |
| New optional config field, new method (additive) | minor (0.1.1 → 0.2.0) |
| Breaking API change, raised `astrocyte` floor | major (0.2.0 → 1.0.0) |

Pin `astrocyte` to a known-good minor range: `astrocyte>=0.13,<2`. The SPI Protocols are versioned; if a major SPI bump happens, your adapter bumps major too. `SPI_VERSION: ClassVar[int]` on your adapter class declares which SPI generation it implements.

---

## Scaffolding a new adapter

Two paths:

### Option A — Cookiecutter (recommended)

```bash
pip install cookiecutter
cookiecutter ./tooling/cookiecutter-astrocyte-adapter
# Answers prompts:
#   package_slug       [astrocyte-mybackend]: astrocyte-pinecone
#   spi_family         [vector_stores]:        vector_stores
#   backend_name       [MyBackend]:            Pinecone
#   backend_label      [my-backend]:           pinecone
#   astrocyte_min_version [0.13]: 0.13
```

Produces a working skeleton under the current directory, ready to `uv sync --extra dev && uv run pytest`.

See [`tooling/cookiecutter-astrocyte-adapter/README.md`](https://github.com/AstrocyteAI/astrocyte/blob/main/tooling/cookiecutter-astrocyte-adapter/README.md) for the variables list and what gets generated.

### Option B — Copy an existing adapter

For families the cookiecutter doesn't cover yet, copy the closest existing adapter and edit names:

```bash
# Pick the SPI family closest to yours
cp -r adapters-storage-py/astrocyte-qdrant adapters-storage-py/astrocyte-myvector
cd adapters-storage-py/astrocyte-myvector

# Rename module + package
mv astrocyte_qdrant astrocyte_myvector
# Then sed -i (or your editor) across: package name, module name, class name,
# entry-point key, env var prefix.
```

The qdrant adapter is the smallest reference (no schema migrations, no per-tenant table juggling). For larger reference points, `astrocyte-postgres` shows the migrations pattern; `astrocyte-neo4j` shows the graph SPI; `astrocyte-elasticsearch` shows the document SPI.

---

## Publishing checklist

Before tagging a release:

- [ ] Adapter passes its own contract tests locally (`uv run pytest`).
- [ ] Integration test added to the appropriate `adapters-{storage,ingestion,llm}-ci.yml`.
- [ ] `publish-astrocyte-<slug>.yml` exists with OIDC `id-token: write` permissions.
- [ ] PyPI project name registered + Trusted Publisher configured for the workflow's repo + filename.
- [ ] `release.yml` has a `publish-<slug>` job that calls the new workflow.
- [ ] `README.md` has install snippet, config snippet, and link to backend's docs.
- [ ] Entry-point key added to [`ecosystem-and-packaging.md`](/plugins/ecosystem-and-packaging/) install matrix table.
- [ ] Narrow per-adapter extra added to `astrocyte-py/pyproject.toml`:
      `mybackend = ["astrocyte-myvector>=0.1.0,<1"]`
- [ ] [`RELEASING.md`](https://github.com/AstrocyteAI/astrocyte/blob/main/RELEASING.md) updated if there are known cross-package constraints (see the `astrocyte-llm-litellm` example).

Cut the tag (`v0.X.Y`); `release.yml` fans out automatically. Each publish job idempotency-checks PyPI before pushing, so re-runs after a partial failure are safe.

---

## See also

- [`provider-spi.md`](/plugins/provider-spi/) — full SPI Protocol definitions (VectorStore, GraphStore, DocumentStore, PageIndexStore, LLMProvider, …).
- [`ecosystem-and-packaging.md`](/plugins/ecosystem-and-packaging/) — distribution model, package layer table, install matrix.
- [`RELEASING.md`](https://github.com/AstrocyteAI/astrocyte/blob/main/RELEASING.md) — release fan-out + OIDC Trusted Publishing config.
- Reference adapters:
  - [`astrocyte-qdrant`](https://github.com/AstrocyteAI/astrocyte/tree/main/adapters-storage-py/astrocyte-qdrant) — minimal VectorStore.
  - [`astrocyte-postgres`](https://github.com/AstrocyteAI/astrocyte/tree/main/adapters-storage-py/astrocyte-postgres) — full-featured (VectorStore + GraphStore + DocumentStore + PageIndexStore + migrations).
  - [`astrocyte-neo4j`](https://github.com/AstrocyteAI/astrocyte/tree/main/adapters-storage-py/astrocyte-neo4j) — GraphStore.
  - [`astrocyte-elasticsearch`](https://github.com/AstrocyteAI/astrocyte/tree/main/adapters-storage-py/astrocyte-elasticsearch) — DocumentStore.
