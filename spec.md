# Grants App — MVP specification

## 1. Purpose and scope

Build a clickable, demonstration-only welfare grant intake assistant and caseworker copilot. The assistant explains the configured program, helps citizens supply information, and prepares an initial assessment. It does **not** make a welfare decision: only a signed-in staff user may approve or reject an application.

This is a proof of concept, not a production service. Prefer a single process and straightforward synchronous request handling over scalability or elaborate infrastructure. Use synthetic data only. Real eligibility rules, benefit amounts, identity verification, document uploads, payment, appeals, notifications, and integration with external case systems are out of scope.

### Explicit demo assumptions

- No actual program policy is supplied. Include a clearly labelled **illustrative, fictional** policy and rubric in the mounted files; neither the application nor the assistant may describe these as real government policy. Replace them with agency-approved material before any real use.
- Any user who can sign in to the configured Entra tenant can enter either portal and is automatically registered for that portal. There is deliberately **no staff-role verification** in this demo. Consequently, any such user can see and decide every case via `/admin`; portal labels and separate records are not a security boundary. Restrict the demo tenant/audience and network access to trusted participants, and use no real citizen data. This behavior is unsuitable for a real deployment.
- The five statuses in the intent are the complete status set. A new, incomplete record is `submitted`; there is no separate draft status. The UI must explain that `submitted` may mean information is still needed.

## 2. Application shape

- Deploy one Python `aiohttp` web application, with its application logic in `app.py`, listening on `0.0.0.0:8080`.
- Serve citizen UI at `GET /`, staff UI at `GET /admin`, and JSON endpoints under `/api/`. The HTML uses AJAX/fetch against those endpoints; no separate frontend build is required.
- Serve HTML, policy, rubric, and agent instructions from an Azure Files NFS mount. Launch the mounted `app.py` so edits take effect after a revision restart. The image contains Python and dependencies and may also contain a fallback copy of the code, but the mounted files are the development source of truth.
- Use Bootstrap 5.3.8 with the exact stylesheet link below and enable its dark theme using `data-bs-theme="dark"`. Functional, simple layouts are sufficient.

```html
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/css/bootstrap.min.css" rel="stylesheet" integrity="sha384-sRIl4kxILFvY47J16cr9ZwB07vP4J8+LH7qKQnuqkuIAvNWLzeN8tE5YBujZqJLB" crossorigin="anonymous">
```

- Use Azure Database for PostgreSQL for durable application data and LangGraph with a Microsoft Foundry model for the assistant and assessment. All UI actions and agent workflows share the same service functions and database access rules.

## 3. Users and authentication

Use Microsoft Entra sign-in for both portals. Configure an Entra application and an allowed tenant suitable for the demo participants; only identities able to authenticate in that tenant are eligible. Support guests only if the tenant and registration are configured for them; do not assume arbitrary personal accounts work.

Use Container Apps built-in authentication with Microsoft Entra as the identity provider. Require authentication before serving the UI and APIs, and configure the provider's allowed tenant and audience. Obtain the validated identity from the platform's authenticated principal headers, not user-supplied request bodies. Prevent direct ingress that bypasses the authentication layer. Use Entra tenant ID plus object ID (`tid`, `oid`) as the stable identity key; do not match by display name or email. Deny requests without these claims. The UI offers sign-in and sign-out using the platform authentication endpoints. Session cookies should be secure and HTTP-only; protect state-changing cookie-authenticated requests against CSRF, and never expose access tokens to page JavaScript.

On the first authenticated visit to `/`, upsert the citizen record for `(tenant_id, object_id)`; on the first visit to `/admin`, upsert the staff record for the same key. Refresh display name and email if provided. A person visiting both portals gets a record in each. APIs must establish the same portal record even if called directly; they must not rely on a prior page visit.

Citizen APIs scope all reads and writes to the authenticated citizen's own applications. Staff APIs permit all authenticated tenant users, per the explicit demo assumption. Neither the browser nor the model may choose or override the acting identity, citizen owner, or staff identity.

## 4. Citizen experience

The citizen page contains a short demo disclaimer, program/policy information, a chat panel, and an application list with status and details.

