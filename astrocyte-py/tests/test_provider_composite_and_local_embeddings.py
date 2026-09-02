"""Tests for CompositeLLMProvider and LocalEmbeddingsProvider.

No model weights are downloaded and no network is touched: the
sentence-transformers dependency is stubbed, so these run anywhere and stay
safe to execute alongside a live bench.

The load-bearing invariant under test is the **zero-padding contract**: a
384-dim local model must drop into the reference schema's ``vector(1536)``
columns without a migration, and padding must preserve cosine ordering.
"""

from __future__ import annotations

import math
import sys
import types
from typing import Any

import pytest

from astrocyte.providers.composite import CompositeLLMProvider
from astrocyte.types import Completion, LLMCapabilities, Message

# ── Fakes ────────────────────────────────────────────────────────────────

class _FakeCompletionProvider:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def capabilities(self) -> LLMCapabilities:
        return LLMCapabilities(
            supports_multimodal_completion=True,
            modalities_supported=("text", "image_url"),
            supports_multimodal_embedding=False,
            supports_batch_embed=False,
        )

    async def complete(self, messages, **kwargs) -> Completion:
        self.calls.append({"messages": messages, **kwargs})
        return Completion(text="completed", model="fake-completion", usage=None)

    async def embed(self, texts, model=None):  # pragma: no cover — must never run
        raise AssertionError("composite routed embed() to the completion provider")

    async def close(self) -> None:
        self.closed = True


class _FakeEmbeddingProvider:
    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.closed = False

    def capabilities(self) -> LLMCapabilities:
        return LLMCapabilities(
            supports_multimodal_completion=False,
            modalities_supported=("text",),
            supports_multimodal_embedding=True,
            supports_batch_embed=True,
        )

    async def complete(self, messages, **kwargs):  # pragma: no cover — must never run
        raise AssertionError("composite routed complete() to the embedding provider")

    async def embed(self, texts, model=None) -> list[list[float]]:
        self.calls.append(texts)
        return [[0.1, 0.2] for _ in texts]

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def stub_sentence_transformers(monkeypatch):
    """Install a fake ``sentence_transformers`` module returning fixed vectors."""

    def _install(dim: int = 384):
        class _FakeVec(list):
            def tolist(self):
                return list(self)

        class _FakeModel:
            def __init__(self, *a, **k):
                self.encode_kwargs: dict[str, Any] = {}

            def encode(self, texts, **kwargs):
                self.encode_kwargs = kwargs
                out = []
                for i, _ in enumerate(texts):
                    v = [0.0] * dim
                    v[i % dim] = 1.0  # unit vector, distinct per input
                    out.append(_FakeVec(v))
                return out

        mod = types.ModuleType("sentence_transformers")
        mod.SentenceTransformer = _FakeModel
        monkeypatch.setitem(sys.modules, "sentence_transformers", mod)
        return mod

    return _install


# ── CompositeLLMProvider ─────────────────────────────────────────────────

class TestCompositeRouting:
    @pytest.mark.asyncio
    async def test_complete_goes_to_completion_provider_only(self):
        comp, emb = _FakeCompletionProvider(), _FakeEmbeddingProvider()
        p = CompositeLLMProvider(completion_provider=comp, embedding_provider=emb)
        result = await p.complete([Message(role="user", content="hi")])
        assert result.text == "completed"
        assert len(comp.calls) == 1
        assert emb.calls == []

    @pytest.mark.asyncio
    async def test_embed_goes_to_embedding_provider_only(self):
        comp, emb = _FakeCompletionProvider(), _FakeEmbeddingProvider()
        p = CompositeLLMProvider(completion_provider=comp, embedding_provider=emb)
        vecs = await p.embed(["a", "b"])
        assert len(vecs) == 2
        assert emb.calls == [["a", "b"]]
        assert comp.calls == []

    @pytest.mark.asyncio
    async def test_complete_kwargs_are_forwarded_intact(self):
        comp = _FakeCompletionProvider()
        p = CompositeLLMProvider(
            completion_provider=comp, embedding_provider=_FakeEmbeddingProvider(),
        )
        await p.complete(
            [Message(role="user", content="hi")],
            max_tokens=512, temperature=0.7,
            response_format={"type": "json_object"},
        )
        call = comp.calls[0]
        assert call["max_tokens"] == 512
        assert call["temperature"] == 0.7
        assert call["response_format"] == {"type": "json_object"}

    def test_capabilities_merge_from_the_right_side_of_each_split(self):
        p = CompositeLLMProvider(
            completion_provider=_FakeCompletionProvider(),
            embedding_provider=_FakeEmbeddingProvider(),
        )
        caps = p.capabilities()
        # completion-side traits come from the completion provider …
        assert caps.supports_multimodal_completion is True
        assert caps.modalities_supported == ("text", "image_url")
        # … embed-side traits from the embedding provider
        assert caps.supports_batch_embed is True
        assert caps.supports_multimodal_embedding is True

    @pytest.mark.asyncio
    async def test_close_drains_both_backends(self):
        comp, emb = _FakeCompletionProvider(), _FakeEmbeddingProvider()
        await CompositeLLMProvider(completion_provider=comp, embedding_provider=emb).close()
        assert comp.closed and emb.closed

    @pytest.mark.asyncio
    async def test_close_is_best_effort_when_one_backend_raises(self):
        class _Boom(_FakeCompletionProvider):
            async def close(self):
                raise RuntimeError("teardown failed")

        emb = _FakeEmbeddingProvider()
        await CompositeLLMProvider(
            completion_provider=_Boom(), embedding_provider=emb,
        ).close()
        assert emb.closed is True  # second backend still drained

    @pytest.mark.asyncio
    async def test_backend_without_close_is_tolerated(self):
        class _NoClose:
            def capabilities(self): return LLMCapabilities()
            async def complete(self, *a, **k): return Completion(text="", model="x")
            async def embed(self, *a, **k): return []

        await CompositeLLMProvider(
            completion_provider=_NoClose(), embedding_provider=_NoClose(),
        ).close()


