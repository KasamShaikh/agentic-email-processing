using './phase2.bicep'

param location = 'centralindia'
param storageAccountName = 'agenticemailks'
param logicAppName = 'logic-email-ks'
param office365ConnectionName = 'office365-ks'
param blobConnectionName = 'azureblob-ks'
// Set this to the hosted dashboard's /api/process URL to enable zero-click agentic processing.
// Left empty for the localhost PoC (Azure cannot reach a local dashboard).
param processorEndpoint = ''
