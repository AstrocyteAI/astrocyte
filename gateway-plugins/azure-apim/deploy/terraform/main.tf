# Deploys Astrocyte policy fragments and named values to an existing
# Azure API Management instance using the AzAPI provider.
#
# Usage:
#   terraform init
#   terraform apply -var="apim_name=..." -var="resource_group=..." \
#     -var="astrocyte_url=https://astrocyte.internal:8900" \
#     -var="astrocyte_api_key=..."

terraform {
  required_providers {
    azapi = {
      source  = "Azure/azapi"
      version = "~> 2.0"
    }
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
}

provider "azurerm" {
  features {}
}

provider "azapi" {}

# ---------------------------------------------------------------------------
# Variables
# ---------------------------------------------------------------------------

variable "resource_group" {
  type        = string
  description = "Resource group containing the APIM instance."
}

variable "apim_name" {
  type        = string
  description = "Name of the existing API Management instance."
}

variable "astrocyte_url" {
  type        = string
  description = "Base URL of the Astrocyte standalone gateway."
}

variable "astrocyte_api_key" {
  type        = string
  sensitive   = true
  description = "API key for Astrocyte gateway authentication."
}

# ---------------------------------------------------------------------------
# Data: existing APIM instance
# ---------------------------------------------------------------------------

data "azurerm_api_management" "apim" {
  name                = var.apim_name
  resource_group_name = var.resource_group
}

# ---------------------------------------------------------------------------
# Named values
# ---------------------------------------------------------------------------

resource "azapi_resource" "named_value_url" {
  type      = "Microsoft.ApiManagement/service/namedValues@2023-09-01-preview"
  name      = "astrocyte-gateway-url"
  parent_id = data.azurerm_api_management.apim.id

  body = {
    properties = {
      displayName = "astrocyte-gateway-url"
      value       = var.astrocyte_url
      secret      = false
    }
  }
}

resource "azapi_resource" "named_value_key" {
  type      = "Microsoft.ApiManagement/service/namedValues@2023-09-01-preview"
  name      = "astrocyte-api-key"
  parent_id = data.azurerm_api_management.apim.id

  body = {
    properties = {
      displayName = "astrocyte-api-key"
      value       = var.astrocyte_api_key
      secret      = true
    }
  }
}

# ---------------------------------------------------------------------------
# Policy fragments
# ---------------------------------------------------------------------------

resource "azapi_resource" "recall_fragment" {
  type      = "Microsoft.ApiManagement/service/policyFragments@2023-09-01-preview"
  name      = "astrocyte-recall-inject"
  parent_id = data.azurerm_api_management.apim.id

  body = {
    properties = {
      description = "Astrocyte pre-hook: recall memories and inject into LLM system prompt."
      format      = "rawxml"
      value       = file("${path.module}/../../policy-fragments/astrocyte-recall-inject.xml")
    }
  }

  depends_on = [
    azapi_resource.named_value_url,
    azapi_resource.named_value_key,
  ]
}

resource "azapi_resource" "retain_fragment" {
  type      = "Microsoft.ApiManagement/service/policyFragments@2023-09-01-preview"
  name      = "astrocyte-retain-extract"
  parent_id = data.azurerm_api_management.apim.id

  body = {
    properties = {
      description = "Astrocyte post-hook: retain assistant response as new memory."
      format      = "rawxml"
      value       = file("${path.module}/../../policy-fragments/astrocyte-retain-extract.xml")
    }
  }

  depends_on = [
    azapi_resource.named_value_url,
    azapi_resource.named_value_key,
  ]
}

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

output "recall_fragment_id" {
  value = azapi_resource.recall_fragment.id
}

output "retain_fragment_id" {
  value = azapi_resource.retain_fragment.id
}

output "instructions" {
  value = "Add these to your API policy: <include-fragment fragment-id=\"astrocyte-recall-inject\" /> (inbound) and <include-fragment fragment-id=\"astrocyte-retain-extract\" /> (outbound)"
}
