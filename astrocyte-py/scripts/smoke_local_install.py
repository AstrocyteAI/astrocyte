#!/usr/bin/env python3
"""Install ``astrocyte[local]`` the way the quick-start says, then use it.

Proves the documented path works for someone without a checkout: the wheels
carry every module, entry point and extra; ``astrocyte setup`` writes a config
and wires an agent; the MCP server starts from that install; ``doctor`` and
``astrocyte memory`` run. Everything happens in a throwaway HOME and tool
directory — nothing touches the caller's agents, config or memories.

    # before a release: from wheels built out of this checkout
    python3 astrocyte-py/scripts/smoke_local_install.py --build

    # after a release: from PyPI, exactly as a new user gets it
    python3 astrocyte-py/scripts/smoke_local_install.py --pypi 0.16.0

No completion calls: completions are configured for OpenAI with a dummy key,
which only fact extraction would use (it fails and retain keeps the text
verbatim). The embedding model is not loaded unless ``--with-models`` is given:
that downloads bge-small (~130 MB) and saves and recalls a memory through the
MCP server. Stdlib only; needs ``uv``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PACKAGES = (REPO / "astrocyte-py", REPO / "adapters-storage-py" / "astrocyte-sqlite")


WINDOWS = os.name == "nt"
# A minimal PATH for the sandboxed CLI: nothing of the caller's, so no agent
# CLI is found and nothing outside the sandbox can be configured.
SYSTEM_PATH = ([os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32"),
                os.environ.get("SYSTEMROOT", r"C:\Windows")] if WINDOWS else ["/usr/bin", "/bin"])


def step(title: str) -> None:
    print(f"\n── {title}", flush=True)


def run(argv: list[str], *, env: dict[str, str] | None = None, cwd: Path | None = None, check: bool = True,
        stdin: str | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(argv, env=env, cwd=cwd, input=stdin, capture_output=True, text=True, timeout=900,
                          encoding="utf-8", errors="replace")
    out = (proc.stdout + proc.stderr).strip()
    if out:
        print("   " + out.replace("\n", "\n   "))
    if check and proc.returncode != 0:
        raise SystemExit(f"✗ {' '.join(argv[:3])}… exited {proc.returncode}")
    return proc


def build(dist: Path, version: str) -> None:
    for pkg in PACKAGES:
        run(["uv", "build", "-q", "--out-dir", str(dist)], cwd=pkg,
            env={**os.environ, "SETUPTOOLS_SCM_PRETEND_VERSION": version})


def mcp_round_trip(python: Path, cfg: Path, env: dict[str, str]) -> None:
    """Save a memory through the installed MCP server, as an agent would."""
    script = f"""
import json, subprocess
p = subprocess.Popen([{str(python)!r}, "-I", "-m", "astrocyte.mcp", "--config", {str(cfg)!r}],
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
def send(m):
    p.stdin.write(json.dumps(m) + "\\n"); p.stdin.flush()
def reply(i):
    while True:
        line = p.stdout.readline()
        if not line: raise SystemExit("server exited")
        m = json.loads(line)
        if m.get("id") == i: return m
send({{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {{"protocolVersion": "2025-06-18",
      "capabilities": {{}}, "clientInfo": {{"name": "smoke", "version": "1"}}}}}}); reply(1)
send({{"jsonrpc": "2.0", "method": "notifications/initialized"}})
send({{"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {{"name": "memory_retain",
      "arguments": {{"content": "Smoke test: the staging deploy freeze is on Thursdays."}}}}}})
result = reply(2)["result"]
p.kill()
assert '\\\\"stored\\\\": true' in json.dumps(result), result
print("retained via MCP")
"""
    run([str(python), "-I", "-c", script], env=env, cwd=Path(env["HOME"]))


def shells() -> list[tuple[str, list[str]]]:
    """Every shell an agent may run a hook command through here (found on the
    caller's PATH: the sandbox PATH is deliberately bare)."""
    if not WINDOWS:
        return [("sh", ["/bin/sh", "-c"])]
    found = [("cmd", [os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "cmd.exe"),
                      "/d", "/s", "/c"])]
    if ps := shutil.which("pwsh") or shutil.which("powershell"):
        found.append(("PowerShell", [ps, "-NoProfile", "-NonInteractive", "-Command"]))
    # Git Bash, the shell Claude Code prefers on Windows: where Claude Code
    # looks for it, then beside the git on PATH (Git\\cmd or Git\\bin).
    candidates = [Path(os.environ[var]) / "Git" / "bin" / "bash.exe"
                  for var in ("ProgramFiles", "ProgramFiles(x86)") if os.environ.get(var)]
    if git := shutil.which("git"):
        here = Path(git).resolve().parent
        candidates += [here / "bash.exe", here.parent / "bin" / "bash.exe"]
    bash = next((c for c in candidates if c.is_file()), None)
    # CI runners have Git for Windows: a missing Git Bash would silently skip
    # the shell that matters most, so it is an error, not a skip.
    assert bash or not os.environ.get("CI"), f"Git Bash not found (looked at {candidates})"
    if bash:
        found.append(("Git Bash", [str(bash), "-c"]))
    return found


