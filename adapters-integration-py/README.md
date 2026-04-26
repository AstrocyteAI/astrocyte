# Astrocyte integration adapters (vendor / product)

This directory holds **optional packages** for **vendor or product integrations** — typically **outbound** domain APIs, **bidirectional** flows (events in + API calls out), or OAuth-heavy connectors.

It is **distinct** from:

- **[`adapters-storage-py/`](../adapters-storage-py/README.md)** — storage SPIs (`VectorStore`, `GraphStore`, `DocumentStore`).
- **[`adapters-ingestion-py/`](../adapters-ingestion-py/README.md)** — **ingest transport** wheels split from core (`astrocyte-ingestion-kafka`, `astrocyte-ingestion-redis`, …).

## Packages

| Directory | PyPI | Role |
|-----------|------|------|
| [`astrocyte-integration-tavus/`](./astrocyte-integration-tavus/) | `astrocyte-integration-tavus` | [Tavus](https://www.tavus.io/) CVI REST client (`x-api-key`, v2 API). |
| [`astrocyte-integration-llm-wrapper/`](./astrocyte-integration-llm-wrapper/) | `astrocyte-integration-llm-wrapper` | OpenAI-compatible memory wrapper: recall before chat completion, retain after completion. |
