# Haystack (deepset) integration

Astrocyte as Retriever and Writer pipeline components for Haystack.

**Module:** `astrocyte.integrations.haystack`
**Pattern:** Pipeline components — `arun()` returning `{"documents": [...]}` or `{"written": N}`
**Framework dependency:** `haystack-ai` (optional)

## Install

```bash
pip install astrocyte haystack-ai
```

## Usage — Retriever

```python
from astrocyte import Astrocyte
from astrocyte.integrations.haystack import AstrocyteRetriever

brain = Astrocyte.from_config("astrocyte.yaml")
retriever = AstrocyteRetriever(brain, bank_id="knowledge-base", top_k=10)

# Use in a Haystack pipeline
from haystack import Pipeline
pipe = Pipeline()
pipe.add_component("retriever", retriever)
pipe.add_component("reader", reader)
pipe.connect("retriever.documents", "reader.documents")

result = pipe.run({"retriever": {"query": "What is dark mode?"}})

# Or use standalone
docs = await retriever.arun("dark mode", top_k=5)
# → {"documents": [AstrocyteDocument(content="...", score=0.9, meta={...}), ...]}
```

## Usage — Writer

```python
from astrocyte.integrations.haystack import AstrocyteWriter, AstrocyteDocument

writer = AstrocyteWriter(brain, bank_id="knowledge-base")

# Write documents to memory
await writer.arun([
    AstrocyteDocument(content="New finding", meta={"source": "research"}, score=1.0, id="d1"),
    {"content": "Dict format also works", "meta": {"source": "upload"}},
])
# → {"written": 2}
```

## Integration pattern

| Component | Method | Input | Output |
|---|---|---|---|
| `AstrocyteRetriever` | `arun(query)` | Query string | `{"documents": list[AstrocyteDocument]}` |
| `AstrocyteRetriever` | `run(query)` | Query string (sync) | Same |
| `AstrocyteWriter` | `arun(documents)` | List of docs/dicts | `{"written": int}` |

### `AstrocyteDocument`

Compatible with `haystack.Document`:

| Field | Type | Description |
|---|---|---|
| `content` | `str` | Document text |
| `meta` | `dict` | Metadata |
| `score` | `float` | Relevance score |
| `id` | `str` | Document/memory ID |

## API reference

### `AstrocyteRetriever(brain, bank_id, *, top_k=10)`

### `AstrocyteWriter(brain, bank_id)`
