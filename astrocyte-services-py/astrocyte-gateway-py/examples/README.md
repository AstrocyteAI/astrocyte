# Gateway configuration examples

Each **scenario** lives in its **own directory** with a small, predictable entry file so you can point the gateway at one folder:

```bash
export ASTROCYTE_CONFIG_PATH="$(pwd)/astrocyte.yaml"   # run from inside the chosen example directory
astrocyte-gateway-py
```

Or use an absolute path, e.g. `…/examples/tier1-minimal/astrocyte.yaml`.

Paths inside YAML (e.g. `mip_config_path: ./mip.minimal.yaml`) resolve **relative to the main config file’s directory**.

| Directory | Main config | Purpose |
|-----------|-------------|---------|
| [`tier1-minimal/`](./tier1-minimal/) | [`astrocyte.yaml`](./tier1-minimal/astrocyte.yaml) | Tier 1 **in-memory** + **mock** LLM — dev / CI smoke. |
| [`tier1-pgvector/`](./tier1-pgvector/) | [`astrocyte.yaml`](./tier1-pgvector/astrocyte.yaml) | Tier 1 + **PostgreSQL + pgvector** — set `DATABASE_URL` / `ASTROCYTE_PG_DSN`, run migrations for production-shaped deploys. |
| [`tier1-with-mip/`](./tier1-with-mip/) | [`astrocyte.yaml`](./tier1-with-mip/astrocyte.yaml) | Tier 1 + **MIP** (includes [`mip.minimal.yaml`](./tier1-with-mip/mip.minimal.yaml)). |
| [`mip-education/`](./mip-education/) | [`astrocyte.yaml`](./mip-education/astrocyte.yaml) + [`mip.yaml`](./mip-education/mip.yaml) | Tier 1 + **illustrative MIP** (education-style banks/rules). The MIP file can also be reused from another `astrocyte.yaml` via `mip_config_path`. |
| [`webhook-ingest/`](./webhook-ingest/) | [`astrocyte.yaml`](./webhook-ingest/astrocyte.yaml) | Tier 1 + **`sources:`** for **`POST /v1/ingest/webhook/{source_id}`** (see gateway README). |

The canonical large MIP demo remains under **`astrocyte-py/examples/`** (`mip.yaml` + `astrocyte-mip.yaml`); that pair targets **Tier 2 engine** demos. Use the directories here for **gateway + Tier 1** workflows.
