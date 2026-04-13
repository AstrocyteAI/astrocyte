#!/usr/bin/env bash
# Astrocyte Azure APIM deployment script.
#
# Deploys policy fragments and named values to an existing Azure API
# Management instance. Supports Bicep, Terraform, APIOps export, or
# portal-ready output.
#
# Usage:
#   ./deploy.sh                                          # Interactive
#   ./deploy.sh --method bicep    --apim-name my-apim --resource-group my-rg --astrocyte-url https://... --api-key sk-...
#   ./deploy.sh --method terraform --apim-name my-apim --resource-group my-rg --astrocyte-url https://... --api-key sk-...
#   ./deploy.sh --method apiops   --output ./my-apiops-repo/
#   ./deploy.sh --method portal

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
METHOD=""
APIM_NAME=""
RESOURCE_GROUP=""
ASTROCYTE_URL=""
API_KEY=""
OUTPUT_DIR=""

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --method)         METHOD="$2";         shift 2 ;;
    --apim-name)      APIM_NAME="$2";      shift 2 ;;
    --resource-group) RESOURCE_GROUP="$2";  shift 2 ;;
    --astrocyte-url)  ASTROCYTE_URL="$2";  shift 2 ;;
    --api-key)        API_KEY="$2";        shift 2 ;;
    --output)         OUTPUT_DIR="$2";     shift 2 ;;
    -h|--help)
      echo "Usage: $0 [--method bicep|terraform|apiops|portal] [options]"
      echo ""
      echo "Options:"
      echo "  --method          Deployment method (bicep, terraform, apiops, portal)"
      echo "  --apim-name       Azure APIM instance name (bicep/terraform)"
      echo "  --resource-group  Azure resource group (bicep/terraform)"
      echo "  --astrocyte-url   Astrocyte gateway URL (bicep/terraform)"
      echo "  --api-key         Astrocyte API key (bicep/terraform)"
      echo "  --output          Output directory (apiops)"
      exit 0
      ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# Interactive prompts
# ---------------------------------------------------------------------------
if [[ -z "$METHOD" ]]; then
  echo "Astrocyte Azure APIM Deployment"
  echo "================================"
  echo ""
  echo "Select deployment method:"
  echo "  1) bicep      — Deploy via Azure Bicep (az deployment)"
  echo "  2) terraform  — Deploy via Terraform (azapi provider)"
  echo "  3) apiops     — Export APIOps-compatible directory for your pipeline"
  echo "  4) portal     — Print policy XML for manual paste in Azure Portal"
  echo ""
  read -rp "Method [1-4]: " choice
  case "$choice" in
    1|bicep)     METHOD="bicep" ;;
    2|terraform) METHOD="terraform" ;;
    3|apiops)    METHOD="apiops" ;;
    4|portal)    METHOD="portal" ;;
    *) echo "Invalid choice."; exit 1 ;;
  esac
fi

# ---------------------------------------------------------------------------
# Bicep deployment
# ---------------------------------------------------------------------------
deploy_bicep() {
  if ! command -v az &>/dev/null; then
    echo "Error: Azure CLI (az) is required. Install: https://aka.ms/installazurecli"
    exit 1
  fi

  [[ -z "$RESOURCE_GROUP" ]] && read -rp "Resource group: " RESOURCE_GROUP
  [[ -z "$APIM_NAME" ]]      && read -rp "APIM instance name: " APIM_NAME
  [[ -z "$ASTROCYTE_URL" ]]  && read -rp "Astrocyte gateway URL: " ASTROCYTE_URL
  [[ -z "$API_KEY" ]]        && read -rsp "Astrocyte API key: " API_KEY && echo ""

  echo ""
  echo "Deploying Astrocyte policy fragments to $APIM_NAME..."
  az deployment group create \
    --resource-group "$RESOURCE_GROUP" \
    --template-file "$SCRIPT_DIR/deploy/bicep/main.bicep" \
    --parameters \
      apimName="$APIM_NAME" \
      astrocyteUrl="$ASTROCYTE_URL" \
      astrocyteApiKey="$API_KEY" \
    --output table

  echo ""
  echo "Done. Add these to your API policy:"
  echo "  Inbound:  <include-fragment fragment-id=\"astrocyte-recall-inject\" />"
  echo "  Outbound: <include-fragment fragment-id=\"astrocyte-retain-extract\" />"
}

