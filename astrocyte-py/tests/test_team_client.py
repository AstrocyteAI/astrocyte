"""``astrocyte team`` (team-memory.md §7, C1): push, pull, forget, leave.

Two local users (each an in-memory store and their own state directory)
share a bank through a fake gateway: an httpx transport that serves the
gateway's two sync routes from a real ``Astrocyte`` (``push_records`` and
``list_changes``), so the server's semantics are the real ones and only HTTP
is simulated. The real gateway app is exercised in the gateway's own suite.
"""

from __future__ import annotations

import json
import os
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
from astrocyte.types import AstrocyteContext, SyncPushRecord, VectorItem

BANK = "project:api-1a2b3c"
URL = "https://memory.example.com"
TOKENS = {"tok-alice": "user:alice", "tok-bob": "user:bob"}


def _brain() -> tuple[Astrocyte, PipelineOrchestrator]:
    config = AstrocyteConfig()
    config.provider_tier = "storage"
    config.barriers.pii.mode = "disabled"
    brain = Astrocyte(config)
    pipeline = PipelineOrchestrator(vector_store=InMemoryVectorStore(), llm_provider=MockLLMProvider())
    brain.set_pipeline(pipeline)
    return brain, pipeline


class Gateway:
    """The gateway's sync routes over a real Astrocyte; counts requests."""

    def __init__(self) -> None:
        self.brain, self.pipeline = _brain()
        self.requests: list[str] = []
        self.status: int | None = None  # force every answer to this status
        self.posts = 0
        self.erased = 0  # memories /v1/forget was asked to erase, and did
        self.fail_post: int | None = None  # answer this push (1-based) with a 500

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(f"{request.method} {request.url.path}")
        if self.status:
            return httpx.Response(self.status, json={"detail": "forced"})
        if request.method == "POST":
            self.posts += 1
            if self.posts == self.fail_post:
                return httpx.Response(500, json={"detail": "boom"})
        principal = TOKENS.get(request.headers.get("authorization", "").removeprefix("Bearer "))
        if principal is None:
            return httpx.Response(401, json={"detail": "invalid token"})
        ctx = AstrocyteContext(principal=principal)
        if request.url.path == "/v1/forget":
            body = json.loads(request.content)
            result = await self.brain.forget(body["bank_id"], memory_ids=body["memory_ids"], context=ctx)
            self.erased += body.get("erase") and await self.brain.erase(body["bank_id"], body["memory_ids"],
                                                                       context=ctx) or 0
            return httpx.Response(200, json={"deleted_count": result.deleted_count})
        bank = request.url.path.split("/")[3].replace("%3A", ":")
        if request.url.path.endswith("/changes"):
            page = await self.brain.list_changes(bank, cursor=request.url.params.get("cursor"),
                                                 limit=int(request.url.params.get("limit", 100)),
                                                 settle_seconds=0, context=ctx)
            return httpx.Response(200, json={"changes": [_change(c) for c in page.changes],
                                             "next_cursor": page.next_cursor, "has_more": page.has_more})
        body = json.loads(request.content)
        records = [SyncPushRecord(id=r["id"], text=r["text"], tags=r.get("tags"), fact_type=r.get("fact_type"),
                                  metadata=r.get("metadata"), content_hash=r.get("content_hash"),
                                  occurred_at=datetime.fromisoformat(r["occurred_at"]) if r.get("occurred_at")
                                  else None)
                   for r in body["records"]]
        results = await self.brain.push_records(bank, records, context=ctx)
        return httpx.Response(200, json={"results": [
            {k: v for k, v in {"id": r.id, "status": r.status, "duplicate_of": r.duplicate_of,
                               "reason": r.reason}.items() if v is not None} for r in results]})

    async def texts(self) -> set[str]:
        return {i.text for i in await self.pipeline.vector_store.list_vectors(BANK, limit=1000)}


def _change(c) -> dict:
    if c.deleted:
        return {"id": c.id, "deleted": True, "changed_at": c.changed_at.isoformat()}
    return {"id": c.id, "deleted": False, "changed_at": c.changed_at.isoformat(), "text": c.text,
            "occurred_at": c.occurred_at.isoformat() if c.occurred_at else None,
            "retained_at": c.retained_at.isoformat() if c.retained_at else None, "tags": c.tags,
            "fact_type": c.fact_type, "memory_layer": c.memory_layer, "metadata": c.metadata}


