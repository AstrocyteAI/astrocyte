"""Astrocyte MCP server — exposes memory tools via Model Context Protocol.

Usage:
    astrocyte-mcp --config astrocyte.yaml
    astrocyte-mcp --config astrocyte.yaml --transport sse --port 8080

See docs/_design/mcp-server.md for the full specification.

Note: Access control in MCP deployments typically uses one principal from
``mcp.principal`` (defaults to ``agent:mcp``), or pass
``astrocyte_context=...`` to :func:`create_mcp_server` when your host maps
callers to identity. Per-caller identity still requires transport-level auth
at the MCP host.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from typing import Any

from fastmcp import FastMCP

from astrocyte._astrocyte import Astrocyte
from astrocyte._mcp_identity import JwtIdentityMiddleware, build_jwt_middleware
from astrocyte.config import AstrocyteConfig, access_grants_for_astrocyte, load_config
from astrocyte.types import AstrocyteContext

logger = logging.getLogger("astrocyte.mcp")

# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


def create_mcp_server(
    brain: Astrocyte,
    config: AstrocyteConfig,
    *,
    astrocyte_context: AstrocyteContext | None = None,
    jwt_middleware: JwtIdentityMiddleware | None = None,
) -> FastMCP:
    """Create a FastMCP server wired to an Astrocyte instance.

    Identity resolution order (per call):

    1. If ``astrocyte_context`` was passed at server-creation time, it is
       used as-is (legacy embedding pattern). This takes precedence over
       all header-based resolution so tests and embedded deployments that
       pre-bind identity are unaffected.
    2. Else if ``jwt_middleware`` is set OR
       ``config.identity.jwt_middleware.enabled`` is true, per-call
       identity is resolved from the inbound request's Authorization
       header (identity spec §3 Gap 1 wiring). See
       ``docs/_plugins/jwt-identity-middleware.md`` for operator setup.
    3. Else the server falls back to the static ``mcp.principal`` label
       (default ``agent:mcp``) — the pre-middleware behavior.

    Args:
        jwt_middleware: Optional pre-built middleware. When None but the
            config enables JWT middleware, the server constructs one via
            :func:`build_jwt_middleware`. Pass explicitly to inject a
            test decoder or bypass PyJWT.
    """

    mcp_cfg = config.mcp
    mcp = FastMCP(
        name="astrocyte-memory",
        instructions=(
            "Astrocyte memory server. Use memory_retain to store information, "
            "memory_recall to search memories, memory_reflect to synthesize answers, "
            "and memory_forget to remove memories."
        ),
    )

    # Static context — still built for the pre-middleware path (legacy
    # deployments that don't enable JWT middleware). When middleware is
    # enabled, this is only used if header resolution is somehow bypassed.
    static_ctx = astrocyte_context or AstrocyteContext(
        principal=mcp_cfg.principal or "agent:mcp",
    )

    # Build or accept JWT middleware. Explicit > config > none.
    if jwt_middleware is None and config.identity.jwt_middleware.enabled:
        jwt_middleware = build_jwt_middleware(config.identity.jwt_middleware)

    def _resolve_context() -> AstrocyteContext:
        """Resolve the AstrocyteContext for the current request.

        Called on every tool invocation so the middleware sees each call's
        headers. Static ``astrocyte_context`` (when set at server creation)
        always wins — that preserves backward compatibility for embedded
        hosts that pre-bind identity.
        """
        # An explicit pre-bound context always wins (embedded / test use).
        if astrocyte_context is not None:
            return astrocyte_context
        # Middleware path — pull headers, resolve identity, build ctx.
        if jwt_middleware is not None:
            try:
                from fastmcp.server.dependencies import get_http_headers

                headers = get_http_headers(include={"authorization"})
            except Exception:
                # stdio transport or pre-request context — no headers.
                # Middleware policy decides how to treat the missing
                # header (fail-closed vs anonymous).
                headers = {}
            return jwt_middleware.resolve(headers)
        # No middleware, no pre-bound ctx — fall through to static.
        return static_ctx

    # Default bank
    default_bank = mcp_cfg.default_bank_id

    _MAX_TAGS = 20
    _MAX_TAG_LENGTH = 255

    def _resolve_bank(bank_id: str | None) -> str:
        if bank_id:
            return bank_id
        if default_bank:
            return default_bank
        raise ValueError("bank_id is required (no default_bank_id configured)")

    def _validate_tags(tags: list[str] | None) -> list[str] | None:
        if not tags:
            return tags
        if len(tags) > _MAX_TAGS:
            raise ValueError(f"Too many tags ({len(tags)}), max {_MAX_TAGS}")
        for tag in tags:
            if not isinstance(tag, str) or len(tag) > _MAX_TAG_LENGTH:
                raise ValueError(f"Invalid tag (must be string, max {_MAX_TAG_LENGTH} chars)")
        return tags

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
        try:
            bid = _resolve_bank(bank_id)
            tags = _validate_tags(tags)
            kwargs: dict[str, Any] = {}
            if occurred_at:
                try:
                    kwargs["occurred_at"] = datetime.fromisoformat(occurred_at)
                except ValueError:
                    return json.dumps({"stored": False, "error": "Invalid occurred_at format; expected ISO 8601"})
            result = await brain.retain(
                content,
                bank_id=bid,
                tags=tags,
                metadata=metadata,
                context=_resolve_context(),
                **kwargs,
            )
            if result.stored:
                return json.dumps({"stored": True, "memory_id": result.memory_id})
            return json.dumps({"stored": False, "error": "Failed to store memory"})
        except Exception as exc:
            logger.exception("memory_retain failed")
            return json.dumps({"stored": False, "error": type(exc).__name__})

    # ── memory_recall ──────────────────────────────────────────────

    @mcp.tool()
    async def memory_recall(
        query: str,
        bank_id: str | None = None,
        banks: list[str] | None = None,
        strategy: str | None = None,
        max_results: int = 10,
        tags: list[str] | None = None,
        session_id: str | None = None,
    ) -> str:
        """Retrieve relevant memories for a query — RAW retrieval, no synthesis.

        Returns a ranked list of memory ``hits``. The caller is expected to
        compose its own answer from these hits (this is the "retrieval
        primitive" — you, the calling agent, are the answerer).

        **Use `memory_recall` when**: you want to read the underlying
        memories and synthesize them yourself, OR you need to show citations
        / quotes verbatim, OR you need fine control over which memories
        end up in your final response.

        **Use `memory_reflect` instead when**: you want a finished answer
        you can pass straight through to the user. Reflect does its own
        synthesis via an agentic loop (multi-step retrieval + reasoning)
        and returns one composed answer with cited sources. Cheaper for
        you than calling `memory_recall` + a follow-up LLM round-trip.

        Args:
            query: Natural language query.
            bank_id: Single memory bank to search. Uses default if omitted.
            banks: Multiple banks to search (overrides bank_id).
            strategy: Multi-bank strategy: "cascade", "parallel", or "first_match".
            max_results: Maximum number of results.
            tags: Optional tag filter.
            session_id: M31 Fix 2 — opaque session identifier. When the
                calling application knows which conversation session the
                query targets, pass it to scope retrieval to that
                session's facts/sections. ``None`` (default) preserves
                cross-session retrieval.
        """
        try:
            max_results = max(1, min(max_results, mcp_cfg.max_results_limit))
            tags = _validate_tags(tags)

            if banks:
                result = await brain.recall(
                    query,
                    banks=banks,
                    strategy=strategy,
                    max_results=max_results,
                    tags=tags,
                    context=_resolve_context(),
                    session_id=session_id,  # M31 Fix 2
                )
            else:
                bid = _resolve_bank(bank_id)
                result = await brain.recall(
                    query,
                    bank_id=bid,
                    max_results=max_results,
                    tags=tags,
                    context=_resolve_context(),
                    session_id=session_id,  # M31 Fix 2
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
        except Exception as exc:
            logger.exception("memory_recall failed")
            return json.dumps({"hits": [], "total_available": 0, "error": type(exc).__name__})

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
            """Get a finished, cited answer to a question from memory.

            Returns ``{"answer": <synthesized text>, "sources": [...]}``.
            The ``answer`` IS the response — Astrocyte's internal agentic
            loop ran multi-step retrieval + reasoning and composed it.

            **How to use the `answer` field:**
            - Quote it verbatim, OR paraphrase it into your own voice, OR
              return it directly to the user.
            - **Do NOT pass it back through another LLM for re-synthesis.**
              That double-synthesis pattern loses citation fidelity, doubles
              cost, and produces strictly worse answers than either pure
              path. If you want raw memories to compose yourself, use
              `memory_recall` instead.
            - `sources` carries the cited memory chunks for traceability /
              UI rendering — show them as citations next to the answer,
              don't re-feed them into a synth prompt.

            **Use `memory_reflect` when**: the user asked a question and you
            want a complete, citation-grounded answer back. Especially good
            for multi-hop questions ("how did X relate to Y?"), synthesis
            questions ("summarize what we discussed about Z"), and any
            question where the answer requires reasoning across several
            memories.

            **Use `memory_recall` instead when**: you want raw memories
            (e.g. to show as a search result list) or you want to do your
            own synthesis with strong opinions on prompt / format.

            Args:
                query: The question to answer from memory.
                bank_id: Single memory bank. Uses default if omitted.
                banks: Multiple banks (overrides bank_id).
                strategy: Multi-bank strategy: "cascade", "parallel", or "first_match".
                max_tokens: Maximum tokens for the synthesized answer.
                include_sources: Whether to include source memories
                    (default True — needed for showing citations).
            """
            try:
                if banks:
                    result = await brain.reflect(
                        query,
                        banks=banks,
                        strategy=strategy,
                        max_tokens=max_tokens,
                        context=_resolve_context(),
                        include_sources=include_sources,
                    )
                else:
                    bid = _resolve_bank(bank_id)
                    result = await brain.reflect(
                        query,
                        bank_id=bid,
                        max_tokens=max_tokens,
                        context=_resolve_context(),
                        include_sources=include_sources,
                    )

                out: dict[str, Any] = {"answer": result.answer}
                if include_sources and result.sources:
                    out["sources"] = [
                        {"text": s.text, "score": round(s.score, 4), "bank_id": s.bank_id} for s in result.sources
                    ]
                return json.dumps(out)
            except Exception as exc:
                logger.exception("memory_reflect failed")
                return json.dumps({"answer": "", "error": type(exc).__name__})

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
            try:
                bid = _resolve_bank(bank_id)
                tags = _validate_tags(tags)
                result = await brain.forget(
                    bid,
                    memory_ids=memory_ids,
                    tags=tags,
                    context=_resolve_context(),
                )
                return json.dumps(
                    {
                        "deleted_count": result.deleted_count,
                        "archived_count": result.archived_count,
                    }
                )
            except Exception as exc:
                logger.exception("memory_forget failed")
                return json.dumps({"deleted_count": 0, "error": type(exc).__name__})

    # ── memory_history ─────────────────────────────────────────────

    @mcp.tool()
    async def memory_history(
        query: str,
        bank_id: str | None = None,
        as_of: str | None = None,
        max_results: int = 10,
        tags: list[str] | None = None,
    ) -> str:
        """Reconstruct what the agent knew at a past point in time (M9 time travel).

        Args:
            query: Recall query to run against the historical snapshot.
            bank_id: Bank to query. Uses default if omitted.
            as_of: ISO 8601 UTC datetime — memories retained after this moment
                are hidden (e.g. "2025-01-01T00:00:00Z").
            max_results: Maximum hits to return (default 10).
            tags: Optional tag filter applied on top of the time filter.
        """
        try:
            bid = _resolve_bank(bank_id)
            tags = _validate_tags(tags)
            if not as_of:
                return json.dumps({"hits": [], "error": "as_of (ISO 8601) is required"})
            try:
                as_of_dt = datetime.fromisoformat(as_of)
            except ValueError:
                return json.dumps({"hits": [], "error": "Invalid as_of format; expected ISO 8601"})
            result = await brain.history(
                query,
                bid,
                as_of_dt,
                max_results=max(1, min(max_results, mcp_cfg.max_results_limit)),
                tags=tags,
                context=_resolve_context(),
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
                    "as_of": result.as_of.isoformat(),
                    "bank_id": result.bank_id,
                }
            )
        except Exception as exc:
            logger.exception("memory_history failed")
            return json.dumps({"hits": [], "error": type(exc).__name__})

    # ── memory_audit ────────────────────────────────────────────────

    @mcp.tool()
    async def memory_audit(
        scope: str,
        bank_id: str | None = None,
        max_memories: int = 50,
        tags: list[str] | None = None,
    ) -> str:
        """Identify knowledge gaps for a topic in a memory bank (M10).

        Use this to discover what the agent does not know about a subject
        before relying on recall for important decisions.

        Args:
            scope: Natural-language topic to audit (e.g. "Alice's employment history").
            bank_id: Bank to audit. Uses default if omitted.
            max_memories: Memories to retrieve and scan (default 50).
            tags: Optional tag filter to narrow the retrieved memories.
        """
        try:
            bid = _resolve_bank(bank_id)
            tags = _validate_tags(tags)
            result = await brain.audit(
                scope,
                bid,
                max_memories=max(1, min(max_memories, 200)),
                tags=tags,
            )
            gaps = [{"topic": g.topic, "severity": g.severity, "reason": g.reason} for g in result.gaps]
            return json.dumps(
                {
                    "scope": result.scope,
                    "bank_id": result.bank_id,
                    "coverage_score": round(result.coverage_score, 3),
                    "memories_scanned": result.memories_scanned,
                    "gaps": gaps,
                }
            )
        except Exception as exc:
            logger.exception("memory_audit failed")
            return json.dumps({"gaps": [], "coverage_score": 0.0, "error": type(exc).__name__})

    # ── memory_compile ─────────────────────────────────────────────

    @mcp.tool()
    async def memory_compile(
        bank_id: str | None = None,
        scope: str | None = None,
    ) -> str:
        """Compile raw memories into structured wiki pages (M8).

        Synthesises a wiki page for each detected topic scope using the LLM.
        Call this periodically to distil accumulated memories into a curated
        knowledge base that recall can surface ahead of raw fragments.

        Args:
            bank_id: Bank to compile. Uses default if omitted.
            scope: Compile only memories tagged with this scope string.
                   Omit to trigger full scope discovery (tag grouping +
                   embedding cluster labelling across the whole bank).
        """
        try:
            bid = _resolve_bank(bank_id)
            result = await brain.compile(bid, scope=scope)
            out: dict[str, Any] = {
                "bank_id": result.bank_id,
                "pages_created": result.pages_created,
                "pages_updated": result.pages_updated,
                "scopes_compiled": result.scopes_compiled,
                "noise_memories": result.noise_memories,
                "tokens_used": result.tokens_used,
                "elapsed_ms": result.elapsed_ms,
            }
            if result.error:
                out["error"] = result.error
            return json.dumps(out)
        except Exception as exc:
            logger.exception("memory_compile failed")
            return json.dumps({"pages_created": 0, "pages_updated": 0, "error": type(exc).__name__})

    # ── memory_graph_search ────────────────────────────────────────

    @mcp.tool()
    async def memory_graph_search(
        query: str,
        bank_id: str | None = None,
        limit: int = 10,
    ) -> str:
        """Search the knowledge graph for entities matching a name.

        Returns matching entities with their IDs. Use the IDs with
        memory_graph_neighbors to traverse connected memories.

        Args:
            query: Entity name or partial name to search for.
            bank_id: Bank whose graph to search. Uses default if omitted.
            limit: Maximum number of entities to return.
        """
        try:
            bid = _resolve_bank(bank_id)
            entities = await brain.graph_search(query, bid, limit=limit)
            return json.dumps(
                {
                    "entities": [
                        {
                            "id": e.id,
                            "name": e.name,
                            "entity_type": e.entity_type,
                            "aliases": e.aliases or [],
                        }
                        for e in entities
                    ]
                }
            )
        except Exception as exc:
            logger.exception("memory_graph_search failed")
            return json.dumps({"entities": [], "error": type(exc).__name__})

    # ── memory_graph_neighbors ─────────────────────────────────────

    @mcp.tool()
    async def memory_graph_neighbors(
        entity_ids: list[str],
        bank_id: str | None = None,
        max_depth: int = 2,
        limit: int = 20,
    ) -> str:
        """Traverse the knowledge graph from seed entity IDs.

        Walks the entity graph up to max_depth hops from each seed entity
        and returns memories attached to discovered entities, scored by
        proximity. Use memory_graph_search first to resolve entity IDs.

        Args:
            entity_ids: Seed entity IDs to start traversal from.
            bank_id: Bank whose graph to traverse. Uses default if omitted.
            max_depth: Maximum traversal depth (default 2).
            limit: Maximum number of memory hits to return.
        """
        try:
            bid = _resolve_bank(bank_id)
            hits = await brain.graph_neighbors(entity_ids, bid, max_depth=max_depth, limit=limit)
            return json.dumps(
                {
                    "hits": [
                        {
                            "memory_id": h.memory_id,
                            "text": h.text,
                            "connected_entities": h.connected_entities,
                            "depth": h.depth,
                            "score": round(h.score, 4),
                        }
                        for h in hits
                    ]
                }
            )
        except Exception as exc:
            logger.exception("memory_graph_neighbors failed")
            return json.dumps({"hits": [], "error": type(exc).__name__})

    # ── admin tools (expose_admin=true) ────────────────────────────

    if mcp_cfg.expose_admin:

        @mcp.tool()
        async def memory_lifecycle(
            bank_id: str | None = None,
        ) -> str:
            """Run TTL lifecycle sweep on a bank — archives and deletes expired memories.

            Args:
                bank_id: Bank to sweep. Uses default if omitted.
            """
            try:
                bid = _resolve_bank(bank_id)
                result = await brain.run_lifecycle(bid)
                return json.dumps(
                    {
                        "bank_id": bid,
                        "archived_count": result.archived_count,
                        "deleted_count": result.deleted_count,
                        "skipped_count": result.skipped_count,
                    }
                )
            except Exception as exc:
                logger.exception("memory_lifecycle failed")
                return json.dumps({"archived_count": 0, "deleted_count": 0, "error": type(exc).__name__})

        @mcp.tool()
        async def memory_bank_health(
            bank_id: str | None = None,
        ) -> str:
            """Get health score and issues for a memory bank.

            Args:
                bank_id: Bank to assess. Uses default if omitted. Pass "__all__"
                    to get health for every bank that has recorded operations.
            """
            try:
                if bank_id == "__all__":
                    results = await brain.all_bank_health()
                    return json.dumps(
                        {
                            "banks": [
                                {
                                    "bank_id": r.bank_id,
                                    "score": round(r.score, 3),
                                    "status": r.status,
                                    "issues": [
                                        {"severity": i.severity, "code": i.code, "message": i.message} for i in r.issues
                                    ],
                                }
                                for r in results
                            ]
                        }
                    )
                bid = _resolve_bank(bank_id)
                result = await brain.bank_health(bid)
                return json.dumps(
                    {
                        "bank_id": result.bank_id,
                        "score": round(result.score, 3),
                        "status": result.status,
                        "issues": [
                            {"severity": i.severity, "code": i.code, "message": i.message} for i in result.issues
                        ],
                        "metrics": result.metrics,
                    }
                )
            except Exception as exc:
                logger.exception("memory_bank_health failed")
                return json.dumps({"score": 0.0, "error": type(exc).__name__})

        @mcp.tool()
        async def memory_hold_set(
            hold_id: str,
            reason: str,
            bank_id: str | None = None,
            set_by: str = "agent:mcp",
        ) -> str:
            """Place a bank under legal hold — blocks memory_forget until released.

            Args:
                hold_id: Unique identifier for this hold (used to release it later).
                reason: Human-readable reason for the hold.
                bank_id: Bank to place on hold. Uses default if omitted.
                set_by: Actor label, default "agent:mcp".
            """
            try:
                bid = _resolve_bank(bank_id)
                hold = brain.set_legal_hold(bid, hold_id, reason, set_by=set_by)
                return json.dumps(
                    {
                        "hold_id": hold.hold_id,
                        "bank_id": hold.bank_id,
                        "reason": hold.reason,
                        "set_by": hold.set_by,
                        "set_at": hold.set_at.isoformat(),
                    }
                )
            except Exception as exc:
                logger.exception("memory_hold_set failed")
                return json.dumps({"error": type(exc).__name__})

        @mcp.tool()
        async def memory_hold_release(
            hold_id: str,
            bank_id: str | None = None,
        ) -> str:
            """Release a legal hold from a bank.

            Args:
                hold_id: The hold ID to release.
                bank_id: Bank to release the hold from. Uses default if omitted.
            """
            try:
                bid = _resolve_bank(bank_id)
                released = brain.release_legal_hold(bid, hold_id)
                return json.dumps({"bank_id": bid, "hold_id": hold_id, "released": released})
            except Exception as exc:
                logger.exception("memory_hold_release failed")
                return json.dumps({"released": False, "error": type(exc).__name__})

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

    # ── M21 — Mental-model CRUD ─────────────────────────────────────
    #
    # Hindsight parity: the 4 mental-model tools mirror Hindsight's
    # ``_register_create_mental_model`` / ``_update_mental_model`` /
    # ``_delete_mental_model`` MCP surface, with our addition of
    # ``memory_list_mental_models`` for inspection. Requires
    # :meth:`Astrocyte.set_mental_model_store` to have been called
    # (gateway hosts wire this from the configured Postgres store).

    @mcp.tool()
    async def memory_list_mental_models(
        bank_id: str | None = None,
        scope: str | None = None,
    ) -> str:
        """List mental models in a bank.

        Mental models are curated, refreshable summaries — e.g. "user
        prefers async updates", "Project X status: blocked on review".
        Use this to discover what authoritative summaries exist before
        composing an answer.

        Args:
            bank_id: Bank to list. Uses default if omitted.
            scope: Optional scope filter (e.g. ``"person:alice"``).
        """
        try:
            bid = _resolve_bank(bank_id)
            models = await brain.list_mental_models(
                bank_id=bid,
                scope=scope,
                context=_resolve_context(),
            )
            out = [
                {
                    "model_id": m.model_id,
                    "title": m.title,
                    "scope": m.scope,
                    "kind": m.kind,
                    "revision": m.revision,
                    "refreshed_at": m.refreshed_at.isoformat() if m.refreshed_at else None,
                    "source_count": len(m.source_ids),
                }
                for m in models
            ]
            return json.dumps({"models": out})
        except Exception as exc:
            logger.exception("memory_list_mental_models failed")
            return json.dumps({"models": [], "error": type(exc).__name__})

    @mcp.tool()
    async def memory_create_mental_model(
        model_id: str,
        title: str,
        content: str | None = None,
        sections: list[dict] | None = None,
        scope: str = "bank",
        bank_id: str | None = None,
    ) -> str:
        """Author a new mental model.

        Provide ONE of:

        - ``content`` — raw markdown string; the structured doc is
          parsed lazily on first refresh (legacy path).
        - ``sections`` — list of section dicts matching the
          ``StructuredDocument`` JSON shape; the structured doc is
          populated immediately so future ``memory_update_mental_model``
          calls can target sections by id (recommended path).

        Use ``memory_create_directive`` instead for hard-rule
        directives (those are mental models with ``kind="directive"``).
        """
        try:
            bid = _resolve_bank(bank_id)
            model = await brain.create_mental_model(
                model_id=model_id,
                title=title,
                content=content,
                sections=sections,
                scope=scope,
                bank_id=bid,
                context=_resolve_context(),
            )
            return json.dumps(
                {
                    "model_id": model.model_id,
                    "revision": model.revision,
                    "kind": model.kind,
                    "title": model.title,
                }
            )
        except Exception as exc:
            logger.exception("memory_create_mental_model failed")
            return json.dumps({"model_id": None, "error": type(exc).__name__})

    @mcp.tool()
    async def memory_update_mental_model(
        model_id: str,
        operations: list[dict],
        bank_id: str | None = None,
    ) -> str:
        """Apply structured delta operations to an existing mental model.

        Operations let you modify a document without re-emitting
        unchanged content — invaluable for refresh flows where the
        LLM would otherwise drift on sections it didn't intend to
        touch. Each operation is a dict with an ``op`` key; see
        :mod:`astrocyte.pipeline.delta_ops` for the full schema:

        - ``append_block`` / ``insert_block`` / ``replace_block`` /
          ``remove_block`` — block-level edits within a section.
        - ``add_section`` / ``remove_section`` / ``replace_section_blocks``
          / ``rename_section`` — section-level edits.

        Conservative-failure contract: invalid ops (unknown section
        ids, out-of-range indices, malformed payloads) drop with a
        logged reason in the response's ``skipped`` list; the document
        never gets worse than its input.

        Returns ``{"changed": bool, "revision": int, "applied": [...],
        "skipped": [...]}`` so the caller can audit which ops landed.
        """
        try:
            bid = _resolve_bank(bank_id)
            result = await brain.update_mental_model(
                model_id=model_id,
                operations=operations,
                bank_id=bid,
                context=_resolve_context(),
            )
            if result is None:
                return json.dumps({"changed": False, "error": "model not found"})
            model, summary = result
            return json.dumps(
                {
                    "changed": summary.get("changed", False),
                    "revision": model.revision,
                    "applied": summary.get("applied", []),
                    "skipped": summary.get("skipped", []),
                }
            )
        except Exception as exc:
            logger.exception("memory_update_mental_model failed")
            return json.dumps({"changed": False, "error": type(exc).__name__})

    @mcp.tool()
    async def memory_delete_mental_model(
        model_id: str,
        bank_id: str | None = None,
    ) -> str:
        """Soft-delete a mental model. Returns whether it existed."""
        try:
            bid = _resolve_bank(bank_id)
            ok = await brain.delete_mental_model(
                model_id,
                bank_id=bid,
                context=_resolve_context(),
            )
            return json.dumps({"deleted": ok})
        except Exception as exc:
            logger.exception("memory_delete_mental_model failed")
            return json.dumps({"deleted": False, "error": type(exc).__name__})

    @mcp.tool()
    async def memory_refresh_mental_model(
        model_id: str,
        new_source_ids: list[str],
        bank_id: str | None = None,
    ) -> str:
        """Re-derive a mental model from its sources after new memories have been retained.

        Hindsight parity for ``mental_model.refresh()``. When new
        memories that pertain to an existing model land via retain,
        call this to fold them into the model's ``source_ids`` and
        bump the revision. Use ``memory_update_mental_model`` for
        targeted edits via delta ops; use this when the model should
        be re-derived from an expanded provenance set.

        Returns ``{"model_id", "revision", "source_count"}`` on
        success or ``{"error": "model not found"}`` if the model
        doesn't exist.
        """
        try:
            bid = _resolve_bank(bank_id)
            model = await brain.refresh_mental_model(
                model_id,
                new_source_ids,
                bank_id=bid,
                context=_resolve_context(),
            )
            if model is None:
                return json.dumps({"model_id": model_id, "error": "model not found"})
            return json.dumps(
                {
                    "model_id": model.model_id,
                    "revision": model.revision,
                    "source_count": len(model.source_ids),
                }
            )
        except Exception as exc:
            logger.exception("memory_refresh_mental_model failed")
            return json.dumps({"model_id": model_id, "error": type(exc).__name__})

    @mcp.tool()
    async def memory_create_directive(
        rule_text: str,
        directive_id: str | None = None,
        scope: str = "bank",
        bank_id: str | None = None,
    ) -> str:
        """Author a user-curated hard-rule directive.

        Directives are mental models with ``kind="directive"``. They
        participate in the same ``search_mental_models`` retrieval as
        general / preference models, but the discriminator signals to
        the answerer that the rule should be applied as a preference
        override rather than as one input among many.

        Architecturally replaces the M18a-2 ``directive_compile``
        auto-extraction path that was deprecated in M19 (auto-compressed
        directives replicated a −30pp SSP regression because they
        overrode original preference nuance). User-authored directives
        avoid that failure mode by definition — the user gets exactly
        the rule they intended.
        """
        try:
            bid = _resolve_bank(bank_id)
            model = await brain.create_directive(
                rule_text=rule_text,
                directive_id=directive_id,
                scope=scope,
                bank_id=bid,
                context=_resolve_context(),
            )
            return json.dumps(
                {
                    "model_id": model.model_id,
                    "kind": model.kind,
                    "revision": model.revision,
                }
            )
        except Exception as exc:
            logger.exception("memory_create_directive failed")
            return json.dumps({"model_id": None, "error": type(exc).__name__})

    # ── M21 — Observation CRUD ─────────────────────────────────────

    @mcp.tool()
    async def memory_list_observations(
        bank_id: str | None = None,
        scope: str | None = None,
        trend: str | None = None,
        limit: int = 100,
    ) -> str:
        """List observations in a bank, optionally filtered by trend.

        Observations are auto-consolidated distillations of raw
        memories (e.g. "user prefers iced coffee in summer"). Each
        carries a computed ``trend`` — one of ``new`` /
        ``strengthening`` / ``stable`` / ``weakening`` / ``stale``
        derived from the evidence timestamps. Use ``trend`` to filter
        for fresh insights (``new``, ``strengthening``) or to audit
        stale patterns that may no longer apply.
        """
        try:
            bid = _resolve_bank(bank_id)
            rows = await brain.list_observations(
                bank_id=bid,
                scope=scope,
                trend=trend,
                limit=limit,
                context=_resolve_context(),
            )
            return json.dumps({"observations": rows})
        except Exception as exc:
            logger.exception("memory_list_observations failed")
            return json.dumps({"observations": [], "error": type(exc).__name__})

    @mcp.tool()
    async def memory_get_observation(
        observation_id: str,
        bank_id: str | None = None,
    ) -> str:
        """Fetch one observation by id. Returns null if not found."""
        try:
            bid = _resolve_bank(bank_id)
            row = await brain.get_observation(
                observation_id,
                bank_id=bid,
                context=_resolve_context(),
            )
            return json.dumps({"observation": row})
        except Exception as exc:
            logger.exception("memory_get_observation failed")
            return json.dumps({"observation": None, "error": type(exc).__name__})

    @mcp.tool()
    async def memory_create_observation(
        text: str,
        source_ids: list[str] | None = None,
        scope: str | None = None,
        confidence: float = 0.9,
        bank_id: str | None = None,
    ) -> str:
        """Hand-author an observation (bypasses the autonomous consolidator).

        Use this when an agent or user wants to assert a fact directly
        without going through retain → consolidate. The observation
        becomes immediately visible to the recall pipeline's
        ``observation`` strategy and to ``search_observations`` in
        the agentic reflect loop.
        """
        try:
            bid = _resolve_bank(bank_id)
            row = await brain.create_observation(
                text=text,
                source_ids=source_ids,
                scope=scope,
                confidence=confidence,
                bank_id=bid,
                context=_resolve_context(),
            )
            return json.dumps({"observation": row})
        except Exception as exc:
            logger.exception("memory_create_observation failed")
            return json.dumps({"observation": None, "error": type(exc).__name__})

    @mcp.tool()
    async def memory_delete_observation(
        observation_id: str,
        bank_id: str | None = None,
    ) -> str:
        """Delete one observation from the dedicated observations bank."""
        try:
            bid = _resolve_bank(bank_id)
            ok = await brain.delete_observation(
                observation_id,
                bank_id=bid,
                context=_resolve_context(),
            )
            return json.dumps({"deleted": ok})
        except Exception as exc:
            logger.exception("memory_delete_observation failed")
            return json.dumps({"deleted": False, "error": type(exc).__name__})

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
        description="Astrocyte MCP server — memory tools for AI agents",
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