# ---------------------------------------------------------------------------
# Terraform deployment
# ---------------------------------------------------------------------------
deploy_terraform() {
  if ! command -v terraform &>/dev/null; then
    echo "Error: Terraform is required. Install: https://developer.hashicorp.com/terraform/install"
    exit 1
  fi

  [[ -z "$RESOURCE_GROUP" ]] && read -rp "Resource group: " RESOURCE_GROUP
  [[ -z "$APIM_NAME" ]]      && read -rp "APIM instance name: " APIM_NAME
  [[ -z "$ASTROCYTE_URL" ]]  && read -rp "Astrocyte gateway URL: " ASTROCYTE_URL
  [[ -z "$API_KEY" ]]        && read -rsp "Astrocyte API key: " API_KEY && echo ""

  cd "$SCRIPT_DIR/deploy/terraform"
  terraform init -input=false
  terraform apply \
    -var="resource_group=$RESOURCE_GROUP" \
    -var="apim_name=$APIM_NAME" \
    -var="astrocyte_url=$ASTROCYTE_URL" \
    -var="astrocyte_api_key=$API_KEY"
}

# ---------------------------------------------------------------------------
# APIOps export
# ---------------------------------------------------------------------------
deploy_apiops() {
  local target="${OUTPUT_DIR:-./apiops-export}"
  mkdir -p "$target/policy-fragments"
  cp "$SCRIPT_DIR/policy-fragments/astrocyte-recall-inject.xml" "$target/policy-fragments/"
  cp "$SCRIPT_DIR/policy-fragments/astrocyte-retain-extract.xml" "$target/policy-fragments/"

  echo "APIOps-compatible directory created at: $target"
  echo ""
  echo "Copy $target/policy-fragments/ into your APIOps repository"
  echo "and push to trigger your publisher pipeline."
  echo ""
  echo "You will also need to create named values manually or via APIOps:"
  echo "  - astrocyte-gateway-url = <your Astrocyte gateway URL>"
  echo "  - astrocyte-api-key     = <your Astrocyte API key> (mark as secret)"
}

# ---------------------------------------------------------------------------
# Portal (manual paste)
# ---------------------------------------------------------------------------
deploy_portal() {
  echo ""
  echo "=== Recall + Inject Fragment (inbound) ==="
  echo "Create a policy fragment named 'astrocyte-recall-inject' with this XML:"
  echo ""
  cat "$SCRIPT_DIR/policy-fragments/astrocyte-recall-inject.xml"
  echo ""
  echo ""
  echo "=== Retain + Extract Fragment (outbound) ==="
  echo "Create a policy fragment named 'astrocyte-retain-extract' with this XML:"
  echo ""
  cat "$SCRIPT_DIR/policy-fragments/astrocyte-retain-extract.xml"
  echo ""
  echo ""
  echo "=== Named Values ==="
  echo "Create these named values in your APIM instance:"
  echo "  - astrocyte-gateway-url = <your Astrocyte gateway URL>"
  echo "  - astrocyte-api-key     = <your Astrocyte API key> (mark as Secret)"
  echo ""
  echo "=== API Policy ==="
  echo "Add these to your API policy XML:"
  echo '  <inbound>'
  echo '    <include-fragment fragment-id="astrocyte-recall-inject" />'
  echo '  </inbound>'
  echo '  <outbound>'
  echo '    <include-fragment fragment-id="astrocyte-retain-extract" />'
  echo '  </outbound>'
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
case "$METHOD" in
  bicep)     deploy_bicep ;;
  terraform) deploy_terraform ;;
  apiops)    deploy_apiops ;;
  portal)    deploy_portal ;;
  *) echo "Unknown method: $METHOD"; exit 1 ;;
esac
