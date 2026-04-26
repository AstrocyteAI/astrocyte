# Storage backend setup

How to install, configure, and run each Astrocyte storage adapter. All adapters are optional PyPI packages that register via Python entry points — install the one you need, set the config key, and Astrocyte picks it up automatically.

---

## Which backend do I need?

| Store type | Role | Adapters | When to use |
|------------|------|----------|-------------|
| **Vector store** | Semantic search (embeddings) | `pgvector`, `qdrant` | Always — required for Tier 1 recall |
| **Graph store** | Entity relationships and traversal | `age`, `neo4j` | When you need "who knows whom" or relationship-aware recall |
| **Document store** | BM25 full-text / keyword search | `elasticsearch` | When you need keyword recall alongside semantic |

You can combine stores for **hybrid recall** — e.g. pgvector (semantic) + Neo4j (graph) + Elasticsearch (keyword). Results are fused with reciprocal rank fusion (RRF).

### In-memory (development only)

For quick prototyping, use the built-in in-memory store (no install required):

```yaml
provider_tier: storage
vector_store: in_memory
llm_provider: mock
```

No persistence — data is lost on restart.

---

## PostgreSQL Reference Stack

Recommended default for production and Hindsight-comparable deployments. One PostgreSQL instance provides:

- Durable memory rows, lifecycle columns, banks, and access grants.
- Dense retrieval through `pgvector`.
- Durable wiki pages, revisions, source provenance, links, and lint state through the `pgvector` package's `WikiStore`.
- Graph traversal through Apache AGE, with canonical entity/link truth mirrored in SQL tables.
- Durable background work through PgQueuer.

### Install

```bash
pip install astrocyte-pgvector
# or from source:
cd adapters-storage-py/astrocyte-pgvector && pip install -e .
```

### Run PostgreSQL

```bash
# Docker Compose (quickest full stack)
cd astrocyte-services-py
cp .env.example .env
docker compose up --build
```

The Compose stack uses the repository's combined Postgres image with `pgvector` and Apache AGE. For production-shaped local runs with migrations applied first, use `./scripts/runbook-up.sh` from `astrocyte-services-py/`.

### Configure

```yaml
provider_tier: storage
vector_store: pgvector
graph_store: age
wiki_store: pgvector
llm_provider: mock
vector_store_config:
  dsn: ${DATABASE_URL}
  embedding_dimensions: 1536        # must match your embedding model
  bootstrap_schema: true            # auto-create tables (dev)
graph_store_config:
  dsn: ${DATABASE_URL}
  bootstrap_schema: true
wiki_store_config:
  dsn: ${DATABASE_URL}
  bootstrap_schema: true
wiki_compile:
  enabled: true
  auto_start: true
entity_resolution:
  enabled: true
async_tasks:
  enabled: true
  backend: pgqueuer
  dsn: ${DATABASE_URL}
  install_on_start: true
  auto_start_worker: true
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `dsn` | string | required | PostgreSQL connection URI |
| `table_name` | string | `astrocyte_vectors` | Table name |
| `embedding_dimensions` | int | `128` | Vector width — must match your embedding model output |
| `bootstrap_schema` | bool | `true` | Auto-create extension, table, and indexes on first use |

**Connection string format:**

```
postgresql://user:password@host:port/database
postgresql://astrocyte:astrocyte@127.0.0.1:5433/astrocyte
postgresql://user:pass@host:5432/db?sslmode=require
```

### Production: run migrations

For production, disable `bootstrap_schema` and run migrations explicitly:

```bash
export DATABASE_URL='postgresql://astrocyte:astrocyte@127.0.0.1:5433/astrocyte'
cd adapters-storage-py/astrocyte-pgvector
./scripts/migrate.sh
```

```yaml
vector_store_config:
  dsn: ${DATABASE_URL}
  embedding_dimensions: 1536
  bootstrap_schema: false           # migrations already applied
