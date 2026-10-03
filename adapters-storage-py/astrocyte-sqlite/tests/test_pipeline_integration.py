"""SqliteStore under the real pipeline and the real MCP server.

The store tests prove storage semantics; these prove the user-visible
promise: configure ``vector_store: sqlite``, and memories retained by one agent
session are recalled by the next — including across separate MCP server
processes, which is how coding agents actually run it.
"""

from __future__ import annotations

import json
import subprocess
import sys

from astrocyte.config import load_config
from astrocyte.wiring import build_astrocyte


def write_config(tmp_path, db: str) -> str:
    path = tmp_path / "astrocyte.yaml"
    path.write_text(
        "vector_store: sqlite\n"
        "vector_store_config:\n"
        f"  path: {db}\n"
        "llm_provider: mock\n"
        "barriers:\n  pii:\n    mode: disabled\n"
    )
    return str(path)


async def test_keyword_leg_is_auto_wired(tmp_path):
    brain = build_astrocyte(load_config(write_config(tmp_path, str(tmp_path / "m.db"))))
    pipeline = brain._pipeline  # noqa: SLF001 — asserting wiring, not behaviour
    assert pipeline.document_store is pipeline.vector_store


async def test_memories_persist_across_brains(tmp_path):
    cfg = write_config(tmp_path, str(tmp_path / "m.db"))
    first = build_astrocyte(load_config(cfg))
    assert (await first.retain("The billing service is written in Go.", bank_id="proj")).stored

    second = build_astrocyte(load_config(cfg))  # a fresh process, in effect
    hits = (await second.recall("what language is billing written in?", bank_id="proj")).hits
    assert any("Go" in h.text for h in hits)


async def test_banks_are_isolated(tmp_path):
    brain = build_astrocyte(load_config(write_config(tmp_path, str(tmp_path / "m.db"))))
    await brain.retain("Project Alpha uses Kafka.", bank_id="alpha")
    hits = (await brain.recall("what does the project use?", bank_id="beta")).hits
    assert not any("Kafka" in h.text for h in hits)


def _mcp_call(config: str, tool: str, arguments: dict) -> dict:
    """One short-lived MCP server process handling one tool call."""
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": arguments}},
    ]
    proc = subprocess.Popen(
        [sys.executable, "-m", "astrocyte.mcp", "--config", config],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        for msg in msgs:
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()
        while True:
            line = proc.stdout.readline()
            assert line, f"server exited early: {proc.stderr.read()}"
            reply = json.loads(line)
            if reply.get("id") == 2:
                return json.loads(reply["result"]["content"][0]["text"])
    finally:
        proc.kill()
        proc.wait()


def test_memory_survives_an_mcp_server_restart(tmp_path):
    cfg = write_config(tmp_path, str(tmp_path / "m.db"))
    assert _mcp_call(cfg, "memory_retain", {"content": "Staging runs on Fly.io.", "bank_id": "proj"})["stored"]
    recalled = _mcp_call(cfg, "memory_recall", {"query": "where does staging run?", "bank_id": "proj"})
    assert "Fly.io" in json.dumps(recalled)
