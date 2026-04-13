# Astrocyte Plugin for Azure API Management

Adds long-term memory to LLM API calls routed through Azure APIM using [policy fragments](https://learn.microsoft.com/en-us/azure/api-management/policy-fragments):

- **Pre-hook (inbound):** Recalls relevant memories from Astrocyte and injects them into the system prompt.
- **Post-hook (outbound):** Retains the assistant's response as a new memory.

Requires a running [Astrocyte standalone gateway](../../astrocyte-services-py/astrocyte-gateway-py/).

## Quick start

```bash
./deploy.sh
```

The interactive script walks you through deployment method selection and configuration. Or pass flags directly:

```bash
# Bicep
./deploy.sh --method bicep \
  --apim-name my-apim \
  --resource-group my-rg \
  --astrocyte-url https://astrocyte.internal:8900 \
  --api-key sk-...

# Terraform
./deploy.sh --method terraform \
  --apim-name my-apim \
  --resource-group my-rg \
  --astrocyte-url https://astrocyte.internal:8900 \
  --api-key sk-...

# APIOps (export for your GitOps pipeline)
./deploy.sh --method apiops --output ./my-apiops-repo/

# Portal (prints XML for manual paste)
./deploy.sh --method portal
```

## Deployment methods

| Method | Tool required | Best for |
|---|---|---|
| **Bicep** | Azure CLI (`az`) | Azure-native teams |
| **Terraform** | Terraform + AzAPI provider | Multi-cloud / Terraform-first teams |
| **APIOps** | [Azure/apiops](https://github.com/Azure/apiops) toolkit | GitOps teams with existing APIOps pipelines |
| **Portal** | Browser | Manual setup / testing |

All methods deploy the same policy fragments — pick the one that matches your existing IaC tooling.

## What gets deployed

| Resource | Type | Description |
|---|---|---|
| `astrocyte-gateway-url` | Named value | Astrocyte gateway base URL |
| `astrocyte-api-key` | Named value (secret) | API key for gateway authentication |
| `astrocyte-recall-inject` | Policy fragment | Inbound: recall + inject into system prompt |
| `astrocyte-retain-extract` | Policy fragment | Outbound: retain assistant response |

## After deployment

Add the fragments to your API policy:

```xml
<policies>
  <inbound>
    <base />
    <include-fragment fragment-id="astrocyte-recall-inject" />
  </inbound>
  <outbound>
    <base />
    <include-fragment fragment-id="astrocyte-retain-extract" />
  </outbound>
</policies>
```

## Configuration

### Bank resolution

The plugin resolves `bank_id` from the `X-Astrocyte-Bank` request header. If not set, it defaults to `"default"`. Configure your client or upstream APIM policy to set this header based on your multi-tenancy model.

### Recall settings

To change `max_results` or other recall parameters, edit the `astrocyte-recall-inject.xml` fragment and redeploy.

## Directory structure

```
azure-apim/
├── deploy.sh                          # Interactive deployment script
├── policy-fragments/
│   ├── astrocyte-recall-inject.xml    # Inbound policy fragment
│   └── astrocyte-retain-extract.xml   # Outbound policy fragment
├── deploy/
│   ├── bicep/
│   │   ├── main.bicep                 # Bicep template
│   │   └── main.bicepparam            # Parameter file
│   └── terraform/
│       ├── main.tf                    # Terraform config (AzAPI)
│       └── variables.tf               # Variable declarations
└── apiops/
    └── policy-fragments/              # APIOps-compatible layout (symlinks)
```
