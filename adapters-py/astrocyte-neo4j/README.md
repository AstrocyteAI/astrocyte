# astrocyte-neo4j

[Neo4j](https://neo4j.com/) adapter implementing Astrocyte’s `GraphStore` protocol.

## Usage

```python
from astrocyte_neo4j import Neo4jGraphStore

store = Neo4jGraphStore(
    uri="bolt://localhost:7687",
    user="neo4j",
    password="password",
)
```

## Development

```bash
uv sync --extra dev
uv run pytest
```

Requires a running Neo4j 5+ instance (`ASTROCYTE_NEO4J_URI`, `ASTROCYTE_NEO4J_USER`, `ASTROCYTE_NEO4J_PASSWORD`).
