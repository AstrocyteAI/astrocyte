"""Smoke test for the Mem0-harness AstrocyteClient adapter.

Drives one LoCoMo conversation through the adapter:
- chunk the conversation's sessions into per-session ``add()`` calls
- pick a few questions from the conversation's qa list
- call ``search()`` for each, print the top-N candidates
- verify ``delete_user()`` clears state

Not a full bench — just a "does the adapter wire end-to-end" check
before forking the Mem0 LoCoMo runner.

Usage:
    cd astrocyte-py
    doppler run -- env DATABASE_URL='postgresql://astrocyte:astrocyte@127.0.0.1:5433/astrocyte_bench' \\
        ASTROCYTE_PG_DSN='postgresql://astrocyte:astrocyte@127.0.0.1:5433/astrocyte_bench' \\
        uv run python scripts/mem0_harness/smoke.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from scripts.mem0_harness.astrocyte_client import AstrocyteClient  # noqa: E402

LOCOMO_PATH = Path("datasets/locomo/data/locomo10.json")


def _session_timestamp(date_str: str | None) -> int | None:
    """Parse LoCoMo's '8:42 pm on 5 May, 2023' format to unix epoch."""
    if not date_str:
        return None
    for fmt in (
        "%I:%M %p on %d %B, %Y",
        "%I:%M %p on %d %b, %Y",
        "%H:%M on %d %B, %Y",
    ):
        try:
            dt = datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            continue
    return None


async def main() -> None:
    if not LOCOMO_PATH.exists():
        print(f"ERROR: {LOCOMO_PATH} not found. Run `make fetch-locomo` first.")
        sys.exit(1)

    data = json.loads(LOCOMO_PATH.read_text())
    if not data:
        print("ERROR: LoCoMo dataset is empty.")
        sys.exit(1)

    sample = data[0]
    conv = sample.get("conversation", sample)
    qa_list = sample.get("qa") or []
    user_id = "smoke_locomo_0"
    print(f"  [smoke] using conversation 0 (speakers: {conv.get('speaker_a')} + {conv.get('speaker_b')})")

    # Numeric session keys only — skip 'session_summary' etc.
    def _is_numeric_session(k: str) -> bool:
        if not (k.startswith("session_") and "date" not in k):
            return False
        suffix = k.split("_", 1)[1]
        return suffix.isdigit()

    session_keys = sorted(
        (k for k in conv if _is_numeric_session(k)),
        key=lambda k: int(k.split("_")[1]),
    )
    print(f"  [smoke] {len(session_keys)} sessions to ingest")

    async with AstrocyteClient(bank_prefix="m13.1.smoke") as client:
        # 1. delete_user from any prior smoke run.
        await client.delete_user(user_id)

        # 2. add() per session.
        ingest_start = time.monotonic()
        for sk in session_keys:
            date_str = conv.get(f"{sk}_date_time")
            ts = _session_timestamp(date_str)
            turns = conv.get(sk) or []
            messages = []
            for t in turns:
                speaker = t.get("speaker") or "user"
                role = "user" if speaker == conv.get("speaker_a") else "assistant"
                messages.append({"role": role, "content": (t.get("text") or "").strip()})
            if not messages:
                continue
            resp = await client.add(messages, user_id=user_id, timestamp=ts)
            assert resp is not None, f"add() returned None for session {sk}"
        print(
            f"  [smoke] add() done — {len(session_keys)} sessions buffered "
            f"({time.monotonic() - ingest_start:.1f}s wall)"
        )

        # 3. First search() triggers lazy ingest. Time it.
        if not qa_list:
            print("  [smoke] WARN: no QA in conversation; skipping search")
            return

        sample_questions = qa_list[:3]
        for i, qa in enumerate(sample_questions, start=1):
            question = qa.get("question") or ""
            expected = qa.get("answer", "?")
            search_start = time.monotonic()
            results = await client.search(question, user_id=user_id, top_k=20)
            elapsed = time.monotonic() - search_start
            print(f"\n  [smoke] Q{i} ({elapsed:.1f}s): {question[:80]}")
            print(f"  [smoke]   expected: {str(expected)[:80]}")
            print(f"  [smoke]   top-{min(3, len(results))} candidates:")
            for r in results[:3]:
                mem = (r.get("memory") or "")[:120]
                print(f"  [smoke]   - score={r.get('score', 0):.3f} | {mem}")
            assert isinstance(results, list), f"search() must return list, got {type(results)}"
            assert len(results) > 0, f"search() returned no candidates for Q{i}"

        # 4. delete_user() round-trip.
        ok = await client.delete_user(user_id)
        assert ok, "delete_user() returned False"
        print("\n  [smoke] delete_user() returned True")
        print("  [smoke] ✓ adapter smoke passed")


if __name__ == "__main__":
    asyncio.run(main())
