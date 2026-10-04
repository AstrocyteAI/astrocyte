"""astrocyte setup / doctor / uninstall against fake agent harnesses.

Everything runs inside a throwaway HOME. The claude / codex / gemini CLIs are
replaced by emulations of the behaviour measured on the real binaries
(claude 2.1, codex 0.160, gemini 0.49), including the awkward parts:

* ``claude mcp add`` for an existing name prints "already exists", exits 0
  and changes nothing — so an update must remove first.
* ``claude mcp remove`` of an absent name exits 1; the others exit 0.
* ``gemini mcp add`` defaults to *project* scope unless ``--scope user``.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tomllib
from argparse import Namespace
from pathlib import Path

import pytest

from astrocyte.harness import hosts as hosts_mod
from astrocyte.harness.commands import cmd_doctor, cmd_setup, cmd_uninstall
from astrocyte.harness.doctor import run_checks
from astrocyte.harness.hosts import (
    ClaudeCodeHost,
    CodexHost,
    CopilotHost,
    CursorHost,
    GeminiHost,
    ServerSpec,
    WindsurfHost,
)
from astrocyte.harness.localconfig import SetupError, choose_providers, render_config

SPEC = ServerSpec(command="/opt/astrocyte/bin/astrocyte-mcp", args=("--config", "/cfg/astrocyte.yaml"))
NEW_SPEC = ServerSpec(command="/opt/astrocyte-2/bin/astrocyte-mcp", args=("--config", "/cfg/astrocyte.yaml"))

FAKE_CLI = r'''#!{python}
"""Emulates `{name} mcp add/remove` as measured on the real CLI."""
import json, os, sys
from pathlib import Path

name, home = {name!r}, Path(os.environ["HOME"])
args = sys.argv[1:]
log = home / "cli-calls.log"
log.open("a").write(name + " " + " ".join(args) + "\n")
assert args[0] == "mcp", args

def load(p):
    return json.loads(p.read_text()) if p.exists() else {{}}

if name == "claude":
    path = home / ".claude.json"
    data = load(path); servers = data.setdefault("mcpServers", {{}})
    assert "--scope" in args and args[args.index("--scope") + 1] == "user", args
    rest = [a for a in args[1:] if a not in ("--scope", "user")]
    if rest[0] == "add":
        srv, cmd = rest[1], rest[3:]  # rest[2] is "--"
        if srv in servers:
            print(f"MCP server {{srv}} already exists in user config"); sys.exit(0)
        servers[srv] = {{"type": "stdio", "command": cmd[0], "args": cmd[1:], "env": {{}}}}
    else:
        if rest[1] not in servers:
            sys.exit(1)
        del servers[rest[1]]
    path.write_text(json.dumps(data))
elif name == "gemini":
    if "--scope" not in args or args[args.index("--scope") + 1] != "user":
        sys.exit("refusing: would write project scope")
    path = home / ".gemini" / "settings.json"; path.parent.mkdir(exist_ok=True)
    data = load(path); servers = data.setdefault("mcpServers", {{}})
    rest = [a for a in args[1:] if a not in ("--scope", "user")]
    if rest[0] == "add":
        srv, cmd, sep = rest[1], rest[2], rest[3]
        assert sep == "--", rest
        servers[srv] = {{"command": cmd, "args": rest[4:]}}
    else:
        servers.pop(rest[1], None)
    path.write_text(json.dumps(data))
elif name == "codex":
    path = Path(os.environ.get("CODEX_HOME", home / ".codex")) / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    side = path.with_suffix(".json")  # emulator state; TOML is rendered from it
    data = load(side)
    if args[1] == "add":
        srv, cmd = args[2], args[4:]
        data[srv] = {{"command": cmd[0], "args": cmd[1:]}}
    else:
        data.pop(args[2], None)
    side.write_text(json.dumps(data))
    body = "model = \"o3\"\n"
    for srv, e in data.items():
        body += f"\n[mcp_servers.{{srv}}]\ncommand = {{json.dumps(e['command'])}}\nargs = {{json.dumps(e['args'])}}\n"
    path.write_text(body)
'''


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    for var in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "COPILOT_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                "ASTROCYTE_CONFIG", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(h))
    # Keep doctor's daemon ping away from the developer's real state dir.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    return h


def install_cli(home: Path, name: str) -> None:
    path = home.parent / "bin" / name
    path.write_text(FAKE_CLI.format(python=sys.executable, name=name))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def cli_calls(home: Path) -> list[str]:
    log = home / "cli-calls.log"
    return log.read_text().splitlines() if log.exists() else []


# ── CLI-managed harnesses ────────────────────────────────────────────────


@pytest.mark.parametrize("host_cls,cli", [(ClaudeCodeHost, "claude"), (CodexHost, "codex"), (GeminiHost, "gemini")])
def test_cli_host_install_is_verified_and_idempotent(home, host_cls, cli):
    install_cli(home, cli)
    host = host_cls()
    assert host.install(SPEC).status == "installed"
    assert host.registration().matches(SPEC)
    calls = len(cli_calls(home))
    assert host.install(SPEC).status == "unchanged"
    assert len(cli_calls(home)) == calls, "an unchanged entry must not invoke the CLI"


@pytest.mark.parametrize("host_cls,cli", [(ClaudeCodeHost, "claude"), (CodexHost, "codex"), (GeminiHost, "gemini")])
def test_cli_host_replaces_a_stale_entry(home, host_cls, cli):
    install_cli(home, cli)
    host = host_cls()
    host.install(SPEC)
    assert host.install(NEW_SPEC).status == "updated"
    assert host.registration().matches(NEW_SPEC)


def test_claude_update_needs_remove_first(home):
    """The real `claude mcp add` silently keeps the old entry; without a
    remove, the stale path would survive and verification must catch it."""
    install_cli(home, "claude")
    ClaudeCodeHost().install(SPEC)

    class AddOnly(ClaudeCodeHost):
        def _write(self, spec):  # the naive implementation
            self._run("mcp", "add", "--scope", "user", "astrocyte", "--", spec.command, *spec.args)

    outcome = AddOnly().install(NEW_SPEC)
    assert outcome.status == "failed" and "did not take effect" in outcome.detail
    assert ClaudeCodeHost().install(NEW_SPEC).status == "updated"


def test_gemini_is_always_written_at_user_scope(home):
    install_cli(home, "gemini")  # the emulator refuses project scope
    assert GeminiHost().install(SPEC).status == "installed"
    assert all("--scope user" in c for c in cli_calls(home))


def test_missing_cli_fails_with_a_manual_command(home):
    (home / ".claude").mkdir()
    outcome = ClaudeCodeHost().install(SPEC)
    assert outcome.status == "failed"
    assert "claude mcp add --scope user astrocyte" in outcome.detail


def test_codex_respects_codex_home(home, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "elsewhere"))
    install_cli(home, "codex")
    CodexHost().install(SPEC)
    data = tomllib.loads((tmp_path / "elsewhere" / "config.toml").read_text())
    assert data["mcp_servers"]["astrocyte"]["command"] == SPEC.command


def test_uninstall_then_reinstall(home):
    install_cli(home, "claude")
    host = ClaudeCodeHost()
    host.install(SPEC)
    assert host.uninstall().status == "removed"
    assert host.uninstall().status == "absent"
    assert host.install(SPEC).status == "installed"


# ── JSON-configured harnesses ────────────────────────────────────────────


@pytest.mark.parametrize("host_cls", [CursorHost, WindsurfHost, CopilotHost])
def test_json_host_preserves_other_servers_and_keeps_a_backup(home, host_cls):
    host = host_cls()
    path = host.config_file()
    path.parent.mkdir(parents=True)
    original = {"mcpServers": {"other": {"command": "x", "args": []}}, "theme": "dark"}
    path.write_text(json.dumps(original))
    path.chmod(0o600)

    assert host.install(SPEC).status == "installed"
    data = json.loads(path.read_text())
    assert data["mcpServers"]["other"] == original["mcpServers"]["other"]
    assert data["theme"] == "dark"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600, "file permissions must survive the rewrite"
    assert json.loads(path.with_name(path.name + ".astrocyte-bak").read_text()) == original
    assert host.install(SPEC).status == "unchanged"


@pytest.mark.parametrize("host_cls", [CursorHost, WindsurfHost, CopilotHost])
def test_json_host_refuses_to_touch_invalid_json(home, host_cls):
    host = host_cls()
    path = host.config_file()
    path.parent.mkdir(parents=True)
    path.write_text("{ not json")
    outcome = host.install(SPEC)
    assert outcome.status == "failed" and "not valid JSON" in outcome.detail
    assert path.read_text() == "{ not json"


def test_copilot_entry_has_the_fields_copilot_requires(home):
    CopilotHost().install(SPEC)
    entry = json.loads(CopilotHost().config_file().read_text())["mcpServers"]["astrocyte"]
    assert entry["type"] == "local" and entry["tools"] == ["*"]


def test_dry_run_changes_nothing(home):
    (home / ".cursor").mkdir()
    outcome = CursorHost().install(SPEC, dry_run=True)
    assert outcome.status == "planned" and outcome.detail.startswith("would install ")
    assert not CursorHost().config_file().exists()


# ── provider choice and the generated config ─────────────────────────────


def test_claude_cli_and_local_embeddings_when_available(home, monkeypatch):
    install_cli(home, "claude")
    monkeypatch.setattr("astrocyte.harness.localconfig.local_embedding_backend", lambda: "fastembed")
    choice = choose_providers()
    assert (choice.llm_provider, choice.embedding_provider) == ("claude_cli", "local_embeddings")
    assert choice.embedding_provider_config == {"pad_to": 0}


def test_openai_alone_embeds_with_openai(home, monkeypatch):
    monkeypatch.setattr("astrocyte.harness.localconfig.local_embedding_backend", lambda: None)
    choice = choose_providers({"OPENAI_API_KEY": "sk-test"})
    assert choice.llm_provider == "openai" and choice.embedding_provider is None


def test_no_model_at_all_explains_both_options(home, monkeypatch):
    monkeypatch.setattr("astrocyte.harness.localconfig.local_embedding_backend", lambda: "fastembed")
    with pytest.raises(SetupError, match="Claude Code.*OPENAI_API_KEY"):
        choose_providers({})


def test_generated_config_loads_and_keeps_native_embedding_width(home, monkeypatch, tmp_path):
    from astrocyte.config import load_config
    from astrocyte.wiring import config_kwargs

    install_cli(home, "claude")
    monkeypatch.setattr("astrocyte.harness.localconfig.local_embedding_backend", lambda: "fastembed")
    path = tmp_path / "astrocyte.yaml"
    path.write_text(render_config(choose_providers(), tmp_path / "mem dir" / "astrocyte.db"))
    cfg = load_config(str(path))
    assert cfg.vector_store == "sqlite"
    assert cfg.vector_store_config["path"] == str(tmp_path / "mem dir" / "astrocyte.db")
    # pad_to must survive config_kwargs (which drops None) — hence 0, not null.
    assert config_kwargs(cfg.embedding_provider_config) == {"pad_to": 0}


# ── commands end to end ──────────────────────────────────────────────────

MINIMAL = "vector_store: in_memory\nllm_provider: mock\nbarriers:\n  pii:\n    mode: disabled\n"


def _ns(**kw) -> Namespace:
    base = {k: False for k in ("claude", "codex", "cursor", "gemini", "windsurf", "copilot", "antigravity")}
    base.update(dry_run=False, no_verify=False, no_hooks=False, config=None, fix=False, json=False, skip_models=True)
    base.update(kw)
    return Namespace(**base)


@pytest.fixture
def wired_home(home):
    """Claude + Cursor installed, a valid config present."""
    install_cli(home, "claude")
    (home / ".claude").mkdir()
    (home / ".cursor").mkdir()
    cfg = home / ".config" / "astrocyte" / "astrocyte.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(MINIMAL)
    return home


def test_setup_wires_every_detected_harness_after_proving_the_server_starts(wired_home, capsys):
    assert cmd_setup(_ns()) == 0
    out = capsys.readouterr().out
    assert "starts and answers" in out
    assert ClaudeCodeHost().registration() is not None
    assert CursorHost().registration() is not None
    assert CodexHost().registration() is None  # not installed, not touched


def test_setup_explains_harness_consent_gates_after_a_fresh_install(wired_home, capsys):
    """Cursor won't load a new MCP server until the user approves it; saying
    so up front beats a first experience of "it doesn't work in Cursor"."""
    cmd_setup(_ns())
    assert "enable 'astrocyte' under Settings" in capsys.readouterr().out
    cmd_setup(_ns())  # nothing new installed → no repeated nagging
    assert "One more step" not in capsys.readouterr().out


def test_setup_keeps_an_existing_config(wired_home):
    cfg = wired_home / ".config" / "astrocyte" / "astrocyte.yaml"
    cmd_setup(_ns())
    assert cfg.read_text() == MINIMAL


def test_setup_wires_nothing_when_the_server_cannot_start(wired_home, capsys):
    (wired_home / ".config" / "astrocyte" / "astrocyte.yaml").write_text("vector_store: no_such_store\n")
    assert cmd_setup(_ns()) == 1
    assert "nothing was wired" in capsys.readouterr().err
    assert ClaudeCodeHost().registration() is None


def test_doctor_reports_unwired_harness_as_info_not_failure(wired_home):
    cmd_setup(_ns(cursor=True))  # only Cursor
    checks = {c.area: c for c in run_checks(wired_home / ".config" / "astrocyte" / "astrocyte.yaml",
                                              model_probes=False)}
    assert checks["Cursor"].level == "ok"
    assert checks["Claude Code"].level == "info"


def test_doctor_fix_repairs_a_broken_path_but_not_an_unwired_harness(wired_home, capsys):
    cmd_setup(_ns(cursor=True))
    path = CursorHost().config_file()
    data = json.loads(path.read_text())
    data["mcpServers"]["astrocyte"]["command"] = "/gone/astrocyte-mcp"
    path.write_text(json.dumps(data))

    assert cmd_doctor(_ns()) == 1
    assert "points at a missing server" in capsys.readouterr().out
    assert cmd_doctor(_ns(fix=True)) == 0
    assert Path(CursorHost().registration().command).exists()
    assert ClaudeCodeHost().registration() is None, "--fix must not wire harnesses the user left out"


def test_doctor_json(wired_home, capsys):
    cmd_setup(_ns())
    capsys.readouterr()
    rc = cmd_doctor(_ns(json=True))
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0 and payload["ok"] is True
    assert {c["area"] for c in payload["checks"]} >= {"install", "config", "store", "server", "Claude Code"}


def test_doctor_fix_writes_a_missing_config(home, monkeypatch, capsys):
    install_cli(home, "claude")
    monkeypatch.setattr("astrocyte.harness.localconfig.local_embedding_backend", lambda: "fastembed")
    assert cmd_doctor(_ns()) == 1
    cmd_doctor(_ns(fix=True))
    assert (home / ".config" / "astrocyte" / "astrocyte.yaml").is_file()


def test_uninstall_removes_entries_and_keeps_memories(wired_home, capsys):
    cmd_setup(_ns())
    assert cmd_uninstall(_ns()) == 0
    assert ClaudeCodeHost().registration() is None and CursorHost().registration() is None
    assert "left in place" in capsys.readouterr().out
    assert (wired_home / ".config" / "astrocyte" / "astrocyte.yaml").is_file()


def test_setup_turns_on_automatic_memory_in_claude_code(wired_home, capsys):
    cmd_setup(_ns())
    commands = ClaudeCodeHost().hook_commands()
    assert all(commands.values()), commands
    assert "Automatic memory is on" in capsys.readouterr().out


def test_setup_turns_on_automatic_memory_in_codex_too(wired_home, capsys):
    install_cli(wired_home, "codex")
    (wired_home / ".codex").mkdir()
    cmd_setup(_ns())
    out = capsys.readouterr().out
    assert all(CodexHost().hook_commands().values())
    assert "Claude Code and Codex CLI: each finished turn is saved" in out
    # Codex skips new hooks until trusted in /hooks; say so on a fresh install only.
    assert "run /hooks" in out
    cmd_setup(_ns())
    assert "run /hooks" not in capsys.readouterr().out
    cmd_setup(_ns(no_hooks=True))
    assert not any(CodexHost().hook_commands().values())


def test_setup_no_hooks_turns_automatic_memory_off_again(wired_home):
    cmd_setup(_ns())
    cmd_setup(_ns(no_hooks=True))
    assert not any(ClaudeCodeHost().hook_commands().values())


def test_uninstall_removes_the_hooks_too(wired_home):
    cmd_setup(_ns())
    cmd_uninstall(_ns())
    assert not any(ClaudeCodeHost().hook_commands().values())


# ── choices that survive a plain `astrocyte setup` ──────────────────────


def _choices_file(home: Path) -> Path:
    return home / ".config" / "astrocyte" / "harnesses.json"


def test_an_agent_removed_by_name_stays_off_through_a_plain_setup(wired_home, capsys):
    """Re-running setup (after an upgrade, say) must not undo `uninstall --claude`."""
    cmd_setup(_ns())
    assert cmd_uninstall(_ns(claude=True)) == 0
    assert "astrocyte setup will leave Claude Code switched off" in capsys.readouterr().out
    assert cmd_setup(_ns()) == 0
    out = capsys.readouterr().out
    assert ClaudeCodeHost().registration() is None
    assert not any(ClaudeCodeHost().hook_commands().values())
    assert "switched off; astrocyte setup --claude turns it back on" in out
    assert CursorHost().registration() is not None, "the others are still wired"


def test_naming_the_agent_turns_it_back_on_for_good(wired_home):
    cmd_setup(_ns())
    cmd_uninstall(_ns(claude=True))
    cmd_setup(_ns(claude=True))
    assert ClaudeCodeHost().registration() is not None and all(ClaudeCodeHost().hook_commands().values())
    cmd_setup(_ns())
    assert ClaudeCodeHost().registration() is not None, "the choice was cleared"
    assert json.loads(_choices_file(wired_home).read_text()) == {"off": [], "hooks_off": []}


def test_no_hooks_is_remembered_until_the_agent_is_named(wired_home, capsys):
    cmd_setup(_ns(no_hooks=True))
    cmd_setup(_ns())
    out = capsys.readouterr().out
    assert ClaudeCodeHost().registration() is not None, "memory tools stay wired"
    assert not any(ClaudeCodeHost().hook_commands().values()), "automatic memory stays off"
    assert "automatic memory off; astrocyte setup --claude turns it on" in out
    assert "Automatic memory is on" not in out
    cmd_setup(_ns(claude=True))
    assert all(ClaudeCodeHost().hook_commands().values())


def test_no_hooks_for_one_named_agent(wired_home):
    install_cli(wired_home, "codex")
    (wired_home / ".codex").mkdir()
    cmd_setup(_ns(claude=True, no_hooks=True))
    cmd_setup(_ns())
    assert not any(ClaudeCodeHost().hook_commands().values())
    assert all(CodexHost().hook_commands().values())


def test_a_plain_uninstall_is_a_teardown_not_a_choice(wired_home):
    cmd_setup(_ns())
    cmd_uninstall(_ns())
    assert not _choices_file(wired_home).exists()
    cmd_setup(_ns())
    assert ClaudeCodeHost().registration() is not None


def test_dry_runs_record_nothing(wired_home):
    cmd_setup(_ns())
    cmd_uninstall(_ns(claude=True, dry_run=True))
    cmd_setup(_ns(no_hooks=True, dry_run=True))
    assert not _choices_file(wired_home).exists()


def test_every_detected_agent_switched_off(wired_home, capsys):
    cmd_setup(_ns())
    cmd_uninstall(_ns(claude=True, cursor=True))
    capsys.readouterr()
    assert cmd_setup(_ns()) == 0
    out = capsys.readouterr().out
    assert "Every detected agent is switched off; nothing was wired" in out
    assert ClaudeCodeHost().registration() is None and CursorHost().registration() is None


def test_doctor_reports_a_switched_off_agent_as_a_choice(wired_home):
    cmd_setup(_ns())
    cmd_uninstall(_ns(claude=True))
    checks = {c.area: c for c in run_checks(_cfg(wired_home), model_probes=False)}
    assert checks["Claude Code"].level == "info"
    assert checks["Claude Code"].summary.startswith("switched off")
    assert "Claude Code hooks" not in checks, "nothing to say about hooks of an agent that is off"
    cmd_setup(_ns(cursor=True, no_hooks=True))
    checks = {c.area: c for c in run_checks(_cfg(wired_home), model_probes=False)}
    assert checks["Cursor"].level == "ok"


def test_doctor_reports_hooks_switched_off_as_a_choice(wired_home):
    cmd_setup(_ns(no_hooks=True))
    checks = {c.area: c for c in run_checks(_cfg(wired_home), model_probes=False)}
    assert checks["Claude Code hooks"].summary.startswith("automatic memory switched off")


@pytest.mark.parametrize("body", ['["claude"]', '{"off": "claude", "hooks_off": null}', '{"off": [1, null]}'])
def test_a_choices_file_of_the_wrong_shape_records_nothing(tmp_path, body):
    from astrocyte.harness.choices import load_choices

    cfg = tmp_path / "astrocyte.yaml"
    (tmp_path / "harnesses.json").write_text(body)
    choices = load_choices(cfg)
    assert choices.off == set() and choices.hooks_off == set()


def test_choices_round_trip_and_no_file_when_nothing_is_off(tmp_path):
    from astrocyte.harness.choices import Choices, choices_path, load_choices, save_choices

    cfg = tmp_path / "astrocyte.yaml"
    save_choices(cfg, Choices())
    assert not choices_path(cfg).exists(), "no file until something is switched off"
    save_choices(cfg, Choices(off={"codex", "claude"}, hooks_off={"cursor"}))
    assert load_choices(cfg) == Choices(off={"claude", "codex"}, hooks_off={"cursor"})
    assert json.loads(choices_path(cfg).read_text())["off"] == ["claude", "codex"], "stable order"


def test_an_unreadable_choices_file_is_ignored(wired_home):
    _choices_file(wired_home).write_text("{not json")
    assert cmd_setup(_ns()) == 0
    assert ClaudeCodeHost().registration() is not None


def test_doctor_reports_and_repairs_hooks_from_a_moved_install(wired_home, capsys):
    cmd_setup(_ns(claude=True))
    host = ClaudeCodeHost()
    host.install_hooks("/old/install/bin/astrocyte")
    capsys.readouterr()
    assert cmd_doctor(_ns()) == 1
    assert "points at another install" in capsys.readouterr().out
    assert cmd_doctor(_ns(fix=True)) == 0
    assert all(not c.startswith("/old/") for c in host.hook_commands().values())


def test_every_host_class_is_registered():
    assert {c.key for c in hosts_mod.SUPPORTED_HOSTS} == {"claude", "codex", "cursor", "gemini", "windsurf", "copilot", "antigravity"}



# ── setup from nothing, and its failure paths ────────────────────────────


@pytest.fixture
def bare_home(home, monkeypatch):
    """Claude Code installed; no Astrocyte config yet; a local embedder."""
    install_cli(home, "claude")
    (home / ".claude").mkdir()
    monkeypatch.setattr("astrocyte.harness.localconfig.local_embedding_backend", lambda: "fastembed")
    return home


def _cfg(home: Path) -> Path:
    return home / ".config" / "astrocyte" / "astrocyte.yaml"


def test_setup_from_nothing_writes_a_local_config_and_explains_it(bare_home, capsys):
    assert cmd_setup(_ns(no_verify=True)) == 0
    out = capsys.readouterr().out
    text = _cfg(bare_home).read_text()
    assert "vector_store: sqlite" in text and "llm_provider: claude_cli" in text
    assert "wrote" in out and "completions: the Claude Code CLI" in out and "embeddings:" in out
    assert ClaudeCodeHost().registration() is not None


def test_dry_run_from_nothing_writes_nothing(bare_home, capsys):
    assert cmd_setup(_ns(dry_run=True)) == 0
    assert "would write" in capsys.readouterr().out
    assert not _cfg(bare_home).exists() and ClaudeCodeHost().registration() is None


def test_setup_without_any_model_stops_before_touching_agents(home, capsys, monkeypatch):
    (home / ".cursor").mkdir()
    monkeypatch.setattr("astrocyte.harness.localconfig.local_embedding_backend", lambda: "fastembed")
    assert cmd_setup(_ns()) == 1
    assert "No language model" in capsys.readouterr().err
    assert CursorHost().registration() is None and not _cfg(home).exists()


def test_setup_without_the_server_installed_wires_nothing(wired_home, capsys, monkeypatch):
    monkeypatch.setattr("astrocyte.harness.commands.locate_mcp_server", lambda: None)
    assert cmd_setup(_ns()) == 1
    assert "astrocyte-mcp not found" in capsys.readouterr().err
    assert ClaudeCodeHost().registration() is None


def test_setup_warns_when_the_server_lives_in_a_prunable_cache(wired_home, capsys, monkeypatch):
    from astrocyte.harness.server import ServerLocation, locate_mcp_server

    real = locate_mcp_server()
    monkeypatch.setattr("astrocyte.harness.commands.locate_mcp_server",
                        lambda: ServerLocation(command=real.command, ephemeral=True))
    cmd_setup(_ns(no_verify=True))
    assert "cache that may be pruned" in capsys.readouterr().out


# ── provider choice edge cases ───────────────────────────────────────────


def test_claude_without_a_local_embedder_is_refused_with_the_fix(home, monkeypatch):
    install_cli(home, "claude")
    monkeypatch.setattr("astrocyte.harness.localconfig.local_embedding_backend", lambda: None)
    with pytest.raises(SetupError, match=r"astrocyte\[local\]"):
        choose_providers()


def test_openai_only_config_says_where_embeddings_come_from(home, monkeypatch, tmp_path):
    monkeypatch.setattr("astrocyte.harness.localconfig.local_embedding_backend", lambda: None)
    text = render_config(choose_providers({"OPENAI_API_KEY": "sk-test"}), tmp_path / "m.db")
    assert "llm_provider: openai" in text and "# Embeddings use OpenAI" in text and "embedding_provider:" not in text


def test_embedding_backend_detection(monkeypatch):
    from astrocyte.harness import localconfig

    monkeypatch.setitem(sys.modules, "fastembed", None)  # import blocked
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    assert localconfig.local_embedding_backend() is None
    monkeypatch.delitem(sys.modules, "sentence_transformers")
    monkeypatch.setattr(localconfig.importlib.util, "find_spec", lambda name: object())
    assert localconfig.local_embedding_backend() == "sentence-transformers"

    def broken(name):
        raise ValueError("bad spec")

    monkeypatch.setattr(localconfig.importlib.util, "find_spec", broken)
    assert localconfig._module_available("sentence_transformers") is False


# ── doctor checks, one failure at a time ─────────────────────────────────


def _levels(checks, area):
    return [(c.level, c.summary) for c in checks if c.area == area]


def test_doctor_install_check(monkeypatch):
    from astrocyte.harness import doctor
    from astrocyte.harness.server import ServerLocation

    assert ("fail", "astrocyte-mcp not found in this installation") in _levels(doctor._check_install(None), "install")
    warn = doctor._check_install(ServerLocation("/cache/py", ephemeral=True))
    assert any(level == "warn" and "pruned" in s for level, s in _levels(warn, "install"))


def test_doctor_reports_a_config_that_does_not_load(home, capsys):
    cfg = home / ".config" / "astrocyte" / "astrocyte.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("vector_store: [unclosed\n")
    assert cmd_doctor(_ns()) == 1
    assert "does not load" in capsys.readouterr().out


def test_doctor_store_check(home):
    import asyncio

    from astrocyte.config import AstrocyteConfig
    from astrocyte.harness import doctor

    bad = AstrocyteConfig()
    bad.vector_store = "no_such_store"
    assert asyncio.run(doctor._check_store(bad))[0].level == "fail"
    none = AstrocyteConfig()
    assert "no vector_store" in asyncio.run(doctor._check_store(none))[0].summary


class _Provider:
    def __init__(self, embed_error=None, complete_error=None):
        self.embed_error, self.complete_error = embed_error, complete_error

    async def embed(self, texts, model=None):
        if self.embed_error:
            raise self.embed_error
        return [[0.0] * 384]

    async def complete(self, messages, **kw):
        from astrocyte.types import Completion

        if self.complete_error:
            raise self.complete_error
        return Completion(text="OK", model="x")


@pytest.mark.parametrize("provider,expected", [
    (_Provider(), [("ok", "embeddings: 384-dim"), ("ok", "completions: replied")]),
    (_Provider(embed_error=RuntimeError("no model file")), [("fail", "embedding failed"), ("ok", "completions")]),
    (_Provider(complete_error=TimeoutError("claude hung")), [("ok", "embeddings"), ("fail", "completion failed")]),
])
def test_doctor_model_probes(monkeypatch, provider, expected):
    import asyncio

    from astrocyte.config import AstrocyteConfig
    from astrocyte.harness import doctor

    monkeypatch.setattr("astrocyte.wiring.resolve_llm_provider", lambda cfg: provider)
    checks = asyncio.run(doctor._check_models(AstrocyteConfig()))
    assert [(c.level, c.summary[: len(want)]) for c, (_, want) in zip(checks, expected)] == expected


def test_doctor_model_probe_reports_an_unbuildable_provider(monkeypatch):
    import asyncio

    from astrocyte.config import AstrocyteConfig
    from astrocyte.harness import doctor

    def broken(cfg):
        raise ValueError("unknown provider 'gpt5'")

    monkeypatch.setattr("astrocyte.wiring.resolve_llm_provider", broken)
    [check] = asyncio.run(doctor._check_models(AstrocyteConfig()))
    assert check.level == "fail" and "gpt5" in check.summary


def test_doctor_warns_about_the_mock_provider(wired_home, capsys):
    cmd_doctor(_ns(skip_models=False))
    assert "mock provider configured" in capsys.readouterr().out


# ── the start-up handshake against misbehaving servers ───────────────────


FAKE_SERVER = """
import json, sys, time
mode = sys.argv[1]
for line in sys.stdin:
    msg = json.loads(line)
    if mode == "crash":
        print("Traceback: ImportError: no module named fastmcp", file=sys.stderr); sys.exit(1)
    if mode == "silent":
        time.sleep(30)
    if msg.get("id") == 1:
        print("starting up...", flush=True)  # noise on stdout must be skipped
        print(json.dumps({"jsonrpc": "2.0", "id": 99, "result": {}}), flush=True)  # someone else's reply
        print(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}), flush=True)
    elif msg.get("id") == 2:
        if mode == "error":
            print(json.dumps({"jsonrpc": "2.0", "id": 2, "error": {"code": -1}}), flush=True)
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "unrelated"}]}}), flush=True)
"""


@pytest.mark.parametrize("mode,expect", [
    ("crash", "no module named fastmcp"),  # stderr explains a crash
    ("silent", "no initialize reply"),
    ("error", "did not answer tools/list"),
    ("wrong", "exposes no memory_retain"),
])
def test_handshake_explains_each_failure(tmp_path, mode, expect):
    from astrocyte.harness.server import handshake

    script = tmp_path / "server.py"
    script.write_text(FAKE_SERVER)
    result = handshake(ServerSpec(sys.executable, (str(script), mode)), timeout=3)
    assert not result.ok and expect in result.detail


def test_handshake_with_a_missing_binary():
    from astrocyte.harness.server import handshake

    result = handshake(ServerSpec("/nonexistent/python", ("-m", "astrocyte.mcp")))
    assert not result.ok and "could not start" in result.detail


# ── host edge cases: refuse, explain, never half-write ───────────────────


@pytest.mark.parametrize("host_cls,cli,manual", [
    (ClaudeCodeHost, "claude", "claude mcp add --scope user astrocyte --"),
    (CodexHost, "codex", "codex mcp add astrocyte --"),
    (GeminiHost, "gemini", "gemini mcp add --scope user astrocyte"),
])
def test_a_cli_missing_from_path_fails_with_the_manual_command(home, host_cls, cli, manual):
    (home / f".{cli}").mkdir()
    outcome = host_cls().install(SPEC)
    assert outcome.status == "failed" and "not on PATH" in outcome.detail and manual in outcome.detail
    assert SPEC.command in host_cls().manual_command(SPEC)


@pytest.mark.parametrize("body,why", [("[1, 2]", "not a JSON object"), ('{"mcpServers": []}', "must be a JSON object")])
def test_json_hosts_refuse_files_of_the_wrong_shape(home, body, why):
    path = home / ".cursor" / "mcp.json"
    path.parent.mkdir()
    path.write_text(body)
    outcome = CursorHost().install(SPEC)
    assert outcome.status == "failed" and why in outcome.detail and path.read_text() == body


def test_dry_run_uninstall_changes_nothing(home):
    (home / ".cursor").mkdir()
    CursorHost().install(SPEC)
    assert CursorHost().uninstall(dry_run=True).status == "planned"
    assert CursorHost().registration() is not None


def test_claude_config_dir_moves_the_registration_file(home, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
    assert ClaudeCodeHost().config_file() == tmp_path / "cc" / ".claude.json"


def test_unknown_harness_key_names_the_valid_ones():
    with pytest.raises(KeyError, match="claude"):
        hosts_mod.host_by_key("vscode")


class TestCodexHooksRefusals:
    def _host(self, home, body: str):
        path = home / ".codex" / "config.toml"
        path.parent.mkdir(exist_ok=True)
        path.write_text(body)
        return CodexHost(), path

    def test_invalid_toml_is_reported_not_rewritten(self, home):
        host, path = self._host(home, "model = [unclosed\n")
        assert host.install_hooks("/x/astrocyte").status == "failed"
        assert host.uninstall_hooks().status == "failed"
        assert path.read_text() == "model = [unclosed\n"

    def test_an_unclosed_marker_block_is_refused(self, home):
        host, path = self._host(home, 'model = "o3"\n# >>> astrocyte hooks >>>\n')
        outcome = host.install_hooks("/x/astrocyte")
        assert outcome.status == "failed" and "closing marker" in outcome.detail

    def test_dry_runs_and_absent_removal(self, home):
        host, path = self._host(home, 'model = "o3"\n')
        assert host.uninstall_hooks().status == "absent"
        assert host.install_hooks("/x/astrocyte", dry_run=True).status == "planned"
        assert path.read_text() == 'model = "o3"\n'
        host.install_hooks("/x/astrocyte")
        assert host.uninstall_hooks(dry_run=True).status == "planned"
        assert all(host.hook_commands().values())


def test_claude_hooks_dry_run_removal_and_invalid_settings(home):
    host = ClaudeCodeHost()
    host.install_hooks("/x/astrocyte")
    assert host.uninstall_hooks(dry_run=True).status == "planned" and all(host.hook_commands().values())
    host.hooks_file().write_text("{ broken")
    assert host.uninstall_hooks().status == "failed"


def test_openai_is_only_chosen_when_its_sdk_is_installed(home, monkeypatch):
    """A config naming a provider that isn't installed wired a server that
    could not start (found by the clean-install smoke test)."""
    monkeypatch.setattr("astrocyte.harness.localconfig.local_embedding_backend", lambda: "fastembed")
    monkeypatch.setitem(sys.modules, "openai", None)
    with pytest.raises(SetupError, match=r"astrocyte\[local\]"):
        choose_providers({"OPENAI_API_KEY": "sk-test"})


@pytest.mark.parametrize("stderr,expected", [
    ('Traceback (most recent call last):\n  File "x.py", line 51, in __init__\n    raise ImportError(\n'
     "ImportError: The 'openai' package is required\n",
     "ImportError: The 'openai' package is required"),
    ("ValueError: bad\n\nThe above exception was the direct cause of the following exception:\n\n"
     "Traceback (most recent call last):\n  File \"y.py\", line 2\nastrocyte.errors.ConfigError: vector_store 'x' not found\n",
     "astrocyte.errors.ConfigError: vector_store 'x' not found"),
    ("just\nsome\nnoise\nlines\n", "some / noise / lines"),
    ("", ""),
])
def test_a_crashed_server_is_explained_by_its_exception_line(stderr, expected):
    from astrocyte.harness.server import _explain

    assert _explain(stderr) == expected


def test_setup_says_copilot_only_recalls(wired_home, capsys):
    """Copilot CLI's turns aren't captured yet: setup must not claim they are."""
    (wired_home / ".copilot").mkdir()
    cmd_setup(_ns())
    out = capsys.readouterr().out
    assert "Copilot CLI: relevant memories (saved by your other agents)" in out
    assert "its own turns are not saved yet" in out


# ── the store is private to its owner ────────────────────────────────────


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_doctor_reports_and_fixes_a_store_others_can_read(home, capsys, monkeypatch):
    """Stores created before v0.16.1 inherited the umask (world-readable)."""
    pytest.importorskip("astrocyte_sqlite")
    data = home / ".local" / "share" / "astrocyte"
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    data.mkdir(parents=True, mode=0o755)
    db = data / "astrocyte.db"
    db.touch(mode=0o644)
    db.chmod(0o644)
    data.chmod(0o755)
    cfg = home / ".config" / "astrocyte" / "astrocyte.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(MINIMAL.replace("vector_store: in_memory", "vector_store: sqlite")
                   + f"vector_store_config:\n  path: {db}\n")
    cmd_doctor(_ns())
    assert "other accounts on this machine can read your memories" in capsys.readouterr().out
    cmd_doctor(_ns(fix=True))
    assert "made private" in capsys.readouterr().out
    assert oct(db.stat().st_mode & 0o777) == "0o600" and oct(data.stat().st_mode & 0o777) == "0o700"
    cmd_doctor(_ns())
    assert "can read your memories" not in capsys.readouterr().out


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_a_store_in_a_directory_of_your_choosing_leaves_the_directory_alone(tmp_path, monkeypatch):
    from astrocyte.harness.privacy import make_private

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    shared = tmp_path / "team-share"
    shared.mkdir(mode=0o755)
    db = shared / "mem.db"
    db.touch()
    db.chmod(0o644)
    assert make_private(db) == [db]
    assert oct(shared.stat().st_mode & 0o777) == "0o755"


# ── first-run conveniences ───────────────────────────────────────────────


def test_setup_suggests_importing_the_projects_agent_files(wired_home, capsys, monkeypatch, tmp_path):
    repo = tmp_path / "proj"
    (repo / ".github").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "CLAUDE.md").write_text("# Conventions")
    (repo / ".github" / "copilot-instructions.md").write_text("# More")
    monkeypatch.chdir(repo)
    cmd_setup(_ns())
    assert "astrocyte memory import CLAUDE.md .github/copilot-instructions.md" in capsys.readouterr().out


def test_setup_outside_a_project_suggests_nothing(wired_home, capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    cmd_setup(_ns())
    assert "memory import" not in capsys.readouterr().out


def _local_embeddings_config(home, monkeypatch):
    """The wired_home config, as if it named local_embeddings (without
    needing the model installed in the test environment)."""
    from astrocyte.config import load_config

    loaded = load_config(str(home / ".config" / "astrocyte" / "astrocyte.yaml"))
    loaded.embedding_provider = "local_embeddings"
    monkeypatch.setattr("astrocyte.config.load_config", lambda path: loaded)


def test_setup_loads_the_local_embedding_model_up_front(wired_home, capsys, monkeypatch):
    """The ~130 MB download otherwise happens during the first session's first prompt."""
    _local_embeddings_config(wired_home, monkeypatch)
    warmed = []

    class Embedder:
        async def embed(self, texts, model=None):
            warmed.append(texts)
            return [[0.0]]

    monkeypatch.setattr("astrocyte.wiring.resolve_llm_provider", lambda config: Embedder())
    cmd_setup(_ns())
    assert warmed and "local embedding model ready" in capsys.readouterr().out


def test_a_failed_warm_up_does_not_fail_setup(wired_home, capsys, monkeypatch):
    _local_embeddings_config(wired_home, monkeypatch)

    def offline(config):
        raise ConnectionError("no network")

    monkeypatch.setattr("astrocyte.wiring.resolve_llm_provider", offline)
    assert cmd_setup(_ns()) == 0
    assert "will load on first use" in capsys.readouterr().out
