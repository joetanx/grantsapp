# Grants App — MVP specification

## Purpose and scope

Build a clickable, demonstration-only welfare grant intake assistant and caseworker copilot. Citizens can ask about a configured grant, provide an application, and follow its status. The assistant identifies missing information and prepares an initial policy-based assessment. A staff member, not the agent, makes the final approval or rejection decision.

This is a proof of concept, not a production welfare service. Use fictional applicants and a clearly labelled fictional policy; do not use real citizen data. Prioritize a small, working implementation over production-grade architecture, extensive styling, or scale. Payments, document uploads, notifications, appeals, multiple grant programs, and automated final decisions are out of scope.

## Implementation shape

- One Python application file, `app.py`, using `aiohttp`, listening on `0.0.0.0:8080`. It serves the citizen UI at `/`, the staff UI at `/admin`, and JSON endpoints under `/api/`.
- Two static HTML files with vanilla JavaScript and AJAX; no separate frontend build. Load the following Bootstrap stylesheet exactly, and use Bootstrap's dark theme:

  ```html
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/css/bootstrap.min.css" rel="stylesheet" integrity="sha384-sRIl4kxILFvY47J16cr9ZwB07vP4J8+LH7qKQnuqkuIAvNWLzeN8tE5YBujZqJLB" crossorigin="anonymous">
  ```

- Azure Container Apps hosts a single container and revision, with HTTPS ingress to port 8080. Keep one replica for the pilot. The image contains Python and dependencies; the mounted Azure Files share contains `app.py`, HTML, configuration, policy, agent instructions, and assessment rubric. Start the mounted code, for example `python /mnt/grants/app.py`; changing dependencies still requires rebuilding the image. Update share files and restart the revision to pick up changes.
- Azure Database for PostgreSQL Flexible Server stores users, application records, conversations, assessments, and decisions. Initialize tables from startup DDL in `app.py`; no separate migration framework is required.
- LangGraph implements the assistant and assessment flows; an Azure AI Foundry model provides language responses. The UI and agent use the same application service functions for data access and validation, rather than calling HTTP endpoints from the agent.
- Read runtime settings from environment variables, with non-secret policy and prompt files on the share. Never put credentials or access tokens in the share or source files.

## Identity and access

Use Container Apps built-in authentication with Microsoft Entra ID and require authentication for the application. Configure an Entra application and tenant/account audience appropriate for the pilot, the Container Apps callback URI, and any authentication configuration required by the platform. Enable HTTPS only. Login redirects to the requested portal; provide sign-out through `/.auth/logout`.

For authenticated requests, read the identity and claims injected by Container Apps, not user-supplied IDs or names. Key a person by stable Entra tenant ID plus object ID, not email or display name; reject requests if those identifiers are unavailable. Only trust platform-injected identity headers when requests reach the app through the authentication middleware; do not expose the container directly around it. Upsert a citizen row on first use of citizen routes and a staff row on first use of staff routes; refresh display name and email if present. One Entra identity may have both records. Configure Entra to accept the pilot's intended accounts; “any valid Entra login” means any identity accepted by that configuration, not every Microsoft account globally.

For this demo, any accepted Entra user who visits `/admin` becomes staff and can view and decide every application; any accepted user visiting `/` becomes a citizen. Enforce portal access and citizen ownership on every API and agent tool call, not just in the UI. **This intentionally permits citizens to self-provision staff access and is unsafe for real or sensitive data.** Restrict the entire pilot to controlled accounts and fictional data. A real deployment must replace this rule with separately authorized staff roles before handling actual cases.

Cookie-authenticated state-changing requests must check a same-origin `Origin` and require a CSRF token supplied by the UI. Return JSON errors for API failures without disclosing secrets or stack traces.

## Citizen experience

The citizen page shows a demo disclaimer, signed-in identity and sign-out, a conversation panel, a button to start an application, a structured application form, and a list of the citizen's applications with statuses and timestamps. Selecting an application shows its data, conversation, and any final decision and staff reason. Do not expose internal assessment reasoning to the citizen by default.

1. The citizen can ask the assistant about the configured fictional policy and how to apply, even before starting an application. The assistant explains that staff decide and does not promise eligibility or payment. General conversations need not be persisted.
2. Starting an application creates a `submitted` record, initially incomplete, and opens it. Here `submitted` means the citizen has started a request, not that it is ready for staff review. Multiple applications are allowed; each belongs to the signed-in citizen.
3. The form and assistant collect only the fields required by the configured policy. The citizen can save partial values while the application is `submitted`. Show missing fields and allow the citizen to correct saved fields in this state. Chat is tied to the selected application and persisted; the assistant may propose values from chat, but the citizen must confirm them in the form before they are saved as application data. A chat message alone must not silently change a field.
4. After each confirmed save, validate required fields. When complete, run the assessment automatically; show that it is in progress. On success save the assessment and change the record to `pending approval`. If the assessment fails, keep it `submitted`, display a retry action, and preserve the entered data. A retry or subsequent save must not duplicate applications or assessments for the same unchanged input.
5. A citizen may drop their own `submitted` or `pending approval` application. Dropped applications are retained in history, cannot be edited or assessed, and cannot be decided by staff. Pending applications cannot be edited: the citizen can drop one and start a new application if corrections are needed. Approved and rejected applications are final and read-only.

