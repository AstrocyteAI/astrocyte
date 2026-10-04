"""claude_cli hands the prompt over as a file, so a stalled event loop cannot
starve the CLI of stdin.

`claude -p` waits only 3 s for stdin data, then fails with "no stdin data
received in 3s ... Input must be provided either through stdin or as a prompt
argument". The provider used to spawn the CLI and write the prompt into a pipe
afterwards; any event-loop stall in between lost that race. In benchmark logs
(2026-10-04) it happened about once per chunk, and every failure fed the
rate-limit circuit breaker that paused ingest for up to 30 minutes.

The fake CLI below gives up after 0.5 s without stdin data, as the real one
does after 3 s, and the test blocks the event loop for 1 s right after spawn.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from platform_compat import WINDOWS, write_executable

from astrocyte.providers import claude_cli
from astrocyte.providers.claude_cli import ClaudeCliProvider
from astrocyte.types import Message

pytestmark = pytest.mark.skipif(WINDOWS, reason="select() on stdin is POSIX-only")

IMPATIENT_CLI = (
    "import select, sys\n"
    "ready, _, _ = select.select([sys.stdin], [], [], 0.5)\n"
    "data = sys.stdin.read() if ready else ''\n"
    "if not data:\n"
    "    print('Error: Input must be provided either through stdin or as a prompt argument', file=sys.stderr)\n"
    "    sys.exit(1)\n"
    "print('GOT ' + str(len(data)))\n"
)


@pytest.fixture
def stalled_spawn(monkeypatch):
    """Block the event loop for 1 s right after each spawn, as a CPU-bound
    embedding or a long GC pause would."""
    real = asyncio.create_subprocess_exec

    async def spawn_then_stall(*args, **kwargs):
        proc = await real(*args, **kwargs)
        time.sleep(1.0)  # deliberately blocking: the loop cannot write to a pipe now
        return proc

    monkeypatch.setattr(claude_cli.asyncio, "create_subprocess_exec", spawn_then_stall)


async def test_prompt_survives_an_event_loop_stall_after_spawn(tmp_path, stalled_spawn):
    fake = write_executable(tmp_path, "claude", IMPATIENT_CLI)
    provider = ClaudeCliProvider(model="haiku", binary=str(fake), max_retries=1)
    prompt = "remember this " * 5_000  # larger than a pipe buffer (64 KiB)
    reply = await provider.complete([Message(role="user", content=prompt)])
    assert reply.text.startswith("GOT ")
    assert int(reply.text.split()[1]) >= len(prompt)


async def test_the_whole_prompt_arrives_without_a_stall(tmp_path):
    fake = write_executable(tmp_path, "claude", IMPATIENT_CLI)
    reply = await ClaudeCliProvider(model="haiku", binary=str(fake), max_retries=1).complete(
        [Message(role="user", content="hi")]
    )
    assert reply.text.startswith("GOT ")
