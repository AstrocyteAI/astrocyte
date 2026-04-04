# Agent framework middleware

Astrocytes provides thin integration layers for popular agent frameworks. Each integration wires the Astrocytes API into the framework's memory abstraction, giving every framework access to every memory provider through one adapter.

Without Astrocytes, each agent framework needs integrations with each memory provider (N x M). With Astrocytes, it's N + M.

### Scope: memory integration, not orchestration

Astrocytes **does not** specify how an agent is structured (workflow graph, tools, checkpoints, retries, human-in-the-loop, multi-agent handoff). That is the job of **LangGraph**, **CrewAI**, **Pydantic AI**, **AG2**, the **OpenAI / Claude agent SDKs**, or **your own app**. This document only describes **thin mappers** from those frameworks’ memory hooks to `Astrocyte.retain()` / `recall()` / `reflect()` / … through the policy layer.

**Agent cards** (and vendor **agent catalogs** / registry UIs) describe agents—capabilities, metadata, presentation—not how orchestration runs. Astrocytes still **does not** implement an agent runtime or a catalog service; every retain/recall path is keyed by the same runtime facts as today: **principal** (from your AuthN story), **memory bank id**, **provider tier**, and **SPI** config (`provider-spi.md`, `ecosystem-and-packaging.md`, `access-control.md`).

What *is* in scope is making **card → memory context** boring and portable:

- **Declarative mapping** — e.g. in `astrocytes.yaml` (or an included document): stable **card id** or URI → `{ principal, bank_id, … }`, with shared defaults where many cards reuse the same bank or principal pattern.
- **Resolver helpers** in integrations so a host or framework can pass the **active card id** and obtain **principal + bank** before calling `Astrocyte`, without every product reinventing the same glue.

Card payloads can follow emerging standards (for example A2A-style agent metadata) or vendor JSON; only fields needed for **identity + bank selection** need to be understood at the mapper—everything else remains opaque to the core.

**Summary:** Nothing new is “stored inside the engine” as a parallel agent model. Agent cards are **input** to the existing contract via mapping, so operators get one obvious place to connect catalog identity to Astrocytes’ principals and banks.

---

## 1. Supported frameworks

| Framework | Integration package | Memory abstraction |
|---|---|---|
| LangGraph / LangChain | `astrocytes[langgraph]` | `BaseCheckpointSaver` / `BaseMemory` |
| CrewAI | `astrocytes[crewai]` | `CrewMemory` interface |
| Pydantic AI | `astrocytes[pydantic-ai]` | Dependency injection via `Deps` |
| AG2 (AutoGen) | `astrocytes[ag2]` | `MemoryProvider` interface |
| LlamaIndex | `astrocytes[llamaindex]` | `BaseMemory` / `ChatMemoryBuffer` |
| Claude Agent SDK | `astrocytes[claude-sdk]` | Tool-based via MCP or direct |
| OpenAI Agents SDK | `astrocytes[openai-agents]` | Tool definitions |

Integrations ship as **optional dependencies** of the `astrocytes` package, not separate packages. This keeps the ecosystem simple.

---

## 2. Integration patterns

### 2.1 LangGraph

```python
from astrocytes import Astrocyte
from astrocytes.integrations.langgraph import AstrocyteMemory

brain = Astrocyte.from_config("astrocytes.yaml")

# As a LangGraph memory store
memory = AstrocyteMemory(brain, bank_id="user-123")

graph = StateGraph(AgentState)
graph.add_node("agent", agent_node)
# Memory is available in state
app = graph.compile(checkpointer=memory)
```

The integration maps:
- LangGraph `put` → `brain.retain()`
- LangGraph `get` / `search` → `brain.recall()`
- Thread ID → bank ID mapping (configurable)

### 2.2 CrewAI

```python
from astrocytes import Astrocyte
from astrocytes.integrations.crewai import AstrocyteMemory

brain = Astrocyte.from_config("astrocytes.yaml")

crew = Crew(
    agents=[support_agent, research_agent],
    memory=AstrocyteMemory(brain, bank_id="team-support"),
)
```

The integration maps:
- CrewAI memory save → `brain.retain()`
- CrewAI memory search → `brain.recall()`
- Crew-level memory is a shared bank; agent-level memory uses per-agent banks

### 2.3 Pydantic AI

```python
from astrocytes import Astrocyte
from astrocytes.integrations.pydantic_ai import astrocyte_tools

brain = Astrocyte.from_config("astrocytes.yaml")

agent = Agent(
    model="claude-sonnet-4-20250514",
    tools=astrocyte_tools(brain, bank_id="user-123"),
)
```

Exposes `retain`, `recall`, `reflect` as Pydantic AI tools that the agent can call during execution.

### 2.4 OpenAI Agents SDK / Claude Agent SDK

```python
from astrocytes import Astrocyte
from astrocytes.integrations.openai_agents import astrocyte_tool_definitions

brain = Astrocyte.from_config("astrocytes.yaml")

tools = astrocyte_tool_definitions(brain, bank_id="user-123")
# Returns OpenAI-compatible tool definitions for retain/recall/reflect
```

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

Integrations are optional dependencies:

```toml
# pyproject.toml
[project.optional-dependencies]
langgraph = ["langgraph>=0.2"]
crewai = ["crewai>=0.80"]
pydantic-ai = ["pydantic-ai>=0.1"]
ag2 = ["ag2>=0.6"]
llamaindex = ["llama-index-core>=0.11"]
all-integrations = ["astrocytes[langgraph,crewai,pydantic-ai,ag2,llamaindex]"]
```

```bash
pip install astrocytes[langgraph]       # LangGraph integration only
pip install astrocytes[all-integrations] # Everything
```