## Staff experience

`/admin` shows a queue of applications, filterable by status and ordered by most recently updated. The default filter is `pending approval`. Selecting an item shows the applicant identity, submitted fields, relevant conversation, current status, timestamps, policy and rubric version, initial assessment, and decision history. Staff can inspect other states but may decide only a pending application.

For a pending application, staff can approve or reject it and must enter a reason. The server records the staff identity, reason, and decision time, then moves the application to `approved` or `rejected`. Show the decision to the citizen. Refresh the list and details after a decision. If the record changed since it was opened, reject the action as a conflict and refresh rather than overwriting a newer state.

## Policy and data collection

No authoritative welfare policy is supplied. Include a small, explicitly fictional pilot policy and matching rubric in the mounted files. Use a single sample grant, with the following illustrative required fields and rules; they are implementation defaults, not real eligibility advice:

| Field | Type and validation | Fictional assessment rule |
|---|---|---|
| `full_name` | non-empty string | Identification only |
| `residency` | `yes` or `no` | `yes` meets the demo residency rule |
| `household_size` | integer of at least 1 | Used to provide case context |
| `monthly_household_income` | non-negative decimal, in the policy's fictional currency | At or below 2000 meets the demo income rule |
| `assistance_reason` | non-empty string | Case context, not an automatic disqualifier |

Display the fictional currency and thresholds in the policy. Policy and rubric files carry explicit version identifiers. The rubric maps each rule to a citation or section label in the policy and directs the assistant to distinguish facts supplied by the applicant, missing or uncertain information, and rule findings. Preserve the versions used in each assessment. If policy files are missing or invalid, do not invent rules or move applications to pending; show an actionable configuration error.

## Agent behavior

- Implement a citizen intake graph that receives the authenticated citizen context, optional owned application, policy, and conversation. It answers policy/process questions, points out missing data, and proposes structured field values for citizen confirmation. It must not infer missing facts, promise an outcome, or make a final decision.
- Implement an assessment graph triggered only for a complete `submitted` application. Supply a snapshot of validated fields, policy, and rubric. Produce structured output containing policy/rubric versions, rule-by-rule findings with source references, a concise rationale, uncertainties, and a non-binding recommendation (`appears eligible`, `appears ineligible`, or `needs review`). The staff member may disagree. Never set `approved` or `rejected` from an agent or model response.
- Provide narrowly scoped agent tools backed by shared service functions, such as reading the current policy and reading the current authorized application's data. Citizen tools cannot list other people's records or perform staff actions. Do not expose unrestricted SQL, arbitrary file access, or status-changing decisions as model tools. Treat citizen text as untrusted data rather than instructions overriding policy or authorization.
- Validate the assessment structure and policy references before persistence. If the model or validation fails, retain `submitted` and surface a retryable error. Persist the assessment and the transition to `pending approval` together; use a transaction and a conditional update against the application version so stale model output cannot overwrite edited or dropped data. Do not hold a database transaction open while waiting for the model. A repeated request for the same unchanged input must return the existing result or conflict rather than create a second active assessment.

## Application states

| Current state | Action and actor | Next state |
|---|---|---|
| none | Citizen starts application | `submitted` |
| `submitted` | Citizen saves incomplete or changed fields | `submitted` |
| `submitted` | Complete data and successful initial assessment | `pending approval` |
| `submitted` or `pending approval` | Owner drops application | `dropped` |
| `pending approval` | Staff approves with reason | `approved` |
| `pending approval` | Staff rejects with reason | `rejected` |

`approved`, `rejected`, and `dropped` are terminal. An assessment failure leaves the record in `submitted`. Use conditional updates or row locking so simultaneous assessment, drop, or decision actions cannot apply conflicting transitions. Store timestamps in UTC.

## Persistence model

Use UUID primary keys and foreign keys. A compact schema is sufficient:

- `citizens`: `id`, `tenant_id`, `object_id`, `display_name`, `email`, `created_at`, `updated_at`; unique `(tenant_id, object_id)`.
- `staff`: same identity and timestamp fields, with its own unique `(tenant_id, object_id)`.
- `applications`: `id`, `citizen_id`, `status`, `data` JSONB, `version` integer, `created_at`, `updated_at`. Increment `version` on data or status changes. Validate `status` against the five states above.
- `messages`: `id`, `application_id`, `role` (`citizen` or `assistant`), `content`, `created_at`; append messages in order. Never save secrets or tokens in messages.
- `assessments`: `id`, `application_id`, `application_version` for the data snapshot assessed, `policy_version`, `rubric_version`, `result` JSONB, `created_at`; unique `(application_id, application_version)`.
- `decisions`: `id`, `application_id`, `staff_id`, `decision` (`approved` or `rejected`), `reason`, `created_at`; at most one final decision per application. Insert the decision and update application status in one transaction.

