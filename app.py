from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import re
import sys
import uuid
from datetime import UTC, datetime, timedelta
from os import environ
from pathlib import Path
from typing import Annotated, Any, Literal, Optional
from urllib.parse import urlparse

import dns.resolver
import psycopg
import requests
from aiohttp import web
from aiohttp.web import Application, Request, Response, middleware, run_app
from azure.core.credentials import AccessToken
from azure.identity import ManagedIdentityCredential
from azure.monitor.ingestion import LogsIngestionClient
from azure.monitor.query import LogsQueryClient, LogsQueryStatus
from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain.chat_models import init_chat_model
from langchain.tools import BaseTool, tool
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from microsoft_agents.activity import load_configuration_from_env
from microsoft_agents.authentication.msal import MsalConnectionManager
from microsoft_agents.hosting.aiohttp import (
    CloudAdapter,
    jwt_authorization_decorator,
    start_agent_process,
)
from microsoft_agents.hosting.core import (
    AgentApplication,
    Authorization,
    MemoryStorage,
    TurnContext,
    TurnState,
)
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

try:
    from microsoft_agents_a365.notifications import EmailResponse
    from microsoft_agents_a365.notifications.agent_notification import (
        AgentNotification,
        AgentNotificationActivity,
        ChannelId,
        NotificationTypes,
    )
    HAS_A365_NOTIFICATIONS = True
except ImportError:
    HAS_A365_NOTIFICATIONS = False

try:
    from microsoft.opentelemetry import use_microsoft_opentelemetry
    from microsoft.opentelemetry.a365.core import BaggageBuilder
    from microsoft.opentelemetry.a365.hosting.scope_helpers.populate_baggage import populate
    HAS_OTEL = True
except ImportError:
    HAS_OTEL = False


# ---------------------------------------------------------------------------
# Logging & Paths
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("scamintel")

APP_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# M365 Agents SDK / Entra Agent User Token Acquisition
# ---------------------------------------------------------------------------
agents_sdk_config = load_configuration_from_env(environ)
storage = MemoryStorage()

_has_service_connection = bool(
    agents_sdk_config.get("CONNECTIONS", {})
    .get("SERVICE_CONNECTION", {})
    .get("SETTINGS", {})
    .get("CLIENTID")
)

if _has_service_connection:
    connection_manager: Optional[MsalConnectionManager] = MsalConnectionManager(**agents_sdk_config)
    adapter: Optional[CloudAdapter] = CloudAdapter(connection_manager=connection_manager)
    authorization: Optional[Authorization] = Authorization(storage, connection_manager)
    agent_app: Optional[AgentApplication[TurnState]] = AgentApplication[TurnState](
        storage=storage,
        adapter=adapter,
        authorization=authorization,
    )
    agent_notification = AgentNotification(agent_app) if HAS_A365_NOTIFICATIONS else None
    token_provider = connection_manager.get_default_connection()
    tenant_id = agents_sdk_config["CONNECTIONS"]["SERVICE_CONNECTION"]["SETTINGS"].get("TENANTID", "")
else:
    connection_manager = None
    adapter = None
    authorization = None
    agent_app = None
    agent_notification = None
    token_provider = None
    tenant_id = environ.get("TENANT_ID", "")

agent_id = environ.get("AGENTIC_INSTANCE_ID", "")
agent_user = environ.get("AGENTIC_USER_ID", "")


async def get_agent_user_token(scopes: list[str]) -> str:
    """Acquire an Entra Agent User token for downstream Azure services."""
    if token_provider is None:
        raise RuntimeError("M365 Agents SDK connection is not configured.")
    return await token_provider.get_agentic_user_token(
        tenant_id=tenant_id,
        agent_app_instance_id=agent_id,
        agentic_user_id=agent_user,
        scopes=scopes,
    )


class AzureAccessTokenProvider:
    """Static-token TokenCredential wrapper for synchronous Azure SDK clients."""

    def __init__(self, token: str):
        self.token = token

    def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        expires_on = int((datetime.now(UTC) + timedelta(hours=1)).timestamp())
        return AccessToken(self.token, expires_on=expires_on)


# ---------------------------------------------------------------------------
# PostgreSQL Interface (psycopg v3, parameterised queries)
# ---------------------------------------------------------------------------
async def get_db_conn() -> psycopg.AsyncConnection[dict[str, Any]]:
    """Open an async PostgreSQL connection using Entra Agent User token or PGPASSWORD."""
    host = environ.get("PGHOST", "")
    dbname = environ.get("PGDATABASE", "scamintel")
    user = environ.get("PGUSER", "")
    port = int(environ.get("PGPORT", "5432"))
    sslmode = environ.get("PGSSLMODE", "require")

    password = environ.get("PGPASSWORD")
    if not password:
        password = await get_agent_user_token(["https://ossrdbms-aad.database.windows.net/.default"])

    return await psycopg.AsyncConnection.connect(
        host=host,
        port=port,
        dbname=dbname,
        user=user,
        password=password,
        sslmode=sslmode,
        row_factory=dict_row,
    )


async def db_upsert_user(
    tenant_id_val: str,
    object_id_val: str,
    display_name: Optional[str],
    email: Optional[str],
) -> dict[str, Any]:
    async with await get_db_conn() as conn:
        cur = await conn.execute(
            """
            INSERT INTO users (id, tenant_id, object_id, display_name, email, created_at, last_login_at)
            VALUES (%s, %s, %s, %s, %s, now(), now())
            ON CONFLICT (tenant_id, object_id) DO UPDATE
            SET display_name = COALESCE(EXCLUDED.display_name, users.display_name),
                email = COALESCE(EXCLUDED.email, users.email),
                last_login_at = now()
            RETURNING *
            """,
            (uuid.uuid4(), tenant_id_val, object_id_val, display_name, email),
        )
        row = await cur.fetchone()
        await conn.commit()
        return row


async def db_create_report(
    reporter_user_id: Optional[uuid.UUID],
    channel: str,
    channel_message_id: str,
    subject: Optional[str],
    body_text: str,
) -> dict[str, Any]:
    report_id = uuid.uuid4()
    async with await get_db_conn() as conn:
        cur = await conn.execute(
            """
            INSERT INTO reports (
                id, reporter_user_id, channel, channel_message_id,
                received_at, subject, body_text, processing_status, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, now(), %s, %s, 'investigating', now(), now())
            RETURNING *
            """,
            (report_id, reporter_user_id, channel, channel_message_id, subject, body_text),
        )
        row = await cur.fetchone()
        await conn.commit()
        return row


