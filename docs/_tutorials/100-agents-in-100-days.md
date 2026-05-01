# 100 Agents in 100 Days

:::caution[Series in progress]
This page is the **planning catalog** for the series. Daily tutorials are being written and will be linked here as they publish. Browse the use cases below to see what's coming.
:::

Each day adds **one** focused tutorial or example: runnable demos, integration patterns, and production-minded snippets. The catalog below is the **backlog** — daily tutorials will be chosen from this list and linked here as they ship.

**Stack context:** The **v0.9.x** line includes Tier 1 storage adapters, optional **`astrocyte-gateway-py`**, optional **`recall_authority`**, ingest connectors (Kafka, Redis, GitHub poll), and M8–M11 features such as wiki compile, time travel, gap analysis, and entity resolution. See [`CHANGELOG.md`](https://github.com/AstrocyteAI/astrocyte/blob/main/CHANGELOG.md) for release notes.

## What to expect

- Agent patterns (tool use, multi-step flows) **with durable memory** via Astrocyte
- Pairings with the [reference REST gateway](/end-user/production-grade-http-service/), [poll ingest](/end-user/poll-ingest-gateway/), or an [API edge](/end-user/gateway-edge-and-api-gateways/) where the tutorial needs a server-side hook
- Occasional deep links into the [plugin SPIs](/plugins/provider-spi/), [agent framework middleware](/plugins/agent-framework-middleware/), and [design docs](/design/architecture/)

## Use case catalog (before Day 1)

This section is the **backlog** for the series—not a commitment to order. Items are grouped so we can rotate across domains (productivity, home, commerce, support, risk, engineering, etc.) and cover different agent shapes (single-turn Q&A, tool loops, scheduled jobs, human-in-the-loop).

### Consumer use cases (individuals & households)

**Productivity & communication**

1. Daily briefing from news, weather, and calendar (with opt-in sources)
2. Inbox triage: priority signals, draft replies, unsubscribe candidates
3. Calendar negotiation: find slots, propose reschedules, timezone hygiene
4. Meeting notes → decisions, owners, and follow-up drafts
5. Personal “second brain”: recall across notes, PDFs, and saved links
6. Voicemail and long chat threads → short summaries and tasks
7. Voice memo capture → titled notes with tags for a PKM tool
8. Personal CRM: birthdays, last touchpoints, gentle nudge suggestions
9. Writing assistant: tone-matching emails and personal posts (user supplies voice examples)

**Money, shopping, subscriptions**

10. Spending categorization and “where did my money go?” Q&A over exports
11. Subscription audit: renewal dates, duplicate tools, downgrade prompts
12. Comparison shopping against user criteria (price, warranty, reviews, return policy)
13. Receipt and warranty vault: “What’s covered, and until when?”
14. Bill negotiation prep: compile usage patterns and competitor offers (informational)

**Home, mobility, logistics**

15. Move / relocation checklist across utilities, mail, registrations
16. Home maintenance from appliance manuals and seasonal reminders
17. Home inventory for insurance with photo-to-line-item assist
18. Car maintenance log and “what does this dash light mean?” (informational)
19. Smart-home routine suggestions from routines you already use
20. HOA / lease plain-language Q&A over uploaded docs (not legal advice)

**Food, health-adjacent, wellness** _(stay within non-clinical boundaries: planning and education, not diagnosis)_

21. Meal planning from pantry, diet preferences, and time budget
22. Recipe scaling, substitutions, and grocery list consolidation
23. Nutrition label + goal framing (macros targets user supplies)
24. Fitness programming assistant from equipment and schedule constraints
25. Sleep or stress diary pattern surfacing (no medical claims)
26. Meditation / reflection journaling prompts on a schedule

**Learning, career, creativity**

27. Tutor for a subject with spaced prompts grounded in user materials
28. Language practice partner with corrections scoped to learner level
29. Resume and job-description alignment; cover letter first drafts
30. Interview prep from a job posting and the user’s experience bullets
31. Side-project coding helper with repo context (small scope per session)
32. Creative writing partner (plot, dialogue, constraints)
33. Reading list curator: turn saved articles into ranked “read next”
34. Music or media discovery from mood goals and history (via APIs you connect)

**Family, social, local life**

35. Kids’ activity ideas within age, budget, and travel radius
36. Gift ideas from stated preferences, occasions, and constraints
37. Local events and weekend planning with filters (family-friendly, free, indoor)
38. Trip itinerary drafts balancing pace, accessibility, and must-sees
39. Club or PTA secretary: agendas and minutes from rough notes
40. Board-game rules explainers and house-rule proposals

**Hobbies & specialized leisure**

41. Gardening planner by hardiness zone and season
42. Pet care schedules; vet visit prep from past notes
43. Sports training log insights (volume, soreness notes, trends)
44. Maker / electronics BOM sanity checks and supplier compare
45. Photography curation: albums, captions, and duplicate detection hints

**Accessibility & life admin**

46. Form-filling companion for complex applications (user verifies every field)
47. Plain-language summaries of long consumer contracts (with disclaimers)
48. Immigration or benefits checklists as structured trackers (informational)
49. Wedding or large event task graph with dependency reminders

### Enterprise use cases (organizations)

**Customer-facing: support, success, sales**

50. Tier-1 support triage with macros, entitlements, and escalation rules
51. Technical support co-pilot grounded in runbooks and known-error DBs
52. Customer health digests for CSMs from tickets, usage, and CRM notes
53. Sales qualification assistant: ICP fit, stakeholder map, next-best action
54. Outbound personalization within brand and compliance templates
55. RFP / security questionnaire first drafts from a canonical answer bank

**Internal IT, workplace, HR**

56. IT service desk resolutions suggester with change-risk hints
57. Employee onboarding bot across systems-of-record handoffs
58. Internal knowledge base Q&A (Confluence, Notion, SharePoint) with citations
59. HR policy Q&A with locale and employee-type variants
60. Benefits navigator with life-event checklists
61. Leave, overtime, or timesheet anomaly explainer for employees and managers

**Engineering, quality, platform**

62. CI failure summarizer: likely owner, flakiness history, similar incidents
63. Release notes from commits/PRs mapped to customer impact
64. On-call handoff summarizer across alerts, deploys, and bridges
65. Code review comment generator with repo style rules
66. Test ideas from user stories and risk framing (edge cases, negative paths)
67. API doc drift checker vs live OpenAPI and code annotations
68. Developer onboarding navigator for monorepo topology and service owners

**Security, risk, compliance**

69. Security alert enrichment: MITRE mapping, asset context, playbook step-one
70. Phishing or suspicious-message triage assistant for SOC L1
71. Vendor due diligence checklist from uploaded SOC2 / SIG questionnaires
72. Data-classification and sensitivity tag suggestions on document batches
73. Audit narrative prep: control mapping to evidence inventory lists
74. Access review campaign assistant (overprovisioned role surfacing—not decisions)

**Finance & procurement** _(accounting / finance sign-off for binding numbers; counsel for commitments)_

75. Month-end close checklist and flux-comment first drafts
76. AP exception handling: three-way match breaks explained with next step
77. Procurement bid tabulation and **non-binding** award recommendation memo

**Legal department & legal operations** _(privilege, work product, retention, and local bar rules apply—GC-approved AI policy; **no** autonomous legal advice)_

78. Contract review **prep**: clause checklist hits, defined-term consistency, and diffs vs house templates (escalation to counsel)
79. Legal hold **tracking** Q&A: custodian lists, scope narratives, and collection status **read-only** from approved systems
80. NDA / MSA / order-form **playbook** assist: flagged deviations, missing exhibits, and fallback language options for lawyer edit
81. Data-processing addenda (DPA / SCCs) cross-check against data map, subprocessors, and transfer mechanisms
82. Matter intake: conflicts pre-screen questionnaire assembly from client / counterparty facts—**requires** conflicts partner clear
83. eDiscovery logistics: custodian interview **prep**; preservation notice **shells** with merge fields—no send without review
84. Privilege log drafting support: suggested descriptions from metadata + reviewer tags—**no** privileged conclusions
85. Deposition / hearing / filing **deadline** calendars from rules sets and docket extracts—human verifies in jurisdiction
86. Regulatory or agency comment-letter **outline** + citation pull from docket IDs and internal position memos
87. Trademark clearance **knockout** memo scaffolding from search results—**not** a clearance opinion
88. Patent prior-art landscape **memo draft** from classifications and queries—paired with searcher / prosecutor workflow
89. Outside-counsel invoice review: phase-code compliance, block-billing patterns, staffing mix vs guidelines
90. Board / committee resolution packs: cross-check duties charter, prior minutes, and required approvals
91. Commercial claims intake: limitation-period **calendar hints** from rough dates and governing law—**verified** by counsel
92. Entity management: officer/director / registered-agent **delta** reports vs filings (jurisdiction-specific tooling)
93. Policy / attestation campaigns: plain-language FAQ over codes of conduct, insider trading, gifts—**non-binding** employee Q&A
94. Sanctions / export-classification **checklist** triage for product specs—routed to trade-compliance + counsel
95. Contract lifecycle: renewal / termination **notice windows**, price ramps, and auto-renew flags from CLM extracts
96. Litigation / investigation budget **scenarios** from historical phase spend and staffing models—planning aid only

**Operations, supply chain, field**

97. Logistics exception agent: ETA slips, alternate routes, customer comms drafts
98. Inventory reorder hints under MOQ, lead time, and storage constraints
99. Quality inspection summaries from structured checklists and free text
100. Field service notes → structured work orders and parts lists
101. Manufacturing SOP Q&A for floor tablets with document version pins
102. Facilities ticketing triage: category, priority, likely vendor skill

**Data, analytics, product**

103. Data catalog natural language queries with lineage snippets
104. “Why did this metric move?” analyst with guardrails on causality language
105. Experiment readouts: segments, novelty, and recommended follow-ups
106. Product requirement ambiguity checker: missing acceptance criteria
107. Churn risk narrative for accounts with feature-usage surrogates

**Industry-flavored** _(each needs explicit policy + human gates)_

108. Prior authorization checklist assistant for admin staff (not clinical decisioning)
109. Pharmacovigilance case intake structuring from free-text reports (validated workflow)
110. Insurance FNOL triage: missing fields, fraud sketches, **human adjudication**
111. Loan origination document completeness tracker
112. Wealth advisor meeting prep from CRM notes (restricted retention and disclaimers)

**Marketing, comms, design systems**

113. Campaign brief distiller across channel plans and brand guidelines
114. Crisis monitoring digest from feeds with source diversity requirements
115. Content localization reviewer for glossary and banned-claims lists
116. Web accessibility findings explainer for dev teams (paired with tooling output)

**Strategy & leadership**

117. Executive briefing from departmental weeklys with conflict detection
118. OKR drafting assistant aligned to historical commitments and dependencies
119. Architecture Decision Record (ADR) co-author from meeting notes
120. Post-merger integration task master across workstreams (dependency graph)

---

### How this turns into 100 days

- **Pick one row per day** (or merge two thin rows into a richer tutorial). The combined catalog is **intentionally >100 numbered rows** (consumer **1–49**; enterprise **50–120**) so we can swap domains when a dependency lands (e.g., [gateway operations](/end-user/production-grade-http-service/), [poll ingest](/end-user/poll-ingest-gateway/), identity plugins).
- **Tag each tutorial** by agent shape: reactive chat, scheduled job, tool loop with approvals, multi-agent handoff, or offline batch.
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
