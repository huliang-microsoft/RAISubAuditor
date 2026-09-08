# RAI Dev Subscription Monitor

Weekly, read-only cost and idle-resource monitor for the OpenAI RAI Dev subscription.

## Production deployment

| Setting | Value |
|---|---|
| Subscription | `OpenAI RAI Dev` (`c920e969-c175-44e3-a64b-d3009bafe279`) |
| Resource group | `rai-devsub-monitor-rg` |
| Region | East US 2 |
| ACA job | `rai-devsub-monitor-job` |
| Schedule | Mondays at 15:00 UTC (`0 15 * * 1`) |
| Managed identity | `raiglobaldev/raiuai` |
| Managed identity client ID | `309f79d6-3efe-447f-823e-eaf5ad13431c` |
| Recipient | `huliang@microsoft.com` |
| Cost threshold | More than USD 100 over the trailing 30 days |
| Estimated platform cost | Approximately USD 6-10/month |

## Implementation

The implementation is a Python 3.12 container executed as an Azure Container Apps scheduled job. It uses `DefaultAzureCredential` with the attached user-assigned managed identity and performs this workflow:

1. Query Azure Cost Management for trailing 30-day actual cost grouped by resource ID, resource type, and resource group.
2. Retain resources whose aggregated cost is greater than USD 100.
3. Resolve Azure Monitor metric definitions for supported resource types.
4. Query required usage metrics over the same 30-day window.
5. Classify a resource as `Idle candidate` only when every required metric exists, has datapoints, and totals zero.
6. Store complete JSON, CSV, and HTML artifacts in the private `reports` Blob container.
7. Send an HTML email containing **only `Idle candidate` rows**. `Active` and `Unknown` rows are omitted from email.

JSON and CSV intentionally retain all classifications for audit and troubleshooting. The email is restricted to actionable candidates to reduce noise.

## Classification safety

- `Idle candidate`: all required metrics are available and every observed value is zero.
- `Active`: at least one required metric has nonzero activity.
- `Unknown`: the resource type is unsupported, a required metric is absent, no datapoints exist, or the metric request fails.

`Unknown` is never converted to idle. The monitor does not stop, scale, delete, or otherwise modify business resources. An idle candidate is a review recommendation, not deletion approval.

## Supported MVP rules

| Resource type | Required evidence |
|---|---|
| Event Hubs namespace | Incoming/outgoing messages and bytes |
| Virtual machine | CPU and inbound/outbound network |
| Azure Container Registry | Successful push and pull counts |
| Azure AI Search | Search queries and indexing activity |
| Azure Data Explorer | Query and ingestion activity when exposed |
| Azure Container Instance group | CPU and inbound/outbound network |

Other resource types remain `Unknown` and are omitted from email.

## Azure resources

- Azure Container Apps environment and scheduled job
- Basic Azure Container Registry for the scanner image
- Standard LRS Storage account with shared-key access disabled
- Private Blob container named `reports`
- Log Analytics workspace with 30-day retention
- Consumption Logic App
- Office 365 Outlook API connection authorized as `huliang@microsoft.com`

The Logic App callback is stored as an ACA secret. Reports use Entra ID authentication; no Storage account key is used.

## Files

| File | Purpose |
|---|---|
| `monitor.py` | Cost query, metric classification, report generation, Blob upload, and notification |
| `Dockerfile` | Python 3.12 runtime image |
| `requirements.txt` | Pinned Python runtime dependencies |
| `logicapp.bicep` | Logic App and Office 365 connection deployment |
| `deploy.ps1` | Idempotent Azure deployment and ACA configuration |
| `tests/test_monitor.py` | Classification, escaping, email filtering, and time-format regression tests |

## Deploy or update

Authenticate Azure CLI to the Microsoft tenant and run `deploy.ps1`. The script:

- registers required resource providers;
- creates or updates the infrastructure;
- builds the container remotely with ACR Tasks, so local Docker is unnecessary;
- attaches `raiuai`;
- stores the signed Logic App callback as a secret; and
- configures the weekly schedule.

After the first deployment, authorize the `office365` API connection in Azure Portal as `huliang@microsoft.com`. Redeployment normally preserves the authorized connection.

## Manual execution and validation

Start `rai-devsub-monitor-job` from Azure Portal or Azure CLI. A successful run must satisfy all of the following:

1. ACA execution status is `Succeeded`.
2. The dated Blob prefix contains `report.json`, `report.csv`, and `report.html`.
3. The Logic App run status is `Succeeded`.
4. The received email contains only idle candidates.

The first validated production scan processed 204 resources above the threshold and identified 42 idle candidates with approximately USD 20,072.52 in trailing 30-day cost.

## Operations

- Application and platform logs are in `rai-devsub-monitor-law`.
- Reports are private and organized as `YYYY/MM/DD/<run-id>/report.{json,csv,html}`.
- Transient ARM failures, including Cost Management throttling, are retried up to ten times with bounded exponential backoff.
- A Cost Management or authentication failure fails the whole job.
- A per-resource metric failure becomes `Unknown` and does not appear in email.
- The Office 365 delegated connection depends on the authorizing mailbox and may require reauthorization after account or Conditional Access changes.

## Cost model

Expected recurring cost is approximately USD 6-10/month:

- Basic ACR: approximately USD 5/month;
- weekly ACA executions: typically less than USD 1/month;
- low-volume Logic App actions, Blob storage, and Log Analytics ingestion: typically less than USD 5/month combined.

There is no always-on VM, dedicated compute, private endpoint, or Log Analytics commitment tier.