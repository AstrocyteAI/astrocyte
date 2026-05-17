# Research: Multimodal Memory Engine (JEPA + Diffusion)

> **Status**: Research / pre-implementation. Last updated 2026-05-17.
> Goal: SOTA on multimodal episodic + semantic memory via JEPA latent space + diffusion-based consolidation and retrieval.

---

## 1. Motivation

Astrocyte currently operates on text. The goal is a memory engine that handles text today and expands to full multimodal (image, video, audio, document) without re-architecting the core. Two techniques from recent ML research, combined, can support this goal while also improving retrieval quality and continual learning.

---

## 2. Core thesis

> **Run diffusion inside JEPA's latent space — not pixel or token space.**

JEPA (Joint Embedding Predictive Architecture) learns structured representations by predicting masked regions in latent space, not by reconstructing raw inputs. This gives a compact, semantically organised embedding space. A diffusion model trained on that space inherits the structure.

This single architectural choice drives all three goals simultaneously:

| Goal | Why latent diffusion helps |
|---|---|
| **Retrieval quality** | Conditioned diffusion finds better attractors in structured JEPA space than nearest-neighbour search |
| **Compute efficiency** | Diffusion in a compressed latent is orders of magnitude cheaper than raw-space diffusion |
| **Continual learning** | Latent diffusion replay is compact enough to run continuously, preventing forgetting without large replay buffers |

And it covers both memory types:
- **Episodic**: conditioned diffusion recovers specific trajectories from partial cues
- **Semantic**: unconditional diffusion consolidation over the buffer extracts invariant structure (clusters emerge naturally)

---

## 3. Architecture

```
Input (any modality)
    ↓
[Modality encoder]  →  shared latent z  (dim D)
    ↓
[Memory buffer]  (modality-tagged vectors in shared space)
    ↙                        ↘
[D_consolidate]           [D_recall | query z_q]
periodic, background      on-demand, conditioned
semantic compression      episodic + cross-modal retrieval
CL via replay             DDIM, 50 steps
    ↓                        ↓
[JEPA predictor P]        [Retrieved z]
future state prediction   → downstream LLM or task
```

### 3.1 Components

**Modality encoder** — maps raw input to shared latent `z`. Modality-agnostic from the memory system's perspective; only the encoder changes per modality.

**Memory buffer** — vector store of `(z, modality_tag, metadata)` tuples. Standard vector DB (pgvector, Faiss). No change to existing Astrocyte storage layer.

**D_consolidate** — small MLP-based diffusion model trained on buffer latents. Runs periodically (background, async). Serves two purposes: (a) learns `P(z)`, the distribution of stored memories, for semantic compression; (b) generates replay samples to prevent catastrophic forgetting.

**D_recall** — same diffusion model, run in conditioned mode. Given query `z_q`, generates recalled memory `z_recalled` via DDIM (50 steps, practical latency). Conditioned via classifier-free guidance.

**JEPA predictor P** — optional; predicts future latent states given retrieved context. Enables world-model-style inference.

---

## 4. Modality encoders

### 4.1 Phase 1 — Text (current)

No standard text JEPA exists. Options in order of research rigour:

| Option | Encoder | Tradeoff |
|---|---|---|
| **A — Proxy encoder** | Frozen LLM layer or sentence-transformer | Fast to prototype; loses JEPA's predictive training objective |
| **B — Text JEPA** | Train JEPA-style masked prediction on text sequences | Strong claim; significant compute |
| **C — Diffusion only** | Any text encoder + diffusion memory | Simpler; still publishable without JEPA |

**Recommended start**: Option A. Run the probe experiment (§6) to validate that latent diffusion retrieval beats nearest-neighbour. If it does, Option B becomes justified. If not, Option C is the fallback.

### 4.2 Phase 2 — Multimodal

| Model | Modalities | Status |
|---|---|---|
| **I-JEPA** (Meta) | Image | Released; ViT-H, 1280-dim |
| **V-JEPA 2** (Meta) | Video + action | Released 2025 |
| **ImageBind** (Meta) | Image, text, audio, depth, IMU | Released; 6 modalities in one shared space |

**ImageBind is the pragmatic choice for Phase 2** — it provides a shared embedding space across 6 modalities out of the box. The diffusion memory model does not change between Phase 1 and Phase 2; only the encoder swaps.

### 4.3 Why the shared space matters

With a modality-agnostic shared latent, one diffusion model serves all modalities. Cross-modal retrieval — query with text, retrieve an image memory — becomes a natural consequence of conditioned diffusion in the shared space, not a special-cased feature.

```
query: "the meeting where we discussed the budget"  [text]
  → encode → z_q
  → conditioned diffusion
  → z_recalled  [may correspond to a slide image, audio clip, or text chunk]
```

---

## 5. Related work and differentiation

| System | What it does | Gap vs this work |
|---|---|---|
| **D-JEPA** (Chen et al., Oct 2024) | JEPA + diffusion loss for image/video/audio **generation**; SOTA FID on ImageNet | Stateless generative model — no memory store, no retrieval, no consolidation, no continual learning |
| **REMIND** (Hayes et al.) | Latent replay for continual learning | No diffusion; no JEPA structure; no retrieval |
| **Diffuser** (Janner et al.) | Diffusion for trajectory planning | Not a memory system; no JEPA |
| **DreamerV3** | World model with latent imagination | No explicit episodic store; no diffusion consolidation |
| **Larimar** (IBM 2024) | Episodic memory for LLMs | VAE-based, not diffusion; text only |
| **Generative Replay** (Shin et al.) | GAN replay for continual learning | Pixel-space; not a retrieval system |
| **OmniMem** (2026) | Lifelong multimodal agent memory | No JEPA; no diffusion; nearest-neighbour retrieval |
| **MemVerse** (2025) | Hierarchical episodic-semantic MM memory | Knowledge-graph based; no diffusion consolidation |

