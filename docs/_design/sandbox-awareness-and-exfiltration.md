# Sandbox awareness and memory-API exfiltration

This note ties together **compute sandboxes** (where untrusted agent code runs), **network egress**, and **Astrocyte’ memory boundary** so memory APIs are not treated as an implicit **data exfiltration channel**.

---

## 1. Threat: memory as a channel

Untrusted or partially trusted agents often run inside **execution sandboxes** (containers, user-space kernels, microVMs, WASM, or OS-level permission fences). Those controls limit **host escape** and **filesystem** exposure, but—as emphasized in related reading—they are not a substitute for **network policy**: a workload that can still open outbound connections can **exfiltrate whatever it can read**, including material retrieved through **memory APIs**.

For Astrocyte, additional failure modes include:

- **Wrong or overly broad memory context** — e.g. recall against a **bank** or **principal** that should not be visible from this sandbox or environment.
- **Environment bleed** — **staging** or **trial** sandboxes reading **production-shaped** memory because **principal / bank / environment** mapping is ambiguous.
- **Trusted but fooled Backend for Frontend (BFF)** — the HTTP layer forwards a **principal** or **bank_id** supplied or influenced by the sandboxed agent without binding it to **cryptographic identity** or **server-side session**.

Astrocyte does **not** replace **compute** or **network** sandboxes; it must **align** memory operations with the same trust boundaries the operator intends.

---

## 2. What “sandbox-aware” means for Astrocyte

**Sandbox-aware** here means: Astrocyte and its **host integration** carry enough **context** (logical sandbox, environment, deployment tier, or equivalent) that **retain / recall / reflect** cannot accidentally or trivially cross those lines, and that operators can **audit** and **test** those boundaries.

Concrete directions (spec; implementation evolves in `astrocyte-py`, `astrocyte-rs`, and service adapters):

| Layer | Role |
|-------|------|
| **Mapping** | **Agent card id**, **sandbox id**, **tenant**, and **environment** participate in resolving **principal** and **memory bank** (and optional defaults)—see `agent-framework-middleware.md` and `architecture.md` §1. |
| **Authorization** | Per-bank **AuthZ** and grants remain mandatory for production; sandbox or environment may feed **external PDP** inputs—see `access-control.md`, `identity-and-external-policy.md`. |
| **Policy** | Stricter **TTL**, **barriers**, or **export** rules for non-prod or untrusted tiers—see `policy-layer.md`, `data-governance.md`. |
| **HTTP / Backend for Frontend (BFF)** | **Never** trust client-supplied **principal** or **bank** as sole proof; bind identity at the edge; see `_end-user/production-grade-http-service.md` §3. |
| **Observability** | Logs/traces include stable **environment / sandbox** dimensions where policies require proving **who asked what, from where**. |

Astrocyte still **does not** implement gVisor, Firecracker, or WASM; it **consumes** the operator’s notion of sandbox **at the memory contract**.

---

## 3. Compute isolation landscape (related implementations)

Isolation is **not binary**: namespaces, seccomp, user-space kernels, hardware-backed VMs, and WASM define **different boundaries** with different **attack surfaces**—see [Let’s discuss sandbox isolation](https://www.shayon.dev/post/2026/52/lets-discuss-sandbox-isolation/) (Mukherjee, 2026) for a clear breakdown, including why **egress control** is half of the story.

**Representative approaches in the market (non-exhaustive):**

| Category | Examples | Notes |
|----------|-----------|--------|
| **Containers (namespaces + cgroups + seccomp)** | Docker, Podman, Kubernetes default workloads | Fast; **shared host kernel**—treat as **visibility** and **resource** controls more than a full adversarial boundary unless hardened and combined with policy. |
| **User-space kernel** | [gVisor](https://gvisor.dev/) (`runsc`) | Interposes **Sentry**; smaller **host** syscall footprint; performance trade-offs on I/O-heavy paths. |
| **MicroVMs** | [Firecracker](https://firecracker-microvm.github.io/), [Kata Containers](https://katacontainers.io/), Cloud Hypervisor, QEMU microVM | **Guest kernel** + hardware virtualization path; stronger boundary; higher per-instance overhead than thin containers (mitigated by pooling / snapshots on some platforms). |
| **WebAssembly** | Wasmtime, Wasmer, **[WASI](https://wasi.dev/)** | No direct **syscall** surface to the host; **capability**-style host imports; toolchain and language coverage vary for “arbitrary” agent code. |
| **Local dev agents** | macOS Seatbelt, **Landlock** + seccomp (e.g. editor/agent CLIs) | **Filesystem / network ACLs** for “mostly helpful, sometimes careless” agents; different threat model than multi-tenant kernel escape. |

Cloud vendors may package these under product names (**Agent Sandbox**, hosted sandboxes, etc.); the **underlying** pattern is still **compute + network + identity** at the platform layer.

---

## 4. Network egress and memory APIs

Even with strong **compute** isolation, **unrestricted outbound HTTP** from the sandbox can leak **recalled content** or **secrets** that entered the process. Operators should combine:

- **Network:** Private connectivity to the memory **API**, **allowlist** egress, or **proxies** for outbound AI/vendor calls (see also `outbound-transport.md` for **credential gateway** patterns on framework-originated HTTP—not a substitute for sandbox egress policy, but part of defense-in-depth).
- **Memory service:** **mTLS** or private networking to Astrocyte; **short-lived credentials**; **no anonymous** recall in production.

---

## 5. Related reading

- Mukherjee, *Let’s discuss sandbox isolation* — [shayon.dev post](https://www.shayon.dev/post/2026/52/lets-discuss-sandbox-isolation/) (namespaces vs seccomp vs gVisor vs microVM vs WASM, **egress**, local agent patterns).

---

## 6. Where this work lives beyond `_design/`

| Location | Purpose |
|----------|---------|
| **`docs/_design/`** (this doc, `architecture.md`, `access-control.md`, `policy-layer.md`, `data-governance.md`) | Threat model, boundaries, policy hooks. |
| **`docs/_plugins/agent-framework-middleware.md`** | Card + **sandbox/environment** inputs to **principal / bank** resolvers. |
| **`docs/_plugins/outbound-transport.md`** | Enterprise **HTTP/TLS/proxy** and credential gateways for **framework** outbound calls. |
| **`docs/_end-user/production-grade-http-service.md`** | **Backend for Frontend (BFF)** AuthN binding, **no trusting** client-only principal, network and **API** hardening. |
| **`astrocyte-py/`** | Config schema (e.g. environment/sandbox dimensions), validation helpers, optional **context** types on `Astrocyte` calls. |
| **`astrocyte-services-py/astrocyte-gateway-py/`** | Middleware: reject or map **sandbox** headers only when **signed** or **server-side** scoped; document **trusted** vs **untrusted** headers. |
| **CI / security tests** | **AuthZ** matrix tests (sandbox A cannot read bank B); contract tests on mapping config. |

Keeping **design** in `docs/_design/` and **operational checklists** in `_end-user/` avoids duplicating the threat model in every plugin page while still giving implementers a single **spec** to cite.
