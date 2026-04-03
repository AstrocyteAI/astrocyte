# MCP server integration

Astrocytes ships as a **Model Context Protocol (MCP) server**, making memory available to any MCP-capable agent (Claude Code, Cursor, Windsurf, custom agents) without code integration.

---

## 1. What this enables

Any MCP-capable client gets persistent memory by adding one entry to its MCP config:

```json
{
  "mcpServers": {
    "memory": {
      "command": "astrocytes-mcp",
      "args": ["--config", "astrocytes.yaml"]
    }
  }
}
```

The agent can then call `retain`, `recall`, `reflect`, and `forget` as MCP tools. The full Astrocytes policy layer (PII scanning, rate limits, token budgets, observability) applies to every call.

This is a major distribution channel: every Claude Code user, every Cursor user, every MCP-compatible IDE becomes a potential Astrocytes user.

---

## 2. MCP tool surface

### 2.1 Tools exposed

| Tool | Description | Parameters |
|---|---|---|
| `memory_retain` | Store content into memory | `content`, `bank_id`, `tags`, `metadata`, `occurred_at` |
| `memory_recall` | Retrieve relevant memories | `query`, `bank_id`, `max_results`, `tags`, `time_range` |
| `memory_reflect` | Synthesize an answer from memory | `query`, `bank_id`, `max_tokens`, `include_sources` |
| `memory_forget` | Remove memories by selector | `bank_id`, `memory_ids`, `tags`, `before_date` |
| `memory_banks` | List available banks | (none) |
| `memory_health` | Check system status | (none) |

### 2.2 Tool schemas (MCP format)

```json
{
  "name": "memory_recall",
  "description": "Retrieve relevant memories for a query. Returns scored memory hits.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "query": {"type": "string", "description": "Natural language query"},
      "bank_id": {"type": "string", "description": "Memory bank to query"},
      "max_results": {"type": "integer", "default": 10},
      "tags": {"type": "array", "items": {"type": "string"}},
      "time_range": {
        "type": "object",
        "properties": {
          "start": {"type": "string", "format": "date-time"},
          "end": {"type": "string", "format": "date-time"}
        }
      }
    },
    "required": ["query", "bank_id"]
  }
}
```

### 2.3 Multi-bank support via MCP

```json
{
  "name": "memory_recall",
  "inputSchema": {
    "properties": {
      "query": {"type": "string"},
      "bank_id": {"type": "string", "description": "Single bank ID"},
      "banks": {"type": "array", "items": {"type": "string"}, "description": "Multiple bank IDs (overrides bank_id)"},
      "strategy": {"type": "string", "enum": ["cascade", "parallel", "first_match"], "default": "cascade"}
    },
    "required": ["query"]
  }
}
```

---

## 3. Transport modes

### 3.1 stdio (default for local)

```bash
astrocytes-mcp --config astrocytes.yaml
```

Communicates over stdin/stdout. Standard for local MCP servers.

### 3.2 SSE (for remote / shared)

```bash
astrocytes-mcp --config astrocytes.yaml --transport sse --port 8080
```

Server-Sent Events transport for remote agents or multi-agent setups sharing one Astrocytes instance.

---

## 4. Configuration

The MCP server uses the same `astrocytes.yaml` as the Python library. No separate config:

```yaml
# astrocytes.yaml - same config for library and MCP server
profile: personal
provider_tier: engine
provider: mystique
provider_config:
  endpoint: https://mystique.company.com
  api_key: ${MYSTIQUE_API_KEY}

# MCP-specific settings (optional)
mcp:
  default_bank_id: personal        # Used when bank_id is omitted
  expose_reflect: true             # Set false to hide reflect tool
  expose_forget: false             # Set false to hide forget tool (safety)
  max_results_limit: 50            # Hard cap on max_results parameter
```

---

## 5. Packaging

The MCP server ships as part of the `astrocytes` package with an optional CLI entry point:

```toml
# pyproject.toml
[project.scripts]
astrocytes-mcp = "astrocytes.mcp:main"
```

No separate package needed. `pip install astrocytes` (or `pnpm` for npm-based MCP configs - see below).

### 5.1 npm wrapper (for MCP clients that expect npm packages)

Some MCP clients (Claude Code, Cursor) discover servers via npm. A thin npm wrapper:

```json
{
  "name": "@astrocyteai/mcp-server",
  "bin": {
    "astrocytes-mcp": "./bin/astrocytes-mcp.js"
  }
}
```

Where `bin/astrocytes-mcp.js` spawns the Python `astrocytes-mcp` process. This is the pattern used by many Python MCP servers.

---

## 6. Policy enforcement

The full Astrocytes policy layer applies to MCP tool calls:

- **PII barrier**: scans `content` in `memory_retain` calls before storage
- **Rate limits**: per-bank limits apply to the MCP caller
- **Token budgets**: `memory_recall` and `memory_reflect` results are budget-enforced
- **Circuit breaker**: if the provider is down, tools return appropriate MCP errors
- **OTel traces**: every MCP tool call generates spans for observability
- **Access control**: MCP server can be configured with a specific access identity (see `19-access-control.md`). For mapping richer client identity from the host or combining with external PDPs, see `06-identity-and-external-policy.md`.

---

## 7. Agent integration patterns

### 7.1 Claude Code

```json
// .claude/settings.json
{
  "mcpServers": {
    "memory": {
      "command": "astrocytes-mcp",
      "args": ["--config", "/path/to/astrocytes.yaml"]
    }
  }
}
```

Claude Code can then use memory tools in conversations:
- Retain project context, decisions, and learnings
- Recall past decisions when making new ones
- Reflect on accumulated project knowledge

### 7.2 Multi-agent with shared memory

Multiple agents connect to the same MCP server (SSE transport), sharing memory banks:

```
Agent A (coding)  ──┐
Agent B (review)  ──┼── astrocytes-mcp (SSE, port 8080) ── Mystique
Agent C (testing) ──┘
```

Each agent has its own bank plus access to shared team banks (see `14-multi-bank-orchestration.md`).
