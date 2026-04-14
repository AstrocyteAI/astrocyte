# Outbound transport plugins (credential gateways)

This document defines the **optional, cross-cutting** integration surface for tools that sit **between Astrocyte and the network** - for example HTTP(S) proxies that inject API credentials, corporate TLS inspection CAs, or “secret gateway” products (OneCLI-class solutions). For how this fits next to memory and LLM providers, see `architecture.md`. For the protocol sketch in the SPI doc, see `provider-spi.md` section 6.

**Inbound edge vs outbound transport:** Corporate **ingress** (API gateway, JWT validation, coarse rate limits) is separate from this SPI — see [Gateway edge & API gateways](/end-user/gateway-edge-and-api-gateways/). This document is about **egress** from the Astrocyte process to LLMs and other outbound HTTP.

---

## 1. What this is not

| Concern | Outbound transport | Memory providers (Tier 1 / 2) | LLM Provider SPI |
|---|---|---|---|
| Primary job | Configure **how** TCP/TLS/HTTP leaves the process (proxy, trust, optional gateway headers) | Store and retrieve **memories** | **`complete()`** and **`embed()`** semantics |
| Implements `retain` / `recall` | No | Yes (directly or via pipeline) | No |
| Normalizes chat APIs | No | No | No (adapters call your chosen SDK) |
| Stores or rotates secrets inside Astrocyte | **No** - secrets stay in the gateway or external vault | N/A | N/A |

Outbound transport is **not** a third memory tier and **not** an LLM gateway. It does not change the meaning of `complete()` / `embed()`; it only affects **shared HTTP client construction** used by LLM adapters and any core code that performs outbound HTTP.

---

## 2. Why a dedicated surface

Many teams use **credential gateways** or **enterprise proxies**: traffic must go through a local proxy, trust a custom CA, or attach gateway-specific headers. That requirement is **orthogonal** to which vector DB or LLM adapter is configured.

**Two integration depths:**

1. **Environment-only (no plugin)** - If standard proxy environment variables are enough (`HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`, `SSL_CERT_FILE`, etc.), Astrocyte HTTP clients should honor them. No package install required.
2. **Explicit plugin** - When a product needs **more** than env vars (combined CA bundles, dynamic discovery from a control API, `Proxy-Authorization`, container-specific paths), users install an **`astrocyte-transport-*`** package that implements the **Outbound Transport SPI**.

---

## 3. Outbound Transport SPI

Implementations configure outbound HTTP/TLS. They **do not** handle memory content, do not implement LLM semantics, and **must not** log secret material.

### 3.1 Protocol (illustrative)

```python
class OutboundTransportProvider(Protocol):
    """Optional. Configures shared HTTP clients for LLM adapters and core HTTP usage."""

    def apply(self, ctx: HttpClientContext) -> None:
        """Mutate ctx: proxy, verify, mounts, default headers, timeouts, etc."""
        ...

    def subprocess_env(self) -> dict[str, str]:
        """Extra environment variables for child processes (MCP server, eval runners)."""
        ...

    def capabilities(self) -> TransportCapabilities:
        """e.g. custom_ca, proxy_auth, mitm."""
        ...
```

`HttpClientContext` is an internal builder type (e.g. wrapping `httpx.AsyncClient` construction) owned by the core. **Single choke point:** `astrocyte.http` (or equivalent) creates clients used by LLM provider adapters and pipeline HTTP; the configured transport runs **before** any `LLMProvider.complete()` / `embed()` call.

### 3.2 Relationship to the LLM Provider SPI

- **LLM SPI** = *what* to call (`complete` / `embed`).
- **Outbound transport** = *how* the HTTP stack is built for those calls (and for any other outbound HTTP the core performs).

Adapters may still set per-request auth headers **as today**; transport plugins add **process-wide** or **client-wide** proxy/CA behavior that applies across providers.

---

## 4. Configuration

Optional block in `astrocyte.yaml` (exact keys may evolve with implementation):

```yaml
# Optional - omit for env-only proxy usage
outbound_transport:
  provider: onecli              # → astrocyte.outbound_transports:onecli
  config:
    gateway_url: http://localhost:10255
    api_key: ${ONECLI_API_KEY}
```

When `outbound_transport` is absent, the core relies on **standard library / httpx defaults** plus process environment (see section 2).

---

## 5. Discovery and packaging

- **Entry point group:** `astrocyte.outbound_transports`
- **Package naming:** `astrocyte-transport-{name}` (e.g. `astrocyte-transport-onecli` for a OneCLI-oriented adapter).
- **Registration example:**

```toml
[project.entry-points."astrocyte.outbound_transports"]
onecli = "astrocyte_transport_onecli:OneCLIOutboundTransport"
```

Community transports follow the same pattern as other Astrocyte plugins (see `ecosystem-and-packaging.md`).

---

## 6. Policy layer and observability

- **Barriers / PII / content policies** are unchanged. Transport affects **network path**, not what crosses memory barriers.
- **OTel:** implementations may set low-cardinality attributes (e.g. `astrocyte.outbound_transport.provider = onecli`) on relevant spans. **Never** emit raw tokens, API keys, or decrypted secrets in logs or spans.

---

## 7. Summary

| Layer | Role |
|---|---|
| `HTTP_PROXY` / `HTTPS_PROXY` / trust env | Baseline; works without a named plugin |
| **Outbound Transport SPI** | Optional plugins for gateway-specific or enterprise proxy wiring |
| **LLM Provider SPI** | Semantic LLM access (`complete` / `embed`) |
| **Retrieval / Memory Engine SPIs** | Retrieval adapters and full memory engines |

This keeps credential gateways **pluggable** without conflating them with memory tiers or the LLM gateway role that Astrocyte explicitly does not take (`architecture.md`).

---

## Further reading

- [Provider SPI](provider-spi/) — full SPI definitions including outbound transport protocol
- [Ecosystem & packaging](ecosystem-and-packaging/) — entry points, packaging, and distribution model
- [Gateway edge & API gateways](../end-user/gateway-edge-and-api-gateways/) — inbound edge (reverse proxies, API gateways)
- [Production-grade HTTP service](../end-user/production-grade-http-service/) — TLS, proxy, and network hardening checklist
- [Architecture](../design/architecture/) — how outbound transport fits in the layered architecture
