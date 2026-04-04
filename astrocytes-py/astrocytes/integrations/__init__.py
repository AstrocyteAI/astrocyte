"""Astrocytes agent framework integrations.

Thin middleware layers that wire Astrocytes into popular agent frameworks.
Each integration maps the framework's memory abstraction to
brain.retain() / brain.recall() / brain.reflect().

Without Astrocytes: each framework needs integrations with each provider (N × M).
With Astrocytes: N + M.

Integrations are optional — install the framework dependency to use:
    pip install astrocytes[langgraph]
    pip install astrocytes[crewai]
    pip install astrocytes[pydantic-ai]
"""