```

Migrations are plain SQL in `migrations/`:

| File | What it does |
|------|-------------|
| `001_extension.sql` | Install pgvector extension |
| `002_astrocytes_vectors.sql` | Create vectors table |
| `003_indexes.sql` | B-tree on `bank_id`, HNSW on embeddings |
| `004_memory_layer.sql` | Add memory layer column |
| `005_banks_access.sql` | Add bank metadata and access grants |
| `006_lifecycle_indexes.sql` | Add `retained_at` / `forgotten_at` lifecycle columns and time indexes |
| `007_wiki_tables.sql` | Add durable wiki pages, revisions, provenance, links, and lint issues |
| `008_entities_temporal.sql` | Add canonical entity/link tables and normalized temporal facts |

Requires: `psql` client on PATH, PostgreSQL 15+.

### Production checklist

- Set `bootstrap_schema: false` and run `migrate.sh` before deploying
- Match `embedding_dimensions` to your embedding model (OpenAI `text-embedding-3-small` = 1536)
- Use `?sslmode=require` in DSN for remote databases
- Store DSN in env var or secrets manager, not in YAML

---

## Qdrant

Cloud-native vector database with built-in collection management.

### Install

```bash
pip install astrocyte-qdrant
# or from source:
cd adapters-storage-py/astrocyte-qdrant && pip install -e .
```

### Run Qdrant

```bash
docker run -d --name astrocyte-qdrant \
  -p 6333:6333 \
  qdrant/qdrant:v1.17.0
```

### Configure

```yaml
provider_tier: storage
vector_store: qdrant
vector_store_config:
  url: http://localhost:6333
  collection_name: astrocyte_mem
  vector_size: 1536                 # must match your embedding model
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `url` | string | required | Qdrant HTTP API URL |
| `collection_name` | string | required | Collection name |
| `vector_size` | int | required | Embedding dimension |
| `api_key` | string | null | API key for authentication |
| `timeout` | float | `30.0` | Request timeout in seconds |

Collections are created automatically on first use with cosine distance.

### Qdrant Cloud

```yaml
vector_store: qdrant
vector_store_config:
  url: https://your-cluster.cloud.qdrant.io
  collection_name: astrocyte_mem
  vector_size: 1536
  api_key: ${QDRANT_API_KEY}
```

### Production checklist

- Match `vector_size` to your embedding model
- Use `api_key` if Qdrant is network-accessible
- Monitor collection size and memory usage

---

## Neo4j

Graph database for entity relationships and neighborhood-aware recall.

### Install

```bash
pip install astrocyte-neo4j
# or from source:
cd adapters-storage-py/astrocyte-neo4j && pip install -e .
```

### Run Neo4j

```bash
docker run -d --name astrocyte-neo4j \
  -p 7687:7687 -p 7474:7474 \
  -e NEO4J_AUTH=neo4j/your-password \
  neo4j:5
```

Web browser: `http://localhost:7474`

### Configure

```yaml
graph_store: neo4j
graph_store_config:
  uri: bolt://localhost:7687
  user: neo4j
  password: ${NEO4J_PASSWORD}
  database: neo4j
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `uri` | string | required | Bolt URI (`bolt://host:port`) |
| `user` | string | required | Neo4j username |
| `password` | string | required | Neo4j password |
| `database` | string | `neo4j` | Database name |

### Graph model

Astrocyte stores entities and relationships isolated by bank:

- **Nodes:** `AstrocyteEntity` with properties `entity_id`, `bank`, `name`, `entity_type`, `aliases`
- **Relationships:** `ENTITY_LINK` with `link_type` and `metadata`

### Production checklist

- Use Neo4j 5+ for best compatibility
- Store credentials in env vars or secrets manager
- Monitor transaction throughput and heap usage
- Consider Neo4j Aura (managed) for production

---

## Elasticsearch

BM25 full-text search for keyword-based recall. Complements vector search for hybrid retrieval.

### Install

```bash
pip install astrocyte-elasticsearch
# or from source:
cd adapters-storage-py/astrocyte-elasticsearch && pip install -e .
```

### Run Elasticsearch

```bash
docker run -d --name astrocyte-es \
  -p 9200:9200 \
  -e discovery.type=single-node \
  -e xpack.security.enabled=false \
  -e ES_JAVA_OPTS="-Xms512m -Xmx512m" \
  docker.elastic.co/elasticsearch/elasticsearch:8.15.3
```

