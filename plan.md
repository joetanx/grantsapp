# Grants App — implementation plan

## Goal and boundaries

Build the demonstration-only intake assistant and caseworker copilot described in `intent.md` and `spec.md`. The deliverable is one Python `aiohttp` application, two simple browser pages, editable policy and instruction files, a runtime container image, and deployment instructions for Azure. Use only synthetic data. Keep the implementation direct and compact; do not introduce a separate frontend build, background queue, or automatic welfare decisions.

The fictional policy is not real government policy. Any user who can authenticate in the configured Entra tenant can enter both portals and decide any case; there is no staff-role check. Restrict the demo tenant and network to trusted participants. A real deployment requires agency-approved policy and appropriate authorization and security controls.

## 1. Prepare files and runtime

- Create `app.py`, `citizen.html`, `admin.html`, `policy.md`, `rubric.md`, `citizen_instructions.md`, `staff_instructions.md`, and `assessment_instructions.md`. These files are served or read from Azure files mounted to container at `/app`; launch its `app.py` rather than the image's fallback copy. Policy and instruction edits require a revision restart.
- Add a small dependency manifest and Dockerfile with Python, `aiohttp`, an asynchronous PostgreSQL driver such as psycopg 3, Azure Identity, LangGraph, and a Foundry-compatible model client. Keep all application logic in `app.py`. Run `python app.py` and listen on `0.0.0.0:8080`.
- Configure the mount path, PostgreSQL host, port, database and Entra database role, Foundry endpoint and deployment, optional user-assigned managed identity client ID, and required tenant/provider settings through environment variables. Fail clearly when required configuration is missing; never fall back silently to another database or model. Do not store passwords, API keys, or human tokens in files.
- Write an operator deployment guide covering configuration, identity and roles, networking, mount protection, initial file population, revision restart, and the demonstration restrictions below.

## 2. Define editable demo materials

- Write a visibly labelled, versioned fictional program policy describing the application process, human decision, and sample fields: applicant name, contact email, positive integer household size, nonnegative monthly household income, and short financial-need description. State that zero income is valid and `submitted` may still mean information is missing.
- Write a visibly labelled, versioned illustrative rubric with identifiable criteria and references. Its findings are advisory and cannot establish real eligibility or predict a decision.
- Give citizen chat, staff chat, and assessment separate instruction files. Require answers to stay within the configured material, acknowledge missing information, and treat case and chat text as untrusted data. Prohibit decisions and promises; citizen chat may only propose edits and staff chat is read-only.
- Load and validate the policy, rubric, and instructions at startup, retain their version labels or content hashes, and report missing or invalid material clearly. Requests needing unavailable material must fail without fabricating a response or changing case status; an operator fixes files and restarts the revision.

## 3. Implement data and authentication foundations

- Create the specification's `citizens`, `staff`, `applications`, `assessments`, `case_events`, and `chat_messages` tables with idempotent startup DDL, UUID keys, UTC timestamps, foreign keys, identity uniqueness, status and field constraints, and parameterized queries. Keep nullable intake fields and increment application revision for saved field changes.
- Explicitly represent the current assessment, for example with a nullable `current_assessment_id` on `applications` in addition to assessment history. Set it atomically on successful assessment; clear it when staff request clarification. Require its assessment revision to match the application revision before a decision. This avoids treating an older assessment as current after clarification even if no field changed.
- Connect to Azure Database for PostgreSQL over verified TLS using the app managed identity. Obtain a fresh PostgreSQL Entra access token for new connections and use it as the password; the database role must have startup DDL and application data permissions. Keep token acquisition and database access shared by UI and agent service functions.
- Configure Container Apps built-in Entra authentication for the permitted tenant and audience, and prevent ingress that bypasses it. Parse the platform-validated principal headers, require `tid` and `oid`, and never accept actor or owner identity from request bodies. Upsert the citizen or staff record for each portal visit and direct API call, refreshing available name/email. Return 401 when identity claims are absent.
- Expose platform sign-in/sign-out links, retain secure HTTP-only session cookies, and protect state-changing cookie-authenticated requests against CSRF using a server-validated token and same-origin checks. Never expose access tokens to page JavaScript. Treat citizen ownership as mandatory on every case read, write, and agent context/tool call; return 404 for inaccessible cases. Staff may access all cases only under the explicit demo assumption.

## 4. Build shared case services and transactions

- Centralize input validation, actor/portal registration, case authorization, serialization, and error mapping in `app.py`. Reject unknown fields on state-changing requests. Serialize UUIDs and UTC timestamps consistently; return the specified JSON error envelope and status codes, including `missing_fields` for incomplete submission.
- Create incomplete `submitted` cases with a creation event. Permit multiple cases per citizen. Save explicitly supplied values only for owned `submitted` cases; validate supplied types and constraints, preserve other values, and increment the revision on field changes. Determine missing fields without treating zero income as missing.
- Implement the full transition table: `submitted` to `pending approval` after successful assessment; `pending approval` to `submitted` on staff clarification; `pending approval` to `approved` or `rejected` on a staff decision; and `submitted` or `pending approval` to `dropped` by the owner. Final and dropped cases are terminal. Do not expose a generic status-update operation.
- For submission, validate completeness independently of the model, capture fields and revision, and run assessment outside the committing transaction. In the commit transaction, lock or conditionally update the case and recheck owner, status, and revision; insert the assessment and initiating citizen's event and set the current assessment and `pending approval` atomically. Return 409 on a concurrent change, including a drop or competing submission.
- In transactions, recheck status for clarification, drop, and decisions. Require a nonempty reason for clarification and for approve/reject; record the acting staff member and timestamp with the event. Clear the current assessment on clarification while retaining assessment history; a later submission must create a fresh assessment. Staff decisions may differ from advisory findings, but require a current matching assessment.
- On missing/invalid materials, model failure, or invalid structured assessment, retain the case and `submitted` status and return a clear retryable 503; if another action already changed the case, report a conflict instead. Keep internal analysis and staff chat out of citizen responses, while exposing applicable clarification requests and final decision reasons.

