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
| Recipient | `coreairaifte@microsoft.com` |
| Cost threshold | More than USD 100 over the previous 30 complete UTC days |
| Estimated platform cost | Approximately USD 6-10/month |

## Implementation

The implementation is a Python 3.12 container executed as an Azure Container Apps scheduled job. It uses `DefaultAzureCredential` with the attached user-assigned managed identity and performs this workflow:

1. Query every page of Azure Cost Management for actual cost over the previous 30 complete UTC days, grouped by resource ID, resource type, and resource group.
2. Retain resources whose aggregated cost is greater than USD 100.
3. Resolve Azure Monitor metric definitions for supported resource types.
4. Query required usage metrics over the same 30-day window.
5. Classify a resource as `Idle candidate` only when every required metric has valid data on all 30 days in every returned time series, and all observations are zero.
6. Enrich supported idle and unknown findings with the latest nonzero usage metric bucket over the previous 90 complete UTC days.
7. Store complete JSON/CSV findings and the HTML review summary in the private `reports` Blob container.
8. Send an HTML email with separate **`Idle candidate` and `Unknown` sections**, both restricted to costs above USD 100. `Active` rows are omitted.
9. Require a synchronous Logic App acknowledgement for this run, returned only after Outlook's send action succeeds.

JSON and CSV retain all classifications for audit and troubleshooting. Unknown resources appear in email with the reason they could not be classified; they are review items, not deletion recommendations. Resource names link to Azure Portal. The scan excludes the incomplete current UTC day, and the report labels the end date as exclusive.

## Classification safety

- `Idle candidate`: all required metrics have 30/30 daily coverage in every returned series and every observed value is zero.
- `Active`: at least one required metric has nonzero activity.
- `Unknown`: the resource type is unsupported, required evidence is missing/incomplete/invalid, or the metric request fails, with no sufficient evidence of activity.

Each metric uses its explicit aggregation, never a fallback to sampling count. Evidence includes interval, daily coverage and aggregation. ACI does not support a daily time grain, so it uses hourly data and requires all 24 hourly buckets to count a day as covered. Other rules use daily buckets. Coverage verifies the returned buckets, not continuous raw telemetry within each bucket. A resource retaining data or serving as a standby may still need to be kept even when traffic is zero.

`Unknown` is never converted to idle. The monitor does not stop, scale, delete, or otherwise modify business resources. An idle candidate is a review recommendation, not deletion approval.

Idle likelihood describes the strength of available usage evidence: `High`, `Medium`, `Low`, or `Not assessed`. Full zero coverage produces `High`; partial zero coverage is graded by the minimum required-metric coverage; nonzero evidence produces `Low` and an `Active` classification. `Not assessed` means no usable resource-specific evidence exists and is not an idle signal.

For resource types without a deletion-safe idle rule, activity-only rules keep the classification as `Unknown` but provide review priority from 90-day usage history:

- `Low`: nonzero usage observed within the last 30 days.
- `Medium`: last observed usage was 31-60 days ago, or older positive evidence has incomplete history coverage.
- `High`: last observed usage was 61-90 days ago with complete coverage, or no nonzero usage was observed with complete 90-day coverage.
- `Not assessed`: the activity metric is unavailable or coverage is insufficient.

A likelihood is not deletion approval. Activity-only rules can miss service-specific dependencies or usage modes, so these resources remain `Unknown` even when likelihood is `High`.

Last observed activity comes only from Azure Monitor usage metrics, not administrative activity. Event Hubs, ACR, Azure AI Search and ADX use data-plane traffic, push/pull, query, indexing or ingestion signals. VM and ACI CPU/network metrics are labeled as workload activity proxies because they do not identify an end-user request. The observation uses daily buckets, except ACI uses hourly buckets. `Not observed in last 90 days` means every required metric had complete coverage and no nonzero bucket in that bounded window; it never means the resource was never used. Unsupported types and incomplete evidence remain `Unavailable`.

## Supported MVP rules

| Resource type | Required evidence |
|---|---|
| Event Hubs namespace | Incoming/outgoing messages and bytes |
| Virtual machine | CPU and inbound/outbound network |
| Azure Container Registry | Successful push and pull counts |
| Azure AI Search | Search queries and indexing activity |
| Azure Data Explorer | `QueryResult` (`Count`) and ingestion volume/results (`Total`) |
| Azure Container Instance group | CPU and inbound/outbound network |

Other resource types remain `Unknown` and are included in email when cost exceeds the threshold.

## Activity-only rules for Unknown resources

| Resource type | Activity signal |
|---|---|
| Azure Cache for Redis | Commands processed |
| CDN profile | Request count |
| Cognitive Services account | Calls or transactions |
| Virtual machine scale set | CPU and network workload proxy |
| Managed Grafana | HTTP request count |
| Data Factory | Pipeline run outcomes |
| Cosmos DB account | Request count |
| Managed HSM | Service API calls |
| Azure Machine Learning workspace | Workspace runs |
| Azure Firewall | Data processed |
| Service Bus namespace | Incoming and outgoing messages |
| Storage account | Transactions |
| App Service plan | Network workload proxy |

