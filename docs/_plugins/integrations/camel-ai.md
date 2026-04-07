# CAMEL-AI integration

Astrocyte as role-based memory for CAMEL-AI multi-agent role-playing systems.

**Module:** `astrocyte.integrations.camel_ai`
**Pattern:** Role-scoped memory — write/read with role attribution and per-role banks
**Framework dependency:** `camel-ai` (optional)

## Install

```bash
pip install astrocyte camel-ai
```

## Usage

```python
from astrocyte import Astrocyte
from astrocyte.integrations.camel_ai import AstrocyteCamelMemory

brain = Astrocyte.from_config("astrocyte.yaml")

# Role-based memory for multi-agent role-playing
memory = AstrocyteCamelMemory(
    brain,
    bank_id="simulation",
    role_banks={"doctor": "doctor-bank", "patient": "patient-bank"},
)

# Write with role attribution
await memory.write("Patient reports headaches", role="doctor", agent_id="agent-1")
await memory.write("I've had headaches for a week", role="patient")

# Read scoped by role
results = await memory.read("symptoms", role="doctor")
context = await memory.get_context("patient history", role="doctor")

# Synthesize across role's memory
answer = await memory.reflect("What symptoms have been reported?", role="doctor")

# Clear a role's memory
await memory.clear(role="patient")
```

## Integration pattern

Memories are tagged with `role:{role_name}` and `camel-ai` for filtering. Each role can have its own bank or share a common bank.

| Method | Astrocyte call |
|---|---|
| `write(content, role=...)` | `brain.retain()` with role metadata + tags |
| `read(query, role=...)` | `brain.recall()` scoped to role's bank |
| `get_context(query, role=...)` | `brain.recall()` → formatted string |
| `reflect(query, role=...)` | `brain.reflect()` on role's bank |
| `clear(role=...)` | `brain.clear_bank()` on role's bank |

## API reference

### `AstrocyteCamelMemory(brain, bank_id, *, role_banks=None)`

| Parameter | Type | Description |
|---|---|---|
| `brain` | `Astrocyte` | Configured Astrocyte instance |
| `bank_id` | `str` | Shared simulation bank |
| `role_banks` | `dict[str, str]` | Role name → bank ID for per-role isolation |
