// Deploys Astrocyte policy fragments and named values to an existing
// Azure API Management instance.
//
// Usage:
//   az deployment group create \
//     --resource-group <rg> \
//     --template-file main.bicep \
//     --parameters apimName=<name> astrocyteUrl=https://astrocyte.internal:8900 astrocyteApiKey=<key>

@description('Name of the existing API Management instance.')
param apimName string

@description('Base URL of the Astrocyte standalone gateway.')
param astrocyteUrl string

@secure()
@description('API key for Astrocyte gateway authentication.')
param astrocyteApiKey string

// ---------------------------------------------------------------------------
// Reference existing APIM instance
// ---------------------------------------------------------------------------

resource apim 'Microsoft.ApiManagement/service@2023-09-01-preview' existing = {
  name: apimName
}

// ---------------------------------------------------------------------------
// Named values (secrets referenced by policy fragments)
// ---------------------------------------------------------------------------

resource namedValueUrl 'Microsoft.ApiManagement/service/namedValues@2023-09-01-preview' = {
  parent: apim
  name: 'astrocyte-gateway-url'
  properties: {
    displayName: 'astrocyte-gateway-url'
    value: astrocyteUrl
    secret: false
  }
}

resource namedValueKey 'Microsoft.ApiManagement/service/namedValues@2023-09-01-preview' = {
  parent: apim
  name: 'astrocyte-api-key'
  properties: {
    displayName: 'astrocyte-api-key'
    value: astrocyteApiKey
    secret: true
  }
}

// ---------------------------------------------------------------------------
// Policy fragments
// ---------------------------------------------------------------------------

resource recallFragment 'Microsoft.ApiManagement/service/policyFragments@2023-09-01-preview' = {
  parent: apim
  name: 'astrocyte-recall-inject'
  properties: {
    description: 'Astrocyte pre-hook: recall memories and inject into LLM system prompt.'
    format: 'rawxml'
    value: loadTextContent('../../policy-fragments/astrocyte-recall-inject.xml')
  }
  dependsOn: [
    namedValueUrl
    namedValueKey
  ]
}

resource retainFragment 'Microsoft.ApiManagement/service/policyFragments@2023-09-01-preview' = {
  parent: apim
  name: 'astrocyte-retain-extract'
  properties: {
    description: 'Astrocyte post-hook: retain assistant response as new memory.'
    format: 'rawxml'
    value: loadTextContent('../../policy-fragments/astrocyte-retain-extract.xml')
  }
  dependsOn: [
    namedValueUrl
    namedValueKey
  ]
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

output recallFragmentId string = recallFragment.id
output retainFragmentId string = retainFragment.id
output instructions string = 'Add these to your API policy: <include-fragment fragment-id="astrocyte-recall-inject" /> (inbound) and <include-fragment fragment-id="astrocyte-retain-extract" /> (outbound)'