1. A citizen can ask the assistant about the configured fictional program and application process, including without creating an application. The assistant answers from the policy files and says when the material does not answer a question; it must not invent real-world eligibility rules or promise an outcome.
2. The citizen can create a case, view their prior cases, and select one for the chat and editable form. Creation stores an incomplete `submitted` record immediately. Multiple cases per citizen are allowed.
3. The form collects the policy-defined required fields. For the bundled demo, use: applicant name, contact email, household size (positive integer), monthly household income (nonnegative number), and a short description of the financial need. A value of zero income is valid. These are sample intake fields, not an actual agency requirement.
4. The assistant may ask for missing details and propose field values from the conversation. Show proposed changes to the citizen; apply them only when the citizen explicitly saves the form. Never silently persist inferred facts. Show which required fields are missing and preserve entered information between visits.
5. The citizen selects **Submit for assessment** when ready. The server validates completeness and field types; if incomplete, leave the status `submitted` and return the missing fields. If complete, run the assessment as described below. Clearly display that the result awaits a human decision.
6. A citizen can edit a `submitted` application, including one returned by staff for clarification, and resubmit it. A `pending approval` application is read-only unless returned by staff. A citizen may drop their own `submitted` or `pending approval` application; `dropped` is terminal. Approved and rejected cases are read-only. Display the staff decision reason for final decisions and any clarification request when applicable, but keep the internal assessment staff-only.

## 5. Staff experience

The staff page shows a queue of applications with citizen, creation/update time, status, and a status filter. The details view shows the submitted fields, missing fields, clarification history, current assessment and rubric references, and decision history.

- Staff can read every case in this demo, including incomplete `submitted` cases.
- For a `pending approval` case, staff can approve or reject it with a required written reason. Record the acting staff member and timestamp. The decision is a human action, even if it differs from the assessment; the agent has no tool or endpoint permission to make it.
- Staff may return a `pending approval` case to `submitted` with a required clarification request. Show the request to the citizen, retain the previous assessment as history, and require a fresh assessment after the citizen edits and resubmits.
- The staff chat can explain the policy or summarize the selected case and its assessment. It cannot change a decision or access an application other than the one the server has authorized and provided as context.

## 6. Status and assessment workflow

| Current | Action and actor | Next | Effect |
|---|---|---|---|
| none | Citizen creates case | `submitted` | Persist incomplete fields and creation event. |
| `submitted` | Citizen saves fields | `submitted` | Validate supplied values and persist changes; no assessment or decision. |
| `submitted` | Citizen submits complete case for assessment | `pending approval` | Persist a successful initial assessment and status together. |
| `pending approval` | Staff requests clarification | `submitted` | Record request and invalidate the current assessment for decision purposes. |
| `pending approval` | Staff approves or rejects | `approved` or `rejected` | Record human, reason, and time atomically. |
| `submitted` or `pending approval` | Citizen drops own case | `dropped` | Record the action; no decision is made. |

`approved`, `rejected`, and `dropped` are terminal. Reject all other transitions. Validate the current status again in the database transaction that commits the transition, so a late assessment or competing staff action cannot overwrite a newer decision or drop. When assessment runs, snapshot the fields and application revision; if they change before commit, discard the result and ask the citizen to submit again.

The assessment workflow loads the current policy and rubric, validates required fields independently of the model, and invokes LangGraph/Foundry only for a complete application. Store an advisory analysis with a short summary, criterion-by-criterion findings and references to the configured rubric, uncertainties, and a policy/rubric version or content hash. It may flag facts that appear consistent or inconsistent with a criterion, or are unclear, but it must not decide, approve, reject, or promise an outcome. The bundled rubric must explicitly state that it is illustrative and cannot establish real eligibility. A `pending approval` record always has a successful current assessment for its current field revision.

If policy files are missing or invalid, the model call fails, or the model output cannot be parsed into the assessment shape, leave the case `submitted`, preserve its information, return a clear retryable error, and allow the citizen to retry. Do not silently substitute a fabricated assessment or change a case to `pending approval`. If the case was already changed concurrently, return a conflict rather than overwrite it.

