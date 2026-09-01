"""Tests for the Claude CLI provider's pure helpers.

Subprocess execution is exercised by the bench harness (live CLI); these
tests cover the deterministic parts: prompt rendering (system folding,
role labelling, JSON-mode instructions) and JSON-output normalization
(the emulated-response_format hardening added after the claude-native
smoke surfaced fenced/prose-wrapped outputs on section_link_extraction).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from astrocyte.providers.claude_cli import _normalize_json_output, _render_prompt
from astrocyte.types import Message


class TestNormalizeJsonOutput:
    def test_bare_object_passes_through(self):
        assert _normalize_json_output('{"a": 1}') == '{"a": 1}'

    def test_fenced_json_block_stripped(self):
        assert _normalize_json_output('```json\n{"links": []}\n```') == '{"links": []}'

    def test_bare_fence_stripped(self):
        assert _normalize_json_output("```\n[1, 2, 3]\n```") == "[1, 2, 3]"

    def test_prose_wrapped_object_sliced(self):
        raw = 'Here is the result:\n{"links": [{"to_line": 5}]}\nHope that helps!'
        assert _normalize_json_output(raw) == '{"links": [{"to_line": 5}]}'

    def test_prose_wrapped_array_sliced(self):
        assert _normalize_json_output("The answer is [1, 2] as requested.") == "[1, 2]"

    def test_fence_with_prose_inside_falls_through_to_slice(self):
        raw = '```json\nSure! {"a": {"b": 2}}\n```'
        assert _normalize_json_output(raw) == '{"a": {"b": 2}}'

    def test_unrecoverable_returns_none(self):
        assert _normalize_json_output("no json here at all") is None

    def test_empty_returns_none(self):
        assert _normalize_json_output("") is None

    def test_nested_braces_kept_intact(self):
        raw = 'x {"outer": {"inner": [1, {"deep": true}]}} y'
        out = _normalize_json_output(raw)
        assert out is not None
        assert json.loads(out) == {"outer": {"inner": [1, {"deep": True}]}}


class TestRenderPrompt:
    def test_single_user_message_is_bare(self):
        prompt = _render_prompt([Message(role="user", content="hello")], None)
        assert prompt == "hello"

    def test_system_floats_to_top(self):
        prompt = _render_prompt(
            [
                Message(role="system", content="Be terse."),
                Message(role="user", content="hello"),
            ],
            None,
        )
        assert prompt.startswith("Be terse.")
        assert "hello" in prompt

    def test_multi_turn_gets_role_labels(self):
        prompt = _render_prompt(
            [
                Message(role="user", content="q1"),
                Message(role="assistant", content="a1"),
                Message(role="user", content="q2"),
            ],
            None,
        )
        assert "[user]" in prompt and "[assistant]" in prompt

    def test_json_object_instruction_appended(self):
        prompt = _render_prompt(
            [Message(role="user", content="extract")],
            {"type": "json_object"},
        )
        assert "valid JSON object only" in prompt

    def test_json_schema_included_verbatim(self):
        schema = {"name": "links", "schema": {"type": "object"}}
        prompt = _render_prompt(
            [Message(role="user", content="extract")],
            {"type": "json_schema", "json_schema": schema},
        )
        assert "json schema" in prompt.lower()
        assert '"links"' in prompt


class TestProviderGuards:
    @pytest.mark.asyncio
    async def test_tools_raise_not_implemented(self):
        from astrocyte.providers.claude_cli import ClaudeCliProvider
        from astrocyte.types import ToolDefinition

        provider = ClaudeCliProvider(model="haiku", binary="/bin/true")
        with pytest.raises(NotImplementedError):
            await provider.complete(
                [Message(role="user", content="x")],
                tools=[ToolDefinition(name="t", description="d", parameters={})],
            )

    @pytest.mark.asyncio
    async def test_embed_raises_not_implemented(self):
        from astrocyte.providers.claude_cli import ClaudeCliProvider

        provider = ClaudeCliProvider(model="haiku", binary="/bin/true")
        with pytest.raises(NotImplementedError):
            await provider.embed(["x"])


class TestRateLimitDetection:
    @pytest.mark.parametrize(
        "text",
        [
            "Claude AI usage limit reached. Your limit will reset at 3pm.",
            "Error: rate limit exceeded",
            "429 Too Many Requests",
            "API is temporarily overloaded, try again later",
            "You have exceeded your quota",
        ],
    )
    def test_throttle_signatures_detected(self, text):
        from astrocyte.providers.claude_cli import _looks_rate_limited

        assert _looks_rate_limited(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "SyntaxError: unexpected token",
            "command not found",
            "",
            "the model returned an empty response",
        ],
    )
    def test_ordinary_errors_not_flagged(self, text):
        from astrocyte.providers.claude_cli import _looks_rate_limited

        assert _looks_rate_limited(text) is False


class TestCircuitBreaker:
    @pytest.mark.asyncio
    async def test_stays_closed_below_threshold(self):
        from astrocyte.providers.claude_cli import _CircuitBreaker

        cb = _CircuitBreaker(threshold=3, cooldown=60.0)
        for _ in range(2):
            await cb.record_failure()
        # Closed: returns immediately rather than sleeping.
        await asyncio.wait_for(cb.await_closed(), timeout=0.5)

    @pytest.mark.asyncio
    async def test_opens_at_threshold(self):
        from astrocyte.providers.claude_cli import _CircuitBreaker

        cb = _CircuitBreaker(threshold=3, cooldown=60.0)
        for _ in range(3):
            await cb.record_failure()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(cb.await_closed(), timeout=0.3)

    @pytest.mark.asyncio
    async def test_success_closes_and_resets_cooldown(self):
        from astrocyte.providers.claude_cli import _CircuitBreaker

        cb = _CircuitBreaker(threshold=2, cooldown=60.0)
        for _ in range(2):
            await cb.record_failure()
        await cb.record_success()
        await asyncio.wait_for(cb.await_closed(), timeout=0.5)
        assert cb._cooldown == 60.0

    @pytest.mark.asyncio
    async def test_cooldown_doubles_on_repeat_open(self):
        from astrocyte.providers.claude_cli import _CircuitBreaker

        cb = _CircuitBreaker(threshold=1, cooldown=10.0, max_cooldown=100.0)
        await cb.record_failure()
        assert cb._cooldown == 20.0
        await cb.record_failure()
        assert cb._cooldown == 40.0

    @pytest.mark.asyncio
    async def test_cooldown_capped(self):
        from astrocyte.providers.claude_cli import _CircuitBreaker

        cb = _CircuitBreaker(threshold=1, cooldown=50.0, max_cooldown=60.0)
        for _ in range(5):
            await cb.record_failure()
        assert cb._cooldown == 60.0


class TestProviderDefaults:
    def test_safe_defaults(self):
        from astrocyte.providers.claude_cli import ClaudeCliProvider

        p = ClaudeCliProvider(model="haiku", binary="/bin/true")
        assert p._sem._value == 4, "concurrency must default to the tested value"
        assert p._rate_limit_backoff >= 60.0, "throttle backoff must be minutes-scale"
        assert p._breaker._threshold == 5
