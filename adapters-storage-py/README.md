# Astrocyte Tier-1 storage adapters

Optional packages implementing Astrocyte storage protocols (see `astrocyte.provider`):

| Package | Protocol | Backend |
|---------|----------|---------|
| [astrocyte-pgvector](./astrocyte-pgvector/) | `VectorStore` (+ optional `DocumentStore`) | [PostgreSQL](https://www.postgresql.org/) + [pgvector](https://github.com/pgvector/pgvector) |
| [astrocyte-qdrant](./astrocyte-qdrant/) | `VectorStore` | [Qdrant](https://qdrant.tech/) |
| [astrocyte-neo4j](./astrocyte-neo4j/) | `GraphStore` | [Neo4j](https://neo4j.com/) |
| [astrocyte-elasticsearch](./astrocyte-elasticsearch/) | `DocumentStore` | [Elasticsearch](https://www.elastic.co/) |

Each package:

- Depends on the core `astrocyte` wheel (path-linked via `uv` in development).
- Registers an [entry point](https://docs.astral.sh/uv/concepts/projects/entry-points/) under `astrocyte.vector_stores`, `astrocyte.graph_stores`, or `astrocyte.document_stores`.
- Includes integration tests that **skip** when the database is not running (local dev) and run in CI with Docker service containers (`.github/workflows/adapters-storage-ci.yml`).

## Local testing

Start the databases (e.g. Docker Compose), then:

```bash
cd adapters-storage-py/astrocyte-qdrant && uv sync --extra dev && uv run pytest
```

Set `ASTROCYTE_QDRANT_URL`, `ASTROCYTE_NEO4J_URI`, `ASTROCYTE_NEO4J_USER`, `ASTROCYTE_NEO4J_PASSWORD`, and `ASTROCYTE_ELASTICSEARCH_URL` if not using defaults on `127.0.0.1`.