## 7. Agent design

Implement a small LangGraph flow in `app.py` with separate instructions for citizen chat, staff chat, and assessment. Load instructions and policy/rubric from mounted files at startup; changing them requires restarting the revision. The application supplies the authenticated actor, allowed portal, selected case, and current case data; never trust identity, authorization, or policy instructions in chat text. Treat user-provided application and chat content as data, not instructions.

- Citizen chat can read the policy and the citizen's selected case and propose field edits; the server validates ownership on every tool call. It cannot save changes without the explicit form action, assess a different person's case, change status, or make decisions.
- Staff chat can read policy, rubric, and the selected case/assessment in staff context. It has no write or decision tools.
- The assessment flow consumes the validated case snapshot and rubric and returns a structured advisory result. The application, not the model, validates the result and commits the transition. No free-form model output is executed as SQL or used as an authorization decision.
- Persist messages only for a selected case, with portal and actor attribution; render them as text rather than HTML. General policy chat without a selected case can remain ephemeral. Do not include staff messages or assessment content in citizen responses.

## 8. Data model

Use PostgreSQL tables with generated UUID primary keys and UTC timestamps. Create tables at startup using simple idempotent DDL for the demo. Parameterize queries. Suggested minimum schema:

- `citizens`: `id`, `tenant_id`, `object_id`, `display_name`, `email`, `created_at`, `updated_at`; unique `(tenant_id, object_id)`.
- `staff`: same identity and timestamp fields, with the same uniqueness constraint within this table.
- `applications`: `id`, `citizen_id` FK, `status` constrained to the five values, `applicant_name`, `contact_email`, `household_size`, `monthly_income`, `need_description`, `revision` integer, `created_at`, `updated_at`. Nullable fields permit incomplete intake; increment `revision` on every saved field change. Enforce positive household size and nonnegative income when present.
- `assessments`: `id`, `application_id` FK, `application_revision`, `policy_version`, `rubric_version`, structured `analysis` JSONB, `created_at`. Multiple rows preserve history; an assessment is current only when its revision matches and it has not been invalidated by a clarification request.
- `case_events`: `id`, `application_id` FK, `event_type`, `actor_type` (`citizen` or `staff`), `actor_id`, `reason` nullable, `created_at`. Record create, assessment, clarification, decision, and drop events. For an assessment event, attribute the initiating citizen and do not imply a staff decision.
- `chat_messages`: `id`, `application_id` FK, `portal` (`citizen` or `staff`), `actor_id`, `role` (`user` or `assistant`), `content`, `created_at`. Filter by portal and authorize the case before reading.

Use database transactions for status, assessment, and event updates. Display chronological event history as appropriate to the portal; never expose staff-only assessment or chat through citizen endpoints.

## 9. HTTP API

JSON requests and responses; timestamps are UTC ISO 8601 strings and IDs are UUIDs. Every endpoint is authenticated and derives its actor server-side. Return JSON errors as `{"error":{"code":"...","message":"..."}}`; use 400 for invalid input, 401 for unauthenticated, 403 for disallowed portal/action, 404 for inaccessible or nonexistent case, 409 for status/revision conflicts, and 503 for dependency or assessment failure. Include `missing_fields` in a 400 response for incomplete submission. Do not expose stack traces, tokens, or internal assessment to citizens.