class User:
    """One teammate's machine: a local store, config dir and state dir."""

    def __init__(self, root: Path, name: str, monkeypatch) -> None:
        self.name, self.monkeypatch = name, monkeypatch
        self.cfg = root / name / "config" / "astrocyte.yaml"
        self.cfg.parent.mkdir(parents=True)
        self.state = root / name / "state"
        self.brain, self.pipeline = _brain()

    def __enter__(self) -> User:
        self.monkeypatch.setenv("XDG_STATE_HOME", str(self.state))
        return self

    def __exit__(self, *exc) -> None:
        pass

    async def save(self, mid: str, text: str, *, tags: list[str] | None = None, **meta) -> None:
        [vector] = await self.pipeline.llm_provider.embed([text])
        await self.pipeline.vector_store.store_vectors([VectorItem(
            id=mid, bank_id=BANK, vector=vector, text=text, tags=tags, metadata=meta or None,
            retained_at=datetime.now(UTC), memory_layer="fact")])

    async def items(self) -> dict[str, VectorItem]:
        return {i.id: i for i in await self.pipeline.vector_store.list_vectors(BANK, limit=1000)}

    async def run(self, command: str, **kw) -> int:
        args = Namespace(bank=BANK, project=None, config=str(self.cfg), team_command=command, **kw)
        with self:
            try:
                return await team._COMMANDS[command](args, self.cfg, self.pipeline, self.brain)
            except team.TeamError as e:
                print(f"astrocyte team: {e}")
                return 1

    async def join(self, token: str, **kw) -> int:
        kw = {"url": URL, "token": token, "share_captured": False, "yes": True, **kw}
        return await self.run("join", **kw)

    async def sync(self, dry_run: bool = False) -> int:
        return await self.run("sync", dry_run=dry_run)


@pytest.fixture
def gateway(monkeypatch) -> Gateway:
    gw = Gateway()
    monkeypatch.setattr(team, "_keyring", lambda: None)
    monkeypatch.setattr(team, "_client", lambda url, token: httpx.AsyncClient(
        base_url=url, headers={"Authorization": f"Bearer {token}"}, transport=httpx.MockTransport(gw)))
    return gw


@pytest.fixture
def alice(tmp_path, monkeypatch) -> User:
    return User(tmp_path, "alice", monkeypatch)


@pytest.fixture
def bob(tmp_path, monkeypatch) -> User:
    return User(tmp_path, "bob", monkeypatch)


A1, A2, A3, A4 = "a1a1a1a1a1a1a1a1", "a2a2a2a2a2a2a2a2", "a3a3a3a3a3a3a3a3", "a4a4a4a4a4a4a4a4"
B1 = "b1b1b1b1b1b1b1b1"


