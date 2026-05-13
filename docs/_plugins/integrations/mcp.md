# MCP server integration

Astrocyte as a Model Context Protocol server for Claude Code, Cursor, Codex, Windsurf, and any MCP-capable agent.

**Module:** `astrocyte.mcp`
**Pattern:** MCP server — stdio or SSE transport, with tools gated by `mcp:` configuration
**Framework dependency:** `fastmcp` (required for `astrocyte[mcp]`)

## Recommended path — one-command plugin install

Astrocyte ships a packaged plugin at `mcp-plugins/astrocyte/` with marketplace metadata for all three editors. The plugin auto-wires the MCP server, drops in a `SessionStart` hook, and installs the `astrocyte-memory` skill that teaches the agent when to call which tool.

### Claude Code

From inside Claude Code:

```bash
/plugin marketplace add AstrocyteAI/astrocyte
/plugin install astrocyte
```

### Cursor / Codex

Both Cursor and Codex read plugin manifests from a local directory:

```bash
git clone https://github.com/AstrocyteAI/astrocyte.git --depth 1 /tmp/astrocyte
# Cursor:
mkdir -p ~/.cursor/plugins && cp -r /tmp/astrocyte/mcp-plugins/astrocyte ~/.cursor/plugins/
# Codex:
mkdir -p ~/.codex/plugins && cp -r /tmp/astrocyte/mcp-plugins/astrocyte ~/.codex/plugins/
```

All three editors invoke the same command: `uvx --from astrocyte-stack astrocyte-mcp --config $ASTROCYTE_CONFIG`. No separate Python install required — `uv` handles the ephemeral venv.

See [`mcp-plugins/astrocyte/README.md`](https://github.com/AstrocyteAI/astrocyte/blob/main/mcp-plugins/astrocyte/README.md) for the full install + troubleshooting walkthrough.

## Manual MCP wiring (without the plugin)

If you'd rather wire the MCP server by hand:

```bash
pip install astrocyte[mcp]                                  # or: pip install astrocyte-stack
```

### Claude Code — `.claude/settings.json`

```json
{
  "mcpServers": {
    "astrocyte": {
      "command": "astrocyte-mcp",
      "args": ["--config", "astrocyte.yaml"]
    }
  }
}
```

### CLI

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
| `memory_compile` | `bank_id?`, `scope?` | Compile wiki pages when a `WikiStore` is configured |
| `memory_graph_search` | `query`, `bank_id?`, `limit?` | Search graph entities when a `GraphStore` is configured |
| `memory_graph_neighbors` | `entity_ids`, `bank_id?`, `max_depth?`, `limit?` | Traverse graph-linked memories |
| `memory_bank_health` | `bank_id?` | Admin-only bank health when `expose_admin: true` |
| `memory_lifecycle` / legal-hold tools | varies | Admin-only lifecycle and legal hold operations when `expose_admin: true` |

## Configuration

```yaml
# astrocyte.yaml — MCP-specific settings
mcp:
  default_bank_id: personal        # Used when bank_id is omitted
  expose_reflect: true             # Toggle reflect tool (default: true)
  expose_forget: false             # Toggle forget tool (default: false)
  expose_admin: false              # Toggle lifecycle / bank health / legal hold tools
  max_results_limit: 50            # Hard cap on max_results
  principal: "agent:claude-code"   # Identity for access control
```

## Multi-bank support

```
memory_recall(query="preferences", banks=["personal", "team"], strategy="parallel")
memory_reflect(query="summarize everything", banks=["personal", "team", "org"])
```

## Policy enforcement

The full Astrocyte policy layer applies: PII scanning, rate limits, quotas, token budgets, circuit breakers, access control, hooks, metrics. See [Policy layer](/design/policy-layer/) and [MCP server design](/design/mcp-server/).

## Multi-agent with shared memory

```
Agent A (coding)  ──┐
Agent B (review)  ──┼── astrocyte-mcp (SSE, port 8080) ── Provider
Agent C (testing) ──┘
```

Multiple agents connect to the same MCP server via SSE transport, sharing memory banks with per-agent access control.
