# Astrocyte integration adapters (vendor / product)

This directory will hold **optional packages** for **vendor or product integrations** — typically **outbound** domain APIs, **bidirectional** flows (events in + API calls out), or OAuth-heavy connectors.

It is **distinct** from:

- **[`adapters-storage-py/`](../adapters-storage-py/README.md)** — storage SPIs (`VectorStore`, `GraphStore`, `DocumentStore`).
- **[`adapters-ingestion-py/`](../adapters-ingestion-py/README.md)** — **ingest transport** wheels split from core (`astrocyte-ingestion-kafka`, `astrocyte-ingestion-redis`, …).

Nothing is published from here yet; this tree is reserved for upcoming **`astrocyte-integration-*`** (or similar) packages.
