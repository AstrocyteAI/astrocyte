# astrocyte-ingestion-kafka

Optional **[`IngestSource`](https://github.com/AstrocyteAI/astrocyte/blob/main/astrocyte-py/astrocyte/ingest/source.py)** implementation: **Kafka** consumer (`AIOKafkaConsumer`), wired from `sources:` with `type: stream` and `driver: kafka`.

## Install

```bash
pip install astrocyte-ingestion-kafka
```

In this monorepo, `astrocyte[stream]` includes this package (see `astrocyte-py/pyproject.toml`; package lives under **`adapters-ingestion-py/`**).

The package registers **`kafka`** under the **`astrocyte.ingest_stream_drivers`** entry-point group.

## Config sketch

```yaml
sources:
  events:
    type: stream
    driver: kafka
    url: "localhost:9092"
    topic: "my-topic"
    consumer_group: "astrocyte"
    target_bank: "ingest"
```

Message values use **`astrocyte.ingest.payload.parse_ingest_kafka_value`** (see core docs).

## Develop

```bash
cd adapters-ingestion-py/astrocyte-ingestion-kafka
uv sync --extra dev
uv run pytest
```
