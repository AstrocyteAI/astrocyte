"""Tests for ClaudeCliProvider — the subprocess-backed completion provider.

The CLI is never actually invoked here: ``asyncio.create_subprocess_exec`` is
patched so every test is hermetic, fast, and safe to run while a real bench is
using the CLI's subscription quota.

Coverage focus is the contract that the bench depends on:
  - prompt rendering (system/user flattening, multi-turn labelling)
  - JSON-mode emulation for ``response_format``
  - the security invariant: ANTHROPIC_API_KEY never reaches the subprocess
  - retry / timeout / non-zero-exit behaviour
  - loud failure on unsupported features (tools, embed)
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from astrocyte.providers.claude_cli import ClaudeCliProvider, _render_prompt
from astrocyte.types import Message


class _FakeProc:
    """Stand-in for asyncio subprocess: records stdin, returns canned output."""

    def __init__(self, stdout: bytes = b"OK", stderr: bytes = b"", returncode: int = 0,
                 hang: bool = False) -> None:
        self._stdout, self._stderr = stdout, stderr
        self.returncode = returncode
        self._hang = hang
        self.stdin_seen: bytes | None = None
        self.killed = False

    async def communicate(self, data: bytes | None = None) -> tuple[bytes, bytes]:
        self.stdin_seen = data
        if self._hang:
            await asyncio.sleep(3600)
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


@pytest.fixture
def spawn(monkeypatch):
    """Patch subprocess creation; expose the recorded argv/env/proc."""
    calls: list[dict[str, Any]] = []

    def _install(proc_factory):
        async def _fake_exec(*argv, **kwargs):
            proc = proc_factory(len(calls))
            calls.append({"argv": argv, "env": kwargs.get("env", {}),
                          "cwd": kwargs.get("cwd"), "proc": proc})
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        return calls

    return _install


def _provider(**kw: Any) -> ClaudeCliProvider:
    # binary= bypasses PATH lookup so tests pass on machines without the CLI.
    return ClaudeCliProvider(binary="/usr/bin/true", **kw)


class TestPromptRendering:
    def test_single_user_message_is_bare_prompt(self):
        out = _render_prompt([Message(role="user", content="hello")], None)
        assert out == "hello"

    def test_system_is_prepended_with_separator(self):
        out = _render_prompt(
            [Message(role="system", content="be terse"), Message(role="user", content="hi")],
            None,
        )
        assert out.startswith("be terse")
        assert "hi" in out
        assert "---" in out

    def test_multi_turn_gets_role_labels(self):
        out = _render_prompt(
            [Message(role="user", content="q1"), Message(role="assistant", content="a1"),
             Message(role="user", content="q2")],
            None,
        )
        assert "[user]" in out and "[assistant]" in out
        assert out.index("q1") < out.index("a1") < out.index("q2")

    def test_json_object_response_format_appends_instruction(self):
        out = _render_prompt([Message(role="user", content="x")], {"type": "json_object"})
        assert "valid JSON object" in out

    def test_json_schema_response_format_embeds_the_schema(self):
        schema = {"name": "verdict", "schema": {"type": "object"}}
        out = _render_prompt(
            [Message(role="user", content="x")],
            {"type": "json_schema", "json_schema": schema},
        )
        assert "json schema" in out.lower()
        assert "verdict" in out  # schema serialised into the prompt

    def test_list_content_parts_are_flattened(self):
        class _Part:
            def __init__(self, text): self.text = text

        out = _render_prompt(
            [Message(role="user", content=[_Part("alpha"), _Part("beta")])], None,
        )
        assert "alpha" in out and "beta" in out


class TestComplete:
    @pytest.mark.asyncio
    async def test_returns_stdout_text_and_model(self, spawn):
        spawn(lambda _: _FakeProc(stdout=b"  hello world  \n"))
        result = await _provider(model="haiku").complete(
            [Message(role="user", content="hi")],
        )
        assert result.text == "hello world"
        assert result.model == "haiku"
        assert result.tool_calls is None
        # Print mode reports no usage — must not fabricate token counts.
        assert result.usage is None

    @pytest.mark.asyncio
    async def test_prompt_is_passed_on_stdin_not_argv(self, spawn):
        """argv has hard length limits; mt_8192 contexts must go via stdin."""
        calls = spawn(lambda _: _FakeProc())
        big = "x" * 50_000
        await _provider().complete([Message(role="user", content=big)])
        assert calls[0]["proc"].stdin_seen == big.encode()
        assert not any(big in str(a) for a in calls[0]["argv"])

    @pytest.mark.asyncio
    async def test_invokes_print_mode_with_configured_model(self, spawn):
        calls = spawn(lambda _: _FakeProc())
        await _provider(model="sonnet").complete([Message(role="user", content="hi")])
        argv = calls[0]["argv"]
        assert "-p" in argv
        assert "--model" in argv and argv[argv.index("--model") + 1] == "sonnet"
        assert "--max-turns" in argv and argv[argv.index("--max-turns") + 1] == "1"

    @pytest.mark.asyncio
    async def test_anthropic_api_key_is_stripped_from_subprocess_env(self, spawn, monkeypatch):
        """Security invariant: calls must bill the CLI subscription, never a key."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-must-not-leak")
        monkeypatch.setenv("PATH", "/usr/bin")  # control: other vars pass through
        calls = spawn(lambda _: _FakeProc())
        await _provider().complete([Message(role="user", content="hi")])
        env = calls[0]["env"]
        assert "ANTHROPIC_API_KEY" not in env
        assert env.get("PATH") == "/usr/bin"

    @pytest.mark.asyncio
    async def test_runs_in_private_cwd_not_a_project_dir(self, spawn):
        """A project cwd would leak CLAUDE.md context into bench prompts."""
        calls = spawn(lambda _: _FakeProc())
        p = _provider()
        await p.complete([Message(role="user", content="hi")])
        cwd = calls[0]["cwd"]
        assert cwd == p._cwd
        import pathlib
        assert not (pathlib.Path(cwd) / "CLAUDE.md").exists()
        assert not any(pathlib.Path(cwd).iterdir())  # empty scratch dir

    @pytest.mark.asyncio
    async def test_retries_then_succeeds_on_transient_nonzero_exit(self, spawn):
        calls = spawn(lambda n: _FakeProc(returncode=1, stderr=b"boom") if n == 0
                      else _FakeProc(stdout=b"recovered"))
        result = await _provider(max_retries=3).complete([Message(role="user", content="hi")])
        assert result.text == "recovered"
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_raises_after_exhausting_retries(self, spawn):
        spawn(lambda _: _FakeProc(returncode=1, stderr=b"persistent failure"))
        with pytest.raises(RuntimeError, match="persistent failure"):
            await _provider(max_retries=2).complete([Message(role="user", content="hi")])

    @pytest.mark.asyncio
    async def test_timeout_kills_process_and_retries(self, spawn):
        calls = spawn(lambda n: _FakeProc(hang=True) if n == 0 else _FakeProc(stdout=b"ok"))
        result = await _provider(timeout=0.05, max_retries=2).complete(
            [Message(role="user", content="hi")],
        )
        assert result.text == "ok"
        assert calls[0]["proc"].killed is True  # no orphaned subprocess

    @pytest.mark.asyncio
    async def test_per_call_model_override_is_ignored(self, spawn):
        """Legacy call sites pass model='gpt-4o-mini'; CLI model must win."""
        calls = spawn(lambda _: _FakeProc())
        await _provider(model="haiku").complete(
            [Message(role="user", content="hi")], model="gpt-4o-mini",
        )
        argv = calls[0]["argv"]
        assert argv[argv.index("--model") + 1] == "haiku"

    @pytest.mark.asyncio
    async def test_concurrency_is_bounded_by_semaphore(self, spawn, monkeypatch):
        monkeypatch.setenv("ASTROCYTE_CLAUDE_CLI_MAX_CONCURRENCY", "2")
        live = 0
        peak = 0

        class _Counting(_FakeProc):
            async def communicate(self, data=None):
                nonlocal live, peak
                live += 1
                peak = max(peak, live)
                await asyncio.sleep(0.01)
                live -= 1
                return b"ok", b""

        spawn(lambda _: _Counting())
        p = _provider()
        await asyncio.gather(*(
            p.complete([Message(role="user", content=f"q{i}")]) for i in range(8)
        ))
        assert peak <= 2


class TestUnsupportedSurfaces:
    @pytest.mark.asyncio
    async def test_tools_raise_rather_than_silently_dropping(self):
        from astrocyte.types import ToolDefinition

        tool = ToolDefinition(name="search", description="d", parameters={})
        with pytest.raises(NotImplementedError, match="function calling"):
            await _provider().complete([Message(role="user", content="hi")], tools=[tool])

    @pytest.mark.asyncio
    async def test_embed_raises_and_points_at_the_composite_path(self):
        with pytest.raises(NotImplementedError, match="CompositeLLMProvider"):
            await _provider().embed(["text"])

    def test_missing_binary_fails_fast_at_construction(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_CLI_BIN", raising=False)
        monkeypatch.setattr("shutil.which", lambda _: None)
        with pytest.raises(RuntimeError, match="Claude Code CLI"):
            ClaudeCliProvider()

    def test_capabilities_declare_text_only_no_embeddings(self):
        caps = _provider().capabilities()
        assert caps.supports_multimodal_completion is False
        assert caps.supports_batch_embed is False
        assert caps.modalities_supported == ("text",)
