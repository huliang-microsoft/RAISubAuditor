# RAI Dev Subscription Monitor MVP

## Goal

Run a weekly, read-only scan of subscription `c920e969-c175-44e3-a64b-d3009bafe279`. Report resources whose trailing 30-day cost exceeds USD 100 and classify supported resource types as active, idle candidates, or unknown. Email the report to `huliang@microsoft.com`.

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
5. JSON, CSV, and HTML artifacts are written to Blob Storage.
6. The HTML summary lists idle candidates and unknown resources over USD 100 in separate sections, omitting active resources. The Logic App emails `huliang@microsoft.com` and acknowledges the run only after its Outlook action succeeds.

## Safety

- The Python process only issues GET/POST query operations, Blob report writes, and the Logic App notification POST.
- It has no cleanup/remediation code.
- Existing Owner rights on `raiuai` are not required by the application; a dedicated read-only identity should replace it after MVP validation.
- The report labels recommendations as review candidates, not deletion decisions.

## Supported MVP rules

- Event Hubs namespaces: incoming/outgoing messages and bytes
- Virtual machines: CPU plus inbound/outbound network; any observed activity is active
- Container registries: successful pushes and pulls
- Azure AI Search: query and indexing activity
- Azure Data Explorer: query and ingestion activity when those metrics are exposed
- Container Instances: CPU and network activity

AKS, AML/Singularity, and unknown resource types are listed with cost but remain `Unknown` until a resource-specific rule is added.

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
- An email from the authorized Outlook mailbox arrives at `huliang@microsoft.com`.
- JSON/CSV contain all resource-attributed Cost Management rows above USD 100 across all pages. HTML/email contain idle and unknown rows above that threshold.
- Idle requires full 30/30 daily coverage; unavailable evidence is never interpreted as zero usage.
- The Logic App returns a matching successful acknowledgement only after Outlook succeeds, and the failed-execution alert is enabled.