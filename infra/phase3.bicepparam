using './phase3.bicep'

// Phase 3 parameter values. No secrets / subscription IDs stored here
// (AZURE_SUBSCRIPTION_ID is resolved at deploy time from the deployment context).
param webAppName = 'agentic-email-processing'
param appServicePlanName = 'asp-email-ks'
param pythonVersion = '3.12'

param storageAccountName = 'agenticemailks'
param docIntelName = 'docintel-ks'
param foundryName = 'foundry-ks'
param foundryProjectEndpoint = 'https://agentic-email-foundry-ks.services.ai.azure.com/api/projects/email-agentic-ks'
param logicAppName = 'logic-email-ks'
