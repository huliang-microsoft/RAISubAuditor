$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$root = Split-Path $PSScriptRoot -Parent

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

function Read-Template([string]$Name) {
    $compiled = az bicep build --file (Join-Path $root $Name) --stdout
    if ($LASTEXITCODE -ne 0) { throw "Bicep compilation failed: $Name" }
    return $compiled | ConvertFrom-Json -AsHashtable
}

$logic = Read-Template 'logicapp.bicep'
$workflow = $logic.resources | Where-Object type -EQ 'Microsoft.Logic/workflows'
$actions = $workflow.properties.definition.actions
Assert-True (($actions.confirm_sent.runAfter.send_email -join ',') -eq 'Succeeded') 'Success response must depend on email success'
Assert-True ($actions.confirm_sent.inputs.statusCode -eq 200) 'Success response must be HTTP 200'
Assert-True ($actions.confirm_sent.inputs.body.status -eq 'Sent') 'Success must acknowledge Sent'
Assert-True ($actions.confirm_sent.inputs.body.runId -eq '@triggerBody()?[''runId'']') 'Success must echo runId'
Assert-True ($actions.report_failure.inputs.statusCode -eq 502) 'Failure must reach the caller'
Assert-True ($actions.report_failure.runAfter.send_email -contains 'Failed') 'Failed email must be handled'
Assert-True ($actions.report_failure.runAfter.send_email -contains 'TimedOut') 'Timed out email must be handled'
Assert-True ($workflow.properties.definition.triggers.receive_report.inputs.schema.required -contains 'runId') 'runId must be required'

$alerts = Read-Template 'alerts.bicep'
$alert = $alerts.resources | Where-Object type -EQ 'Microsoft.Insights/metricAlerts'
$criterion = $alert.properties.criteria.allOf[0]
Assert-True ($alert.properties.enabled) 'Failure alert must be enabled'
Assert-True ($criterion.metricName -eq 'Executions') 'Alert must watch job executions'
Assert-True ($criterion.dimensions[0].values[0] -ceq 'Failed') 'Alert must select the verified Failed state'
$group = $alerts.resources | Where-Object type -EQ 'Microsoft.Insights/actionGroups'
Assert-True ($group.properties.emailReceivers.Count -eq 1) 'Failure alert needs an independent email receiver'

$tokens = $null
$parseErrors = $null
[System.Management.Automation.Language.Parser]::ParseFile((Join-Path $root 'deploy.ps1'), [ref]$tokens, [ref]$parseErrors) | Out-Null
Assert-True ($parseErrors.Count -eq 0) 'Deployment PowerShell syntax must be valid'
Write-Output 'Deployment checks passed: Logic App confirmation, failure alert and PowerShell syntax.'