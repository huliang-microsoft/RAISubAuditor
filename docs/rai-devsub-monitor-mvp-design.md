# RAI Dev Subscription Monitor MVP

## Goal

Run a weekly, read-only scan of subscription `c920e969-c175-44e3-a64b-d3009bafe279`. Report resources whose trailing 30-day cost exceeds USD 100 and classify supported resource types as active, idle candidates, or unknown. Email the report to `coreairaifte@microsoft.com`.

## Architecture

- Resource group: `rai-devsub-monitor-rg`
- Azure Container Apps scheduled job running Python once per week
- Existing user-assigned identity `raiglobaldev/raiuai` (`clientId` `309f79d6-3efe-447f-823e-eaf5ad13431c`)
- Basic Azure Container Registry, built with ACR Tasks so no local Docker daemon is required
- Storage account/private Blob container for JSON, CSV, and HTML reports
- Log Analytics workspace for ACA execution logs, with 30-day retention
- Consumption Logic App with an HTTP trigger and Office 365 Outlook `SendEmailV2` action

The Office 365 connection uses delegated authorization. Its sender is the mailbox used to authorize the connection. The first deployment requires `huliang@microsoft.com` to authorize that connection in Azure Portal.

## Data flow

1. The job follows all Cost Management pages for the previous 30 complete UTC days, grouped by resource ID. The current partial day is excluded.
2. Rows at or below USD 100 are discarded.
3. For supported resource types, Azure Monitor metrics are queried for the same period.
4. A resource is an idle candidate only when every required usage metric has valid daily data on all 30 days in every returned time series, and all observed values are zero. Missing or invalid evidence never proves idle; explicit nonzero evidence proves activity. Each metric has an explicit aggregation, including ADX `QueryResult` with `Count`.
5. Supported idle and unknown resources are enriched with the latest nonzero Azure Monitor usage metric bucket over the previous 90 complete UTC days. A complete zero window is reported as `Not observed`, while missing evidence remains `Unavailable`.
6. JSON, CSV, and HTML artifacts are written to Blob Storage.
7. The HTML summary lists idle candidates and unknown resources over USD 100 in separate sections, omitting active resources. The Logic App emails `coreairaifte@microsoft.com` and acknowledges the run only after its Outlook action succeeds.

## Safety

- The Python process only issues GET/POST query operations, Blob report writes, and the Logic App notification POST.
- It has no cleanup/remediation code.
- Existing Owner rights on `raiuai` are not required by the application; a dedicated read-only identity should replace it after MVP validation.
- The report labels recommendations as review candidates, not deletion decisions.

## Idle-safe rules

These rules can classify a resource as an idle candidate only when every required metric has complete 30-day coverage and every observed value is zero.

| Resource type | Required 30-day evidence | Signal type |
|---|---|---|
| Event Hubs namespace | Incoming/outgoing messages and bytes | Data plane |
| Virtual machine | CPU and inbound/outbound network | Workload proxy |
| Container registry | Successful push and pull counts | Data plane |
| Azure AI Search | Query and indexing activity | Data plane |
| Azure Data Explorer | Query results and ingestion volume/results | Data plane |
| Container Instance group | Hourly CPU and inbound/outbound network | Workload proxy |

## Activity-only rules for Unknown resources

These rules never convert a resource to an idle candidate. They add a 90-day last-observed activity signal and likelihood to prioritize manual review while the classification remains `Unknown`.

| Resource type | Activity signal | Signal type |
|---|---|---|
| Azure Cache for Redis | Commands processed | Data plane |
| CDN profile | Request count | Data plane |
| Cognitive Services account | Calls or transactions | Data plane |
| Virtual machine scale set | CPU and network | Workload proxy |
| Managed Grafana | HTTP request count | Data plane |
| Data Factory | Pipeline run outcomes | Workload activity |
| Cosmos DB account | Request count | Data plane |
| Managed HSM | Service API calls | Data plane |
| Azure Machine Learning workspace | Workspace runs | Workload activity |
| Azure Firewall | Data processed | Data plane |
| Service Bus namespace | Incoming and outgoing messages | Data plane |
| Storage account | Transactions | Data plane |
| App Service plan | Bytes received and sent | Workload proxy |

Unknown likelihood is derived only from usable activity evidence:

- `Low`: nonzero usage observed in the last 30 days.
- `Medium`: last observed usage was 31-60 days ago, or older positive evidence has incomplete history coverage.
- `High`: last observed usage was 61-90 days ago with complete coverage, or no nonzero usage was observed with complete 90-day coverage.
- `Not assessed`: activity metrics are unavailable or coverage is insufficient.

AKS, Singularity, and other types without either rule remain `Unknown / Not assessed`. A likelihood is review priority, not deletion approval.

## Operations and failure handling

- Schedule: Mondays at 15:00 UTC.
- Cost or authentication failure fails the job and prevents a misleading email.
- Per-resource metric failures are captured in the report and classified `Unknown`.
- Logic App or Blob failure fails the job so ACA records an unsuccessful execution. HTTP 202 is not confirmation; HTTP 200 must acknowledge the same run ID after sending.
- ARM calls retry throttling and transient transport/service failures with jitter and service-provided retry delays. Each cost page has a 20-minute retry budget; metric requests have three minutes, with at most 12 attempts per request.
- ACA allows up to two replica retries and a two-hour replica timeout. A failed-execution metric alert emails the owner through an independent Azure Monitor action group. This does not detect a job that never starts.
- Ambiguous notification timeouts can cause duplicate emails on ACA retries; exactly-once delivery is not guaranteed.

## Estimated cost

Expected recurring cost is approximately USD 6-10/month: Basic ACR about USD 5/month, with low-volume ACA Jobs, Logic Apps, Blob Storage, and Log Analytics generally totaling less than USD 5/month. There is no always-on compute or commitment tier.

## Acceptance criteria

- Infrastructure deploys into `rai-devsub-monitor-rg`.
- A manual execution completes successfully.
- Reports are present in Blob Storage.
- An email from the authorized Outlook mailbox arrives at `coreairaifte@microsoft.com`.
- JSON/CSV contain all resource-attributed Cost Management rows above USD 100 across all pages. HTML/email contain idle and unknown rows above that threshold.
- Idle requires full 30/30 daily coverage; unavailable evidence is never interpreted as zero usage.
- The Logic App returns a matching successful acknowledgement only after Outlook succeeds, and the failed-execution alert is enabled.