def _agy_step(i: int, kind: str, content: str, source: str) -> str:
    return json.dumps({"step_index": i, "source": source, "type": kind, "status": "DONE",
                       "created_at": "2026-10-04T03:03:55Z", "content": content}) + "\n"


def _agy_user(i: int, text: str) -> str:
    return _agy_step(i, "USER_INPUT", f"<USER_REQUEST>\n{text}\n</USER_REQUEST>", "USER_EXPLICIT")


def hooks_round_trip(astrocyte: str, python: Path, home: Path, env: dict[str, str], state: Path) -> None:
    """Automatic memory as an agent drives it: the registered hook commands,
    run through each shell, capture a turn into the background process, and a
    new conversation opens with it. Antigravity's hooks need no agent CLI to
    register (a JSON file), so they stand in for every agent."""
    run([astrocyte, "setup", "--antigravity"], env=env, cwd=home)
    entry = json.loads((home / ".gemini" / "config" / "hooks.json").read_text(encoding="utf-8"))["astrocyte"]
    stop, pre = entry["Stop"][0]["command"], entry["PreInvocation"][0]["command"]
    print(f"   hook command: {stop}")
    # doctor pings every installed hook through every shell; a failure exits 1.
    run([astrocyte, "doctor", "--skip-models"], env=env, cwd=home)

    project = home / "proj"
    project.mkdir()

    def payload(conversation: str, text: str) -> str:
        transcript = home / f"{conversation}.jsonl"
        transcript.write_text(text, encoding="utf-8", newline="\n")
        return json.dumps({"conversationId": conversation, "workspacePaths": [str(project)],
                           "transcriptPath": str(transcript)})

    first_shell = shells()[0]
    run([*first_shell[1], stop], env=env, stdin=payload(
        "conv-a", _agy_user(0, "what day is the staging deploy freeze?")
        + _agy_step(1, "PLANNER_RESPONSE", "Thursdays, agreed in the staging review.", "MODEL")))
    spool = state / "astrocyte" / "spool"
    deadline = time.monotonic() + 240
    while any(spool.glob("*.json")):
        assert time.monotonic() < deadline, "the captured turn was never stored"
        time.sleep(1)
    print("   captured turn stored by the background process")
    try:
        for i, (name, shell) in enumerate(shells()):
            out = run([*shell, pre], env=env, stdin=payload(f"conv-{i}", _agy_user(0, "starting on the release notes")))
            context = json.loads(out.stdout.strip().splitlines()[-1])["injectSteps"][0]["ephemeralMessage"]
            assert "Where you left off" in context and "Thursdays" in context, (name, context)
            print(f"   {name}: a new conversation opens with where the last one left off")
    finally:
        pid = run([str(python), "-I", "-c", "from astrocyte.harness import agentd; r = agentd.request('ping'); "
                   "print(r['pid'] if r else '')"], env=env).stdout.strip()
        if pid:
            os.kill(int(pid), 15)


