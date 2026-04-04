"""Astrocyte agent framework integrations.

Thin middleware layers that wire Astrocyte into popular agent frameworks.
Each integration maps the framework's memory abstraction to
brain.retain() / brain.recall() / brain.reflect().

Without Astrocyte: each framework needs integrations with each provider (N × M).
With Astrocyte: N + M.

Integrations are optional — install the framework dependency to use:
    pip install astrocyte[langgraph]
    pip install astrocyte[crewai]
    pip install astrocyte[pydantic-ai]
"""
