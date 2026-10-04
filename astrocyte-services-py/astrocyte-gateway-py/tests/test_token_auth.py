"""Per-user gateway tokens (``ASTROCYTE_AUTH_MODE=token``): registry, CLI, auth, grants, provenance."""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from astrocyte_gateway import tokens as tk


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ASTROCYTE_CONFIG_PATH", "ASTROCYTE_TOKENS_FILE", "ASTROCYTE_RATE_LIMIT_PER_SECOND"):
        monkeypatch.delenv(name, raising=False)


def _write_acl_config(tmp_path: Path, grants: str = "[]") -> Path:
    cfg = tmp_path / "astrocyte.yaml"
    cfg.write_text(
        textwrap.dedent(
            """
            provider_tier: storage
            vector_store: in_memory
            graph_store: in_memory
            llm_provider: mock
            barriers:
              pii:
                mode: disabled
            access_control:
              enabled: true
              default_policy: deny
            access_grants: {grants}
            """
        ).format(grants=grants),
        encoding="utf-8",
    )
    return cfg


def _token_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, grants: str = "[]"):
    """App in token mode with access control on (deny by default); returns (client, brain, registry path)."""
    registry = tmp_path / "tokens.yaml"
    registry.write_text("tokens: []\n", encoding="utf-8")
    monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "token")
    monkeypatch.setenv("ASTROCYTE_TOKENS_FILE", str(registry))
    monkeypatch.setenv("ASTROCYTE_CONFIG_PATH", str(_write_acl_config(tmp_path, grants)))
    from astrocyte_gateway.app import create_app
    from astrocyte_gateway.brain import build_astrocyte

    brain = build_astrocyte()
    return TestClient(create_app(brain)), brain, registry


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _retain(client: TestClient, token: str, bank_id: str = "project:api", **extra):
    return client.post(
        "/v1/retain", json={"content": "we use SQS", "bank_id": bank_id, **extra}, headers=_bearer(token)
    )


