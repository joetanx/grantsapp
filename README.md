# ScamIntel PoC

ScamIntel is a proof-of-concept agentic application on Azure Container Apps for collecting and investigating citizen-submitted suspected scam reports across **Web**, **Email (Agent 365)**, and **Telegram** channels.

## Mounted `/app` Layout

All application code and static HTML pages are mounted from an Azure Files NFS share into `/app`:

```text
/app/
├── app.py          # aiohttp server, PostgreSQL/Sentinel helpers, LangChain agent, webhooks
├── index.html      # Public splash page with Entra Easy Auth login & logout banner
├── citizen.html    # Citizen AJAX portal (submit report, track status)
└── staff.html      # Staff AJAX triage console (review, edit, commit, Sentinel outbox)
```

`pyproject.toml` and `containerapp.yaml` live in the deployment repo (with `Dockerfile` baking the Python virtual environment from `pyproject.toml` while `/app` is volume-mounted at runtime).

## Investigation Flow

1. **Intake**: Citizens submit reports via Web (`POST /api/reports`), Email (`POST /api/messages` via M365 Agents SDK / A365 Notifications), or Telegram (`POST /api/telegram/webhook`).
2. **Deterministic Extraction & Lookups**: `investigate_report` extracts URLs, emails, domains, IPv4 addresses, and phone numbers (`extract_entities`), resolves domain IPs via `dnspython`, and queries Microsoft Sentinel `ThreatIntelIndicators` and `ScamIntel_CL` via `azure-monitor-query`.
3. **Agent Investigation**: Unmatched entities are investigated by a LangChain agent (`create_agent` with `ToolStrategy(TriageResult)`) using Azure AI Foundry, Keenable/Tavily remote MCP search tools, DNS lookup, and MxToolbox reputation lookup. If any check or the agent fails, the verdict degrades safely to `inconclusive` (never `likely_benign`).
4. **Staff Review & Commit**: Staff review triaged reports in `/staff`, optionally edit entity matches/flags or verdict/rationale, and commit (`POST /api/staff/reports/{id}/commit`). Flagged indicators (`public_web` malice or repeated `scamintel` matches) are recorded in `indicator_registry` and `publication_outbox`, then uploaded to `ScamIntel_CL` via `azure-monitor-ingestion`.

## API Endpoints

| Method & Path | Auth / Protection | Purpose |
|---|---|---|
| `GET /` | Public | Login splash page (`index.html`), shows logout banner on `?logged_out=1` |
| `GET /citizen`, `GET /staff` | Easy Auth | Citizen and Staff AJAX UIs |
| `GET /health`, `GET /api/health` | Public | Container Apps health probe |
| `GET /api/me` | Easy Auth | Upserts and returns current `users` record |
| `GET /api/reports` | Easy Auth | List reports submitted by current user |
| `POST /api/reports` | Easy Auth + CSRF | Submit a new web report (`subject`, `body_text`) |
| `GET /api/reports/{id}` | Easy Auth | Get report detail, entities, assessments, and outbox status |
| `GET /api/staff/reports` | Easy Auth | List all reports (optional `?status=`) |
| `GET /api/staff/reports/{id}` | Easy Auth | Get full report detail for staff review |
| `POST /api/staff/reports/{id}/commit` | Easy Auth + CSRF | Append staff assessment, update report, publish flagged indicators |
| `POST /api/staff/outbox/{id}/retry` | Easy Auth + CSRF | Retry failed `ScamIntel_CL` ingestion for an outbox row |
| `POST /api/telegram/webhook` | `X-Telegram-Bot-Api-Secret-Token` | Telegram Bot message intake |
| `POST /api/messages` | M365 Agents JWT | Agent 365 email notification & Bot Framework intake |

Non-GET `/api/*` endpoints (except webhooks) require a custom `X-Requested-With` (or `X-CSRF-Token`) header and same-host `Origin` check.

## Data Model & Deviations from `intent.md`

`app.py` auto-creates the PostgreSQL schema on startup (`ensure_schema`):
- `users`, `reports`, `report_entities`, `assessments` (as specified in `intent.md`)
- **Added columns on `report_entities`**: `confidence` (`low` | `medium` | `high`) and `flag_for_scamintel` (`boolean`) so agent and staff decisions about `ScamIntel_CL` publication are persisted cleanly.
- **Added column on `assessments`**: `actor_user_id` (`UUID`, nullable) to link staff reviews to `users.id`.
- **Bookkeeping tables**: `indicator_registry` (tracks `first_observed_at`, `last_observed_at`, `latest_version`) and `publication_outbox` (tracks `pending` / `published` / `failed`, `attempts`, `last_error`, `payload`) so the Staff UI accurately reflects ingestion status rather than assuming immediate Log Analytics searchability.

## Environment Variables

| Variable | Purpose |
|---|---|
| `HOST`, `PORT`, `LOG_LEVEL` | `aiohttp` listener (`0.0.0.0:8080`) and log verbosity |
| `FOUNDRY_PROJECT_ENDPOINT`, `FOUNDRY_MODEL`, `UAMI_CLIENT_ID` | Azure AI Foundry model & User-Assigned Managed Identity |
| `CONNECTIONS__SERVICE_CONNECTION__SETTINGS__*`, `AGENTIC_INSTANCE_ID`, `AGENTIC_USER_ID` | M365 Agents SDK / Entra Agent User federated credentials |
| `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGSSLMODE` | Azure PostgreSQL connection (uses Entra token; `PGPASSWORD` optional for local dev) |
| `LOG_ANALYTICS_WORKSPACE_ID`, `DCE_ENDPOINT`, `DCR_IMMUTABLE_ID`, `DCR_STREAM_NAME` | Sentinel Log Analytics query & `ScamIntel_CL` Logs Ingestion |
| `KEENABLE_API_KEY`, `TAVILY_API_KEY`, `MXTOOLBOX_API_KEY`, `DNS_NAMESERVER` | Search MCP servers, MxToolbox API, and optional custom DNS resolver |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET` | Telegram bot reply & webhook secret validation |
| `ALLOW_DEV_AUTH` | Set `"true"` only for local non-Easy-Auth testing |

## Deployment Steps

1. **Build & Push Image**:
   ```bash
   az acr build --registry $ACR_NAME --image scamintel:latest .
   ```
2. **Mount Application Files**: Upload `app.py`, `index.html`, `citizen.html`, and `staff.html` to the Azure Files NFS share mounted at `/app`.
3. **Deploy Container App**:
   ```bash
   envsubst < containerapp.yaml > /tmp/scamintel-aca.yaml
   az containerapp create --yaml /tmp/scamintel-aca.yaml
   ```
4. **Configure Easy Auth Excluded Paths**:
   Ensure `/`, `/index.html`, `/health`, `/api/health`, `/api/messages`, and `/api/telegram/webhook` are excluded in Container Apps Easy Auth (`authConfigs` `excludedPaths`) so splash page, health probes, and external webhooks are reachable without interactive login, while `/citizen`, `/staff`, and `/api/*` trigger Entra ID login.
5. **RBAC & Database Grants**:
   - **UAMI**: `AcrPull` on ACR, `Cognitive Services User` on Azure AI Foundry.
   - **Entra Agent User**: `Log Analytics Reader` and `Monitoring Metrics Publisher` on the Sentinel workspace/DCR, plus PostgreSQL Entra role grants on the `scamintel` database.
6. **Register Telegram Webhook**:
   ```bash
   curl -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
     -d "url=https://<app-fqdn>/api/telegram/webhook" \
     -d "secret_token=${TELEGRAM_WEBHOOK_SECRET}"
   ```
