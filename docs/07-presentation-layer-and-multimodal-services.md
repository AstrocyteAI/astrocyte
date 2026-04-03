# Presentation layer: multimodal, voice, and video services

This document clarifies how Astrocytes relates to **commercial AI products that are not text LLM backends** - conversational video avatars, synthetic voice, real-time video APIs - and how to integrate them **alongside** the LLM Provider SPI. For **multimodal LLM** inputs (image/audio in **chat completions** with GPT-4o, Claude, Gemini, etc.), see **`08-multimodal-llm-spi.md`**. For the base SPI, see `04-provider-spi.md` section 4.

---

## 1. What the LLM Provider SPI covers

Astrocytes needs **`complete()`** (chat-style messages → text) and **`embed()`** (texts → vectors) for the built-in pipeline and policies. Adapters such as **`astrocytes-litellm`** route those calls to providers that expose **chat completion** and **embedding** APIs (directly or via OpenAI-compatible HTTP).

That scope **does not** include:

- Real-time **video conversation** sessions
- **Text-to-speech** or **voice cloning** as primary products
- **Avatar rendering** or lip-sync pipelines
- **Video generation** from scripts alone (non-chat)

Those are **different APIs and products**. They are not drop-in replacements for `LLMProvider`, but they **compose** with Astrocytes in full applications.

---

## 2. Landscape: Tavus and similar products

### 2.1 Conversational video / digital human (closest to Tavus)

These platforms emphasize **real-time or scripted video** with AI personas, developer APIs, and often **REST** or WebRTC flows. They overlap on “AI face + voice + dialogue” but differ on pricing, enterprise features, and API shape.