async def db_insert_entities(report_id: uuid.UUID, entities: list[dict[str, str]]) -> list[dict[str, Any]]:
    inserted: list[dict[str, Any]] = []
    async with await get_db_conn() as conn:
        for ent in entities:
            cur = await conn.execute(
                """
                INSERT INTO report_entities (id, report_id, type, value, matches, confidence, flag_for_scamintel)
                VALUES (%s, %s, %s, %s, 'none', 'medium', FALSE)
                RETURNING *
                """,
                (uuid.uuid4(), report_id, ent["type"], ent["value"]),
            )
            row = await cur.fetchone()
            if row:
                inserted.append(row)
        await conn.commit()
    return inserted


async def db_update_entity_match(
    entity_id: uuid.UUID,
    matches: str,
    evidence_ref: Optional[str],
    confidence: str = "medium",
    flag_for_scamintel: bool = False,
) -> None:
    async with await get_db_conn() as conn:
        await conn.execute(
            """
            UPDATE report_entities
            SET matches = %s,
                evidence_ref = %s,
                confidence = %s,
                flag_for_scamintel = %s
            WHERE id = %s
            """,
            (matches, evidence_ref, confidence, flag_for_scamintel, entity_id),
        )
        await conn.commit()


async def db_add_assessment(
    report_id: uuid.UUID,
    verdict: str,
    rationale: str,
    actor_type: str,
    actor_user_id: Optional[uuid.UUID] = None,
) -> dict[str, Any]:
    async with await get_db_conn() as conn:
        cur = await conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS next_version FROM assessments WHERE report_id = %s",
            (report_id,),
        )
        next_version = (await cur.fetchone())["next_version"]
        cur = await conn.execute(
            """
            INSERT INTO assessments (id, report_id, version, verdict, rationale, actor_type, actor_user_id, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, now())
            RETURNING *
            """,
            (uuid.uuid4(), report_id, next_version, verdict, rationale, actor_type, actor_user_id),
        )
        row = await cur.fetchone()
        await conn.commit()
        return row


async def db_update_report_status(
    report_id: uuid.UUID,
    processing_status: str,
    assessment: Optional[str] = None,
) -> None:
    async with await get_db_conn() as conn:
        await conn.execute(
            """
            UPDATE reports
            SET processing_status = %s,
                assessment = COALESCE(%s, assessment),
                updated_at = now()
            WHERE id = %s
            """,
            (processing_status, assessment, report_id),
        )
        await conn.commit()


async def db_list_reports(
    reporter_user_id: Optional[uuid.UUID] = None,
    status: Optional[str] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if reporter_user_id is not None:
        clauses.append("r.reporter_user_id = %s")
        params.append(reporter_user_id)
    if status:
        clauses.append("r.processing_status = %s")
        params.append(status)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)

    async with await get_db_conn() as conn:
        cur = await conn.execute(
            f"""
            SELECT r.*,
                   u.display_name AS reporter_name,
                   u.email AS reporter_email,
                   (SELECT COUNT(*) FROM report_entities e WHERE e.report_id = r.id) AS entity_count,
                   (SELECT COUNT(*) FROM publication_outbox o WHERE o.report_id = r.id AND o.status = 'published') AS published_count,
                   (SELECT COUNT(*) FROM publication_outbox o WHERE o.report_id = r.id AND o.status = 'failed') AS failed_pub_count
            FROM reports r
            LEFT JOIN users u ON u.id = r.reporter_user_id
            {where_sql}
            ORDER BY r.received_at DESC
            LIMIT %s
            """,
            tuple(params),
        )
        return await cur.fetchall()


async def db_get_report_detail(report_id: uuid.UUID) -> Optional[dict[str, Any]]:
    async with await get_db_conn() as conn:
        cur = await conn.execute(
            """
            SELECT r.*, u.display_name AS reporter_name, u.email AS reporter_email
            FROM reports r
            LEFT JOIN users u ON u.id = r.reporter_user_id
            WHERE r.id = %s
            """,
            (report_id,),
        )
        report = await cur.fetchone()
        if not report:
            return None

        cur = await conn.execute(
            "SELECT * FROM report_entities WHERE report_id = %s ORDER BY created_at ASC",
            (report_id,),
        )
        entities = await cur.fetchall()

        cur = await conn.execute(
            """
            SELECT a.*, u.display_name AS actor_name, u.email AS actor_email
            FROM assessments a
            LEFT JOIN users u ON u.id = a.actor_user_id
            WHERE a.report_id = %s
            ORDER BY a.version DESC
            """,
            (report_id,),
        )
        assessments = await cur.fetchall()

        cur = await conn.execute(
            """
            SELECT o.*, ir.latest_version
            FROM publication_outbox o
            LEFT JOIN indicator_registry ir ON ir.id = o.indicator_id
            WHERE o.report_id = %s
            ORDER BY o.created_at DESC
            """,
            (report_id,),
        )
        outbox = await cur.fetchall()

        return {
            "report": report,
            "entities": entities,
            "assessments": assessments,
            "outbox": outbox,
        }