**Claimed gap**: D-JEPA (arXiv:2410.03755) demonstrates that JEPA + diffusion is a viable and scalable combination for generative modelling. No prior work applies this combination to **memory** — consolidation, retrieval, and continual learning. This work is the first to use a JEPA latent space as a persistent memory store, with diffusion serving as the consolidation and recall mechanism rather than a generative loss.

**D-JEPA as a building block**: D-JEPA's trained encoder produces a rich, generation-capable latent space that already supports image, video, and audio. Its representations are a natural candidate for the Phase 2 multimodal encoder (§4.2) — plugging a diffusion memory system on top of D-JEPA representations is a principled extension of their work into the memory domain.

> **Verification required**: this gap claim must be confirmed via a full literature review before publication. Concurrent work may exist post-August 2025.

---

## 6. Probe experiment (Phase 1 gate)

Before committing to the full architecture, validate the core thesis with a cheap probe:

```python
# 1. Encode memories with proxy text encoder
encoder = SentenceTransformer("all-mpnet-base-v2")
buffer = [encoder.encode(x) for x in memory_stream]  # list of z vectors, dim=768

# 2. Train small MLP diffusion model on buffer
denoiser = MLPDenoiser(dim=768, hidden=512, depth=4)
train_ddpm(denoiser, buffer, steps=1000)

# 3. Retrieval: condition on query z_q
z_q = encoder.encode(query)
z_recalled_diffusion = ddim_sample(denoiser, condition=z_q, steps=50)

# 4. Baseline
z_recalled_nn = nearest_neighbour(z_q, buffer)

# 5. Compare on LME / LoCoMo
score_diffusion = evaluate(z_recalled_diffusion, ground_truth)
score_nn = evaluate(z_recalled_nn, ground_truth)
```

**Decision gate**: if `score_diffusion > score_nn` on LoCoMo or LME, the core thesis holds and Phase 1 proceeds. If not, the hypothesis needs revision before building the full system.

**Estimated effort**: 2–3 days for the probe. Reuses existing bench infrastructure (`make bench-locomo`, `make bench-longmemeval`).

---

## 7. Phased research plan

### Phase 1 — Text (near term)

- Proxy text encoder (sentence-transformer or frozen LLM layer)
- MLP-based diffusion model on text latents
- Benchmark: LoCoMo, LongMemEval, MemoryBench, LMEB
- Contribution: "latent diffusion memory for LLMs"

### Phase 2 — Multimodal (after Phase 1 validates)

- Swap encoder to ImageBind (or multimodal JEPA when available)
- Same diffusion memory system, minimal changes
- Add cross-modal retrieval experiments
- Benchmark: MemLens, Mem-Gallery, MultiHaystack (see `benchmark-multimodal.md`)
- Contribution: "modality-agnostic memory via shared latent diffusion"

Each phase is independently publishable. Phase 1 does not require Phase 2 to succeed.

---

## 8. Ablation matrix

Three ablations isolate each contribution and serve as the experimental backbone of a paper:

| Ablation | Replace with | Measures |
|---|---|---|
| Remove `D_consolidate` | No replay | Forgetting rate — CL contribution |
| Replace `D_recall` with NN | Nearest-neighbour retrieval | Retrieval quality contribution |
| Move diffusion to token space | Raw-space diffusion | Efficiency contribution (FLOPs vs quality) |

Run each ablation on both episodic and semantic memory tasks for a 3×2 experiment table.

---

## 9. Publication path

**Target venues**: NeurIPS, ICML, ICLR (memory / representation learning tracks). arXiv preprint alongside submission.

**arXiv endorsement**: being addressed separately (industry submitter; need endorser from academic collaborator or prior arXiv author).

**Key differentiators reviewers will probe**:
1. Does latent diffusion retrieval actually beat NN? (§6 probe answers this)
2. Is the gap claim (§5) correct? (full literature review required)
3. Does the system handle both episodic and semantic memory, or just one?
4. Is multimodal extension empirically validated or claimed?

---

## 10. References

| Resource | URL |
|---|---|
| I-JEPA paper | https://arxiv.org/abs/2301.08243 |
| D-JEPA (JEPA + diffusion for generation) | https://arxiv.org/abs/2410.03755 |
| V-JEPA 2 | https://arxiv.org/abs/2506.09985 |
| ImageBind | https://arxiv.org/abs/2305.05665 |
| Diffuser (Janner et al.) | https://arxiv.org/abs/2205.09991 |
| DreamerV3 | https://arxiv.org/abs/2301.04104 |
| Larimar (IBM) | https://arxiv.org/abs/2403.11901 |
| REMIND | https://arxiv.org/abs/1910.02509 |
| Generative Replay | https://arxiv.org/abs/1705.08690 |
| OmniMem | https://arxiv.org/abs/2604.01007 |
| MemVerse | https://arxiv.org/abs/2512.03627 |
| Multimodal benchmark landscape | `benchmark-multimodal.md` |
