[CmdletBinding()]
param(
    [string]$SubscriptionId = 'c920e969-c175-44e3-a64b-d3009bafe279',
    [string]$ResourceGroup = 'rai-devsub-monitor-rg',
    [string]$Location = 'eastus2',
    [string]$Recipient = 'huliang@microsoft.com'
)

$ErrorActionPreference = 'Stop'
$suffix = ($SubscriptionId.Replace('-', '').Substring(0, 8))
$acr = "raidevmon$suffix"
$storage = "raidevmon$suffix"
$workspace = 'rai-devsub-monitor-law'
$environment = 'rai-devsub-monitor-env'
$job = 'rai-devsub-monitor-job'
$workflow = 'rai-devsub-monitor-email'
$identityId = '/subscriptions/c920e969-c175-44e3-a64b-d3009bafe279/resourceGroups/raiglobaldev/providers/Microsoft.ManagedIdentity/userAssignedIdentities/raiuai'
$identityClientId = '309f79d6-3efe-447f-823e-eaf5ad13431c'
$identityPrincipalId = '51ebd3d6-b37b-4a7a-9343-500a0406aede'

function Invoke-AzCli {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & az @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Azure CLI failed with exit code $LASTEXITCODE`: az $($Arguments -join ' ')"
    }
}

Invoke-AzCli account set --subscription $SubscriptionId
foreach ($provider in @('Microsoft.App', 'Microsoft.OperationalInsights', 'Microsoft.Storage', 'Microsoft.ContainerRegistry', 'Microsoft.Logic', 'Microsoft.Web', 'Microsoft.Insights')) {
    Invoke-AzCli provider register --namespace $provider --wait
}

Invoke-AzCli group create --name $ResourceGroup --location $Location --output none
Invoke-AzCli monitor log-analytics workspace create --resource-group $ResourceGroup --workspace-name $workspace --location $Location --retention-time 30 --output none
$lawId = az monitor log-analytics workspace show --resource-group $ResourceGroup --workspace-name $workspace --query customerId --output tsv
$lawKey = az monitor log-analytics workspace get-shared-keys --resource-group $ResourceGroup --workspace-name $workspace --query primarySharedKey --output tsv

Invoke-AzCli storage account create --resource-group $ResourceGroup --name $storage --location $Location --sku Standard_LRS --kind StorageV2 --min-tls-version TLS1_2 --allow-blob-public-access false --allow-shared-key-access false --output none
Invoke-AzCli rest --method put --uri "https://management.azure.com/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.Storage/storageAccounts/$storage/blobServices/default/containers/reports?api-version=2023-05-01" --body '{\"properties\":{\"publicAccess\":\"None\"}}' --output none
$storageId = az storage account show --resource-group $ResourceGroup --name $storage --query id --output tsv
Invoke-AzCli role assignment create --assignee-object-id $identityPrincipalId --assignee-principal-type ServicePrincipal --role 'Storage Blob Data Contributor' --scope $storageId --output none

Invoke-AzCli acr create --resource-group $ResourceGroup --name $acr --location $Location --sku Basic --admin-enabled false --output none
Invoke-AzCli acr build --registry $acr --image 'rai-devsub-monitor:latest' $PSScriptRoot

Invoke-AzCli containerapp env create --resource-group $ResourceGroup --name $environment --location $Location --logs-workspace-id $lawId --logs-workspace-key $lawKey --output none
Invoke-AzCli deployment group create --resource-group $ResourceGroup --template-file (Join-Path $PSScriptRoot 'logicapp.bicep') --parameters workflowName=$workflow --output none

$callback = az rest --method post --uri "https://management.azure.com/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.Logic/workflows/$workflow/triggers/receive_report/listCallbackUrl?api-version=2019-05-01" --query value --output tsv

$existing = az containerapp job show --resource-group $ResourceGroup --name $job --query name --output tsv 2>$null
if (-not $existing) {
    Invoke-AzCli containerapp job create --resource-group $ResourceGroup --name $job --environment $environment --trigger-type Schedule --cron-expression '0 15 * * 1' --replica-timeout 3600 --replica-retry-limit 1 --parallelism 1 --replica-completion-count 1 --image "$acr.azurecr.io/rai-devsub-monitor:latest" --registry-server "$acr.azurecr.io" --mi-user-assigned $identityId --registry-identity $identityId --cpu 0.5 --memory 1Gi --output none
}

# Use ARM JSON rather than az.cmd for the signed callback URL: its ampersands are
# otherwise interpreted as command separators on Windows.
$armToken = az account get-access-token --resource 'https://management.azure.com/' --query accessToken --output tsv
$headers = @{ Authorization = "Bearer $armToken" }
$payload = @{
    properties = @{
        configuration = @{ secrets = @(@{ name = 'logic-trigger'; value = $callback }) }
        template = @{
            containers = @(@{
                name = $job
                image = "$acr.azurecr.io/rai-devsub-monitor:latest"
                resources = @{ cpu = 0.5; memory = '1Gi' }
                env = @(
                    @{ name = 'AZURE_CLIENT_ID'; value = $identityClientId }
                    @{ name = 'SUBSCRIPTION_ID'; value = $SubscriptionId }
                    @{ name = 'COST_THRESHOLD_USD'; value = '100' }
                    @{ name = 'EMAIL_TO'; value = $Recipient }
                    @{ name = 'STORAGE_ACCOUNT'; value = $storage }
                    @{ name = 'REPORT_CONTAINER'; value = 'reports' }
                    @{ name = 'LOGIC_APP_TRIGGER_URL'; secretRef = 'logic-trigger' }
                )
            })
        }
    }
} | ConvertTo-Json -Depth 12
try {
    Invoke-RestMethod -Method Patch -Uri "https://management.azure.com/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.App/jobs/$job`?api-version=2024-03-01" -Headers $headers -ContentType 'application/json' -Body $payload | Out-Null
}
finally {
    $callback = $null
    $armToken = $null
    $payload = $null
}

Write-Host "Deployment complete. Authorize the Office 365 connection named 'office365' in the Azure Portal, then run:"
Write-Host "az containerapp job start --subscription $SubscriptionId --resource-group $ResourceGroup --name $job"