| Category | Examples (non-exhaustive) | Notes |
|---|---|---|
| **Conversational video / CVI** | [Tavus](https://www.tavus.io/), [HeyGen](https://www.heygen.com/), [D-ID](https://www.d-id.com/), [Hour One](https://hourone.ai/), [Synthesia](https://www.synthesia.io/) (often enterprise), [ReachOut.AI](https://reachout.ai/), various **digital human** APIs | APIs are usually **conversation / video / avatar** oriented, not “replace OpenAI for all `complete()` calls.” |
| **Video-first marketing / personalization** | Tools in the same space as above; feature emphasis varies (marketing vs support vs L&D). | |

**Tavus** positions on **real-time conversational video** with replicas and personas; many “alternatives” compete on **avatar quality**, **API access**, or **batch video** rather than identical CVI semantics. Treat names as **research starting points**, not guarantees of feature parity.

### 2.2 Voice and audio (related but not the same category as Tavus)

| Category | Examples | Relation to Tavus |
|---|---|---|
| **Voice / TTS / conversational voice** | [ElevenLabs](https://elevenlabs.io/), PlayHT, Cartesia, Amazon Polly, Google Cloud TTS | **ElevenLabs** is a **strong competitor on AI voice** (TTS, agents, dubbing), not a full **video replica CVI** in the same sense as Tavus. Pairing **ElevenLabs (voice) + separate avatar/video** is a common architecture. |
| **STT / telephony** | Deepgram, AssemblyAI, Twilio Voice, etc. | Input/output paths for voice apps; orthogonal to memory. |

### 2.3 Where LiteLLM / OpenRouter still help

Unified gateways excel at **text models** (OpenAI, Anthropic, Gemini, Mistral, open models, …). If a **video platform** lets you bring your **own** model via an **OpenAI-compatible** base URL (some document “custom LLM” onboarding), that **same** LLM can often be configured as **`llm_provider: litellm`** or **`openai` + `api_base`** for Astrocytes - **provided** the endpoint implements chat completions (and optionally embeddings) the adapter expects. That is **“shared brain”** between Astrocytes and the video stack, not “Tavus as the LLMProvider” unless Tavus exposes such an endpoint **to your backend** for arbitrary calls.

---

## 3. How to integrate: architectural patterns

### 3.1 Recommended split: memory (Astrocytes) vs presentation (vendor)

```
User / client
    │
    ├─► Application orchestrator
    │       │
    │       ├─► Astrocytes brain.retain / recall / reflect
    │       │       └─► LLMProvider → LiteLLM / OpenAI / Anthropic / …
    │       │             (text completion + embeddings for pipeline)
    │       │
    │       └─► Presentation service API (HTTP / WebRTC / SDK)
    │               └─► Tavus / HeyGen / D-ID / ElevenLabs / …
    │                     (video session, avatar, TTS audio stream)
    │
    └─► Optional: same “persona” config in both layers (your product contract)
```

- **Astrocytes** holds **durable memory** and runs **retrieval + governance** using the **LLM SPI**.
- **Tavus-class** systems handle **how the agent is seen and heard** in real time or in rendered video.

Integration is **orchestration in your app**, not a second `LLMProvider` for the video API.

### 3.2 When a vendor exposes OpenAI-compatible text APIs

If the commercial product documents a **chat completions** (and optionally **embeddings**) endpoint compatible with your adapter:

```yaml
# Example: custom OpenAI-compatible endpoint used by Astrocytes only
llm_provider: openai
llm_provider_config:
  api_base: https://your-vendor-or-proxy.example/v1
  api_key: ${VENDOR_API_KEY}
  model: vendor-text-model
```

Or via LiteLLM if the provider is listed or proxied:

```yaml
llm_provider: litellm
llm_provider_config:
  model: openai/your-model   # or provider-specific slug per LiteLLM docs
  api_base: https://...
```

Use this **only when** the API truly matches **message-in, text-out** (and embed if needed). **Do not** point `api_base` at a **video-only** REST root expecting it to behave like `/v1/chat/completions`.

### 3.3 Feeding memory into presentation (your glue code)

Typical pattern:

1. **`recall()`** or **`reflect()`** returns context for the agent.
2. Your service passes that text (or a shortened prompt) into **the video platform’s** “conversation” or “script” or **LLM hook** API **according to their docs**.
3. New user utterances from the session can be **`retain()`**’d as memories on a schedule or at session end (with policy checks).

```python
# Illustrative - APIs differ per vendor
context = await brain.recall("What do we know about this user?", bank_id=bank_id, context=ctx)
summary_text = merge_hits_for_prompt(context)

# Pseudocode: start or update vendor session with summary + live STT text
await video_platform.start_conversation(
    replica_id="...",
    system_context=summary_text,
    # ...
)

# On turn end or async:
await brain.retain(
    f"User said: {user_utterance}",
    metadata={"source": "tavus_session", "session_id": sid},
    bank_id=bank_id,
    context=ctx,
)
```

### 3.4 ElevenLabs (voice) alongside Astrocytes

Use **Astrocytes + text LLM** for reasoning and memory; use **ElevenLabs** for **TTS** or **voice agent** audio for the same turn:

```
LLM (via Astrocytes pipeline) → text reply → ElevenLabs TTS → audio to user
```

Memory writes still go through **`retain()`** based on **text** you choose to persist (user message, assistant message, or summaries), not the raw audio.

---

## 4. Summary

| Question | Answer |
|---|---|
| Does LiteLLM integration “include” Tavus / HeyGen / ElevenLabs automatically? | **No** - those are **not** generic chat/embedding backends for the SPI unless they expose **compatible text APIs** you configure explicitly. |
| Are they competitors? | **Partially.** Many **video avatar** platforms compete with **Tavus** on CVI or marketing video; **ElevenLabs** competes on **voice**, often as a **component** next to video or text agents. |
| How does Astrocytes “allow” integration? | **Composition:** memory and text LLM via **`LLMProvider`**; **presentation** via **vendor SDKs/REST** in your app. Optional **shared** OpenAI-compatible LLM URL when the vendor supports it. |

For vendor-specific steps, always follow **their** API reference (sessions, webhooks, custom LLM URLs). Astrocytes stays stable on **`retain` / `recall` / `reflect`** and **text + embeddings** for intelligence and governance.
