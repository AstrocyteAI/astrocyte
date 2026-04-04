# Agent framework middleware

Astrocytes provides thin integration layers for popular agent frameworks. Each integration wires the Astrocytes API into the framework's memory abstraction, giving every framework access to every memory provider through one adapter.

Without Astrocytes, each agent framework needs integrations with each memory provider (N x M). With Astrocytes, it's N + M.

### Scope: memory integration, not orchestration

Astrocytes **does not** specify how an agent is structured (workflow graph, tools, checkpoints, retries, human-in-the-loop, multi-agent handoff). That is the job of **LangGraph**, **CrewAI**, **Pydantic AI**, **AG2**, the **OpenAI / Claude agent SDKs**, or **your own app**. This document only describes **thin mappers** from those frameworks’ memory hooks to `Astrocyte.retain()` / `recall()` / `reflect()` / … through the policy layer.

In **harness vs context** terms (see [Architecture framework](../_design/architecture-framework.md) §1, *Context engineering vs harness engineering*): framework integrations and your app are **harness**—they decide when to call memory and how to run the loop; Astrocytes sits **below** that. Turning `recall` hits into the next system block or user message is **context engineering**, which the **app** still owns—Astrocytes returns governed hits and synthesized text, not the only valid prompt shape.

**Agent cards** (and vendor **agent catalogs** / registry UIs) describe agents—capabilities, metadata, presentation—not how orchestration runs. Astrocytes still **does not** implement an agent runtime or a catalog service; every retain/recall path is keyed by the same runtime facts as today: **principal** (from your AuthN story), **memory bank id**, **provider tier**, and **SPI** config (`provider-spi.md`, `ecosystem-and-packaging.md`, `access-control.md`).

What *is* in scope is making **card → memory context** boring and portable:

- **Declarative mapping** — e.g. in `astrocytes.yaml` (or an included document): stable **card id** or URI → `{ principal, bank_id, … }`, with shared defaults where many cards reuse the same bank or principal pattern.
- **Resolver helpers** in integrations so a host or framework can pass the **active card id** and obtain **principal + bank** before calling `Astrocyte`, without every product reinventing the same glue.

Card payloads can follow emerging standards (for example A2A-style agent metadata) or vendor JSON; only fields needed for **identity + bank selection** need to be understood at the mapper—everything else remains opaque to the core.

**Summary:** Nothing new is “stored inside the engine” as a parallel agent model. Agent cards are **input** to the existing contract via mapping, so operators get one obvious place to connect catalog identity to Astrocytes’ principals and banks.

### Sandbox context and exfiltration

