# astrocyte-qdrant

[Qdrant](https://qdrant.tech/) adapter implementing Astrocyte’s `VectorStore` protocol.

## Usage

```python
from astrocyte_qdrant import QdrantVectorStore

store = QdrantVectorStore(
    url="http://localhost:6333",
    collection_name="astrocyte_mem",
    vector_size=128,
)
```

Register via entry point `astrocyte.vector_stores` / name `qdrant` after installation.

## Development

```bash
uv sync --extra dev
uv run pytest
```

Requires a running Qdrant instance (see CI workflow for Docker image).
