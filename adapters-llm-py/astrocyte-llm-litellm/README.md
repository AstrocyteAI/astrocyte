# astrocyte-llm-litellm

LiteLLM LLMProvider adapter for [Astrocyte](https://github.com/AstrocyteAI/astrocyte) — access 100+ models through a single adapter.

## Install

```bash
pip install astrocyte-llm-litellm
```

## Usage

### YAML config

```yaml
# OpenAI
llm_provider: litellm
llm_provider_config:
  model: openai/gpt-4o-mini
  embedding_model: openai/text-embedding-3-small

# AWS Bedrock
llm_provider: litellm
llm_provider_config:
  model: bedrock/anthropic.claude-sonnet-4-20250514-v1:0
  embedding_model: bedrock/amazon.titan-embed-text-v2:0
  extra:
    aws_region_name: us-east-1

# Azure OpenAI
llm_provider: litellm
llm_provider_config:
  model: azure/gpt-4o
  api_base: https://my-resource.openai.azure.com
  api_key: ${AZURE_OPENAI_API_KEY}

# Google Vertex AI
llm_provider: litellm
llm_provider_config:
  model: vertex_ai/gemini-2.0-flash
  extra:
    vertex_project: my-project
    vertex_location: us-central1

# Self-hosted (Ollama, vLLM, LM Studio)
llm_provider: litellm
llm_provider_config:
  model: ollama/llama3.2
  api_base: http://localhost:11434
```

### Programmatic

```python
from astrocyte_llm_litellm import LiteLLMProvider

llm = LiteLLMProvider(
    model="openai/gpt-4o-mini",
    embedding_model="openai/text-embedding-3-small",
)
```

## What this enables

The LLMProvider powers seven capabilities in the Astrocyte pipeline:

- **Embedding generation** — retain pipeline vectorization
- **Semantic search** — Tier 3 recall via cosine similarity
- **Reflect** — LLM synthesis over retrieved memories
- **Entity extraction** — named entity recognition during retain
- **Curated retain** — LLM-driven memory merge/update decisions
- **Agentic retrieval** — Tier 4 query reformulation
- **PII scanning** — LLM-enhanced detection for compliance profiles (GDPR, HIPAA, PDPA)

## License

Apache-2.0
