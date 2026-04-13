# Astrocyte Plugin for Kong Gateway

Intercepts OpenAI-compatible `/chat/completions` requests and transparently adds long-term memory:

- **Pre-hook (access):** Recalls relevant memories from Astrocyte and injects them into the system prompt.
- **Post-hook (body_filter):** Retains the assistant's response as a new memory.

Requires a running [Astrocyte standalone gateway](../../astrocyte-services-py/astrocyte-gateway-py/).

## Install

Copy the plugin files into Kong's plugin directory:

```bash
mkdir -p /usr/local/share/lua/5.1/kong/plugins/astrocyte
cp handler.lua schema.lua /usr/local/share/lua/5.1/kong/plugins/astrocyte/
```

Enable the plugin in `kong.conf` or via environment variable:

```bash
export KONG_PLUGINS="bundled,astrocyte"
```

## Configure

### Via Kong Admin API

```bash
curl -X POST http://localhost:8001/services/llm-api/plugins \
  --data "name=astrocyte" \
  --data "config.astrocyte_url=http://astrocyte:8900" \
  --data "config.api_key=your-api-key" \
  --data "config.default_bank_id=default" \
  --data "config.max_recall_results=5" \
  --data "config.retain_responses=true" \
  --data "config.timeout_ms=5000"
```

### Via declarative config (kong.yml)

```yaml
plugins:
  - name: astrocyte
    service: llm-api
    config:
      astrocyte_url: http://astrocyte:8900
      api_key: your-api-key
      default_bank_id: default
      max_recall_results: 5
      retain_responses: true
      timeout_ms: 5000
```

## Configuration reference

| Parameter | Type | Default | Description |
|---|---|---|---|
| `astrocyte_url` | string | `http://localhost:8900` | Base URL of the Astrocyte gateway |
| `api_key` | string | — | API key for gateway authentication |
| `default_bank_id` | string | — | Default memory bank (overridden by `X-Astrocyte-Bank` header) |
| `max_recall_results` | integer | `5` | Max memory hits to inject |
| `retain_responses` | boolean | `true` | Retain assistant responses as memories |
| `timeout_ms` | integer | `5000` | HTTP timeout for Astrocyte calls |

## How it works

```
Client → Kong → [Astrocyte Plugin]
                    ├─ access phase:
                    │   1. Extract last user message from request body
                    │   2. POST /v1/recall to Astrocyte gateway
                    │   3. Inject memory hits into system prompt
                    │   4. Forward enriched request to LLM
                    │
                    └─ body_filter phase:
                        1. Buffer complete LLM response
                        2. Extract assistant message
                        3. POST /v1/retain to Astrocyte gateway
                        4. Pass response through to client
```

## Bank resolution

The plugin resolves `bank_id` in this order:

1. `X-Astrocyte-Bank` request header (per-request override)
2. `config.default_bank_id` (plugin configuration)

If neither is set, the recall phase is skipped.
