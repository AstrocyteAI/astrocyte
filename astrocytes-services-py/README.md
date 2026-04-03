# Astrocytes services (Python)

Optional Python packages beside the core [`astrocytes-py`](../astrocytes-py/) library:

| Package | Role |
|---------|------|
| [`astrocytes-rest/`](astrocytes-rest/README.md) | Reference **REST** HTTP server. **CLI:** `astrocytes-rest`. **Python module:** `astrocytes_rest`. Optional **`[pgvector]`** extra installs [`astrocytes-pgvector`](astrocytes-pgvector/README.md) for durable vectors. |
| [`astrocytes-pgvector/`](astrocytes-pgvector/README.md) | **`pgvector`** [`VectorStore`](../docs/04-provider-spi.md) for PostgreSQL + [pgvector](https://github.com/pgvector/pgvector). |

**Docker:** [`docker-compose.yml`](docker-compose.yml) in this directory runs **Postgres (pgvector) + `astrocytes-rest`**. Run from here: `docker compose up --build`, or `docker compose -f astrocytes-services-py/docker-compose.yml up --build` from the repository root. Details: [`astrocytes-rest/README.md`](astrocytes-rest/README.md).
