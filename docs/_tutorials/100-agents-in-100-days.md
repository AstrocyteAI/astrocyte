# 100 Agents in 100 Days

:::caution[Series in progress]
This page is the **planning catalog** for the series. Daily tutorials are being written and will be linked here as they publish. Browse the use cases below to see what's coming.
:::

Each day adds **one** focused tutorial or example: runnable demos, integration patterns, and production-minded snippets. The catalog below is the **backlog** — daily tutorials will be chosen from this list and linked here as they ship.

## Why Astrocyte — the honest filter

Astrocyte's value proposition is **AI long-term memory**. Long-term memory earns its infrastructure in three shapes:

1. **One user, many agents.** You already work across Claude, Codex, Gemini CLI, OpenCode, GLM — and next quarter's tool. Memory locked inside one vendor's product dies when you switch surfaces. Astrocyte is the memory layer *all* your agents share: retain from one, recall from any other.
2. **Many users, shared memory.** Teams and organizations where each person's agents read from and contribute to institutional memory — with banks, access control, PII policy, and audit. This is Astrocyte in server mode.
3. **Corpus-scale memory.** When "memory" is 84 SEC filings or three years of support tickets — larger than any context window or file tree — retrieval quality *is* the product. This is what the LoCoMo / LongMemEval / FinanceBench benches measure.

And the anti-pattern, stated plainly: **one user talking to one AI does not need Astrocyte.** A local `CLAUDE.md` or a folder of markdown notes is simpler and works. Tutorials in this series must never pretend otherwise — every tutorial has to justify itself against the markdown baseline.

## The two tests every tutorial must pass

- **Accumulation test.** Does memory compound over sessions? Stateless Q&A ("what does this error code mean?") fails — nothing worth remembering tomorrow.
- **Markdown test.** Why aren't local files enough? Passing answers: the memory is consumed by **more than one agent** (shape 1), **more than one user** (shape 2), or is **too large for files-in-context** (shape 3).

**The signature proof in every tutorial:** retain from one surface, recall from another — a second agent CLI, a second user, or a second machine. If a tutorial can't stage that moment, it doesn't belong in the series.

