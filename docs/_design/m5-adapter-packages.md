# M5 — Production storage adapters (package layout)

M5 ships **separate PyPI packages** per backend (roadmap: Neo4j graph, Elasticsearch/OpenSearch document, Qdrant vector). They implement the Tier 1 SPIs in `astrocyte.provider` (`GraphStore`, `DocumentStore`, `VectorStore`).

## Conformance

Each adapter should run the same style of tests as:

- **Vector**: `astrocyte-py/tests/test_spi_vector_store_contract.py` (copy or import patterns into the adapter repo’s CI).
- **Graph / document**: mirror the same idea — exercise every protocol method against a disposable test instance.

## Repository layout (recommended)

- **Option A — Monorepo**: `astrocyte-qdrant/`, `astrocyte-graph-neo4j/`, … as top-level siblings under the org repo, each with its own `pyproject.toml` and `astrocyte` as a version-pinned dependency.
- **Option B — Multi-repo**: one GitHub repo per adapter; publish independently.

## Versioning

Pin a supported `astrocyte` minor for SPI stability; bump adapter major when SPI major bumps.
