# {{ cookiecutter.package_slug }}

{{ cookiecutter.description }}

## Install

```bash
pip install {{ cookiecutter.package_slug }}
```

## Use

```yaml
# astrocyte.yaml
{% if cookiecutter.spi_family == 'vector_stores' %}vector_store: {{ cookiecutter.backend_label }}
vector_store_config:
  url: https://your-{{ cookiecutter.backend_label }}-instance.example.com
  api_key: ${ {{ cookiecutter.backend_label|upper }}_API_KEY }{% elif cookiecutter.spi_family == 'graph_stores' %}graph_store: {{ cookiecutter.backend_label }}
graph_store_config:
  url: bolt://localhost:7687{% elif cookiecutter.spi_family == 'document_stores' %}document_store: {{ cookiecutter.backend_label }}
document_store_config:
  url: http://localhost:9200{% elif cookiecutter.spi_family == 'pageindex_stores' %}pageindex_store: {{ cookiecutter.backend_label }}
pageindex_store_config:
  url: ...{% elif cookiecutter.spi_family == 'llm_providers' %}provider: {{ cookiecutter.backend_label }}
{{ cookiecutter.backend_label }}_config:
  model: ...
  api_key: ${ {{ cookiecutter.backend_label|upper }}_API_KEY }{% else %}{{ cookiecutter.spi_family[:-1] }}: {{ cookiecutter.backend_label }}{% endif %}
```

## Development

```bash
# Install in editable mode + test deps
uv sync --extra dev

# Run tests (skips when the backend service is unreachable)
uv run pytest

# Spin up the backend locally first if you want non-skipped runs:
# (replace with the appropriate docker command for your backend)
# docker run -d -p 9090:9090 {{ cookiecutter.backend_label }}/{{ cookiecutter.backend_label }}:latest
```

## SPI conformance

Implements the [`{% if cookiecutter.spi_family == 'vector_stores' %}VectorStore{% elif cookiecutter.spi_family == 'graph_stores' %}GraphStore{% elif cookiecutter.spi_family == 'document_stores' %}DocumentStore{% elif cookiecutter.spi_family == 'pageindex_stores' %}PageIndexStore{% elif cookiecutter.spi_family == 'wiki_stores' %}WikiStore{% elif cookiecutter.spi_family == 'mental_model_stores' %}MentalModelStore{% elif cookiecutter.spi_family == 'source_stores' %}SourceStore{% elif cookiecutter.spi_family == 'llm_providers' %}LLMProvider{% endif %}`](https://AstrocyteAI.github.io/astrocyte/plugins/provider-spi/) Protocol from `astrocyte.provider`. Conformance tests run in CI against a live {{ cookiecutter.backend_name }} container.

## License

{{ cookiecutter.license }}