# ---------------------------------------------------------------------------
# Registry and CLI
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_create_stores_only_the_hash(self, tmp_path: Path):
        path = tmp_path / "tokens.yaml"
        plaintext, record = tk.create_token(
            path, principal="user:alice", banks=["project:*"], permissions=["read"], groups=["team:api"], label="laptop"
        )
        text = path.read_text(encoding="utf-8")
        assert plaintext.startswith(tk.TOKEN_PREFIX)
        assert plaintext not in text
        assert plaintext.removeprefix(tk.TOKEN_PREFIX) not in text
        assert record.token_hash == tk.hash_token(plaintext) and record.token_hash in text
        [loaded] = tk.load_records(path)
        assert loaded == record
        assert loaded.groups == ("team:api",)
        assert loaded.grants == (tk.TokenGrant(bank_id="project:*", permissions=("read",)),)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_registry_file_is_owner_only(self, tmp_path: Path):
        path = tmp_path / "tokens.yaml"
        tk.create_token(path, principal="user:alice")
        assert (path.stat().st_mode & 0o777) == 0o600

    def test_json_registry_round_trips(self, tmp_path: Path):
        path = tmp_path / "tokens.json"
        _, record = tk.create_token(path, principal="agent:ci", banks=["b1"])
        assert json.loads(path.read_text(encoding="utf-8"))["tokens"][0]["id"] == record.id
        assert tk.load_records(path) == [record]

    def test_default_permissions_are_read_write(self, tmp_path: Path):
        _, record = tk.create_token(tmp_path / "t.yaml", principal="user:alice", banks=["b1"])
        assert record.grants[0].permissions == ("read", "write")

    def test_revoke_marks_and_keeps_the_row(self, tmp_path: Path):
        path = tmp_path / "tokens.yaml"
        _, record = tk.create_token(path, principal="user:alice")
        revoked = tk.revoke_token(path, record.id)
        assert revoked.revoked_at is not None and not revoked.active
        assert tk.load_records(path)[0].revoked_at == revoked.revoked_at
        with pytest.raises(ValueError, match="already revoked"):
            tk.revoke_token(path, record.id)
        with pytest.raises(ValueError, match="no token"):
            tk.revoke_token(path, "deadbeef")

    def test_context_carries_principal_groups_and_grants(self, tmp_path: Path):
        _, record = tk.create_token(
            tmp_path / "t.yaml", principal="user:alice", banks=["project:*"], groups=["team:api"]
        )
        ctx = record.context()
        assert ctx.principal == "user:alice"
        assert ctx.groups == ["team:api"]
        assert [(g.bank_id, g.principal, g.permissions) for g in ctx.grants] == [
            ("project:*", "user:alice", ["read", "write"])
        ]
        _, bare = tk.create_token(tmp_path / "t.yaml", principal="user:bob")
        assert bare.context().groups is None and bare.context().grants is None

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"principal": "alice"}, "principal"),
            ({"principal": "user:*"}, "principal"),
            ({"principal": "team:api"}, "principal"),
            ({"principal": "user:alice", "groups": ["api"]}, "team:<name>"),
            ({"principal": "user:alice", "banks": ["bad bank"]}, "bank"),
            ({"principal": "user:alice", "banks": ["b1"], "permissions": ["delete"]}, "invalid"),
        ],
    )
    def test_create_rejects_bad_input(self, tmp_path: Path, kwargs, match):
        path = tmp_path / "tokens.yaml"
        with pytest.raises(ValueError, match=match):
            tk.create_token(path, **kwargs)
        assert not path.exists()

    def test_registry_reloads_when_the_file_changes(self, tmp_path: Path):
        path = tmp_path / "tokens.yaml"
        plaintext, record = tk.create_token(path, principal="user:alice")
        reg = tk.TokenRegistry(path)
        assert reg.load() == 1
        assert reg.lookup(plaintext) == record
        assert reg.lookup("astk_wrong") is None
        tk.revoke_token(path, record.id)
        assert reg.lookup(plaintext) is None
        assert reg.load() == 0

    def test_registry_for_shares_one_instance_per_file(self, tmp_path: Path):
        path = tmp_path / "tokens.yaml"
        assert tk.registry_for(path) is tk.registry_for(str(path))
        assert tk.registry_for(path) is not tk.registry_for(tmp_path / "other.yaml")

    def test_failed_write_leaves_registry_and_no_temp_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        path = tmp_path / "tokens.yaml"
        tk.create_token(path, principal="user:alice")
        before = path.read_text(encoding="utf-8")

        def _boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(tk.os, "replace", _boom)
        with pytest.raises(OSError, match="disk full"):
            tk.create_token(path, principal="user:bob")
        assert path.read_text(encoding="utf-8") == before
        assert sorted(p.name for p in tmp_path.iterdir()) == ["tokens.yaml"]

    def test_unreadable_registry_raises(self, tmp_path: Path):
        with pytest.raises(tk.TokenRegistryError, match="not readable"):
            tk.TokenRegistry(tmp_path / "missing.yaml").load()


class TestLoadRecordsValidation:
    _HASH = "sha256:" + "0" * 64

    def _load(self, tmp_path: Path, text: str):
        path = tmp_path / "tokens.yaml"
        path.write_text(text, encoding="utf-8")
        return tk.load_records(path)

    def test_empty_file_has_no_tokens(self, tmp_path: Path):
        assert self._load(tmp_path, "") == []
        assert self._load(tmp_path, "tokens:\n") == []

    def test_missing_file(self, tmp_path: Path):
        with pytest.raises(tk.TokenRegistryError, match="does not exist"):
            tk.load_records(tmp_path / "nope.yaml")

    def test_directory_is_unreadable(self, tmp_path: Path):
        with pytest.raises(tk.TokenRegistryError, match="could not be read"):
            tk.load_records(tmp_path)

    @pytest.mark.parametrize(
        ("text", "match"),
        [
            ("tokens: [\n", "could not be read"),
            ("- a\n", "mapping with a 'tokens' list"),
            ("tokens: {}\n", "mapping with a 'tokens' list"),
            ("tokens: [x]\n", r"tokens\[0\] must be a mapping"),
            ("tokens: [{id: a, principal: 'user:a'}]\n", "missing 'hash'"),
            ("tokens: [{id: a, principal: 'user:a', hash: 'md5:00'}]\n", "sha256"),
            (f"tokens: [{{id: a, principal: 'nobody', hash: '{_HASH}'}}]\n", "principal"),
            (f"tokens: [{{id: a, principal: 'user:a', hash: '{_HASH}', groups: [x]}}]\n", "team:<name>"),
            (f"tokens: [{{id: a, principal: 'user:a', hash: '{_HASH}', grants: [b1]}}]\n", "permissions list"),
            (
                f"tokens: [{{id: a, principal: 'user:a', hash: '{_HASH}', grants: [{{bank_id: b1, permissions: [x]}}]}}]\n",
                "invalid",
            ),
            (
                f"tokens: [{{id: a, principal: 'user:a', hash: '{_HASH}'}}, {{id: a, principal: 'user:b', hash: '{_HASH}'}}]\n",
                "duplicate",
            ),
        ],
    )
    def test_malformed_registry(self, tmp_path: Path, text: str, match: str):
        with pytest.raises(tk.TokenRegistryError, match=match):
            self._load(tmp_path, text)