class TestJoin:
    async def test_shares_saved_and_imported_memories_only(self, gateway, alice, capsys):
        await alice.save(A1, "Deploys go out on Tuesdays.")
        await alice.save(A2, "## Style\nUse ruff.", import_hash="abc")
        await alice.save(A3, "**user**: hmm, maybe kafka?", tags=["captured"])
        await alice.save(A4, "My home wifi password hint", tags=["private"])
        assert await alice.join("tok-alice") == 0
        out = capsys.readouterr().out
        assert "This shares 2 memories" in out and "captured conversation stays here" in out
        assert await gateway.texts() == {"Deploys go out on Tuesdays.", "## Style\nUse ruff."}

    async def test_captured_turns_go_only_when_the_project_opts_in(self, gateway, alice):
        await alice.save(A3, "**user**: we picked Kafka", tags=["captured"])
        await alice.join("tok-alice", share_captured=True)
        assert await gateway.texts() == {"**user**: we picked Kafka"}

    async def test_asks_before_the_first_push_and_stops_without_a_yes(self, gateway, alice, monkeypatch, capsys):
        await alice.save(A1, "Deploys go out on Tuesdays.")
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        assert await alice.join("tok-alice", yes=False) == 1
        assert "--yes" in capsys.readouterr().err
        assert await gateway.texts() == set() and not team.team_file(alice.cfg).exists()

    async def test_a_no_at_the_prompt_leaves_nothing_behind(self, gateway, alice, monkeypatch):
        await alice.save(A1, "Deploys go out on Tuesdays.")
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _: "n")
        assert await alice.join("tok-alice", yes=False) == 1
        assert await gateway.texts() == set() and not team.team_file(alice.cfg).exists()

    async def test_a_refused_token_saves_nothing(self, gateway, alice, capsys):
        assert await alice.join("tok-mallory") == 1
        assert "refused the token" in capsys.readouterr().out
        assert not team.team_file(alice.cfg).exists()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
    async def test_without_a_keychain_the_token_is_kept_readable_only_by_the_user(self, gateway, alice):
        await alice.join("tok-alice")
        path = team.team_file(alice.cfg)
        assert path.stat().st_mode & 0o777 == 0o600
        assert json.loads(path.read_text())["projects"][BANK]["token"] == "tok-alice"

    async def test_the_keychain_is_used_when_there_is_one(self, gateway, alice, monkeypatch):
        kept: dict = {}

        class Keyring:
            def set_password(self, service, account, token):
                kept[(service, account)] = token

            def get_password(self, service, account):
                return kept.get((service, account))

            def delete_password(self, service, account):
                del kept[(service, account)]

        monkeypatch.setattr(team, "_keyring", lambda: Keyring())
        await alice.join("tok-alice")
        assert kept == {("astrocyte-team", f"{BANK}@{URL}"): "tok-alice"}
        assert "token" not in json.loads(team.team_file(alice.cfg).read_text())["projects"][BANK]
        assert await alice.sync() == 0, "the token is read back from the keychain"
        # Another project on the same gateway, with its own token: leaving it keeps this one's.
        other = "project:web-9f9f9f"
        with alice:
            projects = team.load_memberships(alice.cfg)
            projects[other] = {"url": URL}
            team.save_memberships(alice.cfg, projects)
            team.store_token(other, URL, "tok-other", projects[other])
        args = Namespace(bank=other, project=None, config=str(alice.cfg), team_command="leave", keep_mirror=False)
        with alice:
            await team._leave(args, alice.cfg, alice.pipeline, alice.brain)
        assert kept == {("astrocyte-team", f"{BANK}@{URL}"): "tok-alice"}

    async def test_a_keychain_that_keeps_nothing_falls_back_to_the_file(self, gateway, alice, monkeypatch):
        class NullKeyring:  # accepts every password, returns none (keyring's null backend)
            def set_password(self, *a):
                pass

            def get_password(self, *a):
                return None

        monkeypatch.setattr(team, "_keyring", lambda: NullKeyring())
        await alice.join("tok-alice")
        assert json.loads(team.team_file(alice.cfg).read_text())["projects"][BANK]["token"] == "tok-alice"
        assert await alice.sync() == 0

    async def test_a_gateway_without_a_change_feed_is_explained(self, gateway, alice, capsys):
        gateway.status = 501
        assert await alice.join("tok-alice") == 1
        assert "doesn't support team sync" in capsys.readouterr().out


