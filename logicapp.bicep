param location string = resourceGroup().location
param workflowName string
param connectionName string = 'office365'
param createConnection bool = true

resource office365 'Microsoft.Web/connections@2016-06-01' = if (createConnection) {
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
  dependsOn: [office365]
  properties: {
    state: 'Enabled'
    parameters: {
      '$connections': {
        value: {
          office365: {
            connectionId: resourceId('Microsoft.Web/connections', connectionName)
            connectionName: connectionName
            id: subscriptionResourceId('Microsoft.Web/locations/managedApis', location, 'office365')
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
                'to'
                'subject'
                'html'
                'runId'
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
        confirm_sent: {
          type: 'Response'
          kind: 'Http'
          runAfter: {
            send_email: ['Succeeded']
          }
          inputs: {
            statusCode: 200
            body: {
              status: 'Sent'
              runId: '@triggerBody()?[\'runId\']'
            }
          }
        }
        report_failure: {
          type: 'Response'
          kind: 'Http'
          runAfter: {
            send_email: ['Failed', 'TimedOut', 'Skipped']
          }
          inputs: {
            statusCode: 502
            body: {
              status: 'Failed'
              runId: '@triggerBody()?[\'runId\']'
            }
          }
        }
      }
      outputs: {}
    }
  }
}

output workflowId string = workflow.id
output connectionId string = resourceId('Microsoft.Web/connections', connectionName)