"""``astrocyte team`` against the real gateway app (team-memory.md §8, C1).

Two teammates, each with their own local store and state directory, share a
project bank through the gateway in token mode with access control on: what
Alice saves reaches Bob attributed to her by the gateway, a team forget
erases it on Bob's machine, and a read-only token can pull but not push.
"""

from __future__ import annotations

import asyncio
import textwrap
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from astrocyte._astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig
from astrocyte.harness import team
from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.testing.in_memory import InMemoryVectorStore, MockLLMProvider
from astrocyte.types import VectorItem

from astrocyte_gateway import tokens as tk

BANK = "project:api-1a2b3c"
URL = "http://gateway.test"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ASTROCYTE_CONFIG_PATH", "ASTROCYTE_TOKENS_FILE", "ASTROCYTE_RATE_LIMIT_PER_SECOND"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ASTROCYTE_CHANGES_SETTLE_SECONDS", "0")


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    cfg = tmp_path / "astrocyte.yaml"
    cfg.write_text(textwrap.dedent("""
        provider_tier: storage
        vector_store: in_memory
        llm_provider: mock
        barriers:
          pii:
            mode: disabled
        access_control:
          enabled: true
          default_policy: deny
        """), encoding="utf-8")
    registry = tmp_path / "tokens.yaml"
    registry.write_text("tokens: []\n", encoding="utf-8")
    monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "token")
    monkeypatch.setenv("ASTROCYTE_TOKENS_FILE", str(registry))
    monkeypatch.setenv("ASTROCYTE_CONFIG_PATH", str(cfg))
    from astrocyte_gateway.app import create_app
    from astrocyte_gateway.brain import build_astrocyte

    app = create_app(build_astrocyte())
    monkeypatch.setattr(team, "_keyring", lambda: None)
    monkeypatch.setattr(team, "_client", lambda url, token: httpx.AsyncClient(
        base_url=url, headers={"Authorization": f"Bearer {token}"}, transport=httpx.ASGITransport(app=app)))

    def token(principal: str, permissions=("read", "write", "forget")) -> str:
        return tk.create_token(registry, principal=principal, banks=["project:*"], permissions=list(permissions))[0]

    return Namespace(app=app, token=token)


class Teammate:
    def __init__(self, root: Path, name: str, monkeypatch: pytest.MonkeyPatch) -> None:
        self.cfg = root / name / "astrocyte.yaml"
        self.cfg.parent.mkdir(parents=True)
        self.state, self.monkeypatch = root / name / "state", monkeypatch
        config = AstrocyteConfig()
        config.provider_tier = "storage"
        config.barriers.pii.mode = "disabled"
        self.brain = Astrocyte(config)
        self.pipeline = PipelineOrchestrator(vector_store=InMemoryVectorStore(), llm_provider=MockLLMProvider())
        self.brain.set_pipeline(self.pipeline)

    async def save(self, mid: str, text: str) -> None:
        await self.pipeline.vector_store.store_vectors([VectorItem(
            id=mid, bank_id=BANK, vector=[0.1] * 8, text=text, retained_at=datetime.now(UTC), memory_layer="fact")])

    async def items(self) -> dict[str, VectorItem]:
        return {i.id: i for i in await self.pipeline.vector_store.list_vectors(BANK, limit=100)}

    async def team(self, command: str, **kw) -> int:
        self.monkeypatch.setenv("XDG_STATE_HOME", str(self.state))
        args = Namespace(bank=BANK, project=None, config=str(self.cfg), team_command=command, **kw)
        try:
            return await team._COMMANDS[command](args, self.cfg, self.pipeline, self.brain)
        except team.TeamError as e:
            print(f"astrocyte team: {e}")
            return 1

    async def join(self, token: str) -> int:
        return await self.team("join", url=URL, token=token, share_captured=False, yes=True)


def test_a_decision_alice_saves_reaches_bob_attributed_to_her(gateway, tmp_path, monkeypatch):
    async def body() -> None:
        alice, bob = Teammate(tmp_path, "alice", monkeypatch), Teammate(tmp_path, "bob", monkeypatch)
        await alice.save("a1a1a1a1a1a1a1a1", "Deploys go out on Tuesdays.")
        assert await alice.join(gateway.token("user:alice")) == 0
        assert await bob.join(gateway.token("user:bob")) == 0
        got = (await bob.items())["a1a1a1a1a1a1a1a1"]
        assert got.text == "Deploys go out on Tuesdays." and got.metadata["_actor"] == "user:alice"

    asyncio.run(body())


def test_a_team_forget_erases_it_on_bobs_machine(gateway, tmp_path, monkeypatch):
    async def body() -> None:
        alice, bob = Teammate(tmp_path, "alice", monkeypatch), Teammate(tmp_path, "bob", monkeypatch)
        alice_token = gateway.token("user:alice")
        await alice.save("a1a1a1a1a1a1a1a1", "The old key rotation runbook.")
        await alice.join(alice_token)
        await bob.join(gateway.token("user:bob"))
        async with httpx.AsyncClient(base_url=URL, transport=httpx.ASGITransport(app=gateway.app),
                                     headers={"Authorization": f"Bearer {alice_token}"}) as http:
            reply = await http.post("/v1/forget", json={"bank_id": BANK, "memory_ids": ["a1a1a1a1a1a1a1a1"]})
            assert reply.status_code == 200, reply.text
        assert await bob.team("sync", dry_run=False) == 0
        assert "a1a1a1a1a1a1a1a1" not in await bob.items()

    asyncio.run(body())


def test_a_read_only_token_pulls_but_cannot_push(gateway, tmp_path, monkeypatch, capsys):
    async def body() -> None:
        alice, ci = Teammate(tmp_path, "alice", monkeypatch), Teammate(tmp_path, "ci", monkeypatch)
        await alice.save("a1a1a1a1a1a1a1a1", "Deploys go out on Tuesdays.")
        await alice.join(gateway.token("user:alice"))
        reader = gateway.token("service:ci", permissions=("read",))
        assert await ci.join(reader) == 0, "nothing of its own to push: joining only pulls"
        assert "a1a1a1a1a1a1a1a1" in await ci.items()
        await ci.save("c1c1c1c1c1c1c1c1", "A note from CI.")
        assert await ci.team("sync", dry_run=False) == 1
        assert "403" in capsys.readouterr().out

    asyncio.run(body())
