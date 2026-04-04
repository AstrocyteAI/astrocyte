# LlamaIndex integration

Astrocytes as a memory store for LlamaIndex agents and chat engines.

**Module:** `astrocytes.integrations.llamaindex`
**Pattern:** Memory store — put, get, get_all, search, reset
**Framework dependency:** `llama-index-core` (optional)

## Install

```bash
pip install astrocytes llama-index-core
```

## Usage

```python
from astrocytes import Astrocyte
from astrocytes.integrations.llamaindex import AstrocyteLlamaMemory

brain = Astrocyte.from_config("astrocytes.yaml")
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

| LlamaIndex method | Astrocytes call |
|---|---|
| `put(content)` | `brain.retain()` → returns memory_id |
| `get(query)` | `brain.recall()` → formatted string for prompts |
| `get_all()` | `brain.recall("*")` → all memories as dicts |
| `search(query)` | `brain.recall()` → structured hit dicts |
| `reset()` | `brain.forget(scope="all")` |

## API reference

### `AstrocyteLlamaMemory(brain, bank_id, *, max_results=10)`

| Method | Returns |
|---|---|
| `put(content, *, tags=None, metadata=None)` | `str \| None` (memory_id) |
| `get(query, *, max_results=None)` | `str` (formatted context) |
| `get_all()` | `list[dict]` |
| `search(query, *, max_results=None, tags=None)` | `list[dict]` |
| `reset()` | `None` |
