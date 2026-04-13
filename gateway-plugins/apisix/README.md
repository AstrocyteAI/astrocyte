# Astrocyte Plugin for Apache APISIX

Intercepts OpenAI-compatible `/chat/completions` requests and transparently adds long-term memory:

- **Pre-hook (access):** Recalls relevant memories from Astrocyte and injects them into the system prompt.
- **Post-hook (body_filter):** Retains the assistant's response as a new memory.

Requires a running [Astrocyte standalone gateway](../../astrocyte-services-py/astrocyte-gateway-py/).

## Install

Copy the plugin file into the APISIX plugins directory:

```bash
cp astrocyte.lua /usr/local/apisix/apisix/plugins/
```

Enable the plugin in `config.yaml`:

```yaml
# config.yaml
plugins:
  - ... # existing plugins
  - astrocyte
```

Reload APISIX:

```bash
apisix reload
```

## Configure

### Via APISIX Admin API

```bash
curl -X PUT http://localhost:9180/apisix/admin/routes/1 \
  -H "X-API-KEY: your-admin-key" \
  -d '{
    "uri": "/v1/chat/completions",
    "upstream": {
      "type": "roundrobin",
      "nodes": { "api.openai.com:443": 1 }
    },
    "plugins": {
      "astrocyte": {
        "astrocyte_url": "http://astrocyte:8900",
        "api_key": "your-api-key",
        "default_bank_id": "default",
        "max_recall_results": 5,
        "retain_responses": true,
        "timeout_ms": 5000
      }
    }
  }'
```

### Via declarative config (apisix.yaml)

```yaml
routes:
  - uri: /v1/chat/completions
    upstream:
      type: roundrobin
      nodes:
        "api.openai.com:443": 1
    plugins:
      astrocyte:
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
| `api_key` | string | `""` | API key for gateway authentication |
| `default_bank_id` | string | `""` | Default memory bank (overridden by `X-Astrocyte-Bank` header) |
| `max_recall_results` | integer | `5` | Max memory hits to inject (1–50) |
| `retain_responses` | boolean | `true` | Retain assistant responses as memories |
| `timeout_ms` | integer | `5000` | HTTP timeout for Astrocyte calls (min 100) |

## How it works

```
Client → APISIX → [Astrocyte Plugin]
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