The assessment snapshot version is the version before the pending transition increments it. Only one successful assessment for that data snapshot is retained. The application, its messages, assessment, and decision form the request history; no deletion is required in the pilot.

## HTTP interface

All `/api/` requests require authentication. JSON requests and responses use `application/json`. Derive caller identity from the authenticated request; ignore any client-supplied owner or staff ID. Citizen application endpoints always verify ownership; staff endpoints establish the staff record using the demo rule above. Return `400` for invalid input, `401` for no authenticated session, `403` for a forbidden action, `404` for an unavailable record, `409` for stale versions or invalid state transitions, and `503` for unavailable policy/model/database services. Error bodies use `{"error":{"code":"...","message":"..."}}`.

| Method and route | Actor | Request / result |
|---|---|---|
| `GET /api/me` | either | Current identity and portal context |
| `GET /api/policy` | citizen or staff | Fictional policy, version, required field definitions |
| `GET /api/applications` | citizen | Caller's application summaries |
| `POST /api/applications` | citizen | Create an empty `submitted` application; return its ID and version |
| `GET /api/applications/{id}` | owner | Application fields, status, messages, and any final decision; omit internal assessment |
| `PUT /api/applications/{id}` | owner | `{version, data}` replaces the confirmed field object; validate and save, then trigger assessment if complete; return updated application or assessment error with saved state |
| `POST /api/applications/{id}/assess` | owner | `{version}` retries assessment of complete submitted data; return updated status or a retryable error |
| `POST /api/applications/{id}/drop` | owner | `{version}` drops a submitted or pending application |
| `POST /api/chat` | citizen | `{application_id: null or UUID, message}`; return assistant reply and optional proposed field values; persist messages only when an owned application is selected |
| `GET /api/admin/applications?status=...` | staff | Application summaries; optional valid status filter |
| `GET /api/admin/applications/{id}` | staff | Full application, conversation, assessment and decision |
| `POST /api/admin/applications/{id}/decision` | staff | `{version, decision, reason}`; decision is `approved` or `rejected`; return updated application |

Return the new `version` whenever application state changes. The UI sends the last observed version for writes and reloads on `409`. An assessment may take time: requests can run synchronously in the MVP, with the UI showing progress and allowing a retry after an error. If the response is lost, reload the application before retrying. Do not report the save itself as rolled back when only assessment failed.

## Azure resources and configuration

- Container Apps: custom-VNet environment; external HTTPS ingress; port 8080; one replica; authentication enabled; image pulled from Azure Container Registry with a managed identity authorized for that registry's configured access mode (for example `AcrPull` where applicable).
- Azure Files: use a supported **classic** NFS Azure file share, an environment storage definition and an `NfsAzureFile` volume mounted at `/mnt/grants`. Use a private endpoint, private DNS and VNet routing so the Container Apps environment reaches the share. Restrict write access to deployment operators and mount read-only in the app. Container Apps requires the NFS share not to require encryption in transit; account for this limitation in the pilot network design. NFS mount access is network-based, not managed-identity data-plane authentication; do not claim that the NFS mount authenticates with the app identity. This is a platform constraint on the managed-identity preference, not permission to store a storage key in application code.
- PostgreSQL: enable Entra authentication, create the application managed identity's database principal as an Entra administrator, and grant the necessary database/schema privileges. Obtain a fresh token for `https://ossrdbms-aad.database.windows.net/.default` when opening connections, use it as the PostgreSQL password, use TLS with server certificate validation, and refresh tokens for new connections. Both web actions and agent workflows use this same database identity. Configure network access and DNS from the Container Apps environment.
- Azure AI Foundry: configure a model deployment and endpoint; grant the application managed identity the appropriate Foundry inference role and use an Entra token rather than an API key. Model deployment, endpoint and region are deployment inputs, not invented policy.
- Configure at least `PORT`, `APP_DIR`, `POLICY_PATH`, `RUBRIC_PATH`, `INSTRUCTIONS_PATH`, `POSTGRES_HOST`, `POSTGRES_DATABASE`, `POSTGRES_USER`, `FOUNDRY_PROJECT_ENDPOINT`, `FOUNDRY_MODEL_DEPLOYMENT`, and, for a user-assigned identity, `AZURE_CLIENT_ID`. Keep the authentication client configuration in Container Apps; if its Entra provider requires a client secret, store it as a Container Apps secret, not in source or the share. This interactive login configuration is distinct from managed identity for non-human service access.
- Log operational errors and request identifiers, not full conversations, personal data, tokens, or model prompts. Show a generic failure message to users.

## Reference documentation

- [Container Apps authentication](https://learn.microsoft.com/en-us/azure/container-apps/authentication)
- [Container Apps storage mounts and NFS constraints](https://learn.microsoft.com/en-us/azure/container-apps/storage-mounts)
- [Container Apps managed-identity image pull](https://learn.microsoft.com/en-us/azure/container-apps/managed-identity-image-pull)
- [PostgreSQL Flexible Server managed identity authentication](https://learn.microsoft.com/en-us/azure/postgresql/security/security-connect-with-managed-identity)
