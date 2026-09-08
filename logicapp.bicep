param location string = resourceGroup().location
param workflowName string
param connectionName string = 'office365'

resource office365 'Microsoft.Web/connections@2016-06-01' = {
  name: connectionName
  location: location
  properties: {
    displayName: 'RAI DevSub Monitor Outlook connection'
    api: {
      id: subscriptionResourceId('Microsoft.Web/locations/managedApis', location, 'office365')
    }
  }
}

resource workflow 'Microsoft.Logic/workflows@2019-05-01' = {
  name: workflowName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    state: 'Enabled'
    parameters: {
      '$connections': {
        value: {
          office365: {
            connectionId: office365.id
            connectionName: office365.name
            id: office365.properties.api.id
          }
        }
      }
    }
    definition: {
      '$schema': 'https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#'
      contentVersion: '1.0.0.0'
      parameters: {
        '$connections': {
          type: 'Object'
          defaultValue: {}
        }
      }
      triggers: {
        receive_report: {
          type: 'Request'
          kind: 'Http'
          inputs: {
            schema: {
              type: 'object'
              required: [
                'subject'
                'html'
              ]
              properties: {
                to: { type: 'string' }
                subject: { type: 'string' }
                html: { type: 'string' }
                runId: { type: 'string' }
              }
            }
          }
        }
      }
      actions: {
        send_email: {
          type: 'ApiConnection'
          runAfter: {}
          inputs: {
            host: {
              connection: {
                name: '@parameters(\'$connections\')[\'office365\'][\'connectionId\']'
              }
            }
            method: 'post'
            path: '/v2/Mail'
            body: {
              To: '@triggerBody()?[\'to\']'
              Subject: '@triggerBody()?[\'subject\']'
              Body: '@triggerBody()?[\'html\']'
              Importance: 'Normal'
            }
          }
        }
      }
      outputs: {}
    }
  }
}

output workflowId string = workflow.id
output connectionId string = office365.id