class TestCli:
    def test_create_prints_the_token_once_and_list_never_shows_it(self, tmp_path: Path, capsys):
        path = tmp_path / "tokens.yaml"
        rc = tk.main(
            [
                "create",
                "--principal",
                "user:alice",
                "--banks",
                "project:*, b1",
                "--permissions",
                "read",
                "--groups",
                "team:api",
                "--label",
                "alice laptop",
                "--file",
                str(path),
            ]
        )
        out = capsys.readouterr()
        assert rc == 0
        plaintext = out.out.strip()
        assert plaintext.startswith(tk.TOKEN_PREFIX) and "\n" not in plaintext
        assert plaintext not in out.err and "cannot be shown again" in out.err
        [record] = tk.load_records(path)
        assert [g.bank_id for g in record.grants] == ["project:*", "b1"]

        assert tk.main(["list", "--file", str(path)]) == 0
        listed = capsys.readouterr().out
        assert "user:alice" in listed and "team:api" in listed and "project:*=read" in listed
        assert "label=alice laptop" in listed
        assert plaintext not in listed and record.token_hash not in listed

    def test_revoke_and_list_all(self, tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch):
        path = tmp_path / "tokens.yaml"
        monkeypatch.setenv("ASTROCYTE_TOKENS_FILE", str(path))
        assert tk.main(["create", "--principal", "user:alice"]) == 0
        [record] = tk.load_records(path)
        capsys.readouterr()
        assert tk.main(["revoke", record.id]) == 0
        assert "revoked" in capsys.readouterr().err
        assert tk.main(["list"]) == 0
        assert capsys.readouterr().out == ""
        assert tk.main(["list", "--all"]) == 0
        assert "revoked" in capsys.readouterr().out
        assert tk.main(["revoke", record.id]) == 1
        assert "already revoked" in capsys.readouterr().err

    def test_list_of_missing_file_is_empty(self, tmp_path: Path, capsys):
        assert tk.main(["list", "--file", str(tmp_path / "none.yaml")]) == 0
        assert capsys.readouterr().out == ""

    def test_bad_input_exits_1(self, tmp_path: Path, capsys):
        assert tk.main(["create", "--principal", "nobody", "--file", str(tmp_path / "t.yaml")]) == 1
        assert "error:" in capsys.readouterr().err

    def test_file_is_required(self):
        with pytest.raises(SystemExit, match="ASTROCYTE_TOKENS_FILE"):
            tk.main(["list"])

    def test_module_entry_point(self, tmp_path: Path):
        import subprocess

        path = tmp_path / "tokens.yaml"
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "astrocyte_gateway.tokens",
                "create",
                "--principal",
                "user:alice",
                "--file",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        [record] = tk.load_records(path)
        assert record.token_hash == tk.hash_token(proc.stdout.strip())


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


class TestStartup:
    def test_requires_tokens_file(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "token")
        from astrocyte_gateway.app import create_app

        with pytest.raises(RuntimeError, match="requires ASTROCYTE_TOKENS_FILE"):
            create_app()

    def test_refuses_unloadable_registry(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "token")
        monkeypatch.setenv("ASTROCYTE_TOKENS_FILE", str(tmp_path / "missing.yaml"))
        from astrocyte_gateway.app import create_app

        with pytest.raises(RuntimeError, match="Refusing to start"):
            create_app()

    def test_empty_registry_starts_with_warning_on_public_host(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        path = tmp_path / "tokens.yaml"
        path.write_text("tokens: []\n", encoding="utf-8")
        monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "token")
        monkeypatch.setenv("ASTROCYTE_TOKENS_FILE", str(path))
        monkeypatch.setenv("ASTROCYTE_HOST", "0.0.0.0")
        from astrocyte_gateway import auth
        from astrocyte_gateway.app import create_app

        # Recorded directly: other tests reconfigure process logging, which
        # makes caplog order-dependent here.
        warnings: list[str] = []
        monkeypatch.setattr(auth._logger, "warning", lambda msg, *a: warnings.append(msg % a))
        create_app()
        assert any("no active tokens" in w for w in warnings)


