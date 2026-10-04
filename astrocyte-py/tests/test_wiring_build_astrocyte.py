"""``build_astrocyte`` turns any config into a brain that actually stores.

Regression: ``astrocyte-mcp`` built a bare ``Astrocyte(config)``, which has no
pipeline, so every ``memory_retain`` it served returned ``stored: false`` and
every recall came back empty — under any config, with no error an agent could
see. Store wiring lived only in the gateway and AML adapter.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from platform_compat import system_env, system_path

from astrocyte.config import load_config
from astrocyte.errors import ConfigError
from astrocyte.wiring import build_astrocyte, build_pipeline, resolve_store

MINIMAL = "vector_store: in_memory\nllm_provider: mock\nbarriers:\n  pii:\n    mode: disabled\n"


class HybridStore:
    """A vector store that also answers keyword queries, like Postgres/SQLite."""

    async def search_fulltext(self, *args, **kwargs):
        return []


def write_config(tmp_path, body: str) -> str:
    path = tmp_path / "astrocyte.yaml"
    path.write_text(body)
    return str(path)


class TestBuildPipeline:
    def test_missing_vector_store_fails_loudly_with_next_step(self, tmp_path, monkeypatch):
        """No silent in-memory fallback: it would report success and forget
        everything on restart."""
        monkeypatch.delenv("ASTROCYTE_VECTOR_STORE", raising=False)
        cfg = load_config(write_config(tmp_path, "llm_provider: mock\n"))
        with pytest.raises(ConfigError, match="astrocyte setup"):
            build_pipeline(cfg)

    def test_vector_store_doubling_as_document_store_is_auto_wired(self, tmp_path):
        cfg = load_config(write_config(tmp_path, f"vector_store: {__name__}:HybridStore\nllm_provider: mock\n"))
        pipeline = build_pipeline(cfg)
        assert pipeline.document_store is pipeline.vector_store

    def test_env_var_names_the_store_when_config_omits_it(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ASTROCYTE_VECTOR_STORE", "in_memory")
        cfg = load_config(write_config(tmp_path, "llm_provider: mock\n"))
        assert resolve_store(cfg, "vector_store") is not None

    def test_unknown_store_kind_is_rejected(self, tmp_path):
        cfg = load_config(write_config(tmp_path, MINIMAL))
        with pytest.raises(ValueError, match="unknown store kind"):
            resolve_store(cfg, "vector_stor")


class TestBuildAstrocyte:
    async def test_retain_then_recall_round_trips(self, tmp_path):
        brain = build_astrocyte(load_config(write_config(tmp_path, MINIMAL)))
        stored = await brain.retain("We chose SQLite for the local store.", bank_id="proj")
        assert stored.stored, stored
        recalled = await brain.recall("which local store did we choose?", bank_id="proj")
        assert any("SQLite" in h.text for h in recalled.hits)

    def test_optional_stores_attach_only_when_configured(self, tmp_path):
        brain = build_astrocyte(load_config(write_config(tmp_path, MINIMAL)))
        assert brain._wiki_store is None  # noqa: SLF001 — asserting wiring, not behaviour
        assert brain._mental_model_store is None  # noqa: SLF001

    def test_every_configured_store_is_attached(self, tmp_path):
        """The gateway's wiring, now shared: a config naming optional stores
        gets them, plus the background wiki compiler when asked for."""
        brain = build_astrocyte(load_config(write_config(tmp_path, MINIMAL + (
            "wiki_store: in_memory\nmental_model_store: in_memory\nsource_store: in_memory\n"
            "wiki_compile:\n  enabled: true\n  auto_start: true\n  size_threshold: 3\n"
        ))))
        assert brain._wiki_store is not None  # noqa: SLF001 — asserting wiring, not behaviour
        assert brain._mental_model_store is not None  # noqa: SLF001
        assert brain._source_store is not None  # noqa: SLF001
        assert brain._compile_queue is not None  # noqa: SLF001

    async def test_configured_access_grants_are_enforced(self, tmp_path):
        from astrocyte.errors import AccessDenied
        from astrocyte.types import AstrocyteContext

        brain = build_astrocyte(load_config(write_config(tmp_path, MINIMAL + (
            "access_control:\n  enabled: true\n  default_policy: deny\n"
            "banks:\n  proj:\n    access:\n      - principal: agent:alice\n        permissions: [read, write]\n"
        ))))
        await brain.retain("deploys on Tuesdays", bank_id="proj", context=AstrocyteContext(principal="agent:alice"))
        with pytest.raises(AccessDenied):
            await brain.recall("deploys", bank_id="proj", context=AstrocyteContext(principal="agent:mallory"))

    def test_entity_resolution_without_a_graph_store_fails_loudly(self, tmp_path):
        cfg = load_config(write_config(tmp_path, MINIMAL + "entity_resolution:\n  enabled: true\n"))
        with pytest.raises(ConfigError, match="graph_store"):
            build_pipeline(cfg)


class TestFromConfig:
    """``Astrocyte.from_config`` is the documented entry point (homepage
    hello-world, quick-start, 20+ integration guides). It returned an unwired
    brain whose first retain raised "No provider or pipeline configured"."""

    async def test_the_documented_hello_world_runs(self, tmp_path):
        from astrocyte import Astrocyte

        # docs/src/components/LibraryQuickStart.mdx, verbatim apart from the path.
        brain = Astrocyte.from_config(write_config(tmp_path, MINIMAL))
        await brain.retain("Calvin prefers dark mode.", bank_id="user-123")
        hits = await brain.recall("What theme does Calvin prefer?", bank_id="user-123")
        assert any("dark mode" in h.text for h in hits.hits)

    def test_from_config_dict_is_wired_the_same_way(self):
        from astrocyte import Astrocyte

        brain = Astrocyte.from_config_dict({"vector_store": "in_memory", "llm_provider": "mock"})
        assert brain._pipeline is not None  # noqa: SLF001 — asserting wiring

    def test_config_without_a_vector_store_is_left_for_the_caller(self, tmp_path, monkeypatch):
        """Engine-tier callers attach their own provider; don't second-guess them."""
        from astrocyte import Astrocyte

        monkeypatch.delenv("ASTROCYTE_VECTOR_STORE", raising=False)
        brain = Astrocyte.from_config(write_config(tmp_path, "llm_provider: mock\n"))
        assert brain._pipeline is None  # noqa: SLF001


