"""OpenAI-compatible LLM wrapper for Astrocyte memory.

The wrapper lives outside the core so Astrocyte remains a memory framework,
not a generic LLM gateway. It decorates an existing client and calls
``recall`` before chat completion and ``retain`` after completion.
"""

from astrocyte_integration_llm_wrapper.wrapper import (
    AstrocyteMemoryWrapper,
    MemoryWrapperConfig,
    wrap_openai_client,
)

__all__ = [
    "AstrocyteMemoryWrapper",
    "MemoryWrapperConfig",
    "wrap_openai_client",
]
