# astrocyte-integration-llm-wrapper

Optional OpenAI-compatible wrapper that gives an existing chat client Astrocyte memory without moving LLM gateway responsibilities into the core framework.

The wrapper does two things around `client.chat.completions.create(...)`:

1. Calls `brain.recall()` with the latest user message and injects recalled memory as a system message.
2. Calls `brain.retain()` with the user/assistant exchange after the completion returns.

```python
from openai import AsyncOpenAI
from astrocyte import Astrocyte
from astrocyte_integration_llm_wrapper import wrap_openai_client

brain = Astrocyte.from_config("astrocyte.yaml")
client = wrap_openai_client(
    AsyncOpenAI(),
    brain=brain,
    bank_id="user-calvin",
)

response = await client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "What do I usually prefer?"}],
)
```

This package is intentionally an integration adapter, not a core LLM provider. Astrocyte still owns memory, policy, banks, recall, and retention; your application or LLM gateway still owns model routing, spend controls, and chat API normalization.
