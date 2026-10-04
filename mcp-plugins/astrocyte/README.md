# Astrocyte plugin for Claude Code, Cursor, and Codex

Persistent memory for your coding agent that stays on your machine: one local
SQLite file, local embeddings, no server to run and no API key.

## Install — two commands

```bash
uv tool install 'astrocyte[local]'
astrocyte setup
```

`astrocyte setup` creates `~/.config/astrocyte/astrocyte.yaml`, proves the
memory server starts, then registers it with every agent it finds — Claude
Code, Codex CLI, Cursor, Gemini CLI, Windsurf, and Copilot CLI — and verifies
each registration took effect. Run it again any time; it only changes what is
out of date. Check everything end to end with `astrocyte doctor`, and repair a
moved install with `astrocyte doctor --fix`.

This plugin is optional on top of that: it adds the `astrocyte-memory` skill
(when to recall, what to retain) and a session check that tells you if
Astrocyte isn't installed or set up yet.

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

## Install the plugin (optional)

**Claude Code:**

```
/plugin marketplace add AstrocyteAI/astrocyte
/plugin install astrocyte
```

**Cursor / Codex:** copy this directory into `~/.cursor/plugins/astrocyte` or
`~/.codex/plugins/astrocyte`.

## Why the plugin doesn't launch the server

Earlier versions registered the server from each plugin manifest via
`uvx --from astrocyte-stack astrocyte-mcp`. That crashed on first use
(`astrocyte-stack` did not include the MCP dependency), depended on an
`ASTROCYTE_CONFIG` variable most shells never set, and — alongside
`astrocyte setup` — would register a second, duplicate server. Registration
now has one owner: `astrocyte setup` writes a single entry per agent with
absolute paths, verifies it, and `astrocyte doctor --fix` repairs it.

## What's in this plugin

```
mcp-plugins/astrocyte/
├── .claude-plugin/plugin.json     # Claude Code manifest
├── .cursor-plugin/plugin.json     # Cursor manifest
├── .codex-plugin/plugin.json      # Codex manifest (+ marketplace interface block)
├── hooks/hooks.json               # SessionStart hook
├── scripts/on_session_start.sh    # Reports a missing install or setup; never blocks
├── skills/astrocyte-memory/
│   └── SKILL.md                   # When to recall, when to retain — the protocol
├── logo.svg                       # Astrocyte mark
└── README.md                      # This file
```

## Automatic memory

In Claude Code, Codex and Antigravity, `astrocyte setup` also turns on automatic memory for interactive
sessions (headless `claude -p` / `codex exec` / `agy -p` runs are left alone): each finished turn of a conversation is saved to the project's local memory, and memories
relevant to a new prompt are added to the agent's context — only when they are
genuinely similar, never twice in one session. A new session opens with where the
previous one in that project left off. Nothing leaves your machine.
Pause it with `ASTROCYTE_HOOKS=off`; remove it with `astrocyte setup --no-hooks`
(remembered by later `astrocyte setup` runs until you run `astrocyte setup --claude`).
In Codex, trust the three astrocyte hooks once with `/hooks`.

`astrocyte memory` lists what is remembered about the current project; `astrocyte memory search`,
`astrocyte memory forget <id>` (erases from disk) and `astrocyte memory banks` cover the rest.
Seed a project's memory with `astrocyte memory import CLAUDE.md AGENTS.md docs/`.

## Updating

```bash
uv tool upgrade astrocyte          # the memory server and CLI
astrocyte doctor --fix             # re-point agents if the install moved
/plugin update astrocyte           # this plugin, in Claude Code
```

## Reporting issues

<https://github.com/AstrocyteAI/astrocyte/issues>

## License

Apache-2.0.