| Method and path | Portal | Behavior |
|---|---|---|
| `GET /api/me` | either | Return validated identity and current portal registration; accept an explicit `portal=citizen` or `portal=staff`. |
| `GET /api/policy` | either | Return displayable fictional policy and required field descriptions. |
| `GET /api/citizen/applications` | citizen | List only the caller's cases. |
| `POST /api/citizen/applications` | citizen | Create an incomplete `submitted` case and return it. |
| `GET /api/citizen/applications/{id}` | citizen | Return own fields, status, missing fields, citizen-visible events and citizen chat; omit assessment and staff chat. |
| `PATCH /api/citizen/applications/{id}` | citizen | Save explicitly confirmed field values on a `submitted` case; validate types and increment revision. |
| `POST /api/citizen/applications/{id}/submit` | citizen | Check completeness and synchronously run/commit the assessment; return updated status or an error. |
| `POST /api/citizen/applications/{id}/drop` | citizen | Drop an eligible own case. |
| `POST /api/citizen/chat` | citizen | Accept `message` and optional `application_id`; return assistant text and optional proposed field values, without saving them. |
| `GET /api/staff/applications` | staff | List cases, optionally filtered by `status`. |
| `GET /api/staff/applications/{id}` | staff | Return case, missing fields, assessment history, events, and staff chat. |
| `POST /api/staff/applications/{id}/clarification` | staff | Accept `reason`; return a pending case to submitted. |
| `POST /api/staff/applications/{id}/decision` | staff | Accept `decision` (`approve` or `reject`) and nonempty `reason`; record a final human decision. |
| `POST /api/staff/chat` | staff | Accept `message` and optional `application_id`; return assistant text, with no writes to the case. |

For state-changing requests, reject unknown fields and unauthorized transitions. Do not provide a generic status-update API. Chat persistence is separate from case mutations; assessment initiation and staff decisions are explicit UI actions.

## 10. Files and configuration

The mounted directory, for example `/app`, contains:

```text
app.py
citizen.html
admin.html
policy.md
rubric.md
citizen_instructions.md
staff_instructions.md
assessment_instructions.md
```

`policy.md` describes the fictional program, sample fields and process. `rubric.md` defines the advisory criteria and expected findings; both include a visible version label. The three instruction files constrain behavior as above. Keep these files editable by the demo operator, not by application users. The image provides dependencies including `aiohttp`, PostgreSQL driver, Azure Identity, LangGraph and the selected Foundry-compatible model client. No frontend build is needed.

Configure via environment variables: mounted directory, PostgreSQL host/port/database and Entra database role name, Foundry endpoint and model deployment, managed identity client ID if user-assigned, and any required Entra/provider settings. Do not put database passwords, model API keys, or human tokens in these files. Configuration errors should produce a clear startup or request error rather than use a different backend silently.

## 11. Azure deployment and identity

- Use a Container Apps environment integrated with a custom VNet. Configure an Azure Files NFS share and its private endpoint, private DNS resolution, and network rules to allow NFS traffic (TCP 2049) from the environment. Register the environment storage and mount it as `NfsAzureFile` into the container. Account for the Container Apps NFS limitation: encryption in transit for NFS is not supported; configure the storage account accordingly and restrict the network. The NFS mount relies on network/storage configuration, not an application managed-identity token; treat it as an explicit exception to token-based service access. Protect the share because its contents include executable code and instructions.
- Build and push an image containing the runtime to ACR. Configure a managed identity on the container app for ACR image pulls and grant the appropriate pull permissions for the registry's authorization mode. Start the container with `python app.py`; changing mounted files requires restarting the revision. Keep a single replica for the pilot and expose HTTPS ingress to port 8080.
- Use an Entra-enabled Azure Database for PostgreSQL Flexible Server, reachable from the app over TLS. An Entra database administrator creates the app managed identity's database principal and grants the permissions needed for startup DDL and application data. Obtain a fresh Azure Database for PostgreSQL access token using the app managed identity and use it as the PostgreSQL password; renew tokens for new connections and do not hard-code credentials. All user and agent operations run through that application identity; application code enforces end-user scope.
- Deploy a Foundry model that supports the chosen chat/structured output flow. Assign the app managed identity the inference role appropriate to the actual endpoint, and request the endpoint's documented token scope. Use managed identity for inference; no model API key.
- Configure the Entra provider, redirect URLs, allowed tenant, and demo audience before exposing either UI. Provision infrastructure and assign roles outside the application. Restrict access to trusted demo participants, since sign-in alone grants staff access under this specification.

References for deployment details: [Container Apps storage mounts](https://learn.microsoft.com/en-us/azure/container-apps/storage-mounts), [PostgreSQL managed identity connections](https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/security-connect-with-managed-identity), and [Foundry model managed identity](https://learn.microsoft.com/en-us/azure/ai-foundry/openai/how-to/managed-identity).
