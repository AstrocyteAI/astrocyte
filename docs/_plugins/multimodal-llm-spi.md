# Multimodal LLM SPI and DTOs

This document specifies **first-class image and audio** (and optional document) payloads for the **LLM Provider SPI** and the **built-in pipeline**, so gateways such as LiteLLM and OpenRouter can pass **vision / audio** inputs to multimodal models **without** ad-hoc string encoding. For **non-LLM** video/voice platforms (Tavus, ElevenLabs TTS-only, etc.), see `presentation-layer-and-multimodal-services.md`.

---

## 1. Design principles

| Principle | Implication |
|---|---|
| **Backward compatible** | `Message.content: str` remains valid; all existing adapters and configs work unchanged. |
| **Gateway-aligned** | DTOs map cleanly to **OpenAI-style** chat messages (text + `image_url` / `input_audio` parts) and to LiteLLM’s multimodal completion API. |
| **Explicit capability** | Providers declare **`LLMCapabilities`** so the pipeline can degrade or refuse when a model cannot accept a modality. |
| **Text-first pipeline default** | Tier 1 pipeline stages that need plain text (chunking, NER, BM25) use **deterministic extraction** from multimodal turns (see §6). |
| **No video generation in SPI** | This SPI covers **inputs to multimodal LLMs** (vision/audio understanding). **Video generation** as output is out of scope for `complete()` unless folded into provider-specific extensions later. |

---

## 2. Content parts (DTOs)

Parts are **discriminated unions** (tagged structs). Serializers map them to provider JSON.

### 2.1 Text

```python
@dataclass(frozen=True)
class TextPart:
    type: Literal["text"] = "text"
    text: str
```

### 2.2 Image

| Part | Fields | Notes |
|---|---|---|
| `ImageUrlPart` | `url: str`, optional `detail: Literal["auto","low","high"] \| None` | Remote URL; OpenAI / many gateways. |
| `ImageBase64Part` | `mime_type: str` (e.g. `image/png`), `base64_data: str` | Raw base64 **without** `data:` prefix; adapters may add prefix per provider. |

```python
@dataclass(frozen=True)
class ImageUrlPart:
    type: Literal["image_url"] = "image_url"
    url: str
    detail: Literal["auto", "low", "high"] | None = None

@dataclass(frozen=True)
class ImageBase64Part:
    type: Literal["image_base64"] = "image_base64"
    mime_type: str
    base64_data: str
```

### 2.3 Audio

| Part | Fields | Notes |
|---|---|---|
| `AudioUrlPart` | `url: str` | When the provider accepts a URL for input audio. |
| `AudioBase64Part` | `mime_type: str` (e.g. `audio/wav`, `audio/mpeg`), `base64_data: str` | Inline audio bytes. |

```python
@dataclass(frozen=True)
class AudioUrlPart:
    type: Literal["audio_url"] = "audio_url"
    url: str

@dataclass(frozen=True)
class AudioBase64Part:
    type: Literal["audio_base64"] = "audio_base64"
    mime_type: str
    base64_data: str
```

### 2.4 Optional: documents (PDF, etc.)

```python
@dataclass(frozen=True)
class DocumentUrlPart:
    type: Literal["document_url"] = "document_url"
    url: str
```

Use when the model + gateway support **file / PDF** inputs in chat (e.g. some Gemini / OpenAI flows). Omit from minimal implementations if unsupported.

### 2.5 Union type

```python
ContentPart = (
    TextPart
    | ImageUrlPart
    | ImageBase64Part
    | AudioUrlPart
    | AudioBase64Part
    | DocumentUrlPart
)
```

---

## 3. Message and completion

### 3.1 `Message`

```python
@dataclass
class Message:
    role: str  # "system" | "user" | "assistant" | "tool" (if used by adapter)
    content: str | list[ContentPart]
```

**Rules:**

- **`str` content** - legacy; treated as a single `TextPart`.
- **`list[ContentPart]`** - at least one part; typically one `TextPart` plus zero or more media parts (OpenAI recommends text first for some models).

**Helpers (core library):**

- `message_text_for_policy(msg: Message) -> str` - concatenates **only** `TextPart.text` for PII / token estimation **warnings** when media is present (see §7).
- `message_has_media(msg: Message) -> bool` - true if any non-text part exists.

### 3.2 `Completion` (unchanged default)

```python
@dataclass
class Completion:
    text: str
    model: str
    usage: TokenUsage | None = None
```

Multimodal **models still return text** for `complete()` in the default SPI. Optional **future** extension: `output_parts: list[ContentPart] | None` for image/audio **output** models - not required for Tier 1 pipeline v1.

---

## 4. LLM capabilities and protocol extensions

### 4.1 `LLMCapabilities`

```python
@dataclass(frozen=True)
class LLMCapabilities:
    supports_multimodal_completion: bool
    modalities_supported: frozenset[str]  # subset of {"text", "image", "audio", "document"}
    supports_multimodal_embedding: bool   # e.g. Gemini multimodal embedding APIs
```

