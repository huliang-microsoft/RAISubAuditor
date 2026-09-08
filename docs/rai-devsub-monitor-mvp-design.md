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

1. The job queries Cost Management for the previous 30 days, grouped by resource ID.
2. Rows at or below USD 100 are discarded.
3. For supported resource types, Azure Monitor metrics are queried for the same period.
4. A resource is an idle candidate only when every required usage metric is available and all observed values are zero. Missing metrics, authorization failures, throttling, and unsupported types produce `Unknown`, never `Idle`.
5. JSON, CSV, and HTML artifacts are written to Blob Storage.
6. The HTML summary is posted to the Logic App, which emails `huliang@microsoft.com`.

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
- Logic App or Blob failure fails the job so ACA records an unsuccessful execution.
- API calls use bounded retries for throttling and transient service failures.

## Estimated cost

Expected recurring cost is approximately USD 6-10/month: Basic ACR about USD 5/month, with low-volume ACA Jobs, Logic Apps, Blob Storage, and Log Analytics generally totaling less than USD 5/month. There is no always-on compute or commitment tier.

## Acceptance criteria

- Infrastructure deploys into `rai-devsub-monitor-rg`.
- A manual execution completes successfully.
- Reports are present in Blob Storage.
- An email from the authorized Outlook mailbox arrives at `huliang@microsoft.com`.
- The report contains all Cost Management rows above USD 100 and never interprets unavailable evidence as zero usage.