**Stack context:** The **v0.11.x** line builds on the v0.9.x feature surface (wiki compile, time travel, gap analysis, entity resolution, AGE graph adapter) and the v0.10.x retrieval-quality work (HyDE, observation consolidation, agentic reflect, fact-level causal links, link expansion). v0.11.x adds schema-per-tenant isolation, mental models as a first-class SPI, intent-driven reflect routing, and the four-preset benchmark ablation matrix. Tier 1 storage adapters (`astrocyte-postgres`, `astrocyte-age`, etc.), optional **`astrocyte-gateway-py`**, optional **`recall_authority`**, and ingest connectors (Kafka, Redis, GitHub poll) all ship out of the box. See [`CHANGELOG.md`](https://github.com/AstrocyteAI/astrocyte/blob/main/CHANGELOG.md) for release notes.

## What to expect

- Agent patterns (tool use, multi-step flows) **with durable memory** via Astrocyte — always demonstrated across at least two agents or two users, never one-agent-in-a-vacuum
- Pairings with the [reference REST gateway](/end-user/production-grade-http-service/), [poll ingest](/end-user/poll-ingest-gateway/), or an [API edge](/end-user/gateway-edge-and-api-gateways/) where the tutorial needs a server-side hook
- [Use-case profiles](/design/use-case-profiles/) (`support`, `coding`, `personal`, `research`) as the starting policy configuration for each tutorial
- Occasional deep links into the [plugin SPIs](/plugins/provider-spi/), [agent framework middleware](/plugins/agent-framework-middleware/), and [design docs](/design/architecture/)

## Suggested opening week

The first five days cover all three memory shapes, and each day reuses the previous day's setup:

| Day | Tutorial | Shape it proves |
|-----|----------|-----------------|
| 1 | **#6 Personal CRM** — retain via a Pydantic-AI script, recall from Claude Code over MCP | Cross-agent, smallest possible signature proof |
| 2 | **#24 Side-project coding helper** — Claude Code and Codex sharing one repo's decisions | Cross-agent, for the developer audience |
| 3 | **#5 Personal second brain** — notes, PDFs, and saved links via the Document Engine | Corpus + cross-agent |
| 4 | **#30 Club / PTA secretary** — two members' agents, one institutional memory | Shared, no enterprise infra needed |
| 5 | **#46 Internal KB Q&A with citations** — server mode, `research` profile | Shared + corpus, first enterprise day |

## Use case catalog (before Day 1)

This section is the **backlog** for the series—not a commitment to order. Items are grouped so we can rotate across domains (productivity, home, commerce, support, risk, engineering, etc.) and cover different agent shapes (single-turn Q&A, tool loops, scheduled jobs, human-in-the-loop). Each group is tagged with the **memory shape** that justifies it against the markdown baseline: _cross-agent_ (one user, many AIs), _shared_ (household or team), or _corpus_ (too large for files-in-context).

> **Catalog provenance:** rewritten 2026-07 against the memory-shape filter. Rows that were stateless or one-shot were merged into compounding neighbors or cut; every surviving row states the memory that accumulates.

### Consumer use cases (individuals & households)

Consumer tutorials are built **cross-agent by default**: the same memory bank consumed from at least two different tools — e.g. retained via Claude Code over MCP, recalled from a Gemini CLI session, or vice versa. Household groups add the _shared_ shape: two family members' agents reading one bank.

**Productivity & communication** — _cross-agent_

1. Daily briefing whose **source preferences and read history** live in one bank consumed by your phone assistant and your desktop agent alike
2. Inbox triage that **learns your priority signals** over weeks — sender weights, thread patterns, unsubscribe candidates
3. Calendar negotiation grounded in **accumulated scheduling preferences** (protected hours, timezone habits, meeting-length tolerances)
4. Meeting notes → decisions, owners, and follow-up drafts — a **decision log** both you and your teammates' agents can recall (_shared_)
5. Personal "second brain": recall across notes, PDFs, saved links, voice memos, and long-thread summaries — one corpus, every agent (_corpus_, **flagship**)
6. Personal CRM: birthdays, last touchpoints, gentle nudge suggestions — retained by one agent, recalled by any other (**flagship — Day 1**)
7. Writing assistant whose **voice examples and style canon** follow you from Claude to Gemini to whatever ships next

**Money, shopping, subscriptions** — _cross-agent · shared (household)_

8. Spending categorization and "where did my money go?" Q&A over **months of accumulated exports**
9. Subscription audit: renewal dates, duplicate tools, downgrade prompts — plus usage patterns compiled for **bill-negotiation prep**
10. Comparison shopping grounded in **your accumulated buying criteria and past purchase outcomes** (price sensitivity, warranty weight, return-policy lessons)
11. Household document vault: receipts, warranties, leases, and consumer contracts — "what's covered, until when, and what did we agree to?" for **both partners' agents** (_corpus_)

**Home, mobility, logistics** — _shared (household)_

12. Relocation as **shared project memory**: utilities, mail, registrations — two people's agents working one checklist over months
13. Home maintenance from appliance manuals (_corpus_) plus a **seasonal maintenance log** that compounds year over year
14. Home inventory for insurance with photo-to-line-item assist — grows with every purchase
15. Car maintenance log: service history, part numbers, and mechanic notes accumulated across years and drivers
16. Smart-home routine suggestions grounded in **household routine memory** both partners' assistants share

**Food, health-adjacent, wellness** — _shared (household)_ _(stay within non-clinical boundaries: planning and education, not diagnosis)_

17. Meal planning from pantry, **accumulated diet preferences and macro goals**, and time budget — one bank, two cooks
18. Recipe scaling, substitutions, and grocery list consolidation — a **house recipe memory** that remembers what worked
19. Fitness programming from equipment, schedule constraints, and a **training log** (volume, soreness notes, trends) that compounds
20. Sleep, stress, or reflection journaling with pattern surfacing — logged from your phone, analyzed from your desktop (no medical claims)

**Learning, career, creativity** — _cross-agent_

21. Tutor with **spaced prompts grounded in your materials and your progress history** — spaced repetition *is* long-term memory (**flagship**)
22. Language practice partner whose corrections are scoped to your **accumulated learner level**
23. Resume and job-description alignment from a **living experience-bullet bank** — cover letters and interview prep draw from the same memory
24. Side-project coding helper: Claude Code **and** Codex working the same repo, sharing decisions, conventions, and dead ends through one bank (**flagship — the developer demo**)
25. Creative writing partner with a **world bible / canon** that follows you across every tool you draft in
26. Reading list curator: **accumulated saved articles** ranked into "read next" (_corpus_)

**Family, social, local life** — _shared (household)_

27. Kids' activity and weekend planning within **accumulated constraints** (ages, budget, travel radius, what flopped last time)
28. Gift coordination: preferences and occasions accumulated over years — **two parents' agents that never buy the same gift twice**
29. Trip itinerary drafts grounded in **past-trip learnings** (pace tolerance, accessibility needs, "never book the 6am flight again")
30. Club or PTA secretary: agendas, minutes, and decisions — **institutional memory of a small org**, no enterprise setup required (**flagship — shared**)
31. Wedding or large event task graph as **shared project memory**: couple, planner, and family agents on one dependency board

**Hobbies & specialized leisure** — _cross-agent_

32. Gardening planner whose **season logs compound year over year** by hardiness zone
33. Pet care schedules and vet visit prep from **accumulated past notes** — every family member's agent knows the dog (_shared_)
34. Maker / electronics **project memory**: BOMs, supplier lessons, and design decisions shared between your CAD assistant and your chat agent
35. Photography curation over a **photo-metadata corpus**: albums, captions, duplicate detection hints (_corpus_)

**Accessibility & life admin** — _cross-agent · shared (caregiver)_

36. Form-filling companion drawing on a **PII-governed profile bank** (user verifies every field; showcases the `support` profile's redaction policy)
37. Immigration or benefits checklists as structured trackers spanning **months of process and multiple helpers' agents** (informational)

### Enterprise use cases (organizations)

Enterprise tutorials run Astrocyte in **server mode** — shape 2 by construction: multi-user banks, access control, PII policy, and audit from the [use-case profiles](/design/use-case-profiles/). Many also exercise _corpus_ scale (ticket history, contract repositories, runbooks) where files-in-context breaks down. The signature proof here is **two users, one memory**: one teammate's agent retains, another teammate's agent recalls.

**Customer-facing: support, success, sales**

38. Tier-1 support triage with macros, entitlements, and escalation rules
39. Technical support co-pilot grounded in runbooks and known-error DBs (_corpus_)
40. Customer health digests for CSMs from tickets, usage, and CRM notes
41. Sales qualification assistant: ICP fit, stakeholder map, next-best action
42. Outbound personalization within brand and compliance templates
43. RFP / security questionnaire first drafts from a **canonical answer bank** that improves with every submission

**Internal IT, workplace, HR**

44. IT service desk resolutions suggester with change-risk hints
45. Employee onboarding bot across systems-of-record handoffs
46. Internal knowledge base Q&A (Confluence, Notion, SharePoint) with citations (_corpus_, **flagship**)
47. HR policy Q&A with locale and employee-type variants
48. Benefits navigator with life-event checklists
49. Leave, overtime, or timesheet anomaly explainer grounded in the **policy corpus** and precedent decisions

**Engineering, quality, platform**

50. CI failure summarizer: likely owner, **flakiness history**, similar incidents
51. Release notes from commits/PRs mapped to customer impact
52. On-call handoff summarizer across alerts, deploys, and bridges — institutional memory of incidents
53. Code review comment generator with **repo style rules every developer's agent shares** (_cross-agent too_)
54. Test ideas from user stories, grounded in the team's **accumulated risk-framing and past escaped-defect patterns**
55. API doc drift checker with **spec history**: what changed, when, and what broke last time
56. Developer onboarding navigator for monorepo topology and service owners

**Security, risk, compliance**

57. Security alert enrichment: MITRE mapping, asset context, playbook step-one
58. Phishing or suspicious-message triage assistant for SOC L1 — **campaign patterns accumulate**
59. Vendor due diligence checklist from uploaded SOC2 / SIG questionnaires (_corpus_)
60. Data-classification and sensitivity tag suggestions on document batches
61. Audit narrative prep: control mapping to evidence inventory lists
62. Access review campaign assistant (overprovisioned role surfacing—not decisions)

**Finance & procurement** _(accounting / finance sign-off for binding numbers; counsel for commitments)_

63. Month-end close checklist and flux-comment first drafts — **flux narratives compound quarter over quarter**
64. AP exception handling: three-way match breaks explained with next step, grounded in **past exception patterns**
65. Procurement bid tabulation and **non-binding** award recommendation memo from house templates and award history

**Legal department & legal operations** _(privilege, work product, retention, and local bar rules apply—GC-approved AI policy; **no** autonomous legal advice)_

66. Contract review **prep**: clause checklist hits, defined-term consistency, and diffs vs house templates (escalation to counsel)
67. Legal hold **tracking** Q&A: custodian lists, scope narratives, and collection status **read-only** from approved systems
68. NDA / MSA / order-form **playbook** assist: flagged deviations, missing exhibits, and fallback language options for lawyer edit
69. Data-processing addenda (DPA / SCCs) cross-check against data map, subprocessors, and transfer mechanisms
70. Matter intake: conflicts pre-screen questionnaire assembly from client / counterparty facts—**requires** conflicts partner clear
71. eDiscovery logistics: custodian interview **prep**; preservation notice **shells** with merge fields—no send without review
72. Privilege log drafting support: suggested descriptions from metadata + reviewer tags—**no** privileged conclusions
73. Deposition / hearing / filing **deadline** calendars from rules sets and docket extracts—human verifies in jurisdiction
74. Regulatory or agency comment-letter **outline** + citation pull from docket IDs and internal position memos
75. Trademark clearance **knockout** memo scaffolding over an **accumulating search-history corpus**—**not** a clearance opinion
76. Patent prior-art landscape **memo draft** over the team's **accumulated classification and query corpus**—paired with searcher / prosecutor workflow
77. Outside-counsel invoice review: phase-code compliance, block-billing patterns, staffing mix vs guidelines
78. Board / committee resolution packs: cross-check duties charter, prior minutes, and required approvals
79. Commercial claims intake: limitation-period **calendar hints** from rough dates and governing law—**verified** by counsel
80. Entity management: officer/director / registered-agent **delta** reports vs filings (jurisdiction-specific tooling)
81. Policy / attestation campaigns: plain-language FAQ over codes of conduct, insider trading, gifts—**non-binding** employee Q&A
82. Sanctions / export-classification **checklist** triage for product specs—routed to trade-compliance + counsel
83. Contract lifecycle: renewal / termination **notice windows**, price ramps, and auto-renew flags from CLM extracts
84. Litigation / investigation budget **scenarios** from historical phase spend and staffing models—planning aid only

**Operations, supply chain, field**

85. Logistics exception agent: ETA slips, alternate routes, customer comms drafts — **exception patterns accumulate**
86. Inventory reorder hints under MOQ, lead time, and storage constraints
87. Quality inspection summaries from structured checklists and free text
88. Field service notes → structured work orders and parts lists — **field and office agents share one memory**
89. Manufacturing SOP Q&A for floor tablets with **document version pins** (_corpus_, **flagship**)
90. Facilities ticketing triage: category, priority, likely vendor skill

**Data, analytics, product**

91. Data catalog natural language queries with lineage snippets (_corpus_)
92. "Why did this metric move?" analyst grounded in **past investigations** — guardrails on causality language
93. Experiment readouts: segments, novelty, and recommended follow-ups
94. Product requirement ambiguity checker against the team's **accumulated acceptance-criteria standards**
95. Churn risk narrative for accounts with feature-usage surrogates

**Industry-flavored** _(each needs explicit policy + human gates)_

96. Prior authorization checklist assistant for admin staff (not clinical decisioning)
97. Pharmacovigilance case intake structuring from free-text reports (validated workflow)
98. Insurance FNOL triage: missing fields, fraud sketches, **human adjudication**
99. Loan origination document completeness tracker
100. Wealth advisor meeting prep from CRM notes (restricted retention and disclaimers)

**Marketing, comms, design systems**

101. Campaign brief distiller across channel plans and brand guidelines (_corpus_)
102. Crisis monitoring digest from feeds with source diversity requirements
103. Content localization reviewer — the **glossary and banned-claims list is itself shared memory** every reviewer's agent consults
104. Web accessibility findings explainer with **per-component findings history** (paired with tooling output)

**Strategy & leadership**

105. Executive briefing from departmental weeklys with conflict detection
106. OKR drafting assistant aligned to **historical commitments and dependencies** — institutional memory as input
107. Architecture Decision Record (ADR) co-author from meeting notes — ADR corpus + whole team + every developer's agent (**flagship — all three shapes**)
108. Post-merger integration task master across workstreams (dependency graph)

---

### How this turns into 100 days

- **Pick one row per day** (or merge two thin rows into a richer tutorial). The catalog is **intentionally >100 numbered rows** (consumer **1–37**; enterprise **38–108**) so we can swap domains when a dependency lands (e.g., [gateway operations](/end-user/production-grade-http-service/), [poll ingest](/end-user/poll-ingest-gateway/), identity plugins).
- **Every tutorial must pass the two tests** (accumulation + markdown) and stage the signature proof: retain on surface A, recall on surface B. A row that can't stage it gets merged into one that can, or cut.
- **Tag each tutorial twice** — by agent shape (reactive chat, scheduled job, tool loop with approvals, multi-agent handoff, offline batch) and by memory shape (_cross-agent_, _shared_, _corpus_).
- **Enterprise days** should link to [identity and policy](/design/identity-and-external-policy/) docs when touching PII or regulated flows.

## Start today

While Day 1 is being prepared:

- [Introduction](/introduction/) — scope, Python/Rust layout, and reading paths
- [Quick Start](/end-user/quick-start/) — install core + run Compose
- [Production-grade HTTP service](/end-user/production-grade-http-service/) — checklist for operating behind an API
- [Poll ingest with the standalone gateway](/end-user/poll-ingest-gateway/) — GitHub poll + `GET /health/ingest`
- [Gateway edge & API gateways](/end-user/gateway-edge-and-api-gateways/) — Kong / APISIX patterns, CORS, rate limits
- [Built-in pipeline](/design/built-in-pipeline/) — how Tier 1 retrieval + intelligence stages fit together

---

_Series hub: add new days as pages under `docs/_tutorials/` and link them from this document, or organize by `day-01-*.md`, `day-02-*.md`, as you publish._
