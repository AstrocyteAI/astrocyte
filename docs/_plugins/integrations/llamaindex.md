# LlamaIndex integration

Astrocyte as a memory store for LlamaIndex agents and chat engines.

**Module:** `astrocyte.integrations.llamaindex`
**Pattern:** Memory store — put, get, get_all, search, reset
**Framework dependency:** `llama-index-core` (optional)

## Install

```bash
pip install astrocyte llama-index-core
```

## Usage

```python
from astrocyte import Astrocyte
from astrocyte.integrations.llamaindex import AstrocyteLlamaMemory

brain = Astrocyte.from_config("astrocyte.yaml")
memory = AstrocyteLlamaMemory(brain, bank_id="user-123", max_results=10)

# Store and retrieve
await memory.put("Calvin prefers dark mode", tags=["preference"])
context = await memory.get("UI preferences")
# → "- Calvin prefers dark mode"

# Get all memories in the bank
all_mems = await memory.get_all()

# Structured search
results = await memory.search("dark mode", tags=["preference"])

# Reset bank
await memory.reset()
```

## Integration pattern

| LlamaIndex method | Astrocyte call |
|---|---|
| `put(content)` | `brain.retain()` → returns memory_id |
| `get(query)` | `brain.recall()` → formatted string for prompts |
| `get_all()` | `brain.recall("*")` → all memories as dicts |
| `search(query)` | `brain.recall()` → structured hit dicts |
| `reset()` | `brain.clear_bank()` |

## End-to-end example

A LlamaIndex chat engine with persistent memory:

```python
import asyncio
from astrocyte import Astrocyte
from astrocyte.integrations.llamaindex import AstrocyteLlamaMemory

brain = Astrocyte.from_config("astrocyte.yaml")
memory = AstrocyteLlamaMemory(brain, bank_id="user-123", max_results=10)

async def main():
    # Store documents
    await memory.put(
        "Astrocyte supports semantic, graph, and keyword retrieval",
        tags=["architecture"],
        metadata={"source": "docs"},
    )
    await memory.put(
        "Hybrid recall fuses results with RRF",
        tags=["architecture"],
    )

    # Get formatted context for prompt injection
    context = await memory.get("How does retrieval work?")
    print(context)
    # "- Astrocyte supports semantic, graph, and keyword retrieval\n- Hybrid recall fuses..."

    # Structured search with tag filtering
    results = await memory.search("retrieval", tags=["architecture"])
    for hit in results:
        print(f"  [{hit['score']:.2f}] {hit['text']}")

    # Get all memories in the bank
    all_mems = await memory.get_all()
    print(f"Total memories: {len(all_mems)}")

asyncio.run(main())
```

## Using in a Haystack-style pipeline

```python
from llama_index.core import VectorStoreIndex
from llama_index.core.chat_engine import CondensePlusContextChatEngine

# Use Astrocyte memory as additional context alongside LlamaIndex's own index
context = await memory.get("user preferences")
chat_engine = CondensePlusContextChatEngine.from_defaults(
    retriever=index.as_retriever(),
    system_prompt=f"Known user context:\n{context}",
)
```

## API reference

### `AstrocyteLlamaMemory(brain, bank_id, *, max_results=10)`

| Method | Returns |
|---|---|
| `put(content, *, tags=None, metadata=None)` | `str \| None` (memory_id) |
| `get(query, *, max_results=None)` | `str` (formatted context) |
| `get_all()` | `list[dict]` |
| `search(query, *, max_results=None, tags=None)` | `list[dict]` |
| `reset()` | `None` |