class TestScopedTokensNeedAccessControl:
    """A token's grants and groups are enforced only by access control; with it
    off they would quietly mean every bank, so the gateway refuses instead."""

    def _app_without_acl(self, monkeypatch, tmp_path, *, scoped: bool):
        from astrocyte_gateway.tokens import create_token

        cfg = _write_acl_config(tmp_path).read_text(encoding="utf-8").replace("enabled: true", "enabled: false")
        (tmp_path / "astrocyte.yaml").write_text(cfg, encoding="utf-8")
        registry = tmp_path / "tokens.yaml"
        token, _ = create_token(registry, principal="user:alice", banks=["project:*"] if scoped else None)
        monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "token")
        monkeypatch.setenv("ASTROCYTE_TOKENS_FILE", str(registry))
        monkeypatch.setenv("ASTROCYTE_CONFIG_PATH", str(tmp_path / "astrocyte.yaml"))
        from astrocyte_gateway.app import create_app

        return create_app, token, registry

    def test_scoped_tokens_without_access_control_refuse_to_start(self, monkeypatch, tmp_path):
        create_app, _, _ = self._app_without_acl(monkeypatch, tmp_path, scoped=True)
        with pytest.raises(RuntimeError, match="access_control.enabled is false"):
            create_app()

    def test_unscoped_tokens_start_and_say_they_reach_every_bank(self, monkeypatch, tmp_path):
        from astrocyte_gateway import auth

        create_app, token, _ = self._app_without_acl(monkeypatch, tmp_path, scoped=False)
        warnings: list[str] = []
        monkeypatch.setattr(auth._logger, "warning", lambda msg, *a: warnings.append(msg % a))
        client = TestClient(create_app())
        assert any("every token can read and write every bank" in w for w in warnings)
        assert _retain(client, token, bank_id="anything").status_code == 200

    def test_a_scoped_token_added_while_running_is_refused(self, monkeypatch, tmp_path):
        from astrocyte_gateway.tokens import create_token

        create_app, _, registry = self._app_without_acl(monkeypatch, tmp_path, scoped=False)
        client = TestClient(create_app())
        scoped, _ = create_token(registry, principal="user:bob", banks=["project:api"])
        assert _retain(client, scoped).status_code == 403

    def test_with_access_control_scoped_tokens_start(self, monkeypatch, tmp_path):
        from astrocyte_gateway.tokens import create_token

        client, _, registry = _token_app(monkeypatch, tmp_path)
        token, _ = create_token(registry, principal="user:alice", banks=["project:*"])
        assert _retain(client, token).status_code == 200


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class TestTokenAuth:
    def test_bearer_and_x_api_key_authenticate(self, monkeypatch, tmp_path):
        client, _, registry = _token_app(monkeypatch, tmp_path)
        token, _ = tk.create_token(registry, principal="user:alice", banks=["project:*"])
        assert _retain(client, token).status_code == 200
        r = client.post("/v1/retain", json={"content": "x", "bank_id": "project:api"}, headers={"X-Api-Key": token})
        assert r.status_code == 200

    def test_missing_and_wrong_tokens_are_401(self, monkeypatch, tmp_path):
        client, _, registry = _token_app(monkeypatch, tmp_path)
        tk.create_token(registry, principal="user:alice", banks=["*"])
        r = client.post("/v1/retain", json={"content": "x", "bank_id": "b1"})
        assert r.status_code == 401 and "required" in r.json()["detail"]
        r = client.post("/v1/retain", json={"content": "x", "bank_id": "b1"}, headers={"Authorization": "Basic x"})
        assert r.status_code == 401
        assert _retain(client, "astk_not-a-real-token").status_code == 401

    def test_principal_header_is_ignored(self, monkeypatch, tmp_path):
        # The header names a principal with write everywhere; the token's
        # principal has none, so honouring the header would let this through.
        client, _, registry = _token_app(
            monkeypatch, tmp_path, grants='[{bank_id: "*", principal: "user:admin", permissions: ["*"]}]'
        )
        token, _ = tk.create_token(registry, principal="user:alice")
        r = client.post(
            "/v1/retain",
            json={"content": "x", "bank_id": "b1"},
            headers={**_bearer(token), "X-Astrocyte-Principal": "user:admin"},
        )
        assert r.status_code == 403
        assert "user:alice" in r.json()["detail"]

    def test_revoked_token_is_refused_without_restart(self, monkeypatch, tmp_path):
        client, _, registry = _token_app(monkeypatch, tmp_path)
        token, record = tk.create_token(registry, principal="user:alice", banks=["project:*"])
        assert _retain(client, token).status_code == 200
        tk.revoke_token(registry, record.id)
        r = _retain(client, token)
        assert r.status_code == 401 and r.json()["detail"] == "Invalid or revoked token"

    def test_broken_registry_fails_closed(self, monkeypatch, tmp_path):
        client, _, registry = _token_app(monkeypatch, tmp_path)
        token, _ = tk.create_token(registry, principal="user:alice", banks=["project:*"])
        registry.write_text("tokens: [\n", encoding="utf-8")
        r = _retain(client, token)
        assert r.status_code == 500 and r.json()["detail"] == "Token registry unavailable"

    def test_tokens_file_unset_at_request_time(self, monkeypatch, tmp_path):
        client, _, registry = _token_app(monkeypatch, tmp_path)
        token, _ = tk.create_token(registry, principal="user:alice")
        monkeypatch.delenv("ASTROCYTE_TOKENS_FILE")
        assert _retain(client, token).status_code == 500


