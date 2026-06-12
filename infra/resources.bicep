// Phase 1 resources (resource-group scope)
@description('Location for data resources (Storage, Document Intelligence).')
param dataLocation string

@description('Location for the AI Foundry account + model deployment.')
param aiLocation string

param foundryName string
param projectName string
param modelDeploymentName string
param modelName string
param modelVersion string
param modelSkuName string
param modelCapacity int
param storageAccountName string
param docIntelName string

// ---------------------------------------------------------------------------
// Storage account (India) + blob containers
// ---------------------------------------------------------------------------
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: dataLocation
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource inputContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'incoming-attachments'
  properties: {
    publicAccess: 'None'
  }
}

resource outputContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'contract-notes-output'
  properties: {
    publicAccess: 'None'
  }
}

// ---------------------------------------------------------------------------
// Azure AI Foundry account (AI Services) + project + model deployment
// ---------------------------------------------------------------------------
resource foundry 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' = {
  name: foundryName
  location: aiLocation
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    allowProjectManagement: true
    customSubDomainName: 'agentic-email-${foundryName}'
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: false
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview' = {
  parent: foundry
  name: projectName
  location: aiLocation
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: projectName
    description: 'Agentic email processing project'
  }
}

resource modelDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-04-01-preview' = {
  parent: foundry
  name: modelDeploymentName
  sku: {
    name: modelSkuName
    capacity: modelCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: modelName
      version: modelVersion
    }
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
    raiPolicyName: 'Microsoft.DefaultV2'
  }
}

// ---------------------------------------------------------------------------
// Document Intelligence (India) — reliable PDF parsing
// ---------------------------------------------------------------------------
resource docIntel 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' = {
  name: docIntelName
  location: dataLocation
  kind: 'FormRecognizer'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    customSubDomainName: 'agentic-email-${docIntelName}'
    publicNetworkAccess: 'Enabled'
  }
}

output foundryAccountName string = foundry.name
output foundryEndpoint string = foundry.properties.endpoint
output projectName string = project.name
output modelDeploymentName string = modelDeployment.name
output storageAccountName string = storage.name
output inputContainer string = inputContainer.name
output outputContainer string = outputContainer.name
output docIntelEndpoint string = docIntel.properties.endpoint