class TestSync:
    async def test_teammates_memories_arrive_under_the_same_id_attributed(self, gateway, alice, bob):
        await alice.save(A1, "Deploys go out on Tuesdays.")
        await alice.join("tok-alice")
        await bob.save(B1, "Staging is at staging.example.com.")
        await bob.join("tok-bob")
        bobs = await bob.items()
        assert bobs[A1].text == "Deploys go out on Tuesdays."
        assert bobs[A1].metadata["_actor"] == "user:alice", "who saved it travels with it"
        await alice.sync()
        assert (await alice.items())[B1].metadata["_actor"] == "user:bob"

    async def test_pulled_memories_are_never_pushed_back(self, gateway, alice, bob):
        await alice.save(A1, "Deploys go out on Tuesdays.")
        await alice.join("tok-alice")
        await bob.join("tok-bob")
        gateway.requests.clear()
        await bob.sync()
        assert not any(r.startswith("POST") for r in gateway.requests), "nothing new of Bob's to send"

    async def test_a_second_sync_sends_and_receives_only_whats_new(self, gateway, alice, bob, capsys):
        await alice.save(A1, "Deploys go out on Tuesdays.")
        await alice.join("tok-alice")
        await bob.join("tok-bob")
        await alice.save(A2, "Feature flags live in LaunchDarkly.")
        await alice.sync()
        capsys.readouterr()
        await bob.sync()
        assert "pushed 0, pulled 1" in capsys.readouterr().out
        assert set(await bob.items()) == {A1, A2}

    async def test_dry_run_sends_nothing_and_moves_no_cursor(self, gateway, alice, bob, capsys):
        await alice.save(A1, "Deploys go out on Tuesdays.")
        await alice.join("tok-alice")
        await bob.save(B1, "Staging is at staging.example.com.")
        with bob:
            team.save_memberships(bob.cfg, {BANK: {"url": URL, "token": "tok-bob"}})
        assert await bob.sync(dry_run=True) == 0
        out = capsys.readouterr().out
        assert "Would push 1 memories" in out and "Would pull 1 changes" in out
        assert "Staging is at staging.example.com." not in await gateway.texts()
        assert set(await bob.items()) == {B1}
        with bob:
            assert team.SyncState.load(BANK).cursor is None

    async def test_a_memory_forgotten_for_the_team_is_erased_everywhere(self, gateway, alice, bob):
        await alice.save(A1, "The old API key rotation process.")
        await alice.join("tok-alice")
        await bob.join("tok-bob")
        assert A1 in await bob.items()
        await gateway.brain.forget(BANK, memory_ids=[A1], context=AstrocyteContext(principal="user:alice"))
        await bob.sync()
        await alice.sync()
        assert A1 not in await bob.items() and A1 not in await alice.items(), "erased for everyone"
        await alice.sync()
        assert await gateway.texts() == set(), "and never pushed again"

    async def test_one_failed_batch_does_not_resend_the_ones_before_it(self, gateway, alice):
        import hashlib

        n_items = team.PUSH_BATCH + 5
        for n in range(n_items):
            words = " ".join(hashlib.sha256(f"{n}-{k}".encode()).hexdigest()[:9] for k in range(6))
            await alice.save(f"{n:016x}", words)
        gateway.fail_post = 2
        assert await alice.join("tok-alice") == 1, "the second batch failed"
        with alice:
            first = team.SyncState.load(BANK).pushed
        assert len(first) == team.PUSH_BATCH, "the acknowledged batch is recorded"
        await alice.sync()
        with alice:
            assert len(team.SyncState.load(BANK).pushed) == n_items
        assert gateway.posts == 3, "one more push: only the 5 left over"
        assert len(await gateway.texts()) == n_items

    async def test_without_joining_it_says_how(self, gateway, alice, capsys):
        assert await alice.sync() == 1
        assert "astrocyte team join" in capsys.readouterr().out


class TestStatusAndLeave:
    async def test_status_reports_what_is_waiting(self, gateway, alice, capsys):
        await alice.save(A1, "Deploys go out on Tuesdays.")
        await alice.join("tok-alice")
        await alice.save(A2, "Feature flags live in LaunchDarkly.")
        capsys.readouterr()
        assert await alice.run("status") == 0
        out = capsys.readouterr().out
        assert "waiting to go  1" in out and "pushed         1 (1 stored)" in out and "token accepted" in out

    async def test_leave_erases_teammates_memories_and_keeps_your_own(self, gateway, alice, bob):
        await alice.save(A1, "Deploys go out on Tuesdays.")
        await alice.join("tok-alice")
        await bob.save(B1, "Staging is at staging.example.com.")
        await bob.join("tok-bob")
        assert await bob.run("leave", keep_mirror=False) == 0
        assert set(await bob.items()) == {B1}
        assert not team.team_file(bob.cfg).exists()
        with bob:
            assert not team.SyncState.path(BANK).exists()

    async def test_leave_keep_mirror_keeps_them(self, gateway, alice, bob):
        await alice.save(A1, "Deploys go out on Tuesdays.")
        await alice.join("tok-alice")
        await bob.join("tok-bob")
        await bob.run("leave", keep_mirror=True)
        assert A1 in await bob.items()


# ── C2: background sync, attribution, forget scopes ──────────────────────


