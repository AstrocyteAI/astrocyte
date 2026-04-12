# Astrocyte ingestion adapters (transport)

This directory holds **optional packages** that implement **memory ingest transports** — data paths into **`retain`** / `IngestSource` — split out of the core `astrocyte` wheel.

It is **distinct** from **[`adapters-storage-py/`](../adapters-storage-py/README.md)** (storage SPIs) and **[`adapters-integration-py/`](../adapters-integration-py/README.md)** (future **vendor/product** integrations — outbound and bidirectional).

## Layout

```
adapters-ingestion-py/
├── README.md
├── astrocyte-ingestion-kafka/   # Kafka IngestSource (aiokafka)
└── astrocyte-ingestion-redis/     # Redis Streams IngestSource (redis-py)
```

Stream **`driver`** values are resolved via **`astrocyte.ingest_stream_drivers`** (see `astrocyte._discovery`). **`astrocyte[stream]`** pulls these packages (versions pinned in **`astrocyte-py/pyproject.toml`**; monorepo **`[tool.uv.sources]`** for local dev).

Package names follow **`astrocyte-ingestion-{transport}`** where possible.

## CI

**`.github/workflows/adapters-ingestion-ci.yml`** runs **`pytest`** for packages under this tree when it or their publish workflows change.

## PyPI

Trusted publishing workflows: **`publish-astrocyte-ingestion-kafka.yml`** and **`publish-astrocyte-ingestion-redis.yml`** (tag **`v*`** — same convention as other Astrocyte packages).
