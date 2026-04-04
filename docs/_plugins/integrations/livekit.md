# LiveKit Agents integration

Astrocyte memory for real-time voice and video AI agents built with LiveKit.

**Module:** `astrocyte.integrations.livekit`
**Pattern:** Session lifecycle memory — pre-fetch context, mid-session recall, post-session retain
**Framework dependency:** `livekit-agents` (optional)

## Install

```bash
pip install astrocyte livekit-agents
```

## Usage

```python
from astrocyte import Astrocyte
from astrocyte.integrations.livekit import AstrocyteLiveKitMemory

brain = Astrocyte.from_config("astrocyte.yaml")
memory = AstrocyteLiveKitMemory(
    brain,
    bank_id="caller-db",
    session_bank_prefix="session-",  # Per-session banks
)

# In a LiveKit agent session handler:

# 1. Session start — load relevant context
context = await memory.get_session_context("user preferences", session_id="abc123")

# 2. Mid-session — quick recall for context enrichment
results = await memory.recall_mid_session("pricing", session_id="abc123", max_results=3)

# 3. Session end — retain key takeaways
await memory.retain_from_session(
    "User prefers morning appointments and mentioned budget constraints",
    session_id="abc123",
    tags=["preference", "scheduling"],
)

# 4. Summarize session
summary = await memory.summarize_session(session_id="abc123")
```

## Design: why LiveKit is different

LiveKit agents are **real-time** — voice/video over WebRTC. Memory integration is about the **session lifecycle**, not batch tool calls:

| Phase | Method | Purpose |
|---|---|---|
| **Session start** | `get_session_context()` | Pre-fetch memories for system prompt |
| **Mid-session** | `recall_mid_session()` | Dynamic context enrichment during conversation |
| **Session end** | `retain_from_session()` | Persist key takeaways |
| **Post-session** | `summarize_session()` | Synthesize session into a summary |

## Per-session banks

With `session_bank_prefix`, each session gets its own isolated bank:

```python
memory = AstrocyteLiveKitMemory(
    brain,
    bank_id="shared-caller-db",      # Fallback for no session_id
    session_bank_prefix="session-",   # session-abc123, session-xyz789, etc.
)
```

## API reference

### `AstrocyteLiveKitMemory(brain, bank_id, *, session_bank_prefix=None, max_context_items=10)`

| Method | Returns |
|---|---|
| `get_session_context(query, *, session_id=None)` | `str` — formatted memory context |
| `recall_mid_session(query, *, session_id=None, max_results=3)` | `list[dict]` |
| `retain_from_session(content, *, session_id=None, tags=None)` | `str \| None` — memory_id |
| `summarize_session(*, session_id=None)` | `str` — synthesized summary |
