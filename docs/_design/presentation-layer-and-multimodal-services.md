# Presentation layer: multimodal, voice, and video services

This document clarifies how Astrocyte relates to **commercial AI products that are not text LLM backends** - conversational video avatars, synthetic voice, real-time video APIs - and how to integrate them **alongside** the LLM Provider SPI. For **multimodal LLM** inputs (image/audio in **chat completions** with GPT-4o, Claude, Gemini, etc.), see **`multimodal-llm-spi.md`**. For the base SPI, see `provider-spi.md` section 4.

---

## 1. What the LLM Provider SPI covers

Astrocyte needs **`complete()`** (chat-style messages → text) and **`embed()`** (texts → vectors) for the built-in pipeline and policies. Adapters such as **`astrocyte-llm-litellm`** (and the built-in **`openai`** provider with a custom **`base_url`**) route those calls to **direct vendor SDKs**, **multi-provider gateways** (LiteLLM, Portkey, OpenRouter, cloud routers, …), or any **OpenAI-compatible HTTP** surface your deployment standardizes on.

That scope **does not** include:

- Real-time **video conversation** sessions
- **Text-to-speech** or **voice cloning** as primary products
- **Avatar rendering** or lip-sync pipelines
- **Video generation** from scripts alone (non-chat)

Those are **different APIs and products**. They are not drop-in replacements for `LLMProvider`, but they **compose** with Astrocyte in full applications.

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

### 2.3 Where LLM gateways still help (LiteLLM, OpenRouter, Portkey, …)

**Unified gateways and aggregators** (LiteLLM, OpenRouter, Portkey, Vercel AI Gateway, cloud model routers, …) excel at **text models** (OpenAI, Anthropic, Gemini, Mistral, open models, …). If a **video platform** lets you bring your **own** model via an **OpenAI-compatible** base URL (some document “custom LLM” onboarding), that **same** LLM can often be configured as **`llm_provider: litellm`** or **`openai` + `base_url`** for Astrocyte - **provided** the endpoint implements chat completions (and optionally embeddings) the adapter expects. That is **“shared brain”** between Astrocyte and the video stack, not “Tavus as the LLMProvider” unless Tavus exposes such an endpoint **to your backend** for arbitrary calls.

---

## 3. How to integrate: architectural patterns

### 3.1 Recommended split: memory (Astrocyte) vs presentation (vendor)

```
User / client
    │
    ├─► Application orchestrator
    │       │
    │       ├─► Astrocyte brain.retain / recall / reflect
    │       │       └─► LLMProvider → gateway (LiteLLM, OpenRouter, …) or direct OpenAI / Anthropic / …
    │       │             (text completion + embeddings for pipeline)
    │       │
    │       └─► Presentation service API (HTTP / WebRTC / SDK)
    │               └─► Tavus / HeyGen / D-ID / ElevenLabs / …
    │                     (video session, avatar, TTS audio stream)
    │
    └─► Optional: same “persona” config in both layers (your product contract)
```

- **Astrocyte** holds **durable memory** and runs **retrieval + governance** using the **LLM SPI**.
- **Tavus-class** systems handle **how the agent is seen and heard** in real time or in rendered video.

Integration is **orchestration in your app**, not a second `LLMProvider` for the video API.

### 3.2 When a vendor exposes OpenAI-compatible text APIs

If the commercial product documents a **chat completions** (and optionally **embeddings**) endpoint compatible with your adapter:

```yaml
# Example: custom OpenAI-compatible endpoint used by Astrocyte only
llm_provider: openai
llm_provider_config:
  base_url: https://your-vendor-or-proxy.example/v1
  api_key: ${VENDOR_API_KEY}
  model: vendor-text-model
```

Or via a **gateway adapter** (e.g. `litellm`) when the model is listed or proxied there — the same pattern applies to other aggregators you expose through an OpenAI-compatible surface your adapter supports:

```yaml
llm_provider: litellm
llm_provider_config:
  model: openai/your-model   # or provider-specific slug per your gateway’s docs
  base_url: https://...
```

Use this **only when** the API truly matches **message-in, text-out** (and embed if needed). **Do not** point `base_url` at a **video-only** REST root expecting it to behave like `/v1/chat/completions`.

### 3.3 Feeding memory into presentation (composer pattern)

Treat your **application orchestrator** (often a **Backend for Frontend** or API service) as the **composer**: it **assembles** what the presentation vendor sees—**Astrocyte** supplies **durable semantic memory** (`retain` / `recall` / `reflect` / `forget`); the **vendor SDK or REST API** supplies the **conversation surface** (room, avatar, realtime audio/video). Astrocyte **does not** fetch your CRM, billing DB, or identity store unless you **retain** derived text there; **canonical business facts** normally live in **your system of record** and are **read at compose time** under your **authorization rules**.

Typical end-to-end pattern:

1. Composer **loads** allowed facts from **your SoR** (profile, entitlements, tickets, …) and **retrieves** from Astrocyte via **`recall()`** or **`reflect()`** (or both—see latency trade-offs in your product).
2. Composer **renders** a single **briefing string** (or vendor-specific fields) with explicit **ordering, truncation, and precedence** (e.g. SoR wins on conflicting subscription tier).
3. Composer **starts or updates** the vendor session using **their** API (session URL, `conversational_context`, script payload, custom LLM URL—per vendor docs).
4. New information from the session is **`retain()`**’d through **explicit tools**, **async summarization**, or **webhooks**, always through **policy** and **`AstrocyteContext`** (principal + bank binding at the server—see `sandbox-awareness-and-exfiltration.md`).

#### SoR vs Astrocyte

