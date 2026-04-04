# LangGraph / LangChain integration

Astrocytes as a memory store for LangGraph agents and LangChain applications.

**Module:** `astrocyte.integrations.langgraph`
**Pattern:** Memory store — save_context, search, load_memory_variables
**Framework dependency:** `langgraph` (optional — integration works without it)

## Install

```bash
pip install astrocyte langgraph
```

## Usage

```python
from astrocyte import Astrocyte
from astrocyte.integrations.langgraph import AstrocyteMemory

brain = Astrocyte.from_config("astrocyte.yaml")
memory = AstrocyteMemory(brain, bank_id="user-123")

# Save interaction context
await memory.save_context(
    inputs={"question": "What is dark mode?"},
    outputs={"answer": "A UI theme with dark background"},
    thread_id="thread-abc",
)

# Search memory
results = await memory.search("dark mode", max_results=5)

# Load formatted memories for prompt injection
variables = await memory.load_memory_variables({"topic": "UI preferences"})
# → {"memory": "- Calvin prefers dark mode\n- ..."}
```

## Thread-to-bank mapping

Map LangGraph thread IDs to Astrocytes memory banks:

```python
memory = AstrocyteMemory(
    brain,
    bank_id="default-bank",
    thread_to_bank={"thread-abc": "custom-bank", "thread-xyz": "another-bank"},
)
```

Threads not in the mapping fall back to `bank_id`.

## Integration pattern

| LangGraph operation | Astrocytes call |
|---|---|
| `save_context(inputs, outputs)` | `brain.retain()` with combined input+output text |
| `search(query)` | `brain.recall()` → list of hit dicts |
| `load_memory_variables(inputs)` | `brain.recall()` → formatted string for prompt injection |

## API reference

### `AstrocyteMemory(brain, bank_id, *, auto_retain=False, thread_to_bank=None)`

| Parameter | Type | Description |
|---|---|---|
| `brain` | `Astrocyte` | Configured Astrocyte instance |
| `bank_id` | `str` | Default memory bank |
| `auto_retain` | `bool` | Auto-retain after each interaction (default: False) |
| `thread_to_bank` | `dict[str, str]` | Thread ID → bank ID mapping |

### Methods

- `save_context(inputs, outputs, *, thread_id=None, tags=None)` → `None`
- `search(query, *, thread_id=None, max_results=5, tags=None)` → `list[dict]`
- `load_memory_variables(inputs, *, thread_id=None)` → `dict[str, str]`
- `save_context_sync(...)` / `search_sync(...)` — synchronous wrappers
