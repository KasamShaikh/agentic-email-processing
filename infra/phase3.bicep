// Phase 3 — Dashboard hosting (Azure App Service, Linux Free F1), into the existing RG.
// Deploy: az deployment group create -g agentic-email-processing --template-file infra/phase3.bicep --parameters infra/phase3.bicepparam
//
// Free F1 Linux does NOT support custom containers, so the app is deployed as CODE
// (built-in Python runtime via Oryx). The Web App uses a system-assigned managed
// identity; role assignments below grant least-privilege access to the existing
// Storage, Document Intelligence, and Foundry resources, plus Reader on the RG so the
// dashboard can read Logic App run history.

@description('Location for the App Service plan + web app.')
param location string = resourceGroup().location

@description('Public web app name -> https://<name>.azurewebsites.net (must be globally unique).')
param webAppName string = 'agentic-email-processing'

@description('App Service plan name.')
param appServicePlanName string = 'asp-email-ks'

@description('Python runtime version for the built-in (code) deployment.')
param pythonVersion string = '3.12'

// Existing resources (for endpoint values + RBAC scopes)
param storageAccountName string = 'agenticemailks'
param docIntelName string = 'docintel-ks'
param foundryName string = 'foundry-ks'

@description('Foundry project endpoint used by the agents SDK.')
param foundryProjectEndpoint string = 'https://agentic-email-foundry-ks.services.ai.azure.com/api/projects/email-agentic-ks'

@description('Logic App (workflow) name whose run history the dashboard reads.')
param logicAppName string = 'logic-email-ks'

// ---------------------------------------------------------------------------
// Existing resources referenced for RBAC + endpoint composition
// ---------------------------------------------------------------------------
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource docintel 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: docIntelName
}

resource foundry 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: foundryName
}

// ---------------------------------------------------------------------------
// App Service plan (Linux, Free F1) + Web App (code deploy, system identity)
// ---------------------------------------------------------------------------
resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: appServicePlanName
  location: location
  kind: 'linux'
  sku: {
    name: 'F1'
    tier: 'Free'
  }
  properties: {
    reserved: true // Linux
  }
}

resource site 'Microsoft.Web/sites@2023-12-01' = {
  name: webAppName
  location: location
  kind: 'app,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'PYTHON|${pythonVersion}'
      alwaysOn: false // not supported on Free F1
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      appCommandLine: 'gunicorn -w 1 -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8000 --timeout 600 app:app'
      appSettings: [
        { name: 'SCM_DO_BUILD_DURING_DEPLOYMENT', value: 'true' }
        { name: 'ENABLE_ORYX_BUILD', value: 'true' }
        { name: 'WEBSITES_CONTAINER_START_TIME_LIMIT', value: '600' }
        { name: 'AUTO_PROCESS', value: '1' }
        { name: 'FOUNDRY_PROJECT_ENDPOINT', value: foundryProjectEndpoint }
        { name: 'DOCINTEL_ENDPOINT', value: docintel.properties.endpoint }
        { name: 'STORAGE_ACCOUNT_URL', value: storage.properties.primaryEndpoints.blob }
        { name: 'RESOURCE_GROUP', value: resourceGroup().name }
        { name: 'LOGIC_APP_NAME', value: logicAppName }
        // resolved at deploy time from the deployment context — never hardcoded
        { name: 'AZURE_SUBSCRIPTION_ID', value: subscription().subscriptionId }
      ]
    }
  }
}

// ---------------------------------------------------------------------------
// Built-in role definition IDs
// ---------------------------------------------------------------------------
var storageBlobDataContributor = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var cognitiveServicesUser = 'a97b65f3-24c7-4388-baec-2e87135dc908'
var azureAIDeveloper = '64702f94-c441-49e6-a78b-ef80e0188fee'
var reader = 'acdd72a7-3385-48ef-bd42-f606fba81ae7'

// ---------------------------------------------------------------------------
// Role assignments (least privilege, scoped to each existing resource)
// ---------------------------------------------------------------------------
resource raStorage 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, site.id, storageBlobDataContributor)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributor)
    principalId: site.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource raDocIntel 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(docintel.id, site.id, cognitiveServicesUser)
  scope: docintel
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesUser)
    principalId: site.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource raFoundryUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, site.id, cognitiveServicesUser)
  scope: foundry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesUser)
    principalId: site.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource raFoundryDev 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, site.id, azureAIDeveloper)
  scope: foundry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', azureAIDeveloper)
    principalId: site.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Reader at the resource-group scope so the dashboard can list Logic App runs.
resource raReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, site.id, reader)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', reader)
    principalId: site.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output webAppUrl string = 'https://${site.properties.defaultHostName}'
output webAppName string = site.name
output principalId string = site.identity.principalId
