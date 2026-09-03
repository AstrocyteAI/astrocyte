"""Tests for the provider-agnostic OpenAI-shaped shim.

These pin the exact wire contract AML's ``pipeline.py complete()`` depends
on, so a provider swap can never silently break the answer/judge halves.
No network, no LLM, no subprocess: a stub provider stands in for the SPI.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from aml_selfeval.shim import build_provider, create_app


@dataclass
class _Usage:
    input_tokens: int = 11
    output_tokens: int = 7


@dataclass
class _Completion:
    text: str
    model: str = "stub-model"
    usage: Any = None


class _StubProvider:
    """Stands in for any SPI-resolved provider."""

    def __init__(self, text: str = "hello", **kwargs: Any):
        self.text = text
        self.kwargs = kwargs
        self.calls: list[dict[str, Any]] = []

    async def complete(self, messages: list[Any], **kw: Any) -> _Completion:
        self.calls.append({"messages": messages, **kw})
        return _Completion(text=self.text, usage=_Usage())


class _ExplodingProvider:
    async def complete(self, messages: list[Any], **kw: Any) -> _Completion:
        raise RuntimeError("provider is down")


def _client(provider: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(provider=provider)),
        base_url="http://test",
    )


def _payload(prompt: str = "What pet?") -> dict[str, Any]:
    """Exactly the body AML's pipeline sends."""
    return {
        "model": "haiku",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }


class TestWireContract:
    @pytest.mark.asyncio
    async def test_response_has_the_shape_the_pipeline_indexes(self):
        """pipeline reads json()["choices"][0]["message"]["content"]."""
        async with _client(_StubProvider(text="a beagle")) as c:
            r = await c.post("/v1/chat/completions", json=_payload())
        assert r.status_code == 200
        assert r.json()["choices"][0]["message"]["content"] == "a beagle"

    @pytest.mark.asyncio
    async def test_both_base_url_spellings_are_mounted(self):
        """ANSWER_API_BASE may or may not carry the /v1 suffix."""
        async with _client(_StubProvider(text="ok")) as c:
            with_v1 = await c.post("/v1/chat/completions", json=_payload())
            without = await c.post("/chat/completions", json=_payload())
        assert with_v1.status_code == without.status_code == 200

    @pytest.mark.asyncio
    async def test_bearer_auth_header_is_accepted_when_no_key_configured(self):
        """The pipeline always sends Bearer, even with an empty key."""
        async with _client(_StubProvider()) as c:
            r = await c.post(
                "/v1/chat/completions",
                json=_payload(),
                headers={"Authorization": "Bearer "},
            )
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_usage_is_reported_in_openai_field_names(self):
        async with _client(_StubProvider()) as c:
            usage = (await c.post("/v1/chat/completions", json=_payload())).json()["usage"]
        assert usage == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}

    @pytest.mark.asyncio
    async def test_judge_json_passes_through_unaltered(self):
        """parse_judge_label regexes the raw text — we must not reformat it."""
        raw = 'Reasoning here.\n{"label": "CORRECT", "why": "matches"}'
        async with _client(_StubProvider(text=raw)) as c:
            content = (await c.post("/v1/chat/completions", json=_payload())).json()[
                "choices"
            ][0]["message"]["content"]
        assert content == raw
        assert json.loads(content[content.index("{"):])["label"] == "CORRECT"


class TestProviderDelegation:
    @pytest.mark.asyncio
    async def test_messages_are_forwarded_as_astrocyte_messages(self):
        stub = _StubProvider()
        async with _client(stub) as c:
            await c.post("/v1/chat/completions", json=_payload("the prompt"))
        sent = stub.calls[0]["messages"]
        assert len(sent) == 1
        assert sent[0].role == "user"
        assert sent[0].content == "the prompt"

    @pytest.mark.asyncio
    async def test_model_is_passed_through_verbatim(self):
        """ANSWER_MODEL means whatever the configured provider says."""
        stub = _StubProvider()
        async with _client(stub) as c:
            await c.post("/v1/chat/completions", json=_payload())
        assert stub.calls[0]["model"] == "haiku"

    @pytest.mark.asyncio
    async def test_empty_model_falls_back_to_provider_default(self):
        stub = _StubProvider()
        async with _client(stub) as c:
            await c.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "x"}], "model": ""},
            )
        assert stub.calls[0]["model"] is None

    @pytest.mark.asyncio
    async def test_temperature_is_forwarded(self):
        stub = _StubProvider()
        async with _client(stub) as c:
            await c.post("/v1/chat/completions", json=_payload())
        assert stub.calls[0]["temperature"] == 0


class TestFailureModes:
    @pytest.mark.asyncio
    async def test_provider_failure_surfaces_as_500_not_a_blank_answer(self):
        """A silent empty answer would score as WRONG and corrupt the run."""
        async with _client(_ExplodingProvider()) as c:
            r = await c.post("/v1/chat/completions", json=_payload())
        assert r.status_code == 500
        assert "provider is down" in r.json()["error"]["message"]

    @pytest.mark.asyncio
    async def test_empty_messages_is_a_client_error(self):
        async with _client(_StubProvider()) as c:
            r = await c.post("/v1/chat/completions", json={"messages": []})
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_health_is_unauthenticated(self):
        async with _client(_StubProvider()) as c:
            assert (await c.get("/health")).status_code == 200


class TestSpiResolution:
    def test_provider_is_resolved_by_name_not_hard_coded(self, monkeypatch):
        """Any SPI name works — including a module:Class path."""
        seen: dict[str, Any] = {}

        def fake_resolve(name: str, group: str) -> Any:
            seen["name"], seen["group"] = name, group
            return _StubProvider

        import astrocyte._discovery as disc

        monkeypatch.setattr(disc, "resolve_provider", fake_resolve)
        provider = build_provider("some_pkg.mod:MyProvider", config={"text": "x"})
        assert seen == {"name": "some_pkg.mod:MyProvider", "group": "llm_providers"}
        assert isinstance(provider, _StubProvider)

    def test_provider_config_json_is_passed_as_constructor_kwargs(self, monkeypatch):
        import astrocyte._discovery as disc

        monkeypatch.setattr(disc, "resolve_provider", lambda n, g: _StubProvider)
        monkeypatch.setenv("ASTROCYTE_SELFEVAL_PROVIDER_CONFIG", '{"model": "haiku"}')
        provider = build_provider("claude_cli")
        assert provider.kwargs == {"model": "haiku"}
