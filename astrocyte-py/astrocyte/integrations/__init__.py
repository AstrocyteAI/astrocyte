"""Astrocyte agent framework integrations.

Thin middleware layers that wire Astrocyte into popular agent frameworks.
Each integration maps the framework's memory abstraction to
brain.retain() / brain.recall() / brain.reflect(). Thin adapters accept an optional ``AstrocyteContext`` on constructors or
tool factories for access control and OBO.

Without Astrocyte: each framework needs integrations with each provider (N × M).
With Astrocyte: N + M.

Integrations are optional — install the framework dependency to use:
    pip install astrocyte[langgraph]
    pip install astrocyte[crewai]
    pip install astrocyte[pydantic-ai]
"""