async def db_commit_staff_review(
    report_id: uuid.UUID,
    staff_user_id: uuid.UUID,
    verdict: str,
    rationale: str,
    entity_updates: list[dict[str, Any]],
) -> list[uuid.UUID]:
    """Commit staff review, update entities, append assessment, and enqueue ScamIntel_CL outbox."""
    created_outbox_ids: list[uuid.UUID] = []
    async with await get_db_conn() as conn:
        cur = await conn.execute("SELECT received_at FROM reports WHERE id = %s", (report_id,))
        report_row = await cur.fetchone()
        if not report_row:
            raise LookupError("Report not found")
        observed_at = report_row["received_at"]

        for upd in entity_updates:
            ent_id = uuid.UUID(str(upd["id"]))
            await conn.execute(
                """
                UPDATE report_entities
                SET matches = COALESCE(%s, matches),
                    evidence_ref = COALESCE(%s, evidence_ref),
                    confidence = COALESCE(%s, confidence),
                    flag_for_scamintel = COALESCE(%s, flag_for_scamintel)
                WHERE id = %s AND report_id = %s
                """,
                (
                    upd.get("matches"),
                    upd.get("evidence_ref"),
                    upd.get("confidence"),
                    upd.get("flag_for_scamintel"),
                    ent_id,
                    report_id,
                ),
            )

        cur = await conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS next_version FROM assessments WHERE report_id = %s",
            (report_id,),
        )
        next_version = (await cur.fetchone())["next_version"]
        await conn.execute(
            """
            INSERT INTO assessments (id, report_id, version, verdict, rationale, actor_type, actor_user_id, created_at)
            VALUES (%s, %s, %s, %s, %s, 'staff', %s, now())
            """,
            (uuid.uuid4(), report_id, next_version, verdict, rationale, staff_user_id),
        )

        await conn.execute(
            """
            UPDATE reports
            SET processing_status = 'committed',
                assessment = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (verdict, report_id),
        )

        # Enqueue flagged entities into indicator_registry + publication_outbox
        if verdict == "suspected_scam":
            cur = await conn.execute(
                """
                SELECT * FROM report_entities
                WHERE report_id = %s
                  AND (flag_for_scamintel = TRUE OR matches IN ('public_web', 'scamintel'))
                """,
                (report_id,),
            )
            flagged = await cur.fetchall()
            now_utc = datetime.now(UTC)
            for ent in flagged:
                cur_ind = await conn.execute(
                    """
                    INSERT INTO indicator_registry (
                        id, entity_type, entity_value, first_observed_at, last_observed_at, latest_version, created_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, 1, now(), now())
                    ON CONFLICT (entity_type, entity_value) DO UPDATE
                    SET last_observed_at = GREATEST(indicator_registry.last_observed_at, EXCLUDED.last_observed_at),
                        latest_version = indicator_registry.latest_version + 1,
                        updated_at = now()
                    RETURNING *
                    """,
                    (uuid.uuid4(), ent["type"], ent["value"], observed_at, observed_at),
                )
                ind = await cur_ind.fetchone()
                payload = {
                    "TimeGenerated": now_utc.isoformat(),
                    "ReportEntityId": str(ent["id"]),
                    "EntityType": ent["type"],
                    "EntityValue": ent["value"],
                    "EvidenceRef": ent.get("evidence_ref") or "",
                    "Confidence": ent.get("confidence") or "medium",
                    "FirstObservedAt": ind["first_observed_at"].isoformat(),
                    "LastObservedAt": ind["last_observed_at"].isoformat(),
                }
                outbox_id = uuid.uuid4()
                await conn.execute(
                    """
                    INSERT INTO publication_outbox (
                        id, report_id, report_entity_id, indicator_id, status, attempts, payload, created_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, 'pending', 0, %s::jsonb, now(), now())
                    """,
                    (outbox_id, report_id, ent["id"], ind["id"], json.dumps(payload)),
                )
                created_outbox_ids.append(outbox_id)

        await conn.commit()
    return created_outbox_ids


# ---------------------------------------------------------------------------
# Azure Monitor / Sentinel Logs Query & Logs Ingestion
# ---------------------------------------------------------------------------
def _kql_string_list(values: list[str]) -> str:
    cleaned = list(dict.fromkeys(v.strip() for v in values if v and v.strip()))
    return ", ".join(json.dumps(v) for v in cleaned)


async def query_log_analytics(kql: str, timespan_days: int = 30) -> list[dict[str, Any]]:
    workspace_id = environ.get("LOG_ANALYTICS_WORKSPACE_ID", "").strip()
    if not workspace_id:
        raise RuntimeError("LOG_ANALYTICS_WORKSPACE_ID is not configured.")
    token = await get_agent_user_token(["https://api.loganalytics.io/.default"])

    def _run_query() -> list[dict[str, Any]]:
        client = LogsQueryClient(credential=AzureAccessTokenProvider(token))
        resp = client.query_workspace(
            workspace_id=workspace_id,
            query=kql,
            timespan=timedelta(days=timespan_days),
        )
        if resp.status == LogsQueryStatus.SUCCESS:
            tables = resp.tables
        elif resp.status == LogsQueryStatus.PARTIAL:
            tables = resp.partial_data
        else:
            raise RuntimeError(f"Log Analytics query failed: {resp.status}")

        rows: list[dict[str, Any]] = []
        for table in tables or []:
            cols = [c if isinstance(c, str) else c.name for c in table.columns]
            for r in table.rows:
                rows.append(dict(zip(cols, r)))
        return rows

    return await asyncio.to_thread(_run_query)


async def search_microsoft_ti(indicators: list[str]) -> dict[str, dict[str, Any]]:
    if not indicators:
        return {}
    kql = (
        "ThreatIntelIndicators\n"
        f"| where ObservableValue in~ ({_kql_string_list(indicators)})\n"
        "| project TimeGenerated, SourceSystem, ObservableKey, ObservableValue, IsActive, Confidence\n"
        "| summarize arg_max(TimeGenerated, *) by ObservableValue"
    )
    rows = await query_log_analytics(kql, timespan_days=30)
    return {str(r["ObservableValue"]).lower(): r for r in rows if r.get("ObservableValue")}


async def search_scamintel(indicators: list[str]) -> dict[str, dict[str, Any]]:
    if not indicators:
        return {}
    kql = (
        "ScamIntel_CL\n"
        f"| where EntityValue in~ ({_kql_string_list(indicators)})\n"
        "| project TimeGenerated, ReportEntityId, EntityType, EntityValue, EvidenceRef, Confidence, FirstObservedAt, LastObservedAt\n"
        "| summarize arg_max(TimeGenerated, *) by EntityValue"
    )
    rows = await query_log_analytics(kql, timespan_days=90)
    return {str(r["EntityValue"]).lower(): r for r in rows if r.get("EntityValue")}


async def ingest_scamintel_records(records: list[dict[str, Any]]) -> None:
    dce_endpoint = environ.get("DCE_ENDPOINT", "").strip()
    dcr_id = environ.get("DCR_IMMUTABLE_ID", "").strip()
    stream_name = environ.get("DCR_STREAM_NAME", "Custom-ScamIntel_CL").strip()
    if not dce_endpoint or not dcr_id:
        raise RuntimeError("DCE_ENDPOINT or DCR_IMMUTABLE_ID is not configured.")

    token = await get_agent_user_token(["https://monitor.azure.com/.default"])

    def _upload() -> None:
        client = LogsIngestionClient(
            endpoint=dce_endpoint,
            credential=AzureAccessTokenProvider(token),
        )
        client.upload(rule_id=dcr_id, stream_name=stream_name, logs=records)

    await asyncio.to_thread(_upload)


async def publish_outbox_item(outbox_id: uuid.UUID) -> dict[str, Any]:
    """Attempt to publish a single outbox row to Sentinel ScamIntel_CL."""
    async with await get_db_conn() as conn:
        cur = await conn.execute("SELECT * FROM publication_outbox WHERE id = %s", (outbox_id,))
        item = await cur.fetchone()
        if not item:
            raise LookupError("Outbox item not found")

    payload = item["payload"]
    try:
        await ingest_scamintel_records([payload])
        async with await get_db_conn() as conn:
            cur = await conn.execute(
                """
                UPDATE publication_outbox
                SET status = 'published',
                    attempts = attempts + 1,
                    last_error = NULL,
                    published_at = now(),
                    updated_at = now()
                WHERE id = %s
                RETURNING *
                """,
                (outbox_id,),
            )
            updated = await cur.fetchone()
            await conn.commit()
            return updated
    except Exception as exc:
        logger.warning("Failed to publish outbox %s: %s", outbox_id, exc)
        async with await get_db_conn() as conn:
            cur = await conn.execute(
                """
                UPDATE publication_outbox
                SET status = 'failed',
                    attempts = attempts + 1,
                    last_error = %s,
                    updated_at = now()
                WHERE id = %s
                RETURNING *
                """,
                (str(exc), outbox_id),
            )
            updated = await cur.fetchone()
            await conn.commit()
            return updated


# ---------------------------------------------------------------------------
# Enrichment Lookups (DNS & DNSBL)
# ---------------------------------------------------------------------------
async def lookup_dns_records(domain: str) -> dict[str, Any]:
    def _resolve() -> dict[str, Any]:
        resolver = dns.resolver.Resolver()
        resolver.lifetime = 5.0
        result: dict[str, Any] = {"domain": domain, "records": {}}
        for rtype in ("A", "AAAA", "MX", "TXT"):
            try:
                answers = resolver.resolve(domain, rtype)
                result["records"][rtype] = [str(rdata).strip() for rdata in answers]
            except Exception:
                result["records"][rtype] = []
        return result

    return await asyncio.to_thread(_resolve)


DOMAIN_DNSBL_ZONES = ("dbl.spamhaus.org", "multi.surbl.org")
IP_DNSBL_ZONES = ("zen.spamhaus.org", "bl.spamcop.net")


def _query_dnsbl(resolver: dns.resolver.Resolver, qname: str, zone: str) -> dict[str, Any]:
    try:
        answers = resolver.resolve(qname, "A")
        codes = [str(rdata).strip() for rdata in answers]
    except dns.resolver.NXDOMAIN:
        return {"zone": zone, "status": "clean", "codes": []}
    except Exception as exc:
        return {"zone": zone, "status": "error", "error": str(exc), "codes": []}

    # Filter out public/shared resolver block & rate-limit return codes
    if any(c.startswith("127.255.255.") for c in codes) or (zone == "multi.surbl.org" and codes == ["127.0.0.1"]):
        return {"zone": zone, "status": "rate_limited", "codes": codes}

    return {"zone": zone, "status": "listed", "codes": codes}


async def lookup_domain_dnsbl(domain: str) -> dict[str, Any]:
    """Check an email/web domain and its MX (or fallback A) IPv4 addresses against public DNSBLs."""
    cleaned_domain = domain.strip().lower().rstrip(".")

    def _check() -> dict[str, Any]:
        resolver = dns.resolver.Resolver()
        resolver.lifetime = 5.0

        mx_hosts: list[str] = []
        try:
            mx_answers = resolver.resolve(cleaned_domain, "MX")
            mx_hosts = [str(r.exchange).strip().rstrip(".").lower() for r in mx_answers]
        except Exception:
            mx_hosts = []

        checked_ips: list[str] = []
        hosts_to_resolve = mx_hosts if mx_hosts else [cleaned_domain]
        for host in hosts_to_resolve[:5]:
            try:
                a_answers = resolver.resolve(host, "A")
                for r in a_answers:
                    ip = str(r).strip()
                    if ip not in checked_ips:
                        checked_ips.append(ip)
            except Exception:
                continue

        domain_results = [
            _query_dnsbl(resolver, f"{cleaned_domain}.{zone}", zone)
            for zone in DOMAIN_DNSBL_ZONES
        ]

        ip_results: list[dict[str, Any]] = []
        for ip in checked_ips[:5]:
            rev_ip = ".".join(reversed(ip.split(".")))
            for zone in IP_DNSBL_ZONES:
                res = _query_dnsbl(resolver, f"{rev_ip}.{zone}", zone)
                res["ip"] = ip
                ip_results.append(res)

        all_results = domain_results + ip_results
        return {
            "domain": cleaned_domain,
            "mx_hosts": mx_hosts,
            "checked_ips": checked_ips,
            "domain_dnsbl": domain_results,
            "ip_dnsbl": ip_results,
            "listed_count": sum(1 for r in all_results if r["status"] == "listed"),
            "rate_limited_count": sum(1 for r in all_results if r["status"] == "rate_limited"),
        }

    return await asyncio.to_thread(_check)


# ---------------------------------------------------------------------------
# Deterministic Entity Extraction
# ---------------------------------------------------------------------------
URL_RE = re.compile(r"https?://[^\s<>\"'\)\]]+", re.IGNORECASE)
EMAIL_RE = re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b")
IPV4_RE = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
DOMAIN_RE = re.compile(r"\b(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,24}\b")
PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s\-().]{6,18}\d)(?!\w)")


def extract_entities(subject: Optional[str], body_text: str) -> list[dict[str, str]]:
    text = f"{subject or ''}\n{body_text or ''}"
    seen: set[tuple[str, str]] = set()
    entities: list[dict[str, str]] = []

    def _add(etype: str, val: str) -> None:
        cleaned = val.strip().rstrip(".,;:!?")
        if etype in ("domain", "email"):
            cleaned = cleaned.lower()
        if not cleaned:
            return
        key = (etype, cleaned)
        if key not in seen:
            seen.add(key)
            entities.append({"type": etype, "value": cleaned})

    for m in URL_RE.findall(text):
        _add("url", m)
        host = urlparse(m).hostname
        if host and not IPV4_RE.fullmatch(host):
            _add("domain", host)

    for m in EMAIL_RE.findall(text):
        _add("email", m)
        domain_part = m.split("@", 1)[1]
        _add("domain", domain_part)

    for m in IPV4_RE.findall(text):
        _add("ip", m)

    for m in DOMAIN_RE.findall(text):
        if not IPV4_RE.fullmatch(m):
            _add("domain", m)

    for m in PHONE_RE.findall(text):
        digits = re.sub(r"\D", "", m)
        if 7 <= len(digits) <= 15:
            normalized = f"+{digits}" if m.strip().startswith("+") else digits
            _add("phone", normalized)

    return entities


# ---------------------------------------------------------------------------
# LangChain Agent (Structured Output via ToolStrategy)
# ---------------------------------------------------------------------------
class EntityFinding(BaseModel):
    value: str = Field(description="Exact observable value from the report entities")
    matches: Literal["scamintel", "microsoft_ti", "public_web", "none"] = Field(
        description="Match category"
    )
    evidence_ref: Optional[str] = Field(
        default=None,
        description="URL or reference supporting the finding (required when matches is public_web)",
    )
    confidence: Literal["low", "medium", "high"] = Field(default="medium")
    flag_for_scamintel: bool = Field(
        default=False,
        description="True if public_web signals malice or repeated scamintel match",
    )


class TriageResult(BaseModel):
    verdict: Literal["suspected_scam", "likely_benign", "inconclusive"] = Field(
        description="Overall report verdict. Use inconclusive if evidence is insufficient or checks failed."
    )
    rationale: str = Field(description="Concise explanation of the findings and verdict.")
    entity_findings: list[EntityFinding] = Field(
        default_factory=list,
        description="Updated findings for investigated entities.",
    )


@tool
def current_utc_time() -> str:
    """Return the current UTC date and time."""
    return datetime.now(UTC).isoformat()


@tool
async def dns_lookup_tool(
    domain: Annotated[str, Field(description="Domain name to resolve for A, AAAA, MX, TXT records")]
) -> str:
    """Resolve DNS records for a domain name."""
    return json.dumps(await lookup_dns_records(domain))


@tool
async def domain_dnsbl_lookup_tool(
    domain: Annotated[str, Field(description="Email or sender domain to check against domain and MX IP DNSBL blocklists")]
) -> str:
    """Check domain and its MX mail-server IPs against public DNSBLs (Spamhaus DBL/ZEN, SURBL, SpamCop)."""
    return json.dumps(await lookup_domain_dnsbl(domain))


@tool
async def search_scamintel_tool(
    indicators: Annotated[list[str], Field(description="Observables to check in Sentinel ScamIntel_CL")]
) -> str:
    """Query Sentinel ScamIntel_CL for previously recorded malicious indicators."""
    try:
        return json.dumps(await search_scamintel(indicators), default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@tool
async def search_microsoft_ti_tool(
    indicators: Annotated[list[str], Field(description="Observables to check in Sentinel ThreatIntelIndicators")]
) -> str:
    """Query Microsoft Threat Intelligence ThreatIntelIndicators table in Sentinel."""
    try:
        return json.dumps(await search_microsoft_ti(indicators), default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


async def get_mcp_search_tools() -> list[BaseTool]:
    """Load Keenable and Tavily remote MCP search tools when API keys are configured."""
    servers: dict[str, Any] = {}
    keenable_key = environ.get("KEENABLE_API_KEY", "").strip()
    tavily_key = environ.get("TAVILY_API_KEY", "").strip()

    if keenable_key:
        servers["keenable"] = {
            "transport": "streamable_http",
            "url": "https://api.keenable.ai/mcp",
            "headers": {"Authorization": f"Bearer {keenable_key}"},
        }
    if tavily_key:
        servers["tavily"] = {
            "transport": "streamable_http",
            "url": f"https://mcp.tavily.com/mcp/?tavilyApiKey={tavily_key}",
        }
    if not servers:
        return []

    try:
        client = MultiServerMCPClient(servers)
        return await client.get_tools()
    except Exception as exc:
        logger.warning("Failed to load MCP search tools: %s", exc)
        return []


AGENT_SYSTEM_PROMPT = """You are a ScamIntel triage analyst investigating citizen-submitted suspected scam reports.
Deterministic Threat Intelligence (ThreatIntelIndicators) and ScamIntel_CL lookups have already been run.
Your job:
1. For any unmatched entities (matches == 'none'), use the web search tools (Keenable/Tavily), DNS lookup, and DNSBL domain/MX blacklist lookup to check for scam reports, phishing, or malicious reputation.
2. If web search finds suspicious/malicious evidence for an entity, set matches='public_web', provide the source URL in evidence_ref, set confidence, and set flag_for_scamintel=True.
3. For entities already matched in 'scamintel', keep flag_for_scamintel=True so a fresh observation snapshot can be recorded.
4. Provide an overall verdict:
   - 'suspected_scam' if any entity matched TI, ScamIntel, or suspicious public web evidence, or content strongly indicates a scam.
   - 'likely_benign' ONLY if all checks succeeded and there is clear evidence of legitimacy with no malicious indicators.
   - 'inconclusive' if evidence is insufficient or any lookup failed.
"""


async def investigate_report(report_id: uuid.UUID, subject: Optional[str], body_text: str) -> None:
    """Run deterministic extraction + TI/ScamIntel lookups + open-ended agent investigation."""
    check_failures: list[str] = []
    try:
        raw_entities = extract_entities(subject, body_text)
        entities = await db_insert_entities(report_id, raw_entities)

        observables = [e["value"] for e in entities]
        # Also resolve domains to IPs for TI checks
        resolved_ips: dict[str, list[str]] = {}
        for ent in entities:
            if ent["type"] == "domain":
                dns_res = await lookup_dns_records(ent["value"])
                ips = dns_res.get("records", {}).get("A", []) + dns_res.get("records", {}).get("AAAA", [])
                if ips:
                    resolved_ips[ent["value"]] = ips
                    observables.extend(ips)

        observables = list(dict.fromkeys(observables))

        ti_hits: dict[str, dict[str, Any]] = {}
        scam_hits: dict[str, dict[str, Any]] = {}

        if observables:
            try:
                ti_hits = await search_microsoft_ti(observables)
            except Exception as exc:
                logger.warning("Microsoft TI lookup failed for report %s: %s", report_id, exc)
                check_failures.append(f"ThreatIntelIndicators lookup failed: {exc}")

            try:
                scam_hits = await search_scamintel(observables)
            except Exception as exc:
                logger.warning("ScamIntel lookup failed for report %s: %s", report_id, exc)
                check_failures.append(f"ScamIntel_CL lookup failed: {exc}")

        # Apply deterministic hits
        for ent in entities:
            val_lower = ent["value"].lower()
            candidates = [val_lower] + [ip.lower() for ip in resolved_ips.get(ent["value"], [])]
            matched_ti = next((ti_hits[c] for c in candidates if c in ti_hits), None)
            matched_scam = next((scam_hits[c] for c in candidates if c in scam_hits), None)

            if matched_ti:
                ent["matches"] = "microsoft_ti"
                ent["evidence_ref"] = f"ThreatIntelIndicators:{matched_ti.get('SourceSystem', 'MDTI')}"
                ent["confidence"] = "high"
                ent["flag_for_scamintel"] = False
                await db_update_entity_match(
                    ent["id"], "microsoft_ti", ent["evidence_ref"], "high", False
                )
            elif matched_scam:
                ent["matches"] = "scamintel"
                ent["evidence_ref"] = matched_scam.get("EvidenceRef") or f"ScamIntel_CL:{matched_scam.get('ReportEntityId', '')}"
                ent["confidence"] = str(matched_scam.get("Confidence") or "high").lower()
                ent["flag_for_scamintel"] = True
                await db_update_entity_match(
                    ent["id"], "scamintel", ent["evidence_ref"], ent["confidence"], True
                )

        # Open-ended agent step
        verdict = "inconclusive"
        rationale = ""
        entity_findings: list[EntityFinding] = []

        if environ.get("FOUNDRY_PROJECT_ENDPOINT") and environ.get("FOUNDRY_MODEL"):
            try:
                mcp_tools = await get_mcp_search_tools()
                tools: list[BaseTool] = [
                    current_utc_time,
                    dns_lookup_tool,
                    domain_dnsbl_lookup_tool,
                    search_scamintel_tool,
                    search_microsoft_ti_tool,
                    *mcp_tools,
                ]
                model = init_chat_model(
                    f"azure_ai:{environ['FOUNDRY_MODEL']}",
                    project_endpoint=environ["FOUNDRY_PROJECT_ENDPOINT"],
                    credential=ManagedIdentityCredential(client_id=environ["UAMI_CLIENT_ID"]),
                )
                agent = create_agent(
                    model=model,
                    tools=tools,
                    system_prompt=AGENT_SYSTEM_PROMPT,
                    response_format=ToolStrategy(TriageResult),
                )
                summary_entities = [
                    {
                        "type": e["type"],
                        "value": e["value"],
                        "matches": e["matches"],
                        "evidence_ref": e.get("evidence_ref"),
                    }
                    for e in entities
                ]
                user_prompt = json.dumps(
                    {
                        "report_id": str(report_id),
                        "subject": subject or "",
                        "body_text": body_text,
                        "entities": summary_entities,
                        "resolved_domain_ips": resolved_ips,
                        "deterministic_check_failures": check_failures,
                    },
                    indent=2,
                )
                result = await agent.ainvoke(
                    {
                        "messages": [
                            SystemMessage(content=AGENT_SYSTEM_PROMPT),
                            HumanMessage(content=user_prompt),
                        ]
                    }
                )
                structured: Optional[TriageResult] = result.get("structured_response")
                if structured:
                    verdict = structured.verdict
                    rationale = structured.rationale
                    entity_findings = structured.entity_findings
                else:
                    check_failures.append("Agent returned no structured response.")
            except Exception as exc:
                logger.warning("Agent investigation failed for report %s: %s", report_id, exc)
                check_failures.append(f"Agent investigation error: {exc}")
        else:
            check_failures.append("Foundry model not configured; skipped open-ended web investigation.")

        # Apply agent entity findings (do not overwrite existing TI/ScamIntel hits)
        by_value = {e["value"].lower(): e for e in entities}
        for finding in entity_findings:
            ent = by_value.get(finding.value.lower())
            if not ent:
                continue
            if ent["matches"] in ("microsoft_ti", "scamintel"):
                continue
            if finding.matches == "public_web":
                await db_update_entity_match(
                    ent["id"],
                    "public_web",
                    finding.evidence_ref,
                    finding.confidence,
                    finding.flag_for_scamintel or True,
                )
                ent["matches"] = "public_web"

        # Safety rule: failed checks degrade 'likely_benign' -> 'inconclusive'
        any_malicious_hit = any(e["matches"] in ("microsoft_ti", "scamintel", "public_web") for e in entities)
        if check_failures:
            if not any_malicious_hit and verdict == "likely_benign":
                verdict = "inconclusive"
            if not any_malicious_hit and not rationale:
                verdict = "inconclusive"
            failure_note = " | ".join(check_failures)
            rationale = f"{rationale}\n\n[Check warnings: {failure_note}]".strip()
        elif any_malicious_hit and verdict == "inconclusive" and not rationale:
            verdict = "suspected_scam"
            rationale = "Matched known threat intelligence or ScamIntel indicators."

        if not rationale:
            rationale = "Automated triage completed with insufficient evidence."

        await db_add_assessment(report_id, verdict, rationale, actor_type="agent")
        await db_update_report_status(report_id, "triaged", assessment=verdict)
    except Exception as exc:
        logger.exception("Fatal error investigating report %s: %s", report_id, exc)
        try:
            await db_add_assessment(
                report_id,
                "inconclusive",
                f"Automated triage failed: {exc}",
                actor_type="agent",
            )
            await db_update_report_status(report_id, "triaged", assessment="inconclusive")
        except Exception:
            logger.exception("Failed to record fallback assessment for report %s", report_id)


def schedule_investigation(app: Application, report_id: uuid.UUID, subject: Optional[str], body_text: str) -> None:
    task = asyncio.create_task(investigate_report(report_id, subject, body_text))
    app["bg_tasks"].add(task)
    task.add_done_callback(app["bg_tasks"].discard)


# ---------------------------------------------------------------------------
# Easy Auth Identity Parsing & Middleware
# ---------------------------------------------------------------------------
def parse_principal(request: Request) -> Optional[dict[str, Any]]:
    raw = request.headers.get("X-MS-CLIENT-PRINCIPAL")
    if raw:
        try:
            decoded = json.loads(base64.b64decode(raw).decode("utf-8"))
            claims = {
                c.get("typ", ""): c.get("val", "")
                for c in decoded.get("claims", [])
                if isinstance(c, dict)
            }
            tenant = (
                claims.get("tid")
                or claims.get("http://schemas.microsoft.com/identity/claims/tenantid")
                or environ.get("TENANT_ID", "default")
            )
            oid = (
                claims.get("oid")
                or claims.get("http://schemas.microsoft.com/identity/claims/objectidentifier")
                or claims.get("sub")
                or request.headers.get("X-MS-CLIENT-PRINCIPAL-ID")
            )
            name = (
                claims.get("name")
                or request.headers.get("X-MS-CLIENT-PRINCIPAL-NAME")
                or "Entra User"
            )
            email = (
                claims.get("preferred_username")
                or claims.get("email")
                or claims.get("upn")
                or request.headers.get("X-MS-CLIENT-PRINCIPAL-NAME")
            )
            if oid:
                return {
                    "tenant_id": tenant,
                    "object_id": oid,
                    "display_name": name,
                    "email": email,
                }
        except Exception as exc:
            logger.warning("Failed to parse X-MS-CLIENT-PRINCIPAL: %s", exc)

    if environ.get("ALLOW_DEV_AUTH", "").lower() == "true":
        return {
            "tenant_id": environ.get("TENANT_ID", "dev-tenant"),
            "object_id": "dev-user-oid",
            "display_name": "Dev User",
            "email": "dev@example.local",
        }
    return None


EXEMPT_AUTH_PATHS = {
    "/",
    "/index.html",
    "/citizen",
    "/citizen.html",
    "/staff",
    "/staff.html",
    "/health",
    "/api/health",
    "/api/messages",
    "/api/telegram/webhook",
}


@middleware
async def auth_and_csrf_middleware(request: Request, handler: Any) -> Response:
    if request.path in ("/health", "/api/health") and request.method in ("GET", "HEAD"):
        return Response(status=200, text="ok")

    identity = parse_principal(request)
    request["identity"] = identity

    # Protect /api/* except webhooks & health
    if request.path.startswith("/api/") and request.path not in EXEMPT_AUTH_PATHS:
        if identity is None:
            return web.json_response({"error": "Authentication required"}, status=401)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            csrf_header = request.headers.get("X-Requested-With", "") or request.headers.get("X-CSRF-Token", "")
            if not csrf_header:
                return web.json_response({"error": "Missing CSRF custom header"}, status=403)
            origin = request.headers.get("Origin")
            if origin and request.host:
                origin_host = urlparse(origin).netloc
                if origin_host != request.host:
                    return web.json_response({"error": "Origin mismatch"}, status=403)

    return await handler(request)


async def resolve_current_user(request: Request) -> dict[str, Any]:
    ident = request["identity"]
    return await db_upsert_user(
        ident["tenant_id"],
        ident["object_id"],
        ident.get("display_name"),
        ident.get("email"),
    )


# ---------------------------------------------------------------------------
# HTTP Handlers: UI Pages & REST API
# ---------------------------------------------------------------------------
def _serve_html(filename: str) -> Response:
    path = APP_DIR / filename
    if not path.is_file():
        return Response(status=404, text=f"{filename} not found")
    return Response(text=path.read_text(encoding="utf-8"), content_type="text/html")


async def handle_index(_: Request) -> Response:
    return _serve_html("index.html")


async def handle_citizen_page(_: Request) -> Response:
    return _serve_html("citizen.html")


async def handle_staff_page(_: Request) -> Response:
    return _serve_html("staff.html")


async def handle_me(request: Request) -> Response:
    user = await resolve_current_user(request)
    return web.json_response(user, dumps=lambda x: json.dumps(x, default=str))


async def handle_list_my_reports(request: Request) -> Response:
    user = await resolve_current_user(request)
    rows = await db_list_reports(reporter_user_id=user["id"])
    return web.json_response({"reports": rows}, dumps=lambda x: json.dumps(x, default=str))


async def handle_submit_report(request: Request) -> Response:
    user = await resolve_current_user(request)
    data = await request.json()
    subject = (data.get("subject") or "").strip() or None
    body_text = (data.get("body_text") or "").strip()
    if not body_text:
        return web.json_response({"error": "body_text is required"}, status=400)

    report = await db_create_report(
        reporter_user_id=user["id"],
        channel="web",
        channel_message_id=str(uuid.uuid4()),
        subject=subject,
        body_text=body_text,
    )
    schedule_investigation(request.app, report["id"], subject, body_text)
    return web.json_response({"report": report}, status=201, dumps=lambda x: json.dumps(x, default=str))


async def handle_staff_list_reports(request: Request) -> Response:
    await resolve_current_user(request)
    status = request.query.get("status") or None
    rows = await db_list_reports(reporter_user_id=None, status=status)
    return web.json_response({"reports": rows}, dumps=lambda x: json.dumps(x, default=str))


async def handle_report_detail(request: Request) -> Response:
    await resolve_current_user(request)
    try:
        report_id = uuid.UUID(request.match_info["id"])
    except ValueError:
        return web.json_response({"error": "Invalid report ID"}, status=400)

    detail = await db_get_report_detail(report_id)
    if not detail:
        return web.json_response({"error": "Report not found"}, status=404)
    return web.json_response(detail, dumps=lambda x: json.dumps(x, default=str))


async def handle_staff_commit(request: Request) -> Response:
    user = await resolve_current_user(request)
    try:
        report_id = uuid.UUID(request.match_info["id"])
    except ValueError:
        return web.json_response({"error": "Invalid report ID"}, status=400)

    data = await request.json()
    verdict = data.get("verdict")
    rationale = (data.get("rationale") or "").strip()
    entity_updates = data.get("entities") or []
    if verdict not in ("suspected_scam", "likely_benign", "inconclusive"):
        return web.json_response({"error": "Invalid verdict"}, status=400)
    if not rationale:
        return web.json_response({"error": "rationale is required"}, status=400)

    try:
        outbox_ids = await db_commit_staff_review(
            report_id=report_id,
            staff_user_id=user["id"],
            verdict=verdict,
            rationale=rationale,
            entity_updates=entity_updates,
        )
    except LookupError:
        return web.json_response({"error": "Report not found"}, status=404)

    published_results = []
    for oid in outbox_ids:
        published_results.append(await publish_outbox_item(oid))

    detail = await db_get_report_detail(report_id)
    return web.json_response(
        {"detail": detail, "published": published_results},
        dumps=lambda x: json.dumps(x, default=str),
    )


async def handle_retry_outbox(request: Request) -> Response:
    await resolve_current_user(request)
    try:
        outbox_id = uuid.UUID(request.match_info["id"])
    except ValueError:
        return web.json_response({"error": "Invalid outbox ID"}, status=400)
    try:
        updated = await publish_outbox_item(outbox_id)
    except LookupError:
        return web.json_response({"error": "Outbox item not found"}, status=404)
    return web.json_response({"outbox": updated}, dumps=lambda x: json.dumps(x, default=str))


# ---------------------------------------------------------------------------
# Telegram Webhook Intake
# ---------------------------------------------------------------------------
async def handle_telegram_webhook(request: Request) -> Response:
    expected_secret = environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
    if expected_secret:
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(expected_secret, supplied):
            return Response(status=403, text="Invalid Telegram secret")

    update = await request.json()
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return web.json_response({"ok": True, "ignored": True})

    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not text:
        return web.json_response({"ok": True, "ignored": True})

    chat_id = msg.get("chat", {}).get("id")
    message_id = msg.get("message_id", uuid.uuid4())
    sender = msg.get("from", {})
    sender_label = sender.get("username") or sender.get("first_name") or str(chat_id)

    report = await db_create_report(
        reporter_user_id=None,
        channel="telegram",
        channel_message_id=f"{chat_id}:{message_id}",
        subject=f"Telegram report from {sender_label}",
        body_text=text,
    )
    schedule_investigation(request.app, report["id"], report["subject"], text)

    bot_token = environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if bot_token and chat_id:
        def _reply() -> None:
            requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": f"ScamIntel received your report (ID: {report['id']}). Automated triage is underway.",
                },
                timeout=5,
            )
        asyncio.create_task(asyncio.to_thread(_reply))

    return web.json_response({"ok": True, "report_id": str(report["id"])})


# ---------------------------------------------------------------------------
# M365 Agents SDK / Email Notification Intake
# ---------------------------------------------------------------------------
if agent_app is not None:
    @agent_app.activity("message")
    async def on_message(context: TurnContext, _: TurnState) -> None:
        text = (context.activity.text or "").strip()
        if not text:
            return
        report = await db_create_report(
            reporter_user_id=None,
            channel="email",
            channel_message_id=str(getattr(context.activity, "id", uuid.uuid4())),
            subject="Forwarded message",
            body_text=text,
        )
        await investigate_report(report["id"], report["subject"], text)
        detail = await db_get_report_detail(report["id"])
        verdict = detail["report"]["assessment"] if detail else "inconclusive"
        await context.send_activity(f"ScamIntel report {report['id']} triaged. Initial verdict: {verdict}")

    if agent_notification is not None:
        @agent_notification.on_agent_notification(channel_id=ChannelId(channel="agents", sub_channel="*"))
        async def on_notification(
            context: TurnContext,
            _: TurnState,
            notification_activity: AgentNotificationActivity,
        ) -> None:
            if notification_activity.notification_type != NotificationTypes.EMAIL_NOTIFICATION:
                return
            email_obj = notification_activity.email
            subject = getattr(email_obj, "subject", None) or "Forwarded suspicious email"
            body = getattr(email_obj, "body", None) or getattr(email_obj, "html_body", "") or ""
            msg_id = str(getattr(email_obj, "id", None) or getattr(context.activity, "id", uuid.uuid4()))

            report = await db_create_report(
                reporter_user_id=None,
                channel="email",
                channel_message_id=msg_id,
                subject=subject,
                body_text=body,
            )
            await investigate_report(report["id"], subject, body)
            detail = await db_get_report_detail(report["id"])
            verdict = detail["report"]["assessment"] if detail else "inconclusive"
            await context.send_activity(
                EmailResponse.create_email_response_activity(
                    f"<p>ScamIntel received your report (<code>{report['id']}</code>).</p>"
                    f"<p>Initial automated verdict: <strong>{verdict}</strong>. Staff review is pending.</p>"
                )
            )


@jwt_authorization_decorator
async def messages_entry_point(request: Request) -> Response:
    if agent_app is None or adapter is None:
        return Response(status=503, text="M365 Agents SDK is not configured")
    return await start_agent_process(request, agent_app, adapter)


# ---------------------------------------------------------------------------
# Application Factory & Startup
# ---------------------------------------------------------------------------
def create_app() -> Application:
    app = Application(middlewares=[auth_and_csrf_middleware])
    app["bg_tasks"] = set()

    # Static / HTML pages (read from mounted /app per request)
    app.router.add_get("/", handle_index)
    app.router.add_get("/index.html", handle_index)
    app.router.add_get("/citizen", handle_citizen_page)
    app.router.add_get("/citizen.html", handle_citizen_page)
    app.router.add_get("/staff", handle_staff_page)
    app.router.add_get("/staff.html", handle_staff_page)

    # Citizen & Shared APIs
    app.router.add_get("/api/me", handle_me)
    app.router.add_get("/api/reports", handle_list_my_reports)
    app.router.add_post("/api/reports", handle_submit_report)
    app.router.add_get("/api/reports/{id}", handle_report_detail)

    # Staff APIs
    app.router.add_get("/api/staff/reports", handle_staff_list_reports)
    app.router.add_get("/api/staff/reports/{id}", handle_report_detail)
    app.router.add_post("/api/staff/reports/{id}/commit", handle_staff_commit)
    app.router.add_post("/api/staff/outbox/{id}/retry", handle_retry_outbox)

    # Channel Webhooks
    app.router.add_post("/api/telegram/webhook", handle_telegram_webhook)
    app.router.add_post("/api/messages", messages_entry_point)
    app.router.add_get("/api/messages", lambda _: Response(status=200))

    if connection_manager is not None:
        app["agent_configuration"] = connection_manager.get_default_connection_configuration()

    return app


def main() -> None:
    if HAS_OTEL and _has_service_connection:
        try:
            use_microsoft_opentelemetry(
                enable_a365=True,
                a365_token_resolver=lambda _aid, _tid: asyncio.run(
                    get_agent_user_token(["api://9b975845-388f-4429-889e-eab1ef63949c/.default"])
                ),
                a365_enable_observability_exporter=True,
                instrumentation_options={
                    "openai_agents": {"enabled": False},
                    "agent_framework": {"enabled": False},
                },
            )
        except Exception as exc:
            logger.warning("OpenTelemetry initialization skipped: %s", exc)

    app = create_app()
    host = environ.get("HOST", "0.0.0.0")
    port = int(environ.get("PORT", "8080"))
    run_app(app, host=host, port=port)


if __name__ == "__main__":
    main()
