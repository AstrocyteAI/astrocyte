# astrocyte-ingestion-redis

Optional **[`IngestSource`](https://github.com/AstrocyteAI/astrocyte/blob/main/astrocyte-py/astrocyte/ingest/source.py)** implementation: **Redis Streams** consumer (`XREADGROUP` / `XACK`), wired from `sources:` with `type: stream` and `driver: redis`.

## Install

```bash
pip install astrocyte-ingestion-redis
```

In this monorepo, `astrocyte[stream]` includes this package (see `astrocyte-py/pyproject.toml`; package lives under **`adapters-ingestion-py/`**).

The package registers **`redis`** under the **`astrocyte.ingest_stream_drivers`** entry-point group (same mechanism as storage adapters under `astrocyte.vector_stores`, etc.). Core resolves `sources:` with `type: stream` and `driver: redis` via that group.

## Config sketch

```yaml
sources:
  events:
    type: stream
    driver: redis
    url: "redis://localhost:6379/0"
    topic: "my-stream"
    consumer_group: "astrocyte"
    target_bank: "ingest"
```

Message field shapes match **`astrocyte.ingest.payload.parse_ingest_stream_fields`** (see core docs).

## Develop

```bash
cd adapters-ingestion-py/astrocyte-ingestion-redis
uv sync --extra dev
uv run pytest
```