## 5. Implement LangGraph flows

- Build small LangGraph flows in `app.py` for citizen chat, staff chat, and assessment using the corresponding mounted instructions and a Foundry model authenticated with managed identity and the endpoint's documented token scope. Assign inference permissions appropriate to the actual endpoint.
- For citizen chat, provide policy and, if selected, only the server-authorized citizen case. Return assistant text and optional structured field proposals; validate proposed field names and values but never save them or change status. Permit general policy chat without a case.
- For staff chat, provide policy, rubric, and only the server-selected authorized case and its assessment when present. Provide no case-write or decision tools. Neither flow may choose a different actor or case from message content, and no model output may become SQL or an authorization decision.
- For assessment, pass the validated immutable case snapshot and rubric and request a structured result containing a short summary, criterion-by-criterion findings with rubric references, and uncertainties. Validate the full shape and advisory character before committing, and store the policy/rubric versions or hashes with the JSON analysis. The model cannot approve or reject.
- Persist selected-case messages with portal, actor, and role attribution after case authorization; filter history by portal. General chat may remain ephemeral. Render all messages as text, not HTML.

### Using Foundry model in Langchain agent

1. use `ManagedIdentityCredential` instead of `DefaultAzureCredential`
2. use `init_chat_model` instead of `AsyncAzureOpenAI`
3. use In-memory checkpoint for user chat memory
4. exposing Python functions as Langchain tools

example:

```
from langchain.chat_models import init_chat_model
from langchain.agents import create_agent
from langchain.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

checkpointer = InMemorySaver()

@before_model
def trim_conversation_history(state: AgentState, runtime) -> dict:
    # Simple history trimming strategy to keep the conversation within token limits.
    return {
        "messages": trim_messages(
            state["messages"],
            strategy="last",
            token_counter="approximate",
            max_tokens=128_000,
            start_on="human",
            include_system=True,
        )
    }

with open("instructions.md", "r", encoding="utf-8") as instructions:

@tool
def current_utc_time() -> str:
    """Return the current UTC date and time."""
    return datetime.now(UTC).isoformat()

agent = create_agent(
    model=init_chat_model(
        f"azure_ai:{environ['FOUNDRY_DEPLOYMENT']}",
        project_endpoint=environ['FOUNDRY_ENDPOINT'],
        credential=ManagedIdentityCredential(client_id=environ['UAMI_CLIENT_ID']),
    ),
    tools=tools,
    system_prompt=instructions,
    middleware=[trim_conversation_history],
    checkpointer=checkpointer,
)
```

## 6. Implement HTTP endpoints and pages

- Implement the exact endpoint inventory in spec section 9: `/api/me`, `/api/policy`, citizen list/create/detail/save/submit/drop/chat, and staff list/detail/clarification/decision/chat. Derive the actor server-side for every request. Support `portal` on `/api/me`, status filtering for the staff queue, and optional selected application IDs for chat. Use 400/401/403/404/409/503 as specified and do not expose stack traces or tokens.
- Build the citizen page with the exact Bootstrap 5.3.8 stylesheet link from the spec and `data-bs-theme="dark"`. Include the demo disclaimer, policy, sign-in/sign-out, case list and details, missing fields, editable form, and chat. Show model proposals in the form for explicit citizen save; clearly distinguish saving from submitting for assessment. Show applicable clarification and final decision reasons, and enforce read-only/available actions by status while relying on server enforcement.
- Build the staff page with the same Bootstrap and dark theme, demo access warning, sign-in/sign-out, status-filtered queue, case details, missing fields, clarification and decision history, current and previous assessments with rubric references, and read-only case/policy chat. Provide explicit clarification, approve, and reject actions with required reasons.
- Use fetch/AJAX with JSON, CSRF protection, clear loading/error/conflict messages, and text-safe rendering. Keep the internal assessment and staff messages exclusively in staff responses and views.

## 7. Provision and operate the Azure demo

- Provision a Container Apps environment integrated with a custom VNet; configure an Azure Files NFS share, private endpoint, private DNS and network rules for TCP 2049. Register environment storage and mount it as `NfsAzureFile`. Account for NFS encryption-in-transit not being supported by Container Apps: configure storage accordingly, restrict the network, and protect the share because it contains executable code and instructions. NFS access is the explicit network/storage exception to token-based service access.
- Build and push the runtime image to ACR. Attach a managed identity to the app and grant the image-pull permission appropriate to the registry authorization mode. Populate the mount with the application and editable files, run the mounted code, set a single replica, and expose HTTPS ingress to port 8080.
- Provision Entra-enabled PostgreSQL Flexible Server with app network access and TLS; have an Entra database administrator create the app identity's database principal and grant required DDL/data privileges. Deploy the Foundry model and grant the same app identity the applicable inference role. Configure fresh token acquisition for both services.
- Configure the Container Apps Entra provider, redirect URLs, allowed tenant and audience before exposing the UI. Restrict tenant participants and network access, document the absence of staff-role separation, and restart the revision after changing mounted files. Provision infrastructure and assign roles outside the application.
