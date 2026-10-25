# LLM provider adapters

Optional LLM provider adapters for Astrocyte. Each package would implement the `LLMProvider` protocol (`complete()` + `embed()`) and be installed separately. There are none today.

For models beyond the built-in providers, run an OpenAI-compatible gateway such as [AISIX](https://github.com/api7/aisix) and point the built-in `openai` provider at it. The gateway holds the provider credentials and maps model aliases to upstream models (OpenAI, Anthropic, Bedrock, Vertex, Azure, Ollama and more), so Astrocyte needs no per-provider package:

```bash
pip install "astrocyte[openai]"
```

```yaml
llm_provider: openai
llm_provider_config:
  base_url: http://localhost:4000/v1
  api_key: ${GATEWAY_API_KEY}          # an AISIX caller key, not a provider key
  model: chat-default                # an AISIX model alias
  embedding_model: embed-default     # an AISIX model alias
```

See the [AISIX guide](../docs/_end-user/configuration-reference.md#llm-gateway-aisix) for the gateway side.
