# Astrocyte plugin for Claude Code, Cursor, and Codex

Self-hosted agent memory wired into your AI coding agent in three lines. Backed by your own Postgres — no remote service, no API key, your data never leaves your infrastructure.

## What you get

| Tool | What it does |
|---|---|
| `memory_retain` | Store a fact / decision / preference. |
| `memory_recall` | Search prior memories by query + tags + time range. |
| `memory_reflect` | Synthesize an answer from memory with citations. |
| `memory_forget` | Delete memories by ID, tag, or selector. |
| `memory_compile` | Roll multiple facts into a wiki page. |
| `memory_history` | Audit trail for one memory. |
| `memory_banks` / `memory_health` | List banks / probe the server. |
| `memory_graph_search` / `memory_graph_neighbors` | Entity bridging (when a `GraphStore` is configured). |

Plus admin tools (`memory_lifecycle`, `memory_bank_health`, legal-hold) when `expose_admin: true` in your config.

## Prerequisites

1. **A running Astrocyte stack.** Easiest:
   ```bash
   pip install astrocyte-stack    # installs astrocyte + astrocyte-postgres
   ```
   For the full quick-start (Postgres setup, config file), see <https://AstrocyteAI.github.io/astrocyte/end-user/quick-start/>.

2. **`uv` installed.** The plugin's MCP config calls `uvx --from astrocyte-stack astrocyte-mcp`, which uses `uv`'s ephemeral-tool runner.
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

3. **An `astrocyte.yaml`** in a known location. Default convention:
   ```bash
   mkdir -p ~/.config/astrocyte
   # Drop your config there:
   curl -L https://AstrocyteAI.github.io/astrocyte/config.example.yaml > ~/.config/astrocyte/astrocyte.yaml
   export ASTROCYTE_CONFIG=$HOME/.config/astrocyte/astrocyte.yaml
   ```
   Add the `export` line to your shell rc so the env var is set for editors started from a fresh terminal.

## Install — Claude Code

The Astrocyte repository ships a plugin marketplace at its root. From inside Claude Code:

```bash
/plugin marketplace add AstrocyteAI/astrocyte
/plugin install astrocyte
```

That wires the MCP server, registers the `astrocyte-memory` skill, and runs the `SessionStart` hook. Restart Claude Code to pick up the new tools.

Verify:
```bash
/mcp                       # ← astrocyte should be listed
```
Then ask Claude to recall something to confirm the round-trip:
> "Recall what we know about my coding preferences."

## Install — Cursor

Cursor reads `.cursor-plugin/plugin.json` directly. Drop the plugin somewhere Cursor can see it:

```bash
mkdir -p ~/.cursor/plugins
git clone https://github.com/AstrocyteAI/astrocyte.git --depth 1 /tmp/astrocyte
cp -r /tmp/astrocyte/mcp-plugins/astrocyte ~/.cursor/plugins/astrocyte
```

Cursor picks it up on next start. The MCP server config lives in `.cursor-mcp.json` and uses the same `uvx --from astrocyte-stack astrocyte-mcp` invocation.

## Install — Codex

Same shape as Cursor; Codex reads `.codex-plugin/plugin.json`:

```bash
mkdir -p ~/.codex/plugins
git clone https://github.com/AstrocyteAI/astrocyte.git --depth 1 /tmp/astrocyte
cp -r /tmp/astrocyte/mcp-plugins/astrocyte ~/.codex/plugins/astrocyte
```

The `interface` block in `.codex-plugin/plugin.json` populates the Codex marketplace listing (displayName, capabilities, default prompts, brand color).

## How the MCP server is launched

All three editor configs (`.mcp.json` / `.cursor-mcp.json` / `.codex-mcp.json`) point at the same command:

```
uvx --from astrocyte-stack astrocyte-mcp --config $ASTROCYTE_CONFIG
```

- `uvx` runs the published `astrocyte-stack` package in an ephemeral venv.
- `astrocyte-mcp` is the console script wired in `astrocyte-py/pyproject.toml`.
- `$ASTROCYTE_CONFIG` points at your YAML; the env var is the contract between this plugin and your local stack.

This means **no Python install on your machine is necessary** beyond `uv` itself — `uvx` handles the rest. First call cold-starts ~3-5s while `uv` downloads the wheels; subsequent calls hit the local cache.

## What's in this plugin

```
mcp-plugins/astrocyte/
├── .claude-plugin/plugin.json     # Claude Code manifest
├── .cursor-plugin/plugin.json     # Cursor manifest
├── .codex-plugin/plugin.json      # Codex manifest (+ marketplace interface block)
├── .mcp.json                      # Claude Code MCP wiring
├── .cursor-mcp.json               # Cursor MCP wiring
├── .codex-mcp.json                # Codex MCP wiring
├── hooks/hooks.json               # SessionStart hook (minimal in v0.1)
├── scripts/on_session_start.sh    # The session-start probe
├── skills/astrocyte-memory/
│   └── SKILL.md                   # When to recall, when to retain — the protocol
├── logo.svg                       # Astrocyte mark
└── README.md                      # This file
```

## v0.1.0 scope and the v0.2.0 path

What's in this release:

- **Three editor manifests** — Claude Code, Cursor, Codex.
- **MCP server wiring** — points each editor at local `astrocyte-mcp` via `uvx`.
- **Memory protocol skill** — `astrocyte-memory` SKILL.md teaching the agent when to call which tool.
- **Minimal SessionStart hook** — confirms the plugin loaded + warns if `ASTROCYTE_CONFIG` is unset.

What's deferred to v0.2.0:

- **Auto-capture hooks** (`UserPromptSubmit`, `Stop`, `PreCompact`). Capturing every turn into memory needs a streaming `/retain` endpoint that doesn't block on an LLM round-trip per call — that lives on the Astrocyte roadmap. v0.1.0 is explicit-retain only (the skill teaches when to call `memory_retain`).
- **`@astrocyte/mcp` npm wrapper.** Once we see demand from users without `uv` installed.

## Updating

```bash
/plugin update astrocyte           # Claude Code
# For Cursor / Codex, re-pull the repo and re-copy the plugin dir.
```

## Reporting issues

<https://github.com/AstrocyteAI/astrocyte/issues>

## License

Apache-2.0.