class TestTokenGrants:
    def test_read_only_token_cannot_retain(self, monkeypatch, tmp_path):
        client, _, registry = _token_app(monkeypatch, tmp_path)
        token, _ = tk.create_token(registry, principal="user:alice", banks=["project:*"], permissions=["read"])
        r = _retain(client, token)
        assert r.status_code == 403
        r = client.post("/v1/recall", json={"query": "sqs", "bank_id": "project:api"}, headers=_bearer(token))
        assert r.status_code == 200

    def test_glob_grant_scopes_the_token(self, monkeypatch, tmp_path):
        client, _, registry = _token_app(monkeypatch, tmp_path)
        token, _ = tk.create_token(registry, principal="user:alice", banks=["project:*"])
        assert _retain(client, token, "project:web-9f8e7d").status_code == 200
        assert _retain(client, token, "projectx").status_code == 403
        assert _retain(client, token, "user-alice").status_code == 403

    def test_token_grants_add_to_config_grants(self, monkeypatch, tmp_path):
        client, _, registry = _token_app(
            monkeypatch, tmp_path, grants='[{bank_id: "notes", principal: "user:alice", permissions: [write]}]'
        )
        token, _ = tk.create_token(registry, principal="user:alice", banks=["project:*"])
        assert _retain(client, token, "notes").status_code == 200
        assert _retain(client, token, "project:api").status_code == 200

    def test_team_group_grants_apply_to_members(self, monkeypatch, tmp_path):
        client, _, registry = _token_app(
            monkeypatch, tmp_path, grants='[{bank_id: "project:*", principal: "team:api", permissions: [read, write]}]'
        )
        member, _ = tk.create_token(registry, principal="user:alice", groups=["team:api"])
        outsider, _ = tk.create_token(registry, principal="user:bob", groups=["team:web"])
        assert _retain(client, member).status_code == 200
        assert _retain(client, outsider).status_code == 403


class TestAuthoritativeActor:
    @staticmethod
    def _actors(brain, bank_id: str) -> set[str | None]:
        items = asyncio.run(brain._pipeline.vector_store.list_vectors(bank_id, offset=0, limit=50))
        assert items
        return {(i.metadata or {}).get("_actor") for i in items}

    def test_forged_actor_is_replaced_by_the_token_principal(self, monkeypatch, tmp_path):
        client, brain, registry = _token_app(monkeypatch, tmp_path)
        token, _ = tk.create_token(registry, principal="user:alice", banks=["project:*"])
        r = _retain(client, token, metadata={"_actor": "user:mallory", "source": "codex"})
        assert r.status_code == 200, r.text
        assert self._actors(brain, "project:api") == {"user:alice"}

    def test_anonymous_request_cannot_claim_an_actor(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "dev")
        from astrocyte_gateway.app import create_app
        from astrocyte_gateway.brain import build_astrocyte

        brain = build_astrocyte()
        client = TestClient(create_app(brain))
        r = client.post(
            "/v1/retain", json={"content": "x", "bank_id": "b1", "metadata": {"_actor": "user:mallory", "k": "v"}}
        )
        assert r.status_code == 200
        assert self._actors(brain, "b1") == {None}
