// Phase 2 — Email ingestion (Logic Apps), deployed into the existing resource group.
// Deploy: az deployment group create -g agentic-email-processing --template-file infra/phase2.bicep --parameters infra/phase2.bicepparam

@description('Location for the Logic App and its API connections (data region).')
param location string = 'centralindia'

@description('Existing storage account that receives the email attachments.')
param storageAccountName string = 'agenticemailks'

@description('Consumption Logic App (workflow) name.')
param logicAppName string = 'logic-email-ks'

@description('Office 365 Outlook API connection name (requires one-time manual authorization after deploy).')
param office365ConnectionName string = 'office365-ks'

@description('Azure Blob Storage API connection name.')
param blobConnectionName string = 'azureblob-ks'

@description('Foundry project data-plane endpoint used to call the orchestrator agent.')
param projectAgentsEndpoint string = 'https://agentic-email-foundry-ks.services.ai.azure.com/api/projects/email-agentic-ks'

@description('Orchestrator agent id (set after Phase 3 creates the agents). Empty disables the agent call.')
param orchestratorAgentId string = ''

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource office365Conn 'Microsoft.Web/connections@2016-06-01' = {
  name: office365ConnectionName
  location: location
  properties: {
    displayName: 'Office 365 Outlook'
    api: {
      id: subscriptionResourceId('Microsoft.Web/locations/managedApis', location, 'office365')
    }
  }
}

resource blobConn 'Microsoft.Web/connections@2016-06-01' = {
  name: blobConnectionName
  location: location
  properties: {
    displayName: 'Azure Blob Storage'
    api: {
      id: subscriptionResourceId('Microsoft.Web/locations/managedApis', location, 'azureblob')
    }
    parameterValues: {
      accountName: storageAccountName
      accessKey: storage.listKeys().keys[0].value
    }
  }
}

resource workflow 'Microsoft.Logic/workflows@2019-05-01' = {
  name: logicAppName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    state: 'Enabled'
    definition: loadJsonContent('logic-workflow.json')
    parameters: {
      '$connections': {
        value: {
          office365: {
            connectionId: office365Conn.id
            connectionName: office365ConnectionName
            id: subscriptionResourceId('Microsoft.Web/locations/managedApis', location, 'office365')
          }
          azureblob: {
            connectionId: blobConn.id
            connectionName: blobConnectionName
            id: subscriptionResourceId('Microsoft.Web/locations/managedApis', location, 'azureblob')
          }
        }
      }
      projectAgentsEndpoint: {
        value: projectAgentsEndpoint
      }
      orchestratorAgentId: {
        value: orchestratorAgentId
      }
    }
  }
}

output logicAppName string = workflow.name
output logicAppPrincipalId string = workflow.identity.principalId
output office365ConnectionName string = office365Conn.name
output blobConnectionName string = blobConn.name
