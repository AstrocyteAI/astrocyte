# Astrocytes reference server (Python)

Optional **REST** front end for [`astrocytes-py`](../astrocytes-py/). It embeds the `Astrocyte` API behind HTTP so you can try the framework without writing a host app.

**Defaults:** Tier 1 **in-memory** vector store and **mock** LLM (same pattern as conformance tests). Data is **not** durable across restarts. This package is **not** production-ready as shipped; see below.

**Production and operations:** The full checklist, architecture context, and documentation of this reference server as a **starting point** for a hardened deployment are in **[`docs/24-production-grade-http-service.md`](../docs/24-production-grade-http-service.md)** (especially §3 checklist and §4 reference server).

## Run locally

Install `astrocytes` first, then this package:

```bash
cd /path/to/astrocytes/astrocytes-py && pip install -e .
cd /path/to/astrocytes/astrocytes-server-py && pip install -e .
ASTROCYTES_HOST=0.0.0.0 ASTROCYTES_PORT=8080 astrocytes-server
```

Or:

```bash
uv run --directory astrocytes-server-py astrocytes-server
```

(with `astrocytes` installed into that environment).

## Environment

| Variable | Meaning |
|----------|---------|
| `ASTROCYTES_HOST` | Bind address (default `127.0.0.1`). Use `0.0.0.0` in containers. |
| `ASTROCYTES_PORT` | Port (default `8080`). |
| `ASTROCYTES_CONFIG_PATH` | Optional YAML file merged via `load_config` for policy/homeostasis; the server **still** wires the in-memory Tier 1 pipeline. |

## Identity

Send optional header **`X-Astrocytes-Principal`** (for example `user:alice`) when `access_control` is enabled in config and grants are configured on the `Astrocyte` instance. **This header is not authenticated** in the reference server; do not rely on it for production security (see doc 24).

## HTTP API (summary)

| Method | Path | Body (JSON) |
|--------|------|-------------|
| `GET` | `/health` | - |
| `POST` | `/v1/retain` | `content`, `bank_id`; optional `metadata`, `tags` |
| `POST` | `/v1/recall` | `query`; `bank_id` or `banks`; optional `max_results`, `max_tokens`, `tags` |
| `POST` | `/v1/reflect` | `query`, `bank_id`; optional `max_tokens`, `include_sources` |
| `POST` | `/v1/forget` | `bank_id`; optional `memory_ids`, `tags` |

OpenAPI docs: `/docs` when the server is running.

## Docker

From the **repository root** (`astrocytes/`):

```bash
docker build -f astrocytes-server-py/Dockerfile -t astrocytes-server .
docker run --rm -p 8080:8080 astrocytes-server
```

Then `GET http://localhost:8080/health`.
