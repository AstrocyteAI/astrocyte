# DSPy (Stanford NLP) integration

Astrocytes as a DSPy retrieval model (RM) for declarative AI programming.

**Module:** `astrocytes.integrations.dspy`
**Pattern:** Retrieval model — `__call__(query, k)` → `list[str]` (DSPy RM protocol)
**Framework dependency:** `dspy` (optional)

## Install

```bash
pip install astrocytes dspy
```

## Usage

```python
from astrocytes import Astrocyte
from astrocytes.integrations.dspy import AstrocyteRM

brain = Astrocyte.from_config("astrocytes.yaml")
retriever = AstrocyteRM(brain, bank_id="knowledge-base", default_k=5)

# Use as a DSPy retrieval model
import dspy
dspy.configure(rm=retriever)

# Sync call (DSPy RM protocol)
passages = retriever("What is dark mode?", k=5)
# → ["Calvin prefers dark mode", "Dark mode reduces eye strain"]

# Async methods for programmatic use
passages = await retriever.aretrieve("dark mode")
await retriever.aretain("New knowledge to store")
answer = await retriever.areflect("Summarize what we know")
```

## Integration pattern

DSPy's retrieval model protocol is simple: `__call__(query, k) → list[str]`.

| Method | Description | Returns |
|---|---|---|
| `__call__(query, k)` | Sync retrieval (DSPy RM protocol) | `list[str]` — passage texts |
| `aretrieve(query, k)` | Async retrieval | `list[str]` |
| `aretain(content, **kwargs)` | Store content | `str \| None` — memory_id |
| `areflect(query)` | Synthesize answer | `str` |

## API reference

### `AstrocyteRM(brain, bank_id, *, default_k=5)`

| Parameter | Type | Description |
|---|---|---|
| `brain` | `Astrocyte` | Configured Astrocyte instance |
| `bank_id` | `str` | Memory bank for retrieval |
| `default_k` | `int` | Default number of passages to return |
