# Astrocyte ingestion adapters (transport)

This directory holds **optional packages** that implement **memory ingest transports** — data paths into **`retain`** / `IngestSource` — split out of the core `astrocyte` wheel.

It is **distinct** from **[`adapters-storage-py/`](../adapters-storage-py/README.md)** (storage SPIs) and **[`adapters-integration-py/`](../adapters-integration-py/README.md)** (future **vendor/product** integrations — outbound and bidirectional).

## Layout

```
adapters-ingestion-py/
├── README.md
├── astrocyte-ingestion-kafka/    # Kafka IngestSource (aiokafka)
├── astrocyte-ingestion-redis/      # Redis Streams IngestSource (redis-py)
└── astrocyte-ingestion-github/     # GitHub Issues API poll (httpx)
```

Stream **`driver`** values are resolved via **`astrocyte.ingest_stream_drivers`**; poll drivers via **`astrocyte.ingest_poll_drivers`** (see `astrocyte._discovery`). **`astrocyte[stream]`** / **`astrocyte[poll]`** pull these packages (versions pinned in **`astrocyte-py/pyproject.toml`**; monorepo **`[tool.uv.sources]`** for local dev).

Package names follow **`astrocyte-ingestion-{transport}`** where possible.

**Roadmap (v0.8.x connector track):** additional **stream** backends (e.g. **NATS**) and **poll** drivers (beyond GitHub Issues) follow the same **entry-point** pattern as the packages above — separate PyPI modules registering **`astrocyte.ingest_stream_drivers`** or **`astrocyte.ingest_poll_drivers`**. Prefer edge/API-gateway **rate limits** and **auth** for third-party APIs; see **[`docs/_end-user/gateway-edge-and-api-gateways.md`](../docs/_end-user/gateway-edge-and-api-gateways.md)**.

**Observability:** set **`ASTROCYTE_LOG_FORMAT=json`** (same as the gateway) for structured lines from **`astrocyte.ingest.logutil`** — supervisor lifecycle, GitHub rate-limit warnings, Redis/Kafka transport failures.

## CI

**`.github/workflows/adapters-ingestion-ci.yml`** runs **`pytest`** for packages under this tree when it or their publish workflows change.

## PyPI

Trusted publishing workflows: **`publish-astrocyte-ingestion-kafka.yml`**, **`publish-astrocyte-ingestion-redis.yml`**, and **`publish-astrocyte-ingestion-github.yml`** (tag **`v*`** — same convention as other Astrocyte packages).
