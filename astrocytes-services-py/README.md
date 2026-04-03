# Astrocytes services (Python)

Optional Python packages beside the core [`astrocytes-py`](../astrocytes-py/) library:

| Package | Role |
|---------|------|
| [`astrocytes-rest/`](astrocytes-rest/README.md) | Reference **REST** HTTP server. **CLI:** `astrocytes-rest`. **Python module:** `astrocytes_rest`. Optional **`[pgvector]`** extra installs [`astrocytes-pgvector`](astrocytes-pgvector/README.md) for durable vectors. |
| [`astrocytes-pgvector/`](astrocytes-pgvector/README.md) | **`pgvector`** [`VectorStore`](../docs/04-provider-spi.md) for PostgreSQL + [pgvector](https://github.com/pgvector/pgvector). **Schema:** SQL under [`astrocytes-pgvector/migrations/`](astrocytes-pgvector/migrations/) + [`scripts/migrate.sh`](astrocytes-pgvector/scripts/migrate.sh) (`psql`, no Python migrator). |

**Docker:** [`docker-compose.yml`](docker-compose.yml) in this directory runs **Postgres (pgvector) + `astrocytes-rest`**. Copy **[`.env.example`](.env.example)** to `.env` to override Postgres credentials, ports, and REST settings. Run from here: `docker compose up --build`, or from the repo root: `docker compose -f astrocytes-services-py/docker-compose.yml --env-file astrocytes-services-py/.env up --build`. Details: [`astrocytes-rest/README.md`](astrocytes-rest/README.md).