### Configure

```yaml
document_store: elasticsearch
document_store_config:
  url: http://localhost:9200
  index_prefix: astrocyte_docs
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `url` | string | required | Elasticsearch HTTP URL |
| `index_prefix` | string | `astrocyte_docs` | Index name prefix — one index per bank (`{prefix}_{bank_id}`) |

Indexes are created automatically on first use.

### Elastic Cloud

```yaml
document_store: elasticsearch
document_store_config:
  url: https://user:password@your-cluster.es.cloud.elastic.co:9243
  index_prefix: astrocyte_docs
```

### Production checklist

- Use Elasticsearch 8.12+ with security enabled (`xpack.security`)
- Configure index lifecycle management (ILM) for old indices
- Size heap appropriately (50% of available RAM, max 32 GB)
- Monitor disk usage — especially for high-volume ingestion

---

## Hybrid recall (multiple backends)

Combine vector, graph, and document stores for richer retrieval. Results are fused with reciprocal rank fusion (RRF).

```yaml
provider_tier: storage

# Semantic search
vector_store: pgvector
vector_store_config:
  dsn: ${DATABASE_URL}
  embedding_dimensions: 1536
  bootstrap_schema: false

# Entity relationships
graph_store: neo4j
graph_store_config:
  uri: bolt://localhost:7687
  user: neo4j
  password: ${NEO4J_PASSWORD}

# Keyword / full-text search
document_store: elasticsearch
document_store_config:
  url: http://localhost:9200

# LLM for embedding + reflect
llm_provider: openai
llm_provider_config:
  api_key: ${OPENAI_API_KEY}
  model: gpt-4o-mini
```

Recall automatically queries all configured stores and fuses results. No additional configuration needed — just install the adapter packages and add the config sections.

---

## Comparison

| | pgvector | Qdrant | Neo4j | Elasticsearch |
|---|---------|--------|-------|---------------|
| **Store type** | Vector | Vector | Graph | Document |
| **Search** | Semantic (HNSW) | Semantic (HNSW) | Neighborhood traversal | BM25 keyword |
| **Managed options** | Any managed Postgres | Qdrant Cloud | Neo4j Aura | Elastic Cloud |
| **Best for** | Default choice; existing Postgres | Dedicated vector DB; large scale | Relationship-heavy domains | Keyword recall alongside semantic |
| **Persistence** | SQL (full ACID) | On-disk snapshots | On-disk | Lucene segments |
| **Python package** | `astrocyte-pgvector` | `astrocyte-qdrant` | `astrocyte-neo4j` | `astrocyte-elasticsearch` |
| **Config key** | `vector_store: pgvector` | `vector_store: qdrant` | `graph_store: neo4j` | `document_store: elasticsearch` |

---

## Docker quick reference

Start all backends for local development:

```bash
# pgvector
docker run -d --name pg -p 5433:5432 \
  -e POSTGRES_USER=astrocyte -e POSTGRES_PASSWORD=astrocyte -e POSTGRES_DB=astrocyte \
  pgvector/pgvector:pg16

# Qdrant
docker run -d --name qdrant -p 6333:6333 qdrant/qdrant:v1.17.0

# Neo4j
docker run -d --name neo4j -p 7687:7687 -p 7474:7474 \
  -e NEO4J_AUTH=neo4j/testpass neo4j:5

# Elasticsearch
docker run -d --name es -p 9200:9200 \
  -e discovery.type=single-node -e xpack.security.enabled=false \
  -e ES_JAVA_OPTS="-Xms512m -Xmx512m" \
  docker.elastic.co/elasticsearch/elasticsearch:8.15.3
```

---

## Further reading

- [Configuration reference](configuration-reference/) — full `astrocyte.yaml` schema
- [Memory API reference](memory-api-reference/) — retain/recall/reflect/forget signatures
- [Bank management](bank-management/) — bank creation, multi-bank queries, hybrid recall patterns
- [Provider SPI](/plugins/provider-spi/) — build your own storage adapter
- [Storage adapter packages](/design/adapter-packages/) — architecture and entry point conventions
- [Storage and data planes](/design/storage-and-data-planes/) — retrieval vs export architecture