MINIMAL = "vector_store: in_memory\nllm_provider: mock\nbarriers:\n  pii:\n    mode: disabled\n"


def _forget_args(user: User, ids: list[str], **kw) -> Namespace:
    return Namespace(bank=BANK, project=None, config=str(user.cfg), ids=ids, all=kw.pop("all", False),
                     yes=kw.pop("yes", False), team=kw.pop("team", False), local=kw.pop("local", False), json=False)


async def _forget(user: User, ids: list[str], **kw) -> int:
    from astrocyte.harness import memories

    with user:
        return await memories._forget(_forget_args(user, ids, **kw), user.pipeline, user.brain)


class TestForgetScopes:
    async def test_a_shared_memory_needs_a_scope(self, gateway, alice, bob, capsys):
        await alice.save(A1, "The old key rotation runbook.")
        await alice.join("tok-alice")
        await bob.join("tok-bob")
        capsys.readouterr()
        assert await _forget(bob, [A1[:6]]) == 1
        err = capsys.readouterr().err
        assert "(saved by alice)" in err and "--team" in err and "--local" in err
        assert A1 in await bob.items(), "nothing erased without a scope"

    async def test_team_erases_it_for_everyone(self, gateway, alice, bob):
        await alice.save(A1, "The old key rotation runbook.")
        await alice.join("tok-alice")
        await bob.join("tok-bob")
        assert await _forget(bob, [A1], team=True) == 0
        assert A1 not in await bob.items() and await gateway.texts() == set()
        assert gateway.erased == 1, "erased from the gateway's storage, not only forgotten"
        await alice.sync()
        assert A1 not in await alice.items(), "the author's copy goes too"

    async def test_local_erases_it_here_and_a_replayed_feed_does_not_bring_it_back(self, gateway, alice, bob):
        await alice.save(A1, "Deploys go out on Tuesdays.")
        await alice.join("tok-alice")
        await bob.join("tok-bob")
        assert await _forget(bob, [A1], local=True) == 0
        assert A1 not in await bob.items() and "Deploys go out on Tuesdays." in await gateway.texts()
        with bob:
            state = team.SyncState.load(BANK)
            state.cursor = None  # the whole feed again, as after a lost cursor
            state.save(BANK)
        await bob.sync()
        assert A1 not in await bob.items()

    async def test_a_memory_never_shared_is_forgotten_as_before(self, gateway, alice, capsys):
        await alice.join("tok-alice")
        await alice.save(A2, "A note not synced yet.")
        assert await _forget(alice, [A2]) == 0
        assert A2 not in await alice.items()

    async def test_forget_all_on_a_shared_project_erases_this_machine_only(self, gateway, alice, bob, capsys):
        await alice.save(A1, "Deploys go out on Tuesdays.")
        await alice.join("tok-alice")
        await bob.save(B1, "Staging is at staging.example.com.")
        await bob.join("tok-bob")
        assert await _forget(bob, [], all=True, yes=True) == 0
        assert await bob.items() == {}
        assert "stay on the gateway" in capsys.readouterr().out
        assert await gateway.texts() == {"Deploys go out on Tuesdays.", "Staging is at staging.example.com."}
        await bob.sync()
        assert await bob.items() == {}, "and they aren't pulled back"


