# MCP server integration

Astrocytes as a Model Context Protocol server for Claude Code, Cursor, Windsurf, and any MCP-capable agent.

**Module:** `astrocyte.mcp`
**Pattern:** MCP server with 6 tools — stdio or SSE transport
**Framework dependency:** `fastmcp` (required for `astrocyte[mcp]`)

## Install

```bash
pip install astrocyte[mcp]
```

## Usage — Claude Code

```json
// .claude/settings.json
{
  "mcpServers": {
    "memory": {
      "command": "astrocyte-mcp",
      "args": ["--config", "astrocyte.yaml"]
    }
  }
}
```

## Usage — CLI

```bash
# stdio (default — for local MCP clients)
astrocyte-mcp --config astrocyte.yaml

# SSE (for remote / shared / multi-agent)
astrocyte-mcp --config astrocyte.yaml --transport sse --port 8080
```

## Tools exposed

| Tool | Parameters | Description |
|---|---|---|
| `memory_retain` | `content`, `bank_id?`, `tags?`, `metadata?`, `occurred_at?` | Store content |
| `memory_recall` | `query`, `bank_id?`, `banks?`, `strategy?`, `max_results?`, `tags?` | Search memories |
| `memory_reflect` | `query`, `bank_id?`, `banks?`, `strategy?`, `max_tokens?` | Synthesize answer |
| `memory_forget` | `bank_id?`, `memory_ids?`, `tags?` | Delete memories |
| `memory_banks` | (none) | List available banks |
| `memory_health` | (none) | Check system health |

## Configuration

```yaml
# astrocyte.yaml — MCP-specific settings
mcp:
  default_bank_id: personal        # Used when bank_id is omitted
  expose_reflect: true             # Toggle reflect tool (default: true)
  expose_forget: false             # Toggle forget tool (default: false)
  max_results_limit: 50            # Hard cap on max_results
  principal: "agent:claude-code"   # Identity for access control
```

## Multi-bank support

```
memory_recall(query="preferences", banks=["personal", "team"], strategy="parallel")
memory_reflect(query="summarize everything", banks=["personal", "team", "org"])
```

## Policy enforcement

The full Astrocytes policy layer applies: PII scanning, rate limits, quotas, token budgets, circuit breakers, access control, hooks, metrics. See [Policy layer](../../design/policy-layer/) and [MCP server design](../../design/mcp-server/).

## Multi-agent with shared memory

```
Agent A (coding)  ──┐
Agent B (review)  ──┼── astrocyte-mcp (SSE, port 8080) ── Provider
Agent C (testing) ──┘
```

Multiple agents connect to the same MCP server via SSE transport, sharing memory banks with per-agent access control.