### 4.2 `LLMProvider` protocol

**Required (existing):**

- `async def complete(self, messages: list[Message], ...) -> Completion`
- `async def embed(self, texts: list[str], ...) -> list[list[float]]`

**New optional method (preferred over breaking `embed`):**

```python
async def llm_capabilities(self) -> LLMCapabilities:
    """Default: text-only completion + text embedding."""
    return LLMCapabilities(
        supports_multimodal_completion=False,
        modalities_supported=frozenset({"text"}),
        supports_multimodal_embedding=False,
    )
```

**Optional multimodal embedding** (providers that support image/audio/document vectors):

```python
async def embed_multimodal(
    self,
    inputs: list[MultimodalEmbeddingInput],
    model: str | None = None,
) -> list[list[float]]:
    ...
    raise NotImplementedError
```

```python
@dataclass(frozen=True)
class MultimodalEmbeddingInput:
    """One vector input; provider defines how many modalities."""
    parts: list[ContentPart]  # e.g. TextPart + ImageUrlPart for Gemini multimodal embed
```

If `embed_multimodal` is not implemented, the pipeline uses **text-only** embedding paths (see §6).

### 4.3 Errors

| Exception | When |
|---|---|
| `MultimodalNotSupported` | `complete()` received messages with media parts but `supports_multimodal_completion` is false or model cannot accept the modality. |
| `MultimodalEmbeddingNotSupported` | Pipeline requested multimodal embed but provider did not implement `embed_multimodal`. |

---

## 5. Adapter convention (LiteLLM, OpenRouter, OpenAI SDK)

### 5.1 `complete()` mapping

1. Walk `messages`; for each `Message`, if `content` is `str`, build a single user/system message with string content.
2. If `content` is `list[ContentPart]`, map to provider JSON:
   - **OpenAI-compatible:** `content: [{"type":"text","text":...},{"type":"image_url","image_url":{"url":...}}, ...]`
   - **LiteLLM:** pass the same structure; LiteLLM forwards to provider-specific format.
   - **Anthropic:** map parts to Anthropic `content` blocks per their SDK.
3. Call the gateway SDK **once** per `complete()` request.

### 5.2 `embed()` mapping

- **Unchanged:** `embed(texts: list[str])` for text embeddings.
- **`embed_multimodal`:** only implemented in adapters where the upstream API supports it; otherwise omit and let pipeline fall back (§6).

### 5.3 Capability discovery

- **Static:** config flag `llm_provider_config.multimodal: true` + model name pattern.
- **Dynamic:** optional `llm_capabilities()` calling provider metadata or `litellm.supports_vision(model=...)`.

---

## 6. Built-in pipeline (Tier 1) behavior

### 6.1 Retain path with multimodal content

Callers may pass **`RetainRequest`** (or equivalent) with **`content_parts: list[ContentPart]`** in addition to or instead of plain `content: str` (see public API evolution in core).

**Strategies** (configurable `pipeline.multimodal.retain_strategy`):

| Strategy | Behavior |
|---|---|
| `caption_then_embed` (default) | Run **one** `complete()` with a vision-capable model using all parts → **caption/summary text** → chunk + embed **text** (works with any text embedder). |
| `multimodal_embed` | If `embed_multimodal` is supported, embed parts directly; else **fall back** to `caption_then_embed`. |
| `text_only` | Reject or strip non-text parts if capabilities disallow multimodal. |

### 6.2 Recall / query analysis

- **Text queries** - unchanged.
- **Voice/image query:** caller passes `Message` list with `ImageUrlPart` / `AudioBase64Part` in the user turn; `complete()` for query analysis uses multimodal model when capabilities allow.

### 6.3 Stages that require plain text

Entity extraction, BM25 document store, and similar steps consume **`message_text_for_policy()`** or **caption output**, not raw pixels.

---

## 7. Policy and observability

- **PII / barriers:** Media parts may be **excluded** from regex PII scans; optional **future** integration with vision PII models (out of scope for this SPI).
- **Token budgets:** Token estimation for multimodal requests is **provider-dependent**; adapters may return **approximate** `usage` or mark `input_tokens` as partial.
- **OTel:** Span attributes `astrocytes.llm.modalities` = `image,audio` (low cardinality).

---

## 8. Relationship to other documents

| Document | Role |
|---|---|
| `provider-spi.md` §4.11 | Summary of multimodal extensions in the SPI doc |
| `presentation-layer-and-multimodal-services.md` | Non-LLM video/voice **products** vs LLM multimodal |
| `built-in-pipeline.md` | Pipeline stages; align `multimodal` retain strategies with §6 here |

---

## 9. Summary

- **DTOs:** `ContentPart` union + `Message.content: str | list[ContentPart]`.
- **SPI:** `llm_capabilities()`, optional `embed_multimodal()`, same `complete()` entrypoint with richer messages.
- **Adapters:** Map DTOs → OpenAI-compatible / LiteLLM / provider SDKs.
- **Pipeline:** Text-first defaults; **caption** or **multimodal embed** strategies for retain.
