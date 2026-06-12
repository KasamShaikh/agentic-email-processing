// Phase 1 — Provision foundation (subscription-scope entry point)
// Creates the resource group and deploys all Phase 1 resources into it.
targetScope = 'subscription'

@description('Resource group name.')
param resourceGroupName string = 'agentic-email-processing'

@description('Location for data resources (Storage, Document Intelligence) — kept in India.')
param dataLocation string = 'centralindia'

@description('Location for the AI Foundry account + model deployment (GlobalStandard, pay-as-you-go). Central India is PTU-only, so a GlobalStandard-capable region is used.')
param aiLocation string = 'swedencentral'

@description('Azure AI Foundry (AI Services) account name.')
param foundryName string = 'foundry-ks'

@description('Foundry project name.')
param projectName string = 'email-agentic-ks'

@description('Model deployment name (kept stable so downstream references do not change).')
param modelDeploymentName string = 'gpt-mini-ks'

@description('Model to deploy.')
param modelName string = 'gpt-5.4-mini'

@description('Model version.')
param modelVersion string = '2026-03-17'

@description('Deployment SKU (GlobalStandard = pay-as-you-go).')
param modelSkuName string = 'GlobalStandard'

@description('Model capacity in thousands of tokens-per-minute.')
param modelCapacity int = 20

@description('Storage account name (no hyphens allowed; 3-24 lowercase alphanumeric).')
param storageAccountName string = 'agenticemailks'

@description('Document Intelligence account name.')
param docIntelName string = 'docintel-ks'

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: dataLocation
}

module resources 'resources.bicep' = {
  name: 'phase1-resources'
  scope: rg
  params: {
    dataLocation: dataLocation
    aiLocation: aiLocation
    foundryName: foundryName
    projectName: projectName
    modelDeploymentName: modelDeploymentName
    modelName: modelName
    modelVersion: modelVersion
    modelSkuName: modelSkuName
    modelCapacity: modelCapacity
    storageAccountName: storageAccountName
    docIntelName: docIntelName
  }
}

output resourceGroup string = rg.name
output foundryAccountName string = resources.outputs.foundryAccountName
output foundryEndpoint string = resources.outputs.foundryEndpoint
output projectName string = resources.outputs.projectName
output modelDeploymentName string = resources.outputs.modelDeploymentName
output storageAccountName string = resources.outputs.storageAccountName
output inputContainer string = resources.outputs.inputContainer
output outputContainer string = resources.outputs.outputContainer
output docIntelEndpoint string = resources.outputs.docIntelEndpoint
