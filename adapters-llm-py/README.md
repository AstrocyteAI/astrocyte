# LLM provider adapters

Optional LLM provider adapters for Astrocyte. Each package implements the `LLMProvider` protocol (`complete()` + `embed()`) and is installed separately so users only pull in the dependencies they need.

| Package | Wraps | Models |
|---|---|---|
| [`astrocyte-llm-litellm`](./astrocyte-llm-litellm/) | [LiteLLM](https://github.com/BerriAI/litellm) | 100+ models across OpenAI, Anthropic, Bedrock, Vertex, Azure, Groq, Ollama, and more |

## Install

```bash
pip install astrocyte-llm-litellm
```

## Configure

```yaml
llm_provider: litellm
llm_provider_config:
  model: openai/gpt-4o-mini
  embedding_model: openai/text-embedding-3-small
```

See each package's README for provider-specific configuration (Bedrock, Vertex, self-hosted, etc.).