def main() -> int:
    if WINDOWS:  # piped output there is cp1252, which has no ─ or ✓
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--build", action="store_true", help="build wheels from this checkout and install those")
    src.add_argument("--pypi", metavar="VERSION", help="install this published version from PyPI")
    src.add_argument("--wheels", type=Path, metavar="DIR", help="install from wheels already in DIR")
    # Must satisfy the packages' pins on each other (astrocyte[sqlite] wants
    # astrocyte-sqlite>=0.15.2,<2; it wants astrocyte<2) and be no
    # pre-release, or uv would pick a published release over these wheels.
    ap.add_argument("--version", default="1.99.0+smoke", help="version to stamp on --build wheels")
    ap.add_argument("--with-models", action="store_true", help="also load the embedding model and round-trip a memory")
    ap.add_argument("--keep", action="store_true", help="keep the sandbox for inspection")
    args = ap.parse_args()
    if not shutil.which("uv"):
        raise SystemExit("uv is required: https://docs.astral.sh/uv/")

    # Short root: the agent daemon's AF_UNIX socket path is capped at ~104 bytes on macOS.
    root = Path(tempfile.mkdtemp(prefix="astro-smoke-", dir="/tmp" if os.name != "nt" else None))
    try:
        home, tools, bin_dir = root / "home", root / "tools", root / "bin"
        (home / ".cursor").mkdir(parents=True)  # one JSON-configured agent to wire; no CLI needed

        requirement = "astrocyte[local]"
        install = ["uv", "tool", "install", "--python", "3.12"]
        if args.pypi:
            requirement += f"=={args.pypi}"
        else:
            dist = args.wheels or root / "dist"
            if args.build:
                step(f"build wheels ({args.version})")
                build(dist, args.version)
                requirement += f"=={args.version}"
            install += ["--find-links", str(dist)]

        step(f"uv tool install '{requirement}'")
        tool_env = {**os.environ, "UV_TOOL_DIR": str(tools), "UV_TOOL_BIN_DIR": str(bin_dir)}
        # A just-published version reaches PyPI's CDN edges at different times:
        # on v0.18.0 the index the wait step polled had it, and the install a
        # minute later, served by another edge, did not. Retry only that case.
        for attempt in range(1, (8 if args.pypi else 1) + 1):
            proc = run([*install, requirement], env={**tool_env, "UV_REFRESH": "1"}, check=False)
            if proc.returncode == 0:
                break
            if attempt == 8 or not args.pypi or "No solution found" not in proc.stdout + proc.stderr:
                raise SystemExit(f"✗ uv tool install… exited {proc.returncode}")
            print(f"   {requirement} not visible to this index edge yet; retrying in 30 s ({attempt}/8)", flush=True)
            time.sleep(30)
        python = tools / "astrocyte" / ("Scripts/python.exe" if WINDOWS else "bin/python")
        versions = run([str(python), "-I", "-c", "import importlib.metadata as m, json; print(json.dumps("
                        "{p: m.version(p) for p in ('astrocyte', 'astrocyte-sqlite', 'fastembed', 'fastmcp')}))"])
        assert "astrocyte-sqlite" in versions.stdout
        torch = run([str(python), "-I", "-c", "import torch"], check=False)
        assert torch.returncode != 0, "astrocyte[local] must not pull in torch"

        # A clean environment: no PATH entries of the caller's, so no agent
        # CLI is found and nothing outside the sandbox can be configured.
        env = {"HOME": str(home), "PATH": os.pathsep.join([str(bin_dir), *SYSTEM_PATH]),
               "XDG_STATE_HOME": str(root / "state"), "XDG_CACHE_HOME": str(root / "cache"),
               "OPENAI_API_KEY": "sk-smoke-not-used", "TERM": "dumb"}
        if WINDOWS:
            # `~` is USERPROFILE there, not HOME; and Windows processes need
            # their system variables (Python can't even seed random without SYSTEMROOT).
            env |= {"USERPROFILE": str(home), "APPDATA": str(home / "AppData" / "Roaming"),
                    "LOCALAPPDATA": str(home / "AppData" / "Local"),
                    **{k: os.environ[k] for k in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")
                       if k in os.environ}}
        astrocyte = shutil.which("astrocyte", path=str(bin_dir))
        assert astrocyte, f"astrocyte was not installed into {bin_dir}"
        cfg = home / ".config" / "astrocyte" / "astrocyte.yaml"

        step("astrocyte setup --cursor")
        run([astrocyte, "setup", "--cursor"], env=env, cwd=home)
        text = cfg.read_text()
        assert "vector_store: sqlite" in text and "embedding_provider: local_embeddings" in text, text
        wired = json.loads((home / ".cursor" / "mcp.json").read_text())["mcpServers"]["astrocyte"]
        # Compare resolved paths: on macOS /tmp is a symlink to /private/tmp.
        assert Path(wired["command"]).resolve() == python.resolve(), wired
        assert wired["args"][:3] == ["-I", "-m", "astrocyte.mcp"], wired

        step("astrocyte doctor")
        # --skip-models: the completion probe would call OpenAI with the dummy key.
        run([astrocyte, "doctor", "--skip-models"], env=env, cwd=home)

        if args.with_models:
            step("memory_retain through the MCP server")
            env.pop("XDG_CACHE_HOME")  # reuse a downloaded model when the caller has one
            mcp_round_trip(python, cfg, env)
            step("astrocyte memory search")
            found = run([astrocyte, "memory", "search", "when is the deploy freeze"], env=env, cwd=home)
            assert "Thursdays" in found.stdout

        step("automatic memory: hooks → background process → next session")
        hooks_round_trip(astrocyte, python, home, env, root / "state")

        step("astrocyte memory / banks")
        run([astrocyte, "memory"], env=env, cwd=home)
        run([astrocyte, "memory", "banks"], env=env, cwd=home)

        step("astrocyte uninstall --cursor")
        run([astrocyte, "uninstall", "--cursor"], env=env, cwd=home)
        assert "astrocyte" not in json.loads((home / ".cursor" / "mcp.json").read_text()).get("mcpServers", {})
        print("\n✓ local install works end to end")
        return 0
    finally:
        if args.keep:
            print(f"sandbox kept: {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