class TestMcpEntryPoint:
    """Drive the real ``astrocyte-mcp`` process over stdio, as an agent does."""

    @staticmethod
    def _session(config: str | None, env_home: str) -> list[dict]:
        args = [sys.executable, "-m", "astrocyte.mcp"] + (["--config", config] if config else [])
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "memory_retain",
                "arguments": {"content": "Deploys are frozen on Fridays.", "bank_id": "proj"}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "memory_recall",
                "arguments": {"query": "when are deploys frozen?", "bank_id": "proj"}}},
        ]
        proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={"PATH": os.pathsep.join(system_path()), **system_env(), "HOME": env_home, "XDG_CONFIG_HOME": f"{env_home}/.config"},
        )
        replies: dict[int, dict] = {}
        try:
            for msg in msgs:
                proc.stdin.write(json.dumps(msg) + "\n")
                proc.stdin.flush()
                if "id" not in msg:
                    continue
                while True:
                    line = proc.stdout.readline()
                    if not line:
                        raise AssertionError(f"server exited early: {proc.stderr.read()}")
                    reply = json.loads(line)
                    if reply.get("id") == msg["id"]:
                        replies[msg["id"]] = reply
                        break
        finally:
            proc.kill()
        return [replies[i] for i in (1, 2, 3)]

    @staticmethod
    def _payload(reply: dict) -> dict:
        return json.loads(reply["result"]["content"][0]["text"])

    def test_retain_and_recall_work_end_to_end(self, tmp_path):
        _, retained, recalled = self._session(write_config(tmp_path, MINIMAL), str(tmp_path))
        assert self._payload(retained)["stored"] is True
        assert "Fridays" in json.dumps(self._payload(recalled))

    def test_missing_config_exits_with_setup_guidance(self, tmp_path):
        proc = subprocess.run(
            [sys.executable, "-m", "astrocyte.mcp"],
            capture_output=True, text=True, timeout=60,
            env={"PATH": os.pathsep.join(system_path()), **system_env(), "HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "cfg")},
        )
        assert proc.returncode == 2
        assert "astrocyte setup" in proc.stderr
        assert str(tmp_path / "cfg" / "astrocyte" / "astrocyte.yaml") in proc.stderr

    def test_default_config_location_is_used_when_flag_omitted(self, tmp_path):
        cfg_dir = tmp_path / ".config" / "astrocyte"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "astrocyte.yaml").write_text(MINIMAL)
        _, retained, _ = self._session(None, str(tmp_path))
        assert self._payload(retained)["stored"] is True