class TestDaemon:
    async def _daemon(self, user: User, *, sqlite: bool = False):
        """The session summary needs a store that lists by recency (SQLite, the local default)."""
        from astrocyte.harness.agentd import AgentDaemon

        if sqlite:
            pytest.importorskip("astrocyte_sqlite")
            user.cfg.write_text(MINIMAL.replace("vector_store: in_memory", "vector_store: sqlite")
                                + f"vector_store_config:\n  path: {user.cfg.parent / 'm.db'}\n")
        else:
            user.cfg.write_text(MINIMAL)
        with user:
            daemon = AgentDaemon(user.cfg)
        user.pipeline, user.brain = daemon.pipeline, daemon.brain  # the daemon's store is the user's store
        return daemon

    async def test_syncs_joined_projects_in_the_background(self, gateway, alice, bob):
        await alice.save(A1, "Deploys go out on Tuesdays.")
        await alice.join("tok-alice")
        daemon = await self._daemon(bob)
        await bob.join("tok-bob")
        await alice.save(A2, "Feature flags live in LaunchDarkly.")
        await alice.sync()
        with bob:
            await daemon.team_sync()
        assert {A1, A2} <= set(await bob.items())

    async def test_skips_a_project_another_process_is_syncing(self, gateway, alice, caplog):
        daemon = await self._daemon(alice)
        await alice.join("tok-alice")
        await alice.save(A1, "Deploys go out on Tuesdays.")
        with alice, team._BankLock(BANK):
            await daemon.team_sync()
        assert await gateway.texts() == set() and "another sync is running" in caplog.text

    async def test_a_failure_is_kept_for_status(self, gateway, alice, capsys):
        daemon = await self._daemon(alice)
        await alice.join("tok-alice")
        gateway.status = 503
        with alice:
            await daemon.team_sync()
        gateway.status = None
        capsys.readouterr()
        await alice.run("status")
        assert "last error     the gateway answered 503" in capsys.readouterr().out

    async def test_session_start_and_recall_name_the_teammate(self, gateway, alice, bob):
        await alice.save(A1, "Deploys go out on Tuesdays.")
        await alice.join("tok-alice")
        daemon = await self._daemon(bob, sqlite=True)
        # Bob's own memory saved through the MCP server carries its principal: no label.
        await bob.save(B1, "Staging is at staging.example.com.", _actor="agent:mcp")
        await bob.join("tok-bob")
        with bob:
            boot = (await daemon.op_boot({"bank": BANK, "session_id": "s1"}))["context"]
            recall = (await daemon.op_recall({"bank": BANK, "session_id": "s2",
                                              "prompt": "Deploys go out on Tuesdays."}))["context"]
        assert "(alice) Deploys go out on Tuesdays." in boot
        assert "] Staging is at staging.example.com." in boot, "your own memories carry no label"
        assert "(alice) Deploys go out on Tuesdays." in recall

    async def test_where_you_left_off_is_your_own_session(self, gateway, alice, bob):
        daemon = await self._daemon(bob, sqlite=True)
        await bob.save(B1, "**user**: what's next?\n\n**assistant**: Bob's plan.", tags=["captured"],
                       session_id="bob-1")
        await bob.join("tok-bob")
        await alice.save(A1, "**user**: status?\n\n**assistant**: Alice's newer turn.", tags=["captured"],
                         session_id="alice-9")
        await alice.join("tok-alice", share_captured=True)
        await bob.sync()
        assert A1 in await bob.items()
        with bob:
            boot = (await daemon.op_boot({"bank": BANK, "session_id": "bob-2"}))["context"]
        left_off = boot.split("Most recent memories")[0]
        assert "Bob's plan" in left_off and "Alice's newer turn" not in left_off


def test_who_saved():
    from astrocyte.harness.agentd import who_saved

    assert who_saved({"_actor": "user:alice"}) == "alice"
    assert who_saved({"_actor": "service:ci"}) == "service:ci"
    assert who_saved({}) is None and who_saved(None) is None


# ── C3: share / unshare, re-join settings, doctor ────────────────────────


async def _memory(user: User, command: str, ids: list[str]) -> int:
    from astrocyte.harness import memories

    args = Namespace(bank=BANK, project=None, config=str(user.cfg), ids=ids, json=False)
    with user:
        return await memories._COMMANDS[command](args, user.pipeline, user.brain)


