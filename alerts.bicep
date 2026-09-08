param jobName string
param recipient string

resource job 'Microsoft.App/jobs@2024-03-01' existing = {
  name: jobName
}

resource notifications 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: '${jobName}-alerts'
  location: 'global'
  properties: {
    groupShortName: 'RAIDevMon'
    enabled: true
    emailReceivers: [
      {
        name: 'monitor-owner'
        emailAddress: recipient
        useCommonAlertSchema: true
      }
    ]
  }
}

resource failedJob 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: '${jobName}-failed'
  location: 'global'
  properties: {
    description: 'RAI subscription scan failed. Check ACA execution logs and Logic App send_email before retrying.'
    severity: 2
    enabled: true
    scopes: [job.id]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    autoMitigate: true
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'FailedExecution'
          criterionType: 'StaticThresholdCriterion'
          metricNamespace: 'Microsoft.App/jobs'
          metricName: 'Executions'
          timeAggregation: 'Maximum'
          operator: 'GreaterThan'
          threshold: 0
          dimensions: [
            {
              name: 'state'
              operator: 'Include'
              values: ['Failed']
            }
          ]
        }
      ]
    }
    actions: [
      { actionGroupId: notifications.id }
    ]
  }
}