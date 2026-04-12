# astrocyte-elasticsearch

[Elasticsearch](https://www.elastic.co/) adapter implementing Astrocyte’s `DocumentStore` protocol (BM25 full-text).

## Usage

```python
from astrocyte_elasticsearch import ElasticsearchDocumentStore

store = ElasticsearchDocumentStore(url="http://localhost:9200")
```

## Development

```bash
uv sync --extra dev
uv run pytest
```

Set `ASTROCYTE_ELASTICSEARCH_URL` (default `http://127.0.0.1:9200`).
