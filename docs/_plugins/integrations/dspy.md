# DSPy (Stanford NLP) integration

Astrocyte as a DSPy retrieval model (RM) for declarative AI programming.

**Module:** `astrocyte.integrations.dspy`
**Pattern:** Retrieval model — `__call__(query, k)` → `list[str]` (DSPy RM protocol)
**Framework dependency:** `dspy` (optional)

## Install

```bash
pip install astrocyte dspy
```

## Usage

```python
from astrocyte import Astrocyte
from astrocyte.integrations.dspy import AstrocyteRM

brain = Astrocyte.from_config("astrocyte.yaml")
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

## End-to-end example

A DSPy RAG pipeline backed by Astrocyte memory:

```python
import asyncio
import dspy
from astrocyte import Astrocyte
from astrocyte.integrations.dspy import AstrocyteRM

brain = Astrocyte.from_config("astrocyte.yaml")
retriever = AstrocyteRM(brain, bank_id="knowledge-base", default_k=5)

# Configure DSPy with Astrocyte as the retriever
lm = dspy.LM("openai/gpt-4o-mini")
dspy.configure(lm=lm, rm=retriever)

# Define a RAG signature
class AnswerWithMemory(dspy.Signature):
    """Answer a question using retrieved memory passages."""
    question: str = dspy.InputField()
    answer: str = dspy.OutputField()

# Build a RAG module
rag = dspy.ChainOfThought(AnswerWithMemory)

async def main():
    # Populate the knowledge base
    await retriever.aretain("Astrocyte supports pgvector, Qdrant, Neo4j, and Elasticsearch")
    await retriever.aretain("Hybrid recall fuses results with reciprocal rank fusion (RRF)")
    await retriever.aretain("MIP routes content to banks using declarative rules")

    # Query — DSPy calls retriever automatically
    result = rag(question="What storage backends does Astrocyte support?")
    print(result.answer)

    # Reflect for a synthesis across all knowledge
    summary = await retriever.areflect("Summarize Astrocyte's retrieval capabilities")
    print(summary)

asyncio.run(main())
```

## API reference

### `AstrocyteRM(brain, bank_id, *, default_k=5)`

| Parameter | Type | Description |
|---|---|---|
| `brain` | `Astrocyte` | Configured Astrocyte instance |
| `bank_id` | `str` | Memory bank for retrieval |
| `default_k` | `int` | Default number of passages to return |
