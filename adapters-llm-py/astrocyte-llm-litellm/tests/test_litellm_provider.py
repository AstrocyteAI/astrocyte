"""Unit tests for LiteLLMProvider.

All tests mock litellm — no real API calls are made.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from astrocyte.types import Completion, LLMCapabilities, Message, TokenUsage

from astrocyte_llm_litellm import LiteLLMProvider

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def provider() -> LiteLLMProvider:
    """Provider with defaults."""
    return LiteLLMProvider()


@pytest.fixture
def custom_provider() -> LiteLLMProvider:
    """Provider with custom config."""
    return LiteLLMProvider(
        model="bedrock/anthropic.claude-sonnet-4-20250514-v1:0",
        embedding_model="bedrock/amazon.titan-embed-text-v2:0",
        api_key="test-key",
        api_base="https://proxy.example.com",
        max_retries=5,
        timeout=30.0,
        extra={"aws_region_name": "us-east-1"},
    )


def _mock_completion_response(
    text: str = "Hello, world!",
    model: str = "gpt-4o-mini",
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
) -> SimpleNamespace:
    """Build a mock litellm completion response."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        model=model,
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        ),
    )


def _mock_embedding_response(
    embeddings: list[list[float]] | None = None,
) -> SimpleNamespace:
    """Build a mock litellm embedding response."""
    if embeddings is None:
        embeddings = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    # litellm returns dicts, not objects
    data = [{"index": i, "embedding": emb} for i, emb in enumerate(embeddings)]
    return SimpleNamespace(data=data)


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


class TestCapabilities:
    def test_returns_llm_capabilities(self, provider: LiteLLMProvider) -> None:
        caps = provider.capabilities()
        assert isinstance(caps, LLMCapabilities)

    def test_supports_multimodal(self, provider: LiteLLMProvider) -> None:
        caps = provider.capabilities()
        assert caps.supports_multimodal_completion is True
        assert "text" in caps.modalities_supported
        assert "image_url" in caps.modalities_supported

    def test_batch_embed_supported(self, provider: LiteLLMProvider) -> None:
        assert provider.capabilities().supports_batch_embed is True


# ---------------------------------------------------------------------------
# Complete
# ---------------------------------------------------------------------------


class TestComplete:
    @pytest.mark.asyncio
    async def test_basic_completion(self, provider: LiteLLMProvider) -> None:
        mock_resp = _mock_completion_response(text="Hi there")
        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp):
            result = await provider.complete([Message(role="user", content="Hello")])

        assert isinstance(result, Completion)
        assert result.text == "Hi there"
        assert result.model == "gpt-4o-mini"

    @pytest.mark.asyncio
    async def test_completion_usage(self, provider: LiteLLMProvider) -> None:
        mock_resp = _mock_completion_response(prompt_tokens=20, completion_tokens=15)
        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp):
            result = await provider.complete([Message(role="user", content="Hello")])

        assert isinstance(result.usage, TokenUsage)
        assert result.usage.input_tokens == 20
        assert result.usage.output_tokens == 15

    @pytest.mark.asyncio
    async def test_completion_no_usage(self, provider: LiteLLMProvider) -> None:
        mock_resp = _mock_completion_response()
        mock_resp.usage = None
        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp):
            result = await provider.complete([Message(role="user", content="Hello")])

        assert result.usage is None

    @pytest.mark.asyncio
    async def test_model_override(self, provider: LiteLLMProvider) -> None:
        mock_resp = _mock_completion_response(model="gpt-4o")
        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp) as mock_call:
            await provider.complete(
                [Message(role="user", content="Hello")],
                model="gpt-4o",
            )

        assert mock_call.call_args.kwargs["model"] == "gpt-4o"

    @pytest.mark.asyncio
    async def test_custom_provider_passes_config(self, custom_provider: LiteLLMProvider) -> None:
        mock_resp = _mock_completion_response()
        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp) as mock_call:
            await custom_provider.complete([Message(role="user", content="Hello")])

        kwargs = mock_call.call_args.kwargs
        assert kwargs["api_key"] == "test-key"
        assert kwargs["api_base"] == "https://proxy.example.com"
        assert kwargs["aws_region_name"] == "us-east-1"
        assert kwargs["num_retries"] == 5
        assert kwargs["timeout"] == 30.0

    @pytest.mark.asyncio
    async def test_empty_content_returns_empty_string(self, provider: LiteLLMProvider) -> None:
        mock_resp = _mock_completion_response()
        mock_resp.choices[0].message.content = None
        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp):
            result = await provider.complete([Message(role="user", content="Hello")])

        assert result.text == ""

    @pytest.mark.asyncio
    async def test_system_and_user_messages(self, provider: LiteLLMProvider) -> None:
        mock_resp = _mock_completion_response()
        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp) as mock_call:
            await provider.complete([
                Message(role="system", content="You are helpful."),
                Message(role="user", content="Hi"),
            ])

        messages = mock_call.call_args.kwargs["messages"]
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"

    @pytest.mark.asyncio
    async def test_temperature_and_max_tokens(self, provider: LiteLLMProvider) -> None:
        mock_resp = _mock_completion_response()
        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp) as mock_call:
            await provider.complete(
                [Message(role="user", content="Hello")],
                max_tokens=2048,
                temperature=0.7,
            )

        kwargs = mock_call.call_args.kwargs
        assert kwargs["max_tokens"] == 2048
        assert kwargs["temperature"] == 0.7


