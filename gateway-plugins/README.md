# Astrocyte Gateway Plugins

Thin integration plugins that add long-term memory to LLM API calls routed through existing API gateways. Each plugin intercepts OpenAI-compatible `/chat/completions` requests and transparently:

1. **Pre-hook:** Recalls relevant memories from Astrocyte and injects them into the system prompt.
2. **Post-hook:** Retains the assistant's response as a new memory.

All plugins call the [Astrocyte standalone gateway](../astrocyte-services-py/astrocyte-gateway-py/) via HTTP — the intelligence stays in Astrocyte, the plugins are just integration glue.

## Available plugins

| Plugin | Gateway | Language | Install |
|---|---|---|---|
| [Kong](./kong/) | [Kong Gateway](https://konghq.com/) | Lua | Copy `handler.lua` + `schema.lua` to plugins dir |
| [APISIX](./apisix/) | [Apache APISIX](https://apisix.apache.org/) | Lua | Copy `astrocyte.lua` to plugins dir |
| [Azure APIM](./azure-apim/) | [Azure API Management](https://azure.microsoft.com/en-us/products/api-management) | XML policy fragments | `./deploy.sh` (Bicep, Terraform, APIOps, or portal) |

## Architecture

```
Agent App
  → API Gateway (Kong / APISIX / Azure APIM)
      → [pre-hook]  POST /v1/recall → Astrocyte Gateway → memory hits
      → inject hits into system prompt
      → forward enriched request → LLM API (OpenAI, etc.)
      → [post-hook] POST /v1/retain → Astrocyte Gateway
  ← response to agent
```

## When to use gateway plugins vs standalone gateway

| Use case | Recommended |
|---|---|
| Organization already has Kong/APISIX/Azure APIM routing LLM traffic | Gateway plugin |
| New deployment, no existing gateway | Standalone gateway directly |
| Need reflect, event streams, or poll connectors | Standalone gateway (plugins are request-scoped) |
| Zero agent code changes required | Gateway plugin |

## Configuration

All plugins support:

- **`X-Astrocyte-Bank` header** — per-request bank override
- **Default bank** — fallback when header is not set
- **Retain toggle** — enable/disable post-hook retention
- **Timeout** — HTTP timeout for Astrocyte gateway calls

See each plugin's README for platform-specific configuration details.

## Limitations

Gateway plugins are request-scoped. They support:

- ✅ Recall (pre-hook injection)
- ✅ Retain (post-hook extraction)
- ❌ Reflect (requires LLM call within plugin — exceeds typical latency budget)
- ❌ Event streams / poll connectors (no background tasks)

For full pipeline capabilities, use the [standalone gateway](../astrocyte-services-py/astrocyte-gateway-py/) or [library mode](../astrocyte-py/).
