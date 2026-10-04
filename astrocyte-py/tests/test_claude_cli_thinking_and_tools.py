"""claude_cli: built-in tools are disabled and the thinking budget is configurable.

Both come from measurements on 2026-10-03:

* ``--tools ""`` — Claude Code's built-in tool definitions were ~26,500 of the
  ~33,000 input tokens on every call (33,142 -> 6,676 with the flag). This
  provider never uses them (``complete(tools=...)`` raises), and a model that
  tried one hit ``--max-turns 1`` and failed with "Reached max turns (1)".
* ``max_thinking_tokens`` -> ``MAX_THINKING_TOKENS`` — ~85% of a structured
  extraction call's output was hidden extended thinking. With it off: 31 s
  instead of 68 s, 55% fewer output tokens, and (with metadata joined by
  chunk_index) no alignment or entity-grounding penalty. Default stays
  unchanged because that evidence covers extraction only.

Same fake-binary technique as test_claude_cli_provider: a stand-in ``claude``
records the argv and environment the provider really passes.
"""

from __future__ import annotations

import json

import pytest
from platform_compat import write_executable

from astrocyte.providers.claude_cli import ClaudeCliProvider
from astrocyte.types import Message


@pytest.fixture
def fake_claude(tmp_path):
    record = tmp_path / "call.json"
    fake = write_executable(
        tmp_path, "claude",
        "import json, os, sys\n"
        "sys.stdin.read()\n"
        f"json.dump({{'argv': sys.argv[1:], 'thinking': os.environ.get('MAX_THINKING_TOKENS')}},"
        f" open({str(record)!r}, 'w'))\n"
        "print('OK')\n",
    )

    async def call(**kwargs) -> dict:
        reply = await ClaudeCliProvider(model="haiku", binary=str(fake), **kwargs).complete(
            [Message(role="user", content="hi")]
        )
        assert reply.text.strip() == "OK"
        return json.loads(record.read_text())

    return call


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("MAX_THINKING_TOKENS", raising=False)
    monkeypatch.delenv("ASTROCYTE_CLAUDE_CLI_MAX_THINKING_TOKENS", raising=False)


class TestBuiltInToolsDisabled:
    async def test_tools_flag_is_passed_with_an_empty_value(self, fake_claude):
        argv = (await fake_claude())["argv"]
        i = argv.index("--tools")
        assert argv[i + 1] == "", 'expected --tools "" (all built-in tools disabled)'

    async def test_hermetic_flags_from_eb4fbea_are_kept(self, fake_claude):
        argv = (await fake_claude())["argv"]
        assert "--strict-mcp-config" in argv
        assert json.loads(argv[argv.index("--settings") + 1]) == {"disableAllHooks": True}


class TestThinkingBudget:
    async def test_default_leaves_the_cli_default(self, fake_claude):
        assert (await fake_claude())["thinking"] is None

    async def test_zero_turns_thinking_off(self, fake_claude):
        assert (await fake_claude(max_thinking_tokens=0))["thinking"] == "0"

    async def test_explicit_budget_is_passed_through(self, fake_claude):
        assert (await fake_claude(max_thinking_tokens=2048))["thinking"] == "2048"

    async def test_env_var_configures_it(self, fake_claude, monkeypatch):
        monkeypatch.setenv("ASTROCYTE_CLAUDE_CLI_MAX_THINKING_TOKENS", "0")
        assert (await fake_claude())["thinking"] == "0"

    async def test_explicit_argument_beats_env(self, fake_claude, monkeypatch):
        monkeypatch.setenv("ASTROCYTE_CLAUDE_CLI_MAX_THINKING_TOKENS", "0")
        assert (await fake_claude(max_thinking_tokens=512))["thinking"] == "512"

    def test_negative_budget_is_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            ClaudeCliProvider(model="haiku", binary=str(tmp_path / "x"), max_thinking_tokens=-1)

    def test_reachable_from_yaml_provider_config(self, tmp_path):
        """`llm_provider_config: {max_thinking_tokens: 0}` must reach the constructor."""
        from astrocyte.wiring import instantiate_provider

        p = instantiate_provider(
            "claude_cli", "llm_providers",
            {"model": "haiku", "binary": str(tmp_path / "x"), "max_thinking_tokens": 0},
        )
        assert p._max_thinking_tokens == 0
