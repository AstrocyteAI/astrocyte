"""Astrocytes MCP server — exposes memory tools via Model Context Protocol.

Usage:
    astrocyte-mcp --config astrocyte.yaml
    astrocyte-mcp --config astrocyte.yaml --transport sse --port 8080

See docs/_design/mcp-server.md for the full specification.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from typing import Any

from fastmcp import FastMCP

from astrocyte._astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig, access_grants_for_astrocyte, load_config
from astrocyte.types import AstrocyteContext

# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


def create_mcp_server(brain: Astrocyte, config: AstrocyteConfig) -> FastMCP:
    """Create a FastMCP server wired to an Astrocyte instance."""

    mcp_cfg = config.mcp
    mcp = FastMCP(
        name="astrocyte-memory",
        instructions=(
            "Astrocytes memory server. Use memory_retain to store information, "
            "memory_recall to search memories, memory_reflect to synthesize answers, "
            "and memory_forget to remove memories."
        ),
    )

    # Build context for access control
    ctx = AstrocyteContext(principal=mcp_cfg.principal or "agent:mcp")

    # Default bank
    default_bank = mcp_cfg.default_bank_id

    def _resolve_bank(bank_id: str | None) -> str:
        if bank_id:
            return bank_id
        if default_bank:
            return default_bank
        raise ValueError("bank_id is required (no default_bank_id configured)")

    # ── memory_retain ──────────────────────────────────────────────

    @mcp.tool()
    async def memory_retain(
        content: str,
        bank_id: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, str] | None = None,
        occurred_at: str | None = None,
    ) -> str:
        """Store content into memory.

        Args:
            content: The text to memorize.
            bank_id: Memory bank to store in. Uses default if omitted.
            tags: Optional tags for filtering.
            metadata: Optional key-value metadata.
            occurred_at: ISO 8601 timestamp of when the event happened.
        """
        bid = _resolve_bank(bank_id)
        kwargs: dict[str, Any] = {}
        if occurred_at:
            kwargs["occurred_at"] = datetime.fromisoformat(occurred_at)
        result = await brain.retain(
            content,
            bank_id=bid,
            tags=tags,
            metadata=metadata,
            context=ctx,
            **kwargs,
        )
        if result.stored:
            return json.dumps({"stored": True, "memory_id": result.memory_id})
        return json.dumps({"stored": False, "error": result.error or "unknown"})

    # ── memory_recall ──────────────────────────────────────────────

    @mcp.tool()
    async def memory_recall(
        query: str,
        bank_id: str | None = None,
        banks: list[str] | None = None,
        strategy: str | None = None,
        max_results: int = 10,
        tags: list[str] | None = None,
    ) -> str:
        """Retrieve relevant memories for a query.

        Args:
            query: Natural language query.
            bank_id: Single memory bank to search. Uses default if omitted.
            banks: Multiple banks to search (overrides bank_id).
            strategy: Multi-bank strategy: "cascade", "parallel", or "first_match".
            max_results: Maximum number of results.
            tags: Optional tag filter.
        """
        max_results = min(max_results, mcp_cfg.max_results_limit)

        if banks:
            result = await brain.recall(
                query,
                banks=banks,
                strategy=strategy,
                max_results=max_results,
                tags=tags,
                context=ctx,
            )
        else:
            bid = _resolve_bank(bank_id)
            result = await brain.recall(
                query,
                bank_id=bid,
                max_results=max_results,
                tags=tags,
                context=ctx,
            )

        hits = [
            {
                "text": h.text,
                "score": round(h.score, 4),
                "fact_type": h.fact_type,
                "bank_id": h.bank_id,
                "memory_id": h.memory_id,
            }
            for h in result.hits
        ]
        return json.dumps(
            {
                "hits": hits,
                "total_available": result.total_available,
                "truncated": result.truncated,
            }
        )

    # ── memory_reflect ─────────────────────────────────────────────

    if mcp_cfg.expose_reflect:

        @mcp.tool()
        async def memory_reflect(
            query: str,
            bank_id: str | None = None,
            banks: list[str] | None = None,
            strategy: str | None = None,
            max_tokens: int | None = None,
            include_sources: bool = True,
        ) -> str:
            """Synthesize an answer from memory.

            Args:
                query: The question to answer from memory.
                bank_id: Single memory bank. Uses default if omitted.
                banks: Multiple banks (overrides bank_id).
                strategy: Multi-bank strategy: "cascade", "parallel", or "first_match".
                max_tokens: Maximum tokens for the synthesized answer.
                include_sources: Whether to include source memories.
            """
            if banks:
                result = await brain.reflect(
                    query,
                    banks=banks,
                    strategy=strategy,
                    max_tokens=max_tokens,
                    context=ctx,
                    include_sources=include_sources,
                )
            else:
                bid = _resolve_bank(bank_id)
                result = await brain.reflect(
                    query,
                    bank_id=bid,
                    max_tokens=max_tokens,
                    context=ctx,
                    include_sources=include_sources,
                )

            out: dict[str, Any] = {"answer": result.answer}
            if include_sources and result.sources:
                out["sources"] = [
                    {"text": s.text, "score": round(s.score, 4), "bank_id": s.bank_id} for s in result.sources
                ]
            return json.dumps(out)

    # ── memory_forget ──────────────────────────────────────────────

    if mcp_cfg.expose_forget:

        @mcp.tool()
        async def memory_forget(
            bank_id: str | None = None,
            memory_ids: list[str] | None = None,
            tags: list[str] | None = None,
        ) -> str:
            """Remove memories from a bank.

            Args:
                bank_id: Memory bank. Uses default if omitted.
                memory_ids: Specific memory IDs to delete.
                tags: Delete memories matching these tags.
            """
            bid = _resolve_bank(bank_id)
            result = await brain.forget(
                bid,
                memory_ids=memory_ids,
                tags=tags,
                context=ctx,
            )
            return json.dumps(
                {
                    "deleted_count": result.deleted_count,
                    "archived_count": result.archived_count,
                }
            )

    # ── memory_banks ───────────────────────────────────────────────

    @mcp.tool()
    async def memory_banks() -> str:
        """List available memory banks."""
        # For now, return banks from config. In future, query provider.
        bank_ids: list[str] = []
        if config.banks:
            bank_ids = list(config.banks.keys())
        if default_bank and default_bank not in bank_ids:
            bank_ids.insert(0, default_bank)
        return json.dumps({"banks": bank_ids, "default": default_bank})

    # ── memory_health ──────────────────────────────────────────────

    @mcp.tool()
    async def memory_health() -> str:
        """Check memory system health."""
        status = await brain.health()
        return json.dumps(
            {
                "healthy": status.healthy,
                "message": status.message,
                "latency_ms": round(status.latency_ms, 2) if status.latency_ms else None,
            }
        )

    return mcp


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point for astrocyte-mcp."""
    parser = argparse.ArgumentParser(
        prog="astrocyte-mcp",
        description="Astrocytes MCP server — memory tools for AI agents",
    )
    parser.add_argument(
        "--config",
        required=True,
        help="Path to astrocyte.yaml config file",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="MCP transport (default: stdio)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port for SSE transport (default: 8080)",
    )
    args = parser.parse_args()

    # Load config and create Astrocyte
    config = load_config(args.config)
    brain = Astrocyte(config)

    # Wire access grants from config
    grants = access_grants_for_astrocyte(config)
    if grants:
        brain.set_access_grants(grants)

    # Create MCP server
    mcp = create_mcp_server(brain, config)

    # Run
    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="sse", port=args.port)


if __name__ == "__main__":
    main()