# ── LocalEmbeddingsProvider ──────────────────────────────────────────────

class TestLocalEmbeddings:
    @pytest.mark.asyncio
    async def test_pads_to_schema_width_by_default(self, stub_sentence_transformers):
        """384-dim model must fit vector(1536) columns with no migration."""
        stub_sentence_transformers(dim=384)
        from astrocyte.providers.local_embeddings import LocalEmbeddingsProvider

        vecs = await LocalEmbeddingsProvider().embed(["a", "b"])
        assert all(len(v) == 1536 for v in vecs)
        assert all(v[384:] == [0.0] * (1536 - 384) for v in vecs)

    @pytest.mark.asyncio
    async def test_zero_padding_preserves_norm_and_cosine(self, stub_sentence_transformers):
        """Cosine ordering must survive padding — else recall ranking shifts."""
        stub_sentence_transformers(dim=384)
        from astrocyte.providers.local_embeddings import LocalEmbeddingsProvider

        a, b = await LocalEmbeddingsProvider().embed(["a", "b"])
        assert math.isclose(math.sqrt(sum(x * x for x in a)), 1.0, rel_tol=1e-6)
        # orthogonal unit vectors stay orthogonal; self-similarity stays 1
        assert math.isclose(sum(x * y for x, y in zip(a, b)), 0.0, abs_tol=1e-9)
        assert math.isclose(sum(x * x for x in a), 1.0, rel_tol=1e-6)

    @pytest.mark.asyncio
    async def test_pad_to_none_returns_native_dim(self, stub_sentence_transformers):
        stub_sentence_transformers(dim=384)
        from astrocyte.providers.local_embeddings import LocalEmbeddingsProvider

        vecs = await LocalEmbeddingsProvider(pad_to=None).embed(["a"])
        assert len(vecs[0]) == 384

    @pytest.mark.asyncio
    async def test_oversized_model_raises_instead_of_truncating(self, stub_sentence_transformers):
        """Silent truncation would change geometry — must fail loudly."""
        stub_sentence_transformers(dim=2048)
        from astrocyte.providers.local_embeddings import LocalEmbeddingsProvider

        with pytest.raises(ValueError, match="exceeds pad_to"):
            await LocalEmbeddingsProvider(pad_to=1536).embed(["a"])

    @pytest.mark.asyncio
    async def test_exact_dim_match_is_returned_unpadded(self, stub_sentence_transformers):
        stub_sentence_transformers(dim=1536)
        from astrocyte.providers.local_embeddings import LocalEmbeddingsProvider

        vecs = await LocalEmbeddingsProvider(pad_to=1536).embed(["a"])
        assert len(vecs[0]) == 1536

    @pytest.mark.asyncio
    async def test_empty_input_short_circuits_without_loading_model(self):
        # No stub installed: if the model loaded, the import would fail.
        from astrocyte.providers.local_embeddings import LocalEmbeddingsProvider

        p = LocalEmbeddingsProvider.__new__(LocalEmbeddingsProvider)
        p._model = None
        assert await LocalEmbeddingsProvider.embed(p, []) == []

    @pytest.mark.asyncio
    async def test_model_is_loaded_once_and_cached(self, stub_sentence_transformers):
        stub_sentence_transformers(dim=384)
        from astrocyte.providers.local_embeddings import LocalEmbeddingsProvider

        p = LocalEmbeddingsProvider()
        await p.embed(["a"])
        first = p._model
        await p.embed(["b"])
        assert p._model is first

    @pytest.mark.asyncio
    async def test_requests_normalized_embeddings(self, stub_sentence_transformers):
        """normalize_embeddings=True is what makes zero-padding safe."""
        stub_sentence_transformers(dim=384)
        from astrocyte.providers.local_embeddings import LocalEmbeddingsProvider

        p = LocalEmbeddingsProvider()
        await p.embed(["a"])
        assert p._model.encode_kwargs.get("normalize_embeddings") is True

    @pytest.mark.asyncio
    async def test_long_text_is_truncated_and_empty_text_survives(
        self, stub_sentence_transformers,
    ):
        stub_sentence_transformers(dim=384)
        from astrocyte.providers.local_embeddings import LocalEmbeddingsProvider

        p = LocalEmbeddingsProvider()
        vecs = await p.embed(["x" * 60_000, ""])
        assert len(vecs) == 2  # neither input dropped

    @pytest.mark.asyncio
    async def test_complete_raises_and_points_at_the_composite_path(
        self, stub_sentence_transformers,
    ):
        stub_sentence_transformers()
        from astrocyte.providers.local_embeddings import LocalEmbeddingsProvider

        with pytest.raises(NotImplementedError, match="CompositeLLMProvider"):
            await LocalEmbeddingsProvider().complete([Message(role="user", content="hi")])

    def test_missing_sentence_transformers_gives_actionable_error(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "sentence_transformers", None)
        from astrocyte.providers.local_embeddings import LocalEmbeddingsProvider

        with pytest.raises(ImportError, match="sentence-transformers"):
            LocalEmbeddingsProvider()