# ---------------------------------------------------------------------------
# Embed
# ---------------------------------------------------------------------------


class TestEmbed:
    @pytest.mark.asyncio
    async def test_basic_embedding(self, provider: LiteLLMProvider) -> None:
        mock_resp = _mock_embedding_response([[0.1, 0.2], [0.3, 0.4]])
        with patch("litellm.aembedding", new_callable=AsyncMock, return_value=mock_resp):
            result = await provider.embed(["hello", "world"])

        assert len(result) == 2
        assert result[0] == [0.1, 0.2]
        assert result[1] == [0.3, 0.4]

    @pytest.mark.asyncio
    async def test_embedding_order_preserved(self, provider: LiteLLMProvider) -> None:
        """Verify results are sorted by index even if response is out of order."""
        resp = SimpleNamespace(
            data=[
                {"index": 1, "embedding": [0.4, 0.5]},
                {"index": 0, "embedding": [0.1, 0.2]},
            ]
        )
        with patch("litellm.aembedding", new_callable=AsyncMock, return_value=resp):
            result = await provider.embed(["first", "second"])

        assert result[0] == [0.1, 0.2]
        assert result[1] == [0.4, 0.5]

    @pytest.mark.asyncio
    async def test_embedding_model_override(self, provider: LiteLLMProvider) -> None:
        mock_resp = _mock_embedding_response([[0.1]])
        with patch("litellm.aembedding", new_callable=AsyncMock, return_value=mock_resp) as mock_call:
            await provider.embed(["hello"], model="custom/embed-model")

        assert mock_call.call_args.kwargs["model"] == "custom/embed-model"

    @pytest.mark.asyncio
    async def test_embedding_custom_provider(self, custom_provider: LiteLLMProvider) -> None:
        mock_resp = _mock_embedding_response([[0.1]])
        with patch("litellm.aembedding", new_callable=AsyncMock, return_value=mock_resp) as mock_call:
            await custom_provider.embed(["hello"])

        kwargs = mock_call.call_args.kwargs
        assert kwargs["api_key"] == "test-key"
        assert kwargs["api_base"] == "https://proxy.example.com"
        assert kwargs["aws_region_name"] == "us-east-1"

    @pytest.mark.asyncio
    async def test_long_text_truncated(self, provider: LiteLLMProvider) -> None:
        """Text exceeding the character limit should be truncated."""
        long_text = "x" * 50_000
        mock_resp = _mock_embedding_response([[0.1]])
        with patch("litellm.aembedding", new_callable=AsyncMock, return_value=mock_resp) as mock_call:
            await provider.embed([long_text])

        sent_text = mock_call.call_args.kwargs["input"][0]
        assert len(sent_text) <= 24_576  # _MAX_EMBED_CHARS

    @pytest.mark.asyncio
    async def test_truncation_emits_warning(
        self, provider: LiteLLMProvider, caplog: "pytest.LogCaptureFixture",
    ) -> None:
        """Silent truncation is a correctness hazard — every clip event
        must produce a warning on ``astrocyte_llm_litellm`` so operators
        can detect upstream chunking gaps."""
        import logging

        long_text = "x" * 50_000
        short_text = "fine"
        mock_resp = _mock_embedding_response([[0.1], [0.2]])
        with (
            caplog.at_level(logging.WARNING, logger="astrocyte_llm_litellm"),
            patch("litellm.aembedding", new_callable=AsyncMock, return_value=mock_resp),
        ):
            await provider.embed([long_text, short_text])

        messages = [r.getMessage() for r in caplog.records]
        assert any("truncated 1 of 2" in m for m in messages), messages

    @pytest.mark.asyncio
    async def test_no_warning_when_all_inputs_fit(
        self, provider: LiteLLMProvider, caplog: "pytest.LogCaptureFixture",
    ) -> None:
        """Don't spam the log on the common case — warning fires only
        when at least one input was actually clipped."""
        import logging

        mock_resp = _mock_embedding_response([[0.1]])
        with (
            caplog.at_level(logging.WARNING, logger="astrocyte_llm_litellm"),
            patch("litellm.aembedding", new_callable=AsyncMock, return_value=mock_resp),
        ):
            await provider.embed(["short input"])

        assert not any(
            "truncated" in r.getMessage() for r in caplog.records
        )