class TestShare:
    async def test_share_sends_a_captured_turn_with_all_its_chunks(self, gateway, alice):
        await alice.save(A1, "**user**: why Kafka?", tags=["captured"], _retain_id="r1", _chunk_index=0)
        await alice.save(A2, "**assistant**: Ordering per key.", tags=["captured"], _retain_id="r1", _chunk_index=1)
        await alice.save(A3, "**user**: lunch?", tags=["captured"], _retain_id="r2")
        await alice.join("tok-alice")
        assert await gateway.texts() == set(), "captured turns stay here by default"
        assert await _memory(alice, "share", [A1[:6]]) == 0
        await alice.sync()
        assert await gateway.texts() == {"**user**: why Kafka?", "**assistant**: Ordering per key."}

    async def test_share_overrides_private(self, gateway, alice):
        await alice.save(A1, "Deploys go out on Tuesdays.", tags=["private"])
        await alice.join("tok-alice")
        await _memory(alice, "share", [A1])
        await alice.sync()
        assert await gateway.texts() == {"Deploys go out on Tuesdays."}

    async def test_unshare_before_the_push_keeps_it_here(self, gateway, alice):
        await alice.save(A1, "Deploys go out on Tuesdays.")
        await _memory(alice, "unshare", [A1])  # before joining: remembered for when it does
        await alice.save(A2, "Staging is at staging.example.com.")
        await alice.join("tok-alice")
        assert await gateway.texts() == {"Staging is at staging.example.com."}

    async def test_unshare_after_the_push_takes_it_off_the_team_but_not_off_this_machine(self, gateway, alice, bob):
        await alice.save(A1, "Deploys go out on Tuesdays.")
        await alice.join("tok-alice")
        await bob.join("tok-bob")
        assert A1 in await bob.items()
        assert await _memory(alice, "unshare", [A1]) == 0
        assert await gateway.texts() == set()
        await alice.sync()
        await bob.sync()
        assert A1 in await alice.items(), "your copy stays"
        assert A1 not in await bob.items(), "teammates' copies go"

    async def test_a_teammates_memory_cannot_be_unshared(self, gateway, alice, bob, capsys):
        await alice.save(A1, "Deploys go out on Tuesdays.")
        await alice.join("tok-alice")
        await bob.join("tok-bob")
        assert await _memory(bob, "unshare", [A1]) == 1
        assert "forget <id> --local" in capsys.readouterr().err

    async def test_an_unshared_memory_cannot_be_shared_again(self, gateway, alice, capsys):
        await alice.save(A1, "Deploys go out on Tuesdays.")
        await alice.join("tok-alice")
        await _memory(alice, "unshare", [A1])
        assert await _memory(alice, "share", [A1]) == 1
        assert "can't be shared again" in capsys.readouterr().err

    async def test_joining_again_changes_the_settings_after_a_new_preview(self, gateway, alice, capsys):
        await alice.save(A1, "**user**: why Kafka?", tags=["captured"])
        await alice.join("tok-alice")
        assert await gateway.texts() == set()
        capsys.readouterr()
        await alice.join("tok-alice", share_captured=True)
        out = capsys.readouterr().out
        assert "Updating" in out and "captured conversation included" in out
        assert await gateway.texts() == {"**user**: why Kafka?"}


class TestDoctor:
    def _checks(self, user: User) -> list:
        from astrocyte.harness import doctor

        with user:
            return doctor._check_team(user.cfg)

    def test_nothing_when_no_project_is_shared(self, gateway, alice):
        assert self._checks(alice) == []

    def test_a_synced_project_is_ok(self, gateway, alice):
        import asyncio

        asyncio.run(alice.join("tok-alice"))
        [check] = self._checks(alice)
        assert check.level == "ok" and check.area == f"team {BANK}" and URL in check.summary

    def test_an_unreachable_gateway_fails(self, gateway, alice):
        import asyncio

        asyncio.run(alice.join("tok-alice"))
        gateway.status = 401
        [check] = self._checks(alice)
        assert check.level == "fail" and "refused the token" in check.summary

    def test_a_failed_last_sync_warns(self, gateway, alice):
        import asyncio

        asyncio.run(alice.join("tok-alice"))
        with alice:
            state = team.SyncState.load(BANK)
            state.last_error = "the gateway answered 503: busy"
            state.save(BANK)
        [check] = self._checks(alice)
        assert check.level == "warn" and "503" in check.summary and check.fix == "astrocyte team sync"

    def test_a_missing_token_says_how_to_join_again(self, gateway, alice):
        with alice:
            team.save_memberships(alice.cfg, {BANK: {"url": URL}})
        [check] = self._checks(alice)
        assert check.level == "fail" and "astrocyte team join" in check.fix

    def test_a_long_unsynced_project_warns(self, gateway, alice):
        import asyncio

        asyncio.run(alice.join("tok-alice"))
        with alice:
            state = team.SyncState.load(BANK)
            state.last_sync = "2026-01-01T00:00:00+00:00"
            state.save(BANK)
        [check] = self._checks(alice)
        assert check.level == "warn" and "days ago" in check.summary