Integrations should treat **sandbox id**, **environment** (e.g. dev/staging/prod), or **deployment tier** as first-class inputs alongside the **agent card** when resolving **principal** and **bank_id**. That keeps **recall** from crossing boundaries the compute sandbox was supposed to enforce. Strong **compute** isolation does not remove the need for **network egress** controls or a **trustworthy BFF** that does not let the agent pick an arbitrary production **principal**—see `sandbox-awareness-and-exfiltration.md` and [Let’s discuss sandbox isolation](https://www.shayon.dev/post/2026/52/lets-discuss-sandbox-isolation/).

---

## 1. Supported frameworks

| Framework | Module | Pattern | Status |
|---|---|---|---|
| LangGraph / LangChain | `astrocytes.integrations.langgraph` | Memory store (save_context, search, load_memory_variables) | Implemented |
| CrewAI | `astrocytes.integrations.crewai` | Crew/agent memory (save, search, reset, per-agent banks) | Implemented |
| Pydantic AI | `astrocytes.integrations.pydantic_ai` | Agent tools (retain, recall, reflect as tool functions) | Implemented |
| OpenAI Agents SDK | `astrocytes.integrations.openai_agents` | Function calling (OpenAI-format tool definitions + handlers) | Implemented |
| Claude Agent SDK | `astrocytes.integrations.claude_agent_sdk` | Native @tool + create_sdk_mcp_server (in-process MCP) | Implemented |
| Google ADK | `astrocytes.integrations.google_adk` | Callable tools (async functions with type annotations) | Implemented |
| AutoGen / AG2 | `astrocytes.integrations.autogen` | Memory + tools (save/query/get_context + OpenAI tool defs) | Implemented |
| Smolagents (HuggingFace) | `astrocytes.integrations.smolagents` | Code-centric tools (Tool protocol: name, inputs, forward) | Implemented |
| LlamaIndex | `astrocytes.integrations.llamaindex` | Memory store (put, get, get_all, search, reset) | Implemented |
| Strands Agents (AWS) | `astrocytes.integrations.strands` | Spec + handler tools (JSON Schema spec + async handler) | Implemented |
| MCP (Claude Code, Cursor, Windsurf) | `astrocytes.mcp` | MCP server (6 tools via FastMCP, stdio/SSE) | Implemented |

All integrations are **zero-dependency on the framework** — they use duck typing, not base class inheritance. Testable and functional without installing the target framework.

---

## 2. Integration examples

All examples below assume:

```python
from astrocytes import Astrocyte
brain = Astrocyte.from_config("astrocytes.yaml")
```

### 2.1 LangGraph / LangChain

```python
from astrocytes.integrations.langgraph import AstrocyteMemory

memory = AstrocyteMemory(brain, bank_id="user-123")

# Save interaction context
await memory.save_context(
    inputs={"question": "What is dark mode?"},
    outputs={"answer": "A UI theme with dark background"},
    thread_id="thread-abc",
)

# Search memory
results = await memory.search("dark mode", max_results=5)

# Load formatted memories for prompt injection
variables = await memory.load_memory_variables({"topic": "UI preferences"})
# → {"memory": "- Calvin prefers dark mode\n- ..."}

# Thread → bank mapping
memory = AstrocyteMemory(
    brain,
    bank_id="default-bank",
    thread_to_bank={"thread-abc": "custom-bank"},
)
```

### 2.2 CrewAI

```python
from astrocytes.integrations.crewai import AstrocyteCrewMemory

# Shared crew memory + per-agent banks
memory = AstrocyteCrewMemory(
    brain,
    bank_id="team-shared",
    agent_banks={"devops": "devops-bank", "researcher": "research-bank"},
)

# Save with agent attribution
await memory.save(
    "Kubernetes deploys via Helm charts",
    agent_id="devops",
    tags=["infrastructure"],
)

# Search across the crew's shared bank
results = await memory.search("deployment strategy")

# Reset a specific agent's memory
await memory.reset(agent_id="devops")
```

### 2.3 Pydantic AI

```python
from astrocytes.integrations.pydantic_ai import astrocyte_tools

tools = astrocyte_tools(brain, bank_id="user-123")
# → [{"name": "memory_retain", "function": ..., "description": ...}, ...]

# Register with Pydantic AI agent
agent = Agent(model="claude-sonnet-4-20250514", tools=tools)

# The agent can now call these during execution:
#   memory_retain("Calvin prefers dark mode", tags=["preference"])
#   memory_recall("What are Calvin's preferences?")
#   memory_reflect("Summarize what we know about Calvin")
```

### 2.4 OpenAI Agents SDK

```python
from astrocytes.integrations.openai_agents import astrocyte_tool_definitions

tools, handlers = astrocyte_tool_definitions(brain, bank_id="user-123")

# tools → list of OpenAI function-calling format dicts
# handlers → {"memory_retain": async fn, "memory_recall": async fn, ...}

# Pass tools to OpenAI API
response = client.chat.completions.create(
    model="gpt-4o",
    tools=tools,
    messages=[...],
)

# Dispatch tool calls
for call in response.choices[0].message.tool_calls:
    handler = handlers[call.function.name]
    result = await handler(**json.loads(call.function.arguments))
```

### 2.5 Claude Agent SDK (Anthropic)

```python
from astrocytes.integrations.claude_agent_sdk import astrocyte_claude_agent_server

# Option A: Native SDK MCP server (requires claude_agent_sdk installed)
memory_server = astrocyte_claude_agent_server(brain, bank_id="user-123")

from claude_agent_sdk import query, ClaudeAgentOptions

options = ClaudeAgentOptions(
    mcp_servers={"memory": memory_server},
    allowed_tools=["mcp__astrocytes_memory__*"],  # Allow all memory tools
)

async for message in query(
    prompt="What do you remember about my preferences?",
    options=options,
):
    if isinstance(message, ResultMessage) and message.subtype == "success":
        print(message.result)
```

```python
# Option B: Tool definitions without SDK (for testing or custom integration)
from astrocytes.integrations.claude_agent_sdk import astrocyte_claude_agent_tools

tools = astrocyte_claude_agent_tools(brain, bank_id="user-123")
# Each tool returns SDK format: {"content": [{"type": "text", "text": "..."}]}

result = await tools[0]["handler"]({"content": "Calvin prefers dark mode"})
# → {"content": [{"type": "text", "text": '{"stored": true, "memory_id": "..."}'}]}
```

### 2.6 Google ADK (Google)

```python
from astrocytes.integrations.google_adk import astrocyte_adk_tools

tools = astrocyte_adk_tools(brain, bank_id="user-123")
# → list of async callable functions with type annotations + docstrings

# Register with Google ADK agent
from google.adk import Agent
agent = Agent(model="gemini-2.0-flash", tools=tools)

# Tools available: memory_retain, memory_recall, memory_reflect
# Each is an async function with proper type annotations for ADK schema generation
```

### 2.7 AutoGen / AG2 (Microsoft)

```python
from astrocytes.integrations.autogen import AstrocyteAutoGenMemory

memory = AstrocyteAutoGenMemory(
    brain,
    bank_id="team",
    agent_banks={"agent-a": "bank-a", "agent-b": "bank-b"},
)

# Direct API for conversation hooks
await memory.save("User prefers Python", agent_id="agent-a")
context = await memory.get_context("What does the user prefer?")
# → "- User prefers Python"

# Or register as OpenAI-format tools for function calling
tools = memory.as_tools()
handlers = memory.get_handlers()

# Use with ConversableAgent
agent = ConversableAgent("assistant", llm_config=llm_config)
# Register tools via AutoGen's tool registration pattern
```

### 2.8 Smolagents (HuggingFace)

```python
from astrocytes.integrations.smolagents import astrocyte_smolagent_tools

tools = astrocyte_smolagent_tools(brain, bank_id="user-123")
# → list of AstrocyteSmolTool instances

# Each tool has: name, description, inputs (schema), output_type, forward()
# Compatible with smolagents' Tool protocol

from smolagents import CodeAgent, HfApiModel
agent = CodeAgent(tools=tools, model=HfApiModel())

# The agent writes Python code that calls:
#   memory_retain(content="...", tags="pref,ui")
#   memory_recall(query="...", max_results=5)
#   memory_reflect(query="...")
```

### 2.9 LlamaIndex

```python
from astrocytes.integrations.llamaindex import AstrocyteLlamaMemory

memory = AstrocyteLlamaMemory(brain, bank_id="user-123", max_results=10)

# Store and retrieve
await memory.put("Calvin prefers dark mode", tags=["preference"])
context = await memory.get("UI preferences")
# → "- Calvin prefers dark mode"

# Get all memories in the bank
all_mems = await memory.get_all()

# Structured search
results = await memory.search("dark mode", tags=["preference"])

# Use with LlamaIndex chat engine
chat_engine = index.as_chat_engine(memory=memory)
```

### 2.10 Strands Agents (AWS)

```python
from astrocytes.integrations.strands import astrocyte_strands_tools

tools = astrocyte_strands_tools(brain, bank_id="user-123")
# → list of {"spec": {...}, "handler": async fn}

# Each tool has a JSON Schema spec + async handler function
# Compatible with Strands Agents' tool registration pattern

from strands import Agent
agent = Agent(model=model, tools=tools)

# Handler dispatch:
for tool in tools:
    print(tool["spec"]["name"])  # "memory_retain", "memory_recall", ...
    result = await tool["handler"]({"content": "test"})
```

### 2.11 MCP (Claude Code, Cursor, Windsurf)

```json
// .claude/settings.json or MCP client config
{
  "mcpServers": {
    "memory": {
      "command": "astrocytes-mcp",
      "args": ["--config", "astrocytes.yaml"]
    }
  }
}
```

The MCP server exposes 6 tools: `memory_retain`, `memory_recall`, `memory_reflect`, `memory_forget`, `memory_banks`, `memory_health`. Full policy layer applies. See `mcp-server.md` for details.

---

## 3. What the integration layer does NOT do

- **No business logic.** Integrations are thin mappers. They translate framework-specific interfaces to Astrocytes API calls.
- **No policy bypass.** All calls go through the full Astrocytes policy layer.
- **No framework-specific storage.** Conversation history managed by the framework stays in the framework. Astrocytes stores long-term memory, not turn-by-turn chat logs.
- **No provider-specific code.** Integrations talk to `Astrocyte`, never to providers directly.

---

## 4. Auto-retain patterns

Some integrations can automatically retain agent experiences:

```python
memory = AstrocyteMemory(
    brain,
    bank_id="agent-123",
    auto_retain=True,                    # Automatically retain after each task
    auto_retain_filter="completions",    # Only retain completed tasks, not failures
)
```

Auto-retain is **opt-in** and respects all policies (PII scanning, signal quality, quotas). It should not silently flood memory - the retain gating principles from the policy layer apply.

---

## 5. Packaging

All integrations ship inside the `astrocytes` package — no separate packages to install. The integration modules use **duck typing** (not base class inheritance), so they work **without** installing the target framework. Install the framework only when you actually use the integration:

```bash
# The integrations themselves are always available
pip install astrocytes

# Install framework dependencies as needed
pip install langgraph        # for astrocytes.integrations.langgraph
pip install crewai           # for astrocytes.integrations.crewai
pip install pydantic-ai      # for astrocytes.integrations.pydantic_ai
pip install ag2              # for astrocytes.integrations.autogen
pip install smolagents       # for astrocytes.integrations.smolagents
pip install llama-index-core # for astrocytes.integrations.llamaindex
pip install strands-agents   # for astrocytes.integrations.strands
pip install fastmcp          # for astrocytes.mcp (MCP server)
```

**Note:** `openai_agents` and `google_adk` integrations produce tool definitions as plain dicts/functions — they don't import any framework code, so no extra install is needed.
