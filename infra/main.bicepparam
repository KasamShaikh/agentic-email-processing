using './main.bicep'

// Phase 1 parameter values. No secrets / subscription IDs stored here.
param resourceGroupName = 'agentic-email-processing'
param dataLocation = 'centralindia'
param aiLocation = 'swedencentral'

param foundryName = 'foundry-ks'
param projectName = 'email-agentic-ks'

param modelDeploymentName = 'gpt-mini-ks'
param modelName = 'gpt-5.4-mini'
param modelVersion = '2026-03-17'
param modelSkuName = 'GlobalStandard'
param modelCapacity = 20

param storageAccountName = 'agenticemailks'
param docIntelName = 'docintel-ks'
