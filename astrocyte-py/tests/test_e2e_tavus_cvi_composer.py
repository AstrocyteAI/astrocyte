"""End-to-end test: composer briefing + Tavus CVI session creation.

Exercises the design in ``docs/_design/presentation-layer-and-multimodal-services.md``
§3.3: merge **system-of-record** snippets with **Astrocyte** ``recall`` hits into
``conversational_context``, then call the Tavus **Create Conversation** API.

Requires:

- ``TAVUS_API_KEY`` — from https://platform.tavus.io/ (Developer Portal).
- ``TAVUS_PERSONA_ID`` — persona that accepts ``test_mode`` conversations (see Tavus API docs).
- ``TAVUS_REPLICA_ID`` — replica id (required when the persona has no default replica).

Uses ``test_mode: true`` so the replica **does not** join the call (lower cost, no WebRTC
in CI). See `Tavus create conversation <https://docs.tavus.io/api-reference/conversations/create-conversation>`__.

**GitHub Actions:** add repository secrets ``TAVUS_API_KEY``, ``TAVUS_PERSONA_ID``,
``TAVUS_REPLICA_ID`` (or use ``vars`` for the two IDs if you prefer). Run the
**e2e Tavus CVI** workflow (``.github/workflows/e2e-tavus-cvi.yml``) via **Run workflow**
— it only executes this pytest file, not the full CI matrix.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request

import pytest

from astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig
from astrocyte.testing.in_memory import InMemoryEngineProvider

TAVUS_API_URL = "https://tavusapi.com/v2/conversations"

_has_api_key = bool(os.environ.get("TAVUS_API_KEY"))
_has_persona = bool(os.environ.get("TAVUS_PERSONA_ID"))
_has_replica = bool(os.environ.get("TAVUS_REPLICA_ID"))

_skip_reason = next(
    (
        r
        for r, ok in (
            ("TAVUS_API_KEY not set", _has_api_key),
            ("TAVUS_PERSONA_ID not set", _has_persona),
            ("TAVUS_REPLICA_ID not set", _has_replica),
        )
        if not ok
    ),
    "",
)

_skip = pytest.mark.skipif(
    bool(_skip_reason),
    reason=_skip_reason or "Tavus e2e disabled",
)


def _make_brain() -> Astrocyte:
    config = AstrocyteConfig()
    config.provider = "test"
    config.barriers.pii.mode = "disabled"
    brain = Astrocyte(config)
    brain.set_engine_provider(InMemoryEngineProvider())
    return brain


def _render_briefing(*, sor_lines: list[str], memory_hits: list[str]) -> str:
    """Application composer: deterministic merge (SoR + Astrocyte recall)."""
    mem_lines = [f"- {t}" for t in memory_hits] if memory_hits else ["- (none)"]
    parts = [
        "## System of record (fixture)",
        *[f"- {line}" for line in sor_lines],
        "",
        "## Learned memory (Astrocyte recall)",
        *mem_lines,
    ]
    return "\n".join(parts)


def _tavus_create_conversation(
    *,
    api_key: str,
    persona_id: str,
    replica_id: str,
    conversational_context: str,
) -> dict:
    """Synchronous HTTP POST (stdlib only)."""
    body = {
        "persona_id": persona_id,
        "replica_id": replica_id,
        "conversation_name": "astrocyte-ci-e2e-composer",
        "conversational_context": conversational_context,
        "test_mode": True,
    }
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        TAVUS_API_URL,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise AssertionError(f"Tavus API HTTP {e.code}: {detail}") from e
    return json.loads(raw)


@_skip
@pytest.mark.asyncio
async def test_e2e_composer_briefing_creates_tavus_conversation_test_mode() -> None:
    """Composer merges SoR fixture + Astrocyte recall; Tavus accepts the session (test_mode)."""
    api_key = os.environ["TAVUS_API_KEY"]
    persona_id = os.environ["TAVUS_PERSONA_ID"]
    replica_id = os.environ["TAVUS_REPLICA_ID"]

    bank_id = "e2e-tavus-composer"
    brain = _make_brain()
    await brain.retain(
        "CI user asked to remember: prefer concise answers in video calls.",
        bank_id=bank_id,
        metadata={"source": "tavus_e2e_ci", "kind": "preference"},
    )
    recall = await brain.recall(
        "What should we remember for this user in a call?",
        bank_id=bank_id,
        max_results=5,
    )
    memory_lines = [h.text for h in recall.hits]
    sor_fixture = ["account_tier=demo", "region=us-east-1"]
    briefing = _render_briefing(sor_lines=sor_fixture, memory_hits=memory_lines)
    assert "concise" in briefing.lower()

    data = await asyncio.to_thread(
        _tavus_create_conversation,
        api_key=api_key,
        persona_id=persona_id,
        replica_id=replica_id,
        conversational_context=briefing,
    )

    assert data.get("conversation_id"), f"unexpected response: {data!r}"
    assert data.get("conversation_url"), f"missing conversation_url: {data!r}"
    # test_mode ends immediately; status is typically "ended"
    assert data.get("status") in ("active", "ended", None)