These rules provide likelihood and last-observed activity only. They do not convert an `Unknown` resource into an idle candidate.

## Azure resources

- Azure Container Apps environment and scheduled job
- Basic Azure Container Registry for the scanner image
- Standard LRS Storage account with shared-key access disabled
- Private Blob container named `reports`
- Log Analytics workspace with 30-day retention
- Consumption Logic App
- Office 365 Outlook API connection authorized as `huliang@microsoft.com`
- Azure Monitor metric alert for failed job executions and an independent email action group

The Logic App callback is stored as an ACA secret. Reports use Entra ID authentication; no Storage account key is used. Weekly reports and failure alerts go to `coreairaifte@microsoft.com`; the existing Outlook sender authorization remains `huliang@microsoft.com`.

## Files

| File | Purpose |
|---|---|
| `monitor.py` | Cost query, metric classification, report generation, Blob upload, and notification |
| `Dockerfile` | Python 3.12 runtime image |
| `requirements.txt` | Pinned Python runtime dependencies |
| `logicapp.bicep` | Logic App and Office 365 connection deployment |
| `alerts.bicep` | Failed-execution alert and independent email action group |
| `deploy.ps1` | Idempotent Azure deployment and ACA configuration |
| `tests/test_monitor.py` | Classification, escaping, email filtering, and time-format regression tests |
| `tests/test_deployment.ps1` | Compiled Bicep contracts and deployment script syntax |
| `tests/test_live_metrics.py` | Opt-in, read-only checks of real metric names, aggregations and intervals |

## Deploy or update

Authenticate Azure CLI to the Microsoft tenant and run `deploy.ps1`. The script:

- registers required resource providers;
- creates or updates the infrastructure;
- builds the container remotely with ACR Tasks, so local Docker is unnecessary;
- attaches `raiuai`;
- stores the signed Logic App callback as a secret; and
- configures the weekly schedule, a two-hour replica timeout, and up to two replica retries;
- deploys the failure alert, evaluated every five minutes over a 15-minute window.

For an existing deployment, use PowerShell 7 and a versioned image tag:

```powershell
.\deploy.ps1 -UpdateOnly -ImageTag (git rev-parse --short HEAD)
```

`-UpdateOnly` rebuilds the image, updates the workflow/job and deploys the alert without recreating the storage, identity or authorized Outlook connection. It requires the existing infrastructure. No job execution is started by deployment. The default image tag is a UTC timestamp; `latest` is no longer used.

After the first deployment, authorize the `office365` API connection in Azure Portal as `huliang@microsoft.com`. Redeployment normally preserves the authorized connection.

## Manual execution and validation

Start `rai-devsub-monitor-job` from Azure Portal or Azure CLI. A successful run must satisfy all of the following:

1. ACA execution status is `Succeeded`.
2. The dated Blob prefix contains `report.json`, `report.csv`, and `report.html`.
3. The Logic App run status is `Succeeded`.
4. The `send_email` and `confirm_sent` actions both succeed; email includes idle candidates and high-cost unknown resources.

The first validated production scan processed 204 resources above the threshold and identified 42 idle candidates with approximately USD 20,072.52 in trailing 30-day cost.

## Operations

- Application and platform logs are in `rai-devsub-monitor-law`.
- Reports are private and organized as `YYYY/MM/DD/<run-id>/report.{json,csv,html}`.
- Transient ARM failures and connection/time-out errors get at most 12 attempts per request, with a 20-minute budget for each cost page and a three-minute budget for each metric request. Backoff honors `Retry-After` (seconds or HTTP date) and Cost Management rate-limit retry headers, plus jitter. A server delay outside the remaining budget fails the request rather than retrying early.
- A Cost Management or authentication failure fails the whole job.
- A per-resource metric failure becomes `Unknown` and appears in email above the threshold.
- An HTTP 202 response is not delivery confirmation. The job requires HTTP 200 with `status=Sent` and its own `runId`; failed or unconfirmed sends fail the job without logging the signed callback URL.
- The Azure Monitor action group notifies the recipient of failed executions independently of Outlook. The alert is not a missing-schedule watchdog. Do not unsubscribe its email receiver.
- A timeout can occur after an email is sent; automatic ACA retries can then produce duplicates. Check Logic App run history before a manual retry. The job has no exactly-once delivery guarantee.
- The Office 365 delegated connection depends on the authorizing mailbox and may require reauthorization after account or Conditional Access changes.

## Tests

```powershell
python -m pip install -r requirements.txt pytest
python -m pytest -q tests
.\tests\test_deployment.ps1
```

Live metric tests are skipped unless `LIVE_REPORT_URL` points to an existing private JSON report. They use `az login` credentials and perform only Blob/ARM reads. Do not put a SAS token in that URL; use Entra authorization.

## Cost model

Expected recurring cost is approximately USD 6-10/month:

- Basic ACR: approximately USD 5/month;
- weekly ACA executions: typically less than USD 1/month;
- low-volume Logic App actions, Blob storage, Log Analytics ingestion and a metric alert: typically less than USD 5/month combined, subject to regional pricing and free allowances.

There is no always-on VM, dedicated compute, private endpoint, or Log Analytics commitment tier.