# ---------------------------------------------------------------------------
# Message conversion
# ---------------------------------------------------------------------------


class TestMessageConversion:
    @pytest.mark.asyncio
    async def test_multimodal_image_url(self, provider: LiteLLMProvider) -> None:
        from astrocyte.types import ContentPart

        msg = Message(
            role="user",
            content=[
                ContentPart(type="text", text="Describe this image"),
                ContentPart(type="image_url", image_url="https://example.com/img.png"),
            ],
        )
        mock_resp = _mock_completion_response()
        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp) as mock_call:
            await provider.complete([msg])

        sent_msg = mock_call.call_args.kwargs["messages"][0]
        assert isinstance(sent_msg["content"], list)
        assert sent_msg["content"][0]["type"] == "text"
        assert sent_msg["content"][1]["type"] == "image_url"
        assert sent_msg["content"][1]["image_url"]["url"] == "https://example.com/img.png"

    @pytest.mark.asyncio
    async def test_multimodal_image_base64(self, provider: LiteLLMProvider) -> None:
        from astrocyte.types import ContentPart

        msg = Message(
            role="user",
            content=[
                ContentPart(type="image_base64", image_base64="abc123"),
            ],
        )
        mock_resp = _mock_completion_response()
        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp) as mock_call:
            await provider.complete([msg])

        sent_msg = mock_call.call_args.kwargs["messages"][0]
        assert "data:image/png;base64,abc123" in sent_msg["content"][0]["image_url"]["url"]

    @pytest.mark.asyncio
    async def test_control_chars_sanitized(self, provider: LiteLLMProvider) -> None:
        msg = Message(role="user", content="Hello\x00world\x01!")
        mock_resp = _mock_completion_response()
        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp) as mock_call:
            await provider.complete([msg])

        sent_content = mock_call.call_args.kwargs["messages"][0]["content"]
        assert "\x00" not in sent_content
        assert "\x01" not in sent_content
        assert "Helloworld!" in sent_content


# ---------------------------------------------------------------------------
# Init / import guard
# ---------------------------------------------------------------------------


class TestInit:
    def test_spi_version(self, provider: LiteLLMProvider) -> None:
        assert provider.SPI_VERSION == 1

    def test_missing_litellm_raises(self) -> None:
        with patch.dict("sys.modules", {"litellm": None}):
            with pytest.raises(ImportError, match="litellm"):
                LiteLLMProvider()

    def test_default_models(self, provider: LiteLLMProvider) -> None:
        assert provider._model == "openai/gpt-4o-mini"
        assert provider._embedding_model == "openai/text-embedding-3-small"

    def test_custom_models(self) -> None:
        p = LiteLLMProvider(model="anthropic/claude-sonnet-4-20250514", embedding_model="cohere/embed-v3")
        assert p._model == "anthropic/claude-sonnet-4-20250514"
        assert p._embedding_model == "cohere/embed-v3"

    def test_extra_kwargs_stored(self) -> None:
        p = LiteLLMProvider(extra={"vertex_project": "my-project"})
        assert p._extra["vertex_project"] == "my-project"