| Source | Role in the composed briefing |
|--------|-------------------------------|
| **System of record** (your databases, CRM, support tools) | **Authoritative** operational data; read **through** the composer with field-level **allowlists** and **TTL / freshness** rules you own. |
| **Astrocyte** | **Learned / recalled agent memory** (user-stated preferences, past conversation distilled facts, RAG you chose to retain) with **governance** (PII, quotas, access grants, lifecycle). |

Do not assume the video platform merges these sources; **composition is your contract**.

#### Fusing SoR snippets with memory in one `recall`

When you want **vector memory** and **external snippets** fused under **one token budget**, use **`RecallRequest.external_context`**: pass curated SoR (or RAG) hits as **`MemoryHit`** rows alongside the query. The pipeline / provider **recall** path blends them with stored memory—see **`innovations.md`** (cross-source fusion) and **`built-in-pipeline.md`** (`external_context` and `detail_level`). Your composer still **fetches** SoR data and **maps** it to `MemoryHit`; Astrocyte remains the **memory engine**, not the CRM client. If your integration uses the high-level **`brain.recall(query, bank_id=…)`** helper only, **merge** SoR snippets with `recall` hits **in the composer** before rendering the briefing until your stack wires `external_context` through that entry point.

#### Example: Tavus Conversational Video Interface (CVI)

[Tavus](https://docs.tavus.io/api-reference/overview) is representative of CVI products: **server-side** session creation, **client-side** realtime UI.

**Server (composer),** holding the Tavus API key only here:

1. Build `conversational_context` from **briefing blocks** (SoR summaries + Astrocyte `recall` / `reflect` output), capped for latency and model context.
2. `POST https://tavusapi.com/v2/conversations` with `persona_id`, `replica_id`, and optional [`memory_stores`](https://docs.tavus.io/sections/conversational-video-interface/memories) (vendor-native cross-session memory, orthogonal to Astrocyte unless you design sync).
3. Return **`conversation_url`** (and **`meeting_token`** if using private rooms) to the **browser**; never expose Tavus or Astrocyte secrets to the client.

**Client (CVI / Daily):** Tavus does **not** execute LLM **tool calls** on your behalf. Listen for [`conversation.tool_call`](https://docs.tavus.io/sections/event-schemas/conversation-toolcall) on the realtime channel, call **your** HTTPS endpoint, run **`retain` / `recall` / `forget`** on Astrocyte, then inject compact results with [`conversation.append_llm_context`](https://docs.tavus.io/sections/onboarding-guide/tool-calling-examples) or [`conversation.echo`](https://docs.tavus.io/sections/onboarding-guide/tool-calling-examples) per [Tavus tool-calling guidance](https://docs.tavus.io/sections/onboarding-guide/tool-calling-examples).

```python
# Illustrative: composer builds a briefing, then opens a CVI session (names differ per vendor).
# Merge Astrocyte hits with SoR snippets in the composer (or use RecallRequest.external_context
# on a pipeline recall(RecallRequest) path—see built-in-pipeline.md).
recall_result = await brain.recall(
    "What should we remember for this support call?",
    bank_id=bank_id,
    max_results=5,
    context=ctx,
)
sor_blocks = [f"Plan: {user_row['plan_tier']}"]  # fetch + allowlist in your SoR layer
briefing = render_briefing(sor_blocks=sor_blocks, astrocyte_hits=recall_result.hits)

# Pseudocode: Tavus create conversation — see Tavus API reference for full schema.
# await tavus_client.create_conversation(
#     persona_id="...",
#     replica_id="...",
#     conversational_context=briefing,
#     memory_stores=[f"{user_id}_{persona_id}"],
# )

# On tool execution or async pipeline:
await brain.retain(
    f"User said: {user_utterance}",
    metadata={"source": "tavus_cvi", "session_id": sid},
    bank_id=bank_id,
    context=ctx,
)
```

### 3.4 ElevenLabs (voice) alongside Astrocyte

Use **Astrocyte + text LLM** for reasoning and memory; use **ElevenLabs** for **TTS** or **voice agent** audio for the same turn:

```
LLM (via Astrocyte pipeline) → text reply → ElevenLabs TTS → audio to user
```

Memory writes still go through **`retain()`** based on **text** you choose to persist (user message, assistant message, or summaries), not the raw audio.

---

## 4. Summary

| Question | Answer |
|---|---|
| Does LLM-gateway integration (LiteLLM-style routing) “include” Tavus / HeyGen / ElevenLabs automatically? | **No** - those are **not** generic chat/embedding backends for the SPI unless they expose **compatible text APIs** you configure explicitly. |
| Are they competitors? | **Partially.** Many **video avatar** platforms compete with **Tavus** on CVI or marketing video; **ElevenLabs** competes on **voice**, often as a **component** next to video or text agents. |
| How does Astrocyte “allow” integration? | **Composition:** memory and text LLM via **`LLMProvider`**; **presentation** via **vendor SDKs/REST** in your app. Optional **shared** OpenAI-compatible LLM URL when the vendor supports it. |
| Who merges CRM / DB facts with `recall` for a CVI session? | **Your application composer** (BFF / API). Astrocyte holds **learned memory**; your **system of record** stays authoritative for operational data unless you **retain** derived text into Astrocyte. Use **`RecallRequest.external_context`** on pipeline recall where supported, or **merge** SoR snippets with `brain.recall()` hits before rendering the vendor briefing. |

For vendor-specific steps, always follow **their** API reference (sessions, webhooks, custom LLM URLs). Astrocyte stays stable on **`retain` / `recall` / `reflect`** and **text + embeddings** for intelligence and governance.
