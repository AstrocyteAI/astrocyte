# astrocyte-integration-tavus

Optional **[Tavus](https://www.tavus.io/)** REST client for **Astrocyte** integrations — [Conversational Video Interface](https://docs.tavus.io/api-reference/overview) (personas, replicas, conversations, documents, …).

## Install

```bash
pip install astrocyte-integration-tavus
```

## Usage

Uses the Tavus **`x-api-key`** header against **`https://tavusapi.com/v2`** by default ([auth](https://docs.tavus.io/api-reference/authentication)).

```python
import os

from astrocyte_integration_tavus import TavusClient

async def main() -> None:
    async with TavusClient(os.environ["TAVUS_API_KEY"]) as tavus:
        page = await tavus.list_conversations(limit=10, status="ended")
        conv = await tavus.get_conversation("c123456", verbose=True)
```

This package is **HTTP only** — it does not register Astrocyte ingest drivers or storage SPIs. Use it from gateways, jobs, or MCP tools when you need to call Tavus from application code.

## API surface

The first release exposes **conversations** helpers:

- `list_conversations(limit=..., page=..., status=...)`
- `get_conversation(conversation_id, verbose=...)` — `verbose=true` returns transcript-rich payloads per [Get Conversation](https://docs.tavus.io/api-reference/conversations/get-conversation).

Extend `TavusClient` with more endpoints as needed (same `_request` pattern).

## Develop

```bash
cd astrocyte-integration-tavus
uv sync --extra dev
uv run pytest tests -v
```
