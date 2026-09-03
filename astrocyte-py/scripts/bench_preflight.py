"""Fail fast when a bench run's model config can't actually work.

Runs before the expensive phases (DB reset, ingest). Without it a
misconfigured run dies mid-ingest — which is how 2026-09-01's attempt burned
an hour producing a benchmark over empty memories after OpenAI returned 429
on every extraction call.

Validates only what the run will actually use, then exits non-zero with a
remediation hint.

    uv run python -m scripts.bench_preflight
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from typing import NoReturn


def _fail(msg: str, hint: str) -> NoReturn:
    print(f"\n  [preflight] FAIL — {msg}\n  [preflight] fix: {hint}\n", file=sys.stderr)
    sys.exit(2)


def _check_openai(role: str, model: str, *, kind: str = "chat") -> None:
    """Probe the endpoint this role will actually call.

    ``kind="embed"`` hits /v1/embeddings — a chat probe says nothing about
    whether an embedding model name is valid, and the pipeline uses both.
    """
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        _fail(f"{role} uses openai ({model}) but OPENAI_API_KEY is unset",
              "run under `doppler run --`, or switch provider to claude-cli")
    if kind == "embed":
        url = "https://api.openai.com/v1/embeddings"
        body = {"model": model, "input": "hi"}
    else:
        url = "https://api.openai.com/v1/chat/completions"
        body = {"model": model, "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:200]
        if e.code == 429:
            _fail(f"{role} model {model} returned 429 (no credits / rate limited)",
                  "add OpenAI credits, or use MEM0_HARNESS_PROVIDER=claude-cli "
                  "ASTROCYTE_LLM_PROVIDER=claude_cli ASTROCYTE_EMBEDDING_PROVIDER=local_embeddings")
        _fail(f"{role} model {model} rejected: HTTP {e.code} {body}", "check model name and key")
    except Exception as e:  # noqa: BLE001
        _fail(f"{role} model {model} unreachable: {e}", "check network / key")
    print(f"  [preflight] {role}: openai/{model} OK")


def _check_claude_cli(role: str, model: str) -> None:
    binary = os.environ.get("CLAUDE_CLI_BIN") or shutil.which("claude")
    if not binary:
        _fail(f"{role} uses claude-cli but the `claude` binary is not on PATH",
              "install Claude Code, or set CLAUDE_CLI_BIN=/path/to/claude")
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    with tempfile.TemporaryDirectory(prefix="preflight-") as cwd:
        try:
            r = subprocess.run(
                [binary, "-p", "--model", model, "--output-format", "text", "--max-turns", "1"],
                input=b"Reply with exactly: OK", capture_output=True, timeout=120, cwd=cwd, env=env)
        except subprocess.TimeoutExpired:
            _fail(f"{role} claude-cli ({model}) timed out after 120s", "check `claude -p` works manually")
    if r.returncode != 0:
        out = (r.stderr.decode("utf-8", "replace") + r.stdout.decode("utf-8", "replace")).strip()[:200]
        _fail(f"{role} claude-cli ({model}) exited {r.returncode}: {out or '<no output>'}",
              "run `claude -p --model haiku` manually; check login and usage limits")
    print(f"  [preflight] {role}: claude-cli/{model} OK")


def main() -> int:
    print("  [preflight] validating bench model config...")
    ans_provider = os.environ.get("MEM0_HARNESS_PROVIDER", "openai").lower()
    ans_model = os.environ.get("MEM0_HARNESS_ANSWERER_MODEL", "gpt-4o-mini")
    judge_model = os.environ.get("MEM0_HARNESS_JUDGE_MODEL", ans_model)
    pipeline = os.environ.get("ASTROCYTE_LLM_PROVIDER", "openai").lower()

    # The pipeline provider's constructor kwargs decide which models it uses
    # (both OpenAIProvider.model and .embedding_model are configurable), so
    # read them rather than assuming defaults — validating a hardcoded model
    # passes preflight and then dies mid-ingest, which is the exact failure
    # this script exists to prevent.
    raw_cfg = os.environ.get("ASTROCYTE_LLM_PROVIDER_CONFIG") or "{}"
    cfg: dict = {}
    try:
        cfg = json.loads(raw_cfg)
    except json.JSONDecodeError as e:
        _fail(f"ASTROCYTE_LLM_PROVIDER_CONFIG is not valid JSON: {e}",
              'pass a JSON object in single quotes, e.g. \'{"model": "haiku"}\'')
    if not isinstance(cfg, dict):
        _fail("ASTROCYTE_LLM_PROVIDER_CONFIG must be a JSON object",
              'pass a JSON object in single quotes, e.g. \'{"model": "haiku"}\'')

    if ans_provider in ("claude-cli", "claude_cli"):
        _check_claude_cli("answerer/judge", ans_model)
        if judge_model != ans_model:
            _check_claude_cli("judge", judge_model)
    else:
        _check_openai("answerer", ans_model)
        if judge_model != ans_model:
            _check_openai("judge", judge_model)

    if pipeline in ("claude-cli", "claude_cli"):
        _check_claude_cli("pipeline", cfg.get("model", "haiku"))
    else:
        _check_openai("pipeline", cfg.get("model", "gpt-4o-mini"))

    embed = os.environ.get("ASTROCYTE_EMBEDDING_PROVIDER")
    if embed:
        try:
            from astrocyte._discovery import resolve_provider
            resolve_provider(embed.replace("-", "_"), "llm_providers")
            print(f"  [preflight] embeddings: {embed} resolvable OK")
        except Exception as e:  # noqa: BLE001
            _fail(f"embedding provider {embed!r} does not resolve: {e}",
                  "check the entry point name and that its package is installed")
    elif pipeline in ("claude-cli", "claude_cli"):
        _fail("pipeline is claude-cli but no ASTROCYTE_EMBEDDING_PROVIDER is set "
              "(the CLI has no embeddings surface)",
              "set ASTROCYTE_EMBEDDING_PROVIDER=local_embeddings")
    else:
        # No split embed provider means the pipeline provider does the
        # embedding, under its own embedding_model kwarg. The chat probe above
        # says nothing about whether that model name is valid.
        _check_openai("pipeline embeddings",
                      cfg.get("embedding_model", "text-embedding-3-small"),
                      kind="embed")

    print("  [preflight] all checks passed\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
