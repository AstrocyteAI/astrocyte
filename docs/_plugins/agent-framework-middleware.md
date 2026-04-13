# Agent framework middleware

Astrocyte provides thin integration layers for popular agent frameworks. Each integration wires the Astrocyte API into the framework's memory abstraction, giving every framework access to every memory provider through one adapter.

Without Astrocyte, each agent framework needs integrations with each memory provider (N x M). With Astrocyte, it's N + M.

### Scope: memory integration, not orchestration

Astrocyte **does not** specify how an agent is structured (workflow graph, tools, checkpoints, retries, human-in-the-loop, multi-agent handoff). That is the job of **LangGraph**, **CrewAI**, **Pydantic AI**, **AG2**, the **OpenAI / Claude agent SDKs**, or **your own app**. This document only describes **thin mappers** from those frameworks' memory hooks to `Astrocyte.retain()` / `recall()` / `reflect()` / … through the policy layer.

In **harness vs context** terms (see [Architecture framework](../_design/architecture.md) §1, *Context engineering vs harness engineering*): framework integrations and your app are **harness**—they decide when to call memory and how to run the loop; Astrocyte sits **below** that. Turning `recall` hits into the next system block or user message is **context engineering**, which the **app** still owns—Astrocyte returns governed hits and synthesized text, not the only valid prompt shape.

**Server-side ingest without an agent loop** (scheduled poll, GitHub → `retain`): see [Poll ingest with the standalone gateway](/end-user/poll-ingest-gateway/) and [Production-grade HTTP service](/end-user/production-grade-http-service/).

### Sandbox context and exfiltration

Integrations should treat **sandbox id**, **environment** (e.g. dev/staging/prod), or **deployment tier** as first-class inputs alongside the **agent card** when resolving **principal** and **bank_id**. See `sandbox-awareness-and-exfiltration.md`.

---

## 1. Supported frameworks

| Framework | Guide | Pattern |
|---|---|---|
| LangGraph / LangChain | [LangGraph integration](/plugins/integrations/langgraph/) | Memory store |
| CrewAI | [CrewAI integration](/plugins/integrations/crewai/) | Crew/agent memory |
| Pydantic AI | [Pydantic AI integration](/plugins/integrations/pydantic-ai/) | Agent tools |
| OpenAI Agents SDK | [OpenAI integration](/plugins/integrations/openai-agents/) | Function calling |
| Claude Agent SDK | [Claude Agent SDK integration](/plugins/integrations/claude-agent-sdk/) | Native @tool + MCP |
| Google ADK | [Google ADK integration](/plugins/integrations/google-adk/) | Async callables |
| AutoGen / AG2 | [AutoGen integration](/plugins/integrations/autogen/) | Memory + tools |
| Smolagents (HuggingFace) | [Smolagents integration](/plugins/integrations/smolagents/) | Tool protocol |
| LlamaIndex | [LlamaIndex integration](/plugins/integrations/llamaindex/) | Memory store |
| Strands Agents (AWS) | [Strands integration](/plugins/integrations/strands/) | Spec + handler |
| Semantic Kernel (Microsoft) | [Semantic Kernel integration](/plugins/integrations/semantic-kernel/) | Plugin functions |
| DSPy (Stanford) | [DSPy integration](/plugins/integrations/dspy/) | Retrieval model |
| CAMEL-AI | [CAMEL-AI integration](/plugins/integrations/camel-ai/) | Role-based memory |
| BeeAI (IBM) | [BeeAI integration](/plugins/integrations/beeai/) | Tool with run() |
| Microsoft Agent Framework | [MS Agent integration](/plugins/integrations/microsoft-agent/) | OpenAI-compatible |
| LiveKit Agents | [LiveKit integration](/plugins/integrations/livekit/) | Session lifecycle |
| Haystack (deepset) | [Haystack integration](/plugins/integrations/haystack/) | Retriever + Writer |
| MCP (Claude Code, Cursor) | [MCP server](/plugins/integrations/mcp/) | MCP server (FastMCP) |

All integrations are **zero-dependency on the framework** — they use duck typing, not base class inheritance. Testable and functional without installing the target framework.

All integration guides assume you have a configured Astrocyte instance:

```python
from astrocyte import Astrocyte
brain = Astrocyte.from_config("astrocyte.yaml")
```

---

## 2. What the integration layer does NOT do

- **No business logic.** Integrations are thin mappers. They translate framework-specific interfaces to Astrocyte API calls.
- **No policy bypass.** All calls go through the full Astrocyte policy layer.
- **No framework-specific storage.** Conversation history managed by the framework stays in the framework. Astrocyte stores long-term memory, not turn-by-turn chat logs.
- **No provider-specific code.** Integrations talk to `Astrocyte`, never to providers directly.

---

## 3. Auto-retain patterns

Some integrations can automatically retain agent experiences:

```python
memory = AstrocyteMemory(
    brain,
    bank_id="agent-123",
    auto_retain=True,                    # Automatically retain after each task
    auto_retain_filter="completions",    # Only retain completed tasks, not failures
)
```

Auto-retain is **opt-in** and respects all policies (PII scanning, signal quality, quotas). It should not silently flood memory — the retain gating principles from the policy layer apply.

---

## 4. Packaging

All integrations ship inside the `astrocyte` package — no separate packages to install. The integration modules use **duck typing** (not base class inheritance), so they work **without** installing the target framework. Install the framework only when you actually use the integration:

```bash
pip install astrocyte                 # All integrations included
pip install langgraph                  # Install framework when you use it
pip install crewai                     # etc.
```

`openai_agents`, `google_adk`, `microsoft_agent`, and `claude_agent_sdk` (tool definitions mode) produce plain dicts/functions — no extra install needed.
