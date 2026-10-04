# Design principles for “AI astrocyte” inspired by neuroscience

## 0. Objective (north star)

**Wherever you use AI, Astrocyte captures and checks what it learns, under your
organisation's rules.**

Stated by Calvin on 2026-10-04 and published in
["Agents Need Documents They Can Check"](https://calvinx.com/blog/2026-Oct-04/agents-need-documents-they-can-check). It is the test for design
decisions: a change that does not move Astrocyte towards it needs a reason.

**The target is the "best of both" corner.** Memory is low effort and low
control: it is trusted by its results. Documentation is high effort and high
control: it is trusted by inspection. Astrocyte aims to be low effort and
high control, trusted both ways: captured as easily as memory, checked as
easily as a document.

**The corner needs all four of these together:**

| # | Requirement | State (2026-10-04) |
|---|---|---|
| 1 | **Capture with no effort.** Every conversation and task is retained as evidence, with no one writing it up. | Exists. The structured-extraction metadata that `retain()` used to discard is persisted since `057ac2a` |
| 2 | **Show it as documents.** What the agent knows appears as pages people can read, with no database query. | Basic: wiki tier and Markdown (OKF) export |
| 3 | **Agents draft, people approve,** in tools the team already uses: repository Markdown pull requests first, then Confluence or Notion. No Astrocyte review UI. | Designed ([`anchored-documents.md`](anchored-documents.md)), not built |
| 4 | **Every claim checkable:** sources, anchors, trust, status, and a "may be stale" flag when what it describes changes. | Designed ([`anchored-documents.md`](anchored-documents.md)), not built |

**Two properties make the corner real for an organisation:**
- **Vendor-neutral.** Low effort means Astrocyte is present in whatever AI tool
  people already use (Claude, ChatGPT, Gemini, or any agent), with no switching.
- **Organisation- and team-aware.** High control means team permissions and
  organisation policy apply the same way across every vendor, and to documents
  written back into other systems.

The gaps between today and this objective are tracked in
[`sota-roadmap-m45-m48.md`](sota-roadmap-m45-m48.md) §9 item 16.

---

This document translates recurring **organizing ideas** from astrocyte biology (see `neuroscience-astrocyte.md` and the review in `references/AstrocyteFromthePhysiologytotheDisease.pdf`) into **engineering metaphors** for AI systems. It is analogy, not a claim that software should mimic wetware literally.

---

## 1. Separation of concerns: fast signaling vs. slow regulation

**Biology:** Neurons carry rapid, spike-like signaling; astrocyte Ca²⁺ signals are typically slower and integrate multiple inputs. Astrocyte modulate the **context** in which fast synaptic events occur (uptake, volume, tonic release).

**Design idea:** Pair **fast inference or action channels** (analogous to neurons) with **slower supervisory or homeostatic layers** that update on different timescales - rate limits, budget resets, consolidation of memory, or calibration of uncertainty. Avoid forcing one subsystem to do both millisecond decisions and long-horizon maintenance.

---

## 2. Tripartite synapse: explicit “third party” at the exchange

**Biology:** The tripartite synapse treats the astrocyte as a participant alongside pre- and postsynaptic elements.

**Design idea:** For any core interaction (e.g., user ↔ model, agent ↔ tool, two agents negotiating), add an explicit **mediating component** responsible for:

- **Clearing** stale or overloaded context (like neurotransmitter reuptake).
- **Gating** when consolidation or pruning should run.
- **Observing** both sides without replacing their primary logic.

This is structurally similar to a **policy/memory bus**, **orchestrator**, or **context manager** - but the astrocyte analogy stresses **continuous environmental stewardship**, not one-off routing.

---

## 3. Homeostasis: keep the extracellular “milieu” within bounds

**Biology:** K⁺ buffering, water/ion channels, and transporter systems prevent runaway excitation and osmotic stress.

**Design idea:** Implement **hard and soft limits** on:

- Token or context length, queue depth, and fan-out.
- Repetition, redundancy, or contradictory claims in working memory.
- Resource use (compute, storage, API calls).

Like astrocytic uptake, these mechanisms should be **always-on constraints**, not only error handlers after failure.

---

## 4. Heterogeneity: specialized glia for specialized tissue

**Biology:** Astrocytes differ by region and lineage; reactive states (A1/A2-like) are not one-size-fits-all.

**Design idea:** Prefer **multiple astrocyte-like modules** with clear profiles - for example:

- One tuned for **sanitization and deduplication** of memory.
- One for **cross-agent coordination** and conflict detection.
- One for **safety / policy** alignment with external rules.

Avoid a single “god module” that tries to embody every slow regulatory function.

---

## 5. Coupling computation to “metabolism” and supply

**Biology:** Astrocytes link synaptic activity to metabolic support and vascular coupling; failure starves or stresses neurons.

**Design idea:** Tie **reasoning depth, retrieval breadth, and tool use** to **observed supply signals**: latency budgets, cost ceilings, user priority, or data freshness. An “astrocyte” layer can **downshift** expensive strategies when the system is resource-constrained, analogous to reduced lactate shuttle or impaired support in disease models.

---

## 6. Barrier and interface maintenance (BBB analogue)

**Biology:** Astrocyte–endothelial interactions help maintain a selective barrier and stable internal environment.

**Design idea:** A dedicated component can enforce **what crosses boundaries**: external knowledge ingestion, plugin APIs, PII, or untrusted tool outputs. It **induces** downstream “tight junction” behavior - schemas, validation, sandboxing - rather than assuming each neuron-like agent will reimplement isolation correctly.

---

## 7. Pruning and phagocytosis: structured forgetting

**Biology:** Astrocytes participate in synaptic pruning (e.g., MEGF10/MERTK pathways) and clearance of debris or pathological protein; microglia are partners in this ecosystem.

**Design idea:** Schedule **explicit pruning**: merge redundant embeddings, archive low-utility traces, remove superseded facts, and compact conversation history with **loss functions** tied to downstream task quality - not only raw age. Pair **generation** (neuron-like) with **cleanup** (glia-like) so memory does not grow monotonically without quality control.

---

## 8. Inflammation analogue: escalation paths and de-escalation

**Biology:** Microglial signals can push astrocyte into harmful reactive states; positive feedback can amplify damage.

**Design idea:** When detectors fire (anomalies, repeated tool failures, policy near-violations), **escalate through controlled channels** instead of broadcasting alarm state everywhere. Provide **de-escalation**: timeouts, quarantine of bad trajectories, and return to homeostatic policies once the threat model clears - mirroring the need to avoid chronic A1-like lock-in.

---

## 9. Observable, testable “astrocyte state”

**Biology:** Modern astrocyte science relies on imaging, scRNA-seq, and genetics to **measure state** rather than assume uniformity.

**Design idea:** Expose **metrics and traces** for the regulatory layer: what was pruned, what was blocked at the barrier, current load and saturation, which homeostatic rules fired. This supports debugging the **milieu**, not only final outputs - similar to monitoring extracellular chemistry rather than only spike counts.

---

## 10. Caveats

- **Analogies compress:** Astrocytes are cells with morphology, metabolism, and evolution; AI systems are algorithms and services. Map **functions**, not molecules.
- **Good astroglia can harm:** Reactive gliosis shows that the same cell type can protect or injure depending on context. Any “AI astrocyte” needs **evaluation harnesses** and rollback when regulatory logic misfires.
- **Ethics:** Biological inspiration does not license medical claims or overstated “brain-like” marketing; keep claims proportional to evidence.

---

## Summary table

| Neuroscience theme | AI design hook |
|--------------------|----------------|
| Slow Ca²⁺ / integrative signaling | Multi-timescale control; separate fast and slow loops |
| Tripartite synapse | Mediator at exchanges; context stewardship |
| Transporters / ion buffering | Bounded queues; dedup; anti-excitation limits |
| Regional heterogeneity | Specialized regulatory submodules |
| Metabolic / vascular coupling | Budget-aware depth of reasoning and retrieval |
| BBB / perivascular end feet | Ingestion boundaries; validation; sandboxing |
| Pruning / phagocytosis | Structured forgetting; memory hygiene |
| Microglia–astrocyte inflammation | Escalation with containment and de-escalation |
| scRNA-seq / imaging | Telemetry on regulatory state |

Together, these principles suggest treating **“Astrocyte” in AI** not as a single feature but as a **layered control ecology**: homeostasis, boundaries, cleanup, and slow integration wrapped around fast cognitive or generative cores.
