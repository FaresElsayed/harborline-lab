"""Salesforce REST API mock (prefix ``/sfdc``; instance_url = ``http://<host>/sfdc``).

Serves records that the CRM simulator writes into ``sim_internal.sfdc.record`` with the real REST
shapes: OAuth 2.0 client credentials, query / queryAll with 2,000-row pages and query locators,
describe, getUpdated / getDeleted windows (30 / 15 days of the simulator clock), record GET/PATCH
(Account Health_Score__c and Churn_Risk__c only), limits, the daily API request limit with the
``Sforce-Limit-Info`` header, and Bulk API 2.0 query jobs with CSV results.
Errors use Salesforce's body ``[{"message": ..., "errorCode": ...}]``.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import re
import secrets
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import psycopg
from fastapi import APIRouter, Form, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from harborline.api import soql
from harborline.api.soql import Compiled, Schema, SfField, SObject, SoqlError, sf_datetime
from harborline.ids import sf_id, to_id18
from harborline.tracker.runtime import connection, env, settings, sim_now

DB = "sim_internal"
SUPPORTED_VERSIONS = [
    ("62.0", "Winter '25"), ("63.0", "Spring '25"), ("64.0", "Summer '25"),
    ("65.0", "Winter '26"), ("66.0", "Spring '26"), ("67.0", "Summer '26"),
]
SESSION_TIMEOUT = timedelta(hours=2)
PAGE_SIZE = 2000
MIN_BATCH_SIZE = 200
LOCATOR_IDLE = timedelta(minutes=15)
UPDATED_WINDOW = timedelta(days=30)
RECYCLE_BIN = timedelta(days=15)
MAX_UPDATED_IDS = 600_000
BULK_DEFAULT_MAX_RECORDS = 50_000
BULK_JOB_PREFIX = "750"
QUERY_LOCATOR_PREFIX = "01g"
INTEGRATION_USER_NAME = "Integration User"
DELIMITERS = {"COMMA": ",", "TAB": "\t", "PIPE": "|", "SEMICOLON": ";", "CARET": "^", "BACKQUOTE": "`"}
LINE_ENDINGS = {"LF": "\n", "CRLF": "\r\n"}


def _wall_now() -> datetime:
    """Real time (sessions, locators, daily usage). Tests patch this."""
    return datetime.now(UTC)


def client_id() -> str:
    return settings().sf_client_id


def client_secret() -> str:
    return settings().sf_client_secret


def daily_limit() -> int:
    return int(env("SF_DAILY_API_LIMIT", "15000"))


# --------------------------------------------------------------------------------------------------
# Errors and route class
# --------------------------------------------------------------------------------------------------


class SfError(Exception):
    def __init__(self, status: int, code: str, message: str, fields: list[str] | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.fields = status, code, message, fields

    def response(self) -> JSONResponse:
        body: dict[str, Any] = {"message": self.message, "errorCode": self.code}
        if self.fields is not None:
            body["fields"] = self.fields
        return JSONResponse([body], status_code=self.status)


def not_found() -> SfError:
    return SfError(404, "NOT_FOUND", "The requested resource does not exist")


class SfRoute(APIRoute):
    """Renders SfError / SoqlError in Salesforce's error shape and stamps ``Sforce-Limit-Info``."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def wrapped(request: Request) -> Response:
            try:
                response = await handler(request)
            except SfError as exc:
                response = exc.response()
            except SoqlError as exc:
                response = SfError(400, exc.code, exc.message).response()
            except RequestValidationError as exc:
                first = exc.errors()[0] if exc.errors() else {}
                response = SfError(400, "INVALID_PARAMETER_VALUE", str(first.get("msg", "invalid request"))).response()
            usage = getattr(request.state, "sf_usage", None)
            if usage is not None:
                response.headers["Sforce-Limit-Info"] = f"api-usage={usage}/{daily_limit()}"
            return response

        return wrapped


router = APIRouter(prefix="/sfdc", route_class=SfRoute, tags=["salesforce"])


def _route(path: str, methods: list[str]) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a path with and without a trailing slash (Salesforce accepts both)."""

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        router.add_api_route(path, fn, methods=methods)
        router.add_api_route(path + "/", fn, methods=methods, include_in_schema=False)
        return fn

    return deco


# --------------------------------------------------------------------------------------------------
# OAuth tokens (stateless, HMAC-signed, survive restarts)
# --------------------------------------------------------------------------------------------------


def _org_id15(schema: Schema) -> str:
    return schema.org_id[:15]


def _token_key() -> bytes:
    return hashlib.sha256(f"harborline-sf-session|{client_secret()}".encode()).digest()


def issue_token(now: datetime) -> str:
    body = int(now.timestamp() * 1000).to_bytes(8, "big") + secrets.token_bytes(40)
    mac = hmac.new(_token_key(), body, hashlib.sha256).digest()[:24]
    payload = base64.urlsafe_b64encode(body + mac).decode().rstrip("=").replace("-", ".")
    return f"{_org_id15(soql.base_schema())}!AR{payload}"


def token_issued_at(token: str) -> datetime | None:
    prefix = f"{_org_id15(soql.base_schema())}!AR"
    if not token.startswith(prefix):
        return None
    payload = token[len(prefix) :].replace(".", "-")
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    except (ValueError, TypeError):
        return None
    body, mac = raw[:-24], raw[-24:]
    if len(body) != 48 or not hmac.compare_digest(mac, hmac.new(_token_key(), body, hashlib.sha256).digest()[:24]):
        return None
    return datetime.fromtimestamp(int.from_bytes(body[:8], "big") / 1000, UTC)


def _bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    return token.strip() if scheme.lower() in ("bearer", "oauth") and token.strip() else None


def _integration_user_id(conn: psycopg.Connection) -> str | None:
    row = conn.execute(
        "SELECT id FROM sfdc.record WHERE object = 'User' AND NOT is_deleted "
        "AND (data->>'Name' = %s OR data->>'LastName' = 'Integration User' OR data->>'Alias' = 'integ') "
        "ORDER BY created_date LIMIT 1",
        [INTEGRATION_USER_NAME],
    ).fetchone()
    return row[0] if row else None


def _instance_url(request: Request) -> str:
    return f"{str(request.base_url).rstrip('/')}{router.prefix}"


@router.post("/services/oauth2/token")
def oauth_token(
    request: Request,
    grant_type: str | None = Form(None),
    client_id_: str | None = Form(None, alias="client_id"),
    client_secret_: str | None = Form(None, alias="client_secret"),
) -> JSONResponse:
    if (basic := request.headers.get("authorization", "")).lower().startswith("basic "):
        try:
            cid, _, csec = base64.b64decode(basic[6:]).decode().partition(":")
            client_id_, client_secret_ = client_id_ or cid, client_secret_ or csec
        except (ValueError, UnicodeDecodeError):
            pass
    if grant_type != "client_credentials":
        return JSONResponse({"error": "unsupported_grant_type", "error_description": "grant type not supported"}, 400)
    if client_id_ != client_id():
        return JSONResponse({"error": "invalid_client_id", "error_description": "client identifier invalid"}, 400)
    if not client_secret_ or not hmac.compare_digest(client_secret_, client_secret()):
        return JSONResponse({"error": "invalid_client", "error_description": "invalid client credentials"}, 400)
    now = _wall_now()
    with connection(DB) as conn:
        user_id = _integration_user_id(conn) or sf_id("005", 1)
    instance = _instance_url(request)
    org = soql.base_schema().org_id
    identity = f"{instance}/id/{org}/{user_id}"
    issued_at = str(int(now.timestamp() * 1000))
    signature = base64.b64encode(
        hmac.new(client_secret().encode(), (identity + issued_at).encode(), hashlib.sha256).digest()
    ).decode()
    return JSONResponse({
        "access_token": issue_token(now),
        "signature": signature,
        "scope": "api",
        "instance_url": instance,
        "id": identity,
        "token_type": "Bearer",
        "issued_at": issued_at,
    })


# --------------------------------------------------------------------------------------------------
# Per-call context: version check, session check, daily API usage
# --------------------------------------------------------------------------------------------------


@dataclass
class Call:
    conn: psycopg.Connection
    version: str  # "v67.0"
    now: datetime  # simulator clock
    schema: Schema

    def url(self, path: str) -> str:
        return f"/services/data/{self.version}{path}"


def _check_version(version: str) -> None:
    m = re.fullmatch(r"v(\d+\.0)", version)
    if not m or m.group(1) not in {v for v, _ in SUPPORTED_VERSIONS}:
        raise not_found()


def _count_call(conn: psycopg.Connection) -> int:
    row = conn.execute(
        "INSERT INTO sfdc.api_usage (day, calls) VALUES (%s, 1) "
        "ON CONFLICT (day) DO UPDATE SET calls = sfdc.api_usage.calls + 1 RETURNING calls",
        [_wall_now().date()],
    ).fetchone()
    return int(row[0])


def _schema(conn: psycopg.Connection) -> Schema:
    rows = conn.execute("SELECT object, api_name, definition FROM sfdc.custom_field ORDER BY added_at").fetchall()
    return soql.base_schema().with_custom_fields([(r[0], r[1], r[2]) for r in rows])


def begin(request: Request, conn: psycopg.Connection, version: str) -> Call:
    _check_version(version)
    token = _bearer(request)
    issued = token_issued_at(token) if token else None
    if issued is None or _wall_now() - issued > SESSION_TIMEOUT:
        raise SfError(401, "INVALID_SESSION_ID", "Session expired or invalid")
    usage = _count_call(conn)
    request.state.sf_usage = usage
    if usage > daily_limit():
        raise SfError(403, "REQUEST_LIMIT_EXCEEDED", "TotalRequests Limit exceeded.")
    return Call(conn=conn, version=version, now=sim_now(conn), schema=_schema(conn))


def _object(call: Call, name: str) -> SObject:
    obj = call.schema.get(name)
    if obj is None:
        raise not_found()
    return obj


# --------------------------------------------------------------------------------------------------
# Versions and resources
# --------------------------------------------------------------------------------------------------


@_route("/services/data", ["GET"])
def versions() -> list[dict[str, str]]:
    return [{"label": label, "url": f"/services/data/v{v}", "version": v} for v, label in SUPPORTED_VERSIONS]


@_route("/services/data/{version}", ["GET"])
def resources(request: Request, version: str) -> dict[str, str]:
    with connection(DB) as conn:
        call = begin(request, conn, version)
    names = ["tooling", "metadata", "eclair", "folders", "prechatForms", "contact-tracing", "tabs", "appMenu",
             "quickActions", "queryAll", "commerce", "wave", "iot", "analytics", "search", "identity", "composite",
             "parameterizedSearch", "theme", "nouns", "domino", "event", "serviceTemplates", "recent", "connect",
             "licensing", "limits", "process", "async-queries", "query", "match", "emailConnect", "compactLayouts",
             "knowledgeManagement", "sobjects", "actions", "support", "jobs"]
    out = {n: call.url(f"/{n}") for n in names}
    out["identity"] = f"{_instance_url(request)}/id/{call.schema.org_id}"
    return out


# --------------------------------------------------------------------------------------------------
# Query / queryAll with locators
# --------------------------------------------------------------------------------------------------


def _batch_size(request: Request) -> int:
    header = request.headers.get("sforce-query-options", "")
    m = re.search(r"batchSize\s*=\s*(\d+)", header, re.IGNORECASE)
    if not m:
        return PAGE_SIZE
    return max(MIN_BATCH_SIZE, min(PAGE_SIZE, int(m.group(1))))


def _new_id(conn: psycopg.Connection, prefix: str) -> str:
    row = conn.execute(
        "INSERT INTO sfdc.id_counter (prefix, next_value) VALUES (%s, 2) "
        "ON CONFLICT (prefix) DO UPDATE SET next_value = sfdc.id_counter.next_value + 1 RETURNING next_value - 1",
        [prefix],
    ).fetchone()
    return sf_id(prefix, int(row[0]))


def _create_cursor(conn: psycopg.Connection, kind: str, text: str, include_deleted: bool, ids: list[str],
                   options: dict[str, Any] | None = None) -> str:
    now = _wall_now()
    if kind == "rest":
        conn.execute("DELETE FROM sfdc.query_cursor WHERE kind = 'rest' AND last_access < %s", [now - LOCATOR_IDLE])
    cursor_id = _new_id(conn, QUERY_LOCATOR_PREFIX)
    conn.execute(
        "INSERT INTO sfdc.query_cursor (id, kind, soql, include_deleted, record_ids, options, created_at, last_access) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        [cursor_id, kind, text, include_deleted, ids, json.dumps(options or {}), now, now],
    )
    return cursor_id


def _purge_before(call: Call) -> datetime:
    return call.now - RECYCLE_BIN


def _fetch_records(call: Call, comp: Compiled, ids: list[str]) -> list[dict[str, Any]]:
    if not ids:
        return []
    rows = call.conn.execute(comp.page_sql(), [ids]).fetchall()
    by_id = {row[0]: row for row in rows}
    return [_shape(call, comp, by_id[i]) for i in ids if i in by_id]


def _attributes(call: Call, obj_name: str, rec_id: str) -> dict[str, str]:
    return {"type": obj_name, "url": call.url(f"/sobjects/{obj_name}/{rec_id}")}


def _shape(call: Call, comp: Compiled, row: tuple[Any, ...]) -> dict[str, Any]:
    rec: dict[str, Any] = {"attributes": _attributes(call, row[1], row[0])}
    for col in comp.columns:
        value = soql.out_value(col.field, row[col.select_index])
        if col.join is None:
            rec[col.field.name] = value
            continue
        rel = col.join.relationship
        if rel not in rec:
            idx = comp.parent_index[col.join.alias]
            rec[rel] = None if row[idx] is None else {"attributes": _attributes(call, row[idx + 1], row[idx])}
        if rec[rel] is not None:
            rec[rel][col.field.name] = value
    return rec


def run_query(call: Call, text: str, include_deleted: bool, batch: int, endpoint: str) -> dict[str, Any]:
    comp = soql.compile_soql(text, call.schema, call.now)
    if comp.count_only:
        sql, params = comp.count_sql(include_deleted, _purge_before(call))
        count = int(call.conn.execute(sql, params).fetchone()[0])
        if comp.count_alias:
            agg = {"attributes": {"type": "AggregateResult"}, comp.count_alias: count}
            return {"totalSize": 1, "done": True, "records": [agg]}
        return {"totalSize": count, "done": True, "records": []}
    sql, params = comp.ids_sql(include_deleted, _purge_before(call))
    ids = [r[0] for r in call.conn.execute(sql, params)]
    body: dict[str, Any] = {"totalSize": len(ids), "done": len(ids) <= batch}
    if len(ids) > batch:
        locator = _create_cursor(call.conn, "rest", comp.soql, include_deleted, ids)
        body["nextRecordsUrl"] = call.url(f"/{endpoint}/{locator}-{batch}")
    body["records"] = _fetch_records(call, comp, ids[:batch])
    return body


def query_more(call: Call, locator: str, batch: int, endpoint: str) -> dict[str, Any]:
    invalid = SfError(400, "INVALID_QUERY_LOCATOR", "invalid query locator")
    m = re.fullmatch(r"([A-Za-z0-9]{18})-(\d+)", locator)
    if not m:
        raise invalid
    cursor_id, offset = m.group(1), int(m.group(2))
    row = call.conn.execute(
        "UPDATE sfdc.query_cursor SET last_access = %s WHERE id = %s AND kind = 'rest' AND last_access >= %s "
        "RETURNING soql, include_deleted, coalesce(array_length(record_ids, 1), 0), record_ids[%s:%s]",
        [_wall_now(), cursor_id, _wall_now() - LOCATOR_IDLE, offset + 1, offset + batch],
    ).fetchone()
    if row is None or offset > row[2]:
        raise invalid
    text, total, ids = row[0], int(row[2]), list(row[3] or [])
    comp = soql.compile_soql(text, call.schema, call.now)
    body: dict[str, Any] = {"totalSize": total, "done": offset + batch >= total}
    if not body["done"]:
        body["nextRecordsUrl"] = call.url(f"/{endpoint}/{cursor_id}-{offset + batch}")
    body["records"] = _fetch_records(call, comp, ids)
    return body


def _query_endpoint(request: Request, version: str, q: str | None, include_deleted: bool) -> dict[str, Any]:
    with connection(DB) as conn:
        call = begin(request, conn, version)
        if not q or not q.strip():
            raise SfError(400, "MALFORMED_QUERY", "A query string has to be specified")
        return run_query(call, q, include_deleted, _batch_size(request), "queryAll" if include_deleted else "query")


def _locator_endpoint(request: Request, version: str, locator: str, include_deleted: bool) -> dict[str, Any]:
    with connection(DB) as conn:
        call = begin(request, conn, version)
        return query_more(call, locator, _batch_size(request), "queryAll" if include_deleted else "query")


@_route("/services/data/{version}/query", ["GET"])
def query(request: Request, version: str, q: str | None = None) -> dict[str, Any]:
    return _query_endpoint(request, version, q, include_deleted=False)


@_route("/services/data/{version}/queryAll", ["GET"])
def query_all(request: Request, version: str, q: str | None = None) -> dict[str, Any]:
    return _query_endpoint(request, version, q, include_deleted=True)


@router.get("/services/data/{version}/query/{locator}")
def query_locator(request: Request, version: str, locator: str) -> dict[str, Any]:
    return _locator_endpoint(request, version, locator, include_deleted=False)


@router.get("/services/data/{version}/queryAll/{locator}")
def query_all_locator(request: Request, version: str, locator: str) -> dict[str, Any]:
    return _locator_endpoint(request, version, locator, include_deleted=True)


# --------------------------------------------------------------------------------------------------
# Describe
# --------------------------------------------------------------------------------------------------

_SOAP_TYPES = {
    "id": "tns:ID", "reference": "tns:ID", "boolean": "xsd:boolean", "int": "xsd:int", "long": "xsd:long",
    "double": "xsd:double", "currency": "xsd:double", "percent": "xsd:double", "date": "xsd:date",
    "datetime": "xsd:dateTime",
}
_NO_CHILD_RELATIONSHIP = {"CreatedById", "LastModifiedById", "OwnerId", "MasterRecordId"}


def describe_field(f: SfField) -> dict[str, Any]:
    text_like = f.is_text or f.type in ("id", "reference")
    long_text = f.type == "textarea" and f.length > 255
    return {
        "aggregatable": not long_text,
        "autoNumber": f.auto_number,
        "byteLength": f.length * 3 if text_like else 0,
        "calculated": False,
        "cascadeDelete": False,
        "caseSensitive": False,
        "createable": f.updateable,
        "custom": f.custom,
        "defaultValue": False if f.type == "boolean" else None,
        "defaultedOnCreate": f.type == "boolean" or f.name in soql.NON_NILLABLE_SYSTEM,
        "dependentPicklist": False,
        "digits": 9 if f.type == "int" else 0,
        "externalId": False,
        "filterable": not long_text,
        "groupable": f.type not in ("double", "currency", "percent", "datetime") and not long_text,
        "htmlFormatted": False,
        "idLookup": f.name == "Id",
        "label": f.label,
        "length": f.length if text_like else 0,
        "name": f.name,
        "nameField": f.name == "Name",
        "namePointing": len(f.reference_to) > 1,
        "nillable": f.nillable,
        "picklistValues": [
            {"active": True, "defaultValue": False, "label": v, "validFor": None, "value": v} for v in f.picklist
        ],
        "polymorphicForeignKey": len(f.reference_to) > 1,
        "precision": f.precision,
        "referenceTo": list(f.reference_to),
        "relationshipName": f.relationship_name,
        "relationshipOrder": f.relationship_order,
        "restrictedPicklist": f.updateable and f.type == "picklist",
        "scale": f.scale,
        "soapType": _SOAP_TYPES.get(f.type, "xsd:string"),
        "sortable": not long_text,
        "type": f.type,
        "unique": False,
        "updateable": f.updateable,
    }


def _object_urls(call: Call, obj: SObject) -> dict[str, str]:
    base = call.url(f"/sobjects/{obj.name}")
    return {"compactLayouts": f"{base}/describe/compactLayouts", "rowTemplate": f"{base}/{{ID}}",
            "describe": f"{base}/describe", "sobject": base}


def sobject_summary(call: Call, obj: SObject) -> dict[str, Any]:
    writable = not obj.queryable_only
    return {
        "activateable": False, "createable": writable, "custom": obj.custom, "customSetting": False,
        "deletable": writable, "deprecatedAndHidden": False, "feedEnabled": writable, "hasSubtypes": False,
        "isSubtype": False, "keyPrefix": obj.key_prefix, "label": obj.label, "labelPlural": obj.label_plural,
        "layoutable": writable, "mergeable": obj.name in ("Account", "Contact"), "mruEnabled": writable,
        "name": obj.name, "queryable": True, "replicateable": True, "retrieveable": True, "searchable": writable,
        "triggerable": writable, "undeletable": writable, "updateable": writable, "urls": _object_urls(call, obj),
    }


def _child_relationships(schema: Schema, obj: SObject) -> list[dict[str, Any]]:
    out = []
    for child in schema.objects.values():
        for f in child.fields:
            if f.type != "reference" or obj.name not in f.reference_to:
                continue
            if f.name in _NO_CHILD_RELATIONSHIP:
                name = None
            elif f.custom:
                name = f"{child.name.removesuffix('__c')}s__r"
            else:
                name = child.label_plural.replace(" ", "")
            out.append({"cascadeDelete": f.relationship_order is not None, "childSObject": child.name,
                        "deprecatedAndHidden": False, "field": f.name, "relationshipName": name})
    return out


def describe_object(call: Call, obj: SObject) -> dict[str, Any]:
    return {
        **sobject_summary(call, obj),
        "actionOverrides": [],
        "childRelationships": _child_relationships(call.schema, obj),
        "compactLayoutable": not obj.queryable_only,
        "fields": [describe_field(f) for f in obj.fields],
        "namedLayoutInfos": [],
        "networkScopeFieldName": None,
        "recordTypeInfos": [],
        "searchLayoutable": not obj.queryable_only,
        "supportedScopes": [{"label": "All records", "name": "everything"}],
    }


@_route("/services/data/{version}/sobjects", ["GET"])
def describe_global(request: Request, version: str) -> dict[str, Any]:
    with connection(DB) as conn:
        call = begin(request, conn, version)
        objs = sorted(call.schema.objects.values(), key=lambda o: o.name)
        return {"encoding": "UTF-8", "maxBatchSize": 200, "sobjects": [sobject_summary(call, o) for o in objs]}


@_route("/services/data/{version}/sobjects/{name}/describe", ["GET"])
def describe(request: Request, version: str, name: str) -> dict[str, Any]:
    with connection(DB) as conn:
        call = begin(request, conn, version)
        return describe_object(call, _object(call, name))


# --------------------------------------------------------------------------------------------------
# getUpdated / getDeleted
# --------------------------------------------------------------------------------------------------


def _parse_window_dt(value: str | None, name: str) -> datetime:
    if not value:
        raise SfError(400, "MISSING_ARGUMENT", f"Missing required parameter: {name}")
    text = value.strip().replace(" ", "+")  # an unencoded '+' arrives as a space
    norm = text.replace("Z", "+00:00")
    if re.search(r"[+-]\d{4}$", norm):
        norm = norm[:-2] + ":" + norm[-2:]
    try:
        parsed = datetime.fromisoformat(norm)
    except ValueError:
        raise SfError(400, "INVALID_REPLICATION_DATE",
                      f"{name}: '{value}' is not a valid ISO-8601 dateTime (URL-encode '+' as %2B)") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).replace(second=0, microsecond=0)  # seconds are ignored


def _floor_minute(value: datetime) -> datetime:
    return value.replace(second=0, microsecond=0)


@_route("/services/data/{version}/sobjects/{name}/updated", ["GET"])
def get_updated(request: Request, version: str, name: str, start: str | None = None,
                end: str | None = None) -> dict[str, Any]:
    with connection(DB) as conn:
        call = begin(request, conn, version)
        obj = _object(call, name)
        s, e = _parse_window_dt(start, "start"), _parse_window_dt(end, "end")
        now = _floor_minute(call.now)
        if e <= s:
            raise SfError(400, "INVALID_REPLICATION_DATE", "end date must be after start date")
        if s < now - UPDATED_WINDOW:
            raise SfError(400, "INVALID_REPLICATION_DATE", "startDate cannot be more than 30 days ago")
        latest = min(e, now)
        rows = conn.execute(
            "SELECT id FROM sfdc.record WHERE object = %s AND NOT is_deleted "
            "AND system_modstamp >= %s AND system_modstamp < %s ORDER BY system_modstamp, id LIMIT %s",
            [obj.name, s, latest, MAX_UPDATED_IDS + 1],
        ).fetchall()
        if len(rows) > MAX_UPDATED_IDS:
            raise SfError(400, "EXCEEDED_ID_LIMIT", "record limit reached. narrow the date range")
        return {"ids": [r[0] for r in rows], "latestDateCovered": sf_datetime(latest)}


@_route("/services/data/{version}/sobjects/{name}/deleted", ["GET"])
def get_deleted(request: Request, version: str, name: str, start: str | None = None,
                end: str | None = None) -> dict[str, Any]:
    with connection(DB) as conn:
        call = begin(request, conn, version)
        obj = _object(call, name)
        s, e = _parse_window_dt(start, "start"), _parse_window_dt(end, "end")
        now = _floor_minute(call.now)
        earliest = now - RECYCLE_BIN
        if e <= s:
            raise SfError(400, "INVALID_REPLICATION_DATE", "end date must be after start date")
        if s < earliest:
            raise SfError(400, "INVALID_REPLICATION_DATE",
                          f"startDate is before the earliest date available: {sf_datetime(earliest)}")
        latest = min(e, now)
        rows = conn.execute(
            "SELECT id, coalesce(deleted_date, system_modstamp) AS d FROM sfdc.record "
            "WHERE object = %s AND is_deleted AND coalesce(deleted_date, system_modstamp) >= %s "
            "AND coalesce(deleted_date, system_modstamp) < %s ORDER BY d, id",
            [obj.name, s, latest],
        ).fetchall()
        return {
            "deletedRecords": [{"id": r[0], "deletedDate": sf_datetime(r[1])} for r in rows],
            "earliestDateAvailable": sf_datetime(earliest),
            "latestDateCovered": sf_datetime(latest),
        }


# --------------------------------------------------------------------------------------------------
# Record GET / PATCH
# --------------------------------------------------------------------------------------------------


def _record_id(raw: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9]{15}|[A-Za-z0-9]{18}", raw):
        raise SfError(404, "NOT_FOUND", f"Provided external ID field does not exist or is not accessible: {raw}")
    return to_id18(raw)


def _load_row(call: Call, obj: SObject, rec_id: str, lock: bool = False) -> tuple[dict[str, Any], bool]:
    row = call.conn.execute(
        "SELECT data, is_deleted FROM sfdc.record WHERE id = %s AND object = %s" + (" FOR UPDATE" if lock else ""),
        [rec_id, obj.name],
    ).fetchone()
    if row is None:
        raise not_found()
    if row[1]:
        raise SfError(404, "ENTITY_IS_DELETED", "entity is deleted", fields=[])
    return row[0], row[1]


@router.get("/services/data/{version}/sobjects/{name}")
def sobject_basic(request: Request, version: str, name: str) -> dict[str, Any]:
    with connection(DB) as conn:
        call = begin(request, conn, version)
        return {"objectDescribe": sobject_summary(call, _object(call, name)), "recentItems": []}


@router.get("/services/data/{version}/sobjects/{name}/{record_id}")
def get_record(request: Request, version: str, name: str, record_id: str, fields: str | None = None) -> dict[str, Any]:
    with connection(DB) as conn:
        call = begin(request, conn, version)
        obj = _object(call, name)
        rec_id = _record_id(record_id)
        _load_row(call, obj, rec_id)
        names = [f.strip() for f in fields.split(",") if f.strip()] if fields else [f.name for f in obj.fields]
        for n in names:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?", n):
                raise SfError(400, "INVALID_FIELD", f"No such column '{n}' on sobject of type {obj.name}")
        comp = soql.compile_soql(f"SELECT {', '.join(names)} FROM {obj.name}", call.schema, call.now)
        records = _fetch_records(call, comp, [rec_id])
        if not records:
            raise not_found()
        return records[0]


def _coerce(f: SfField, value: Any) -> Any:
    label = f.label
    if value is None:
        if not f.nillable:
            raise SfError(400, "REQUIRED_FIELD_MISSING", f"Required fields are missing: [{f.name}]", [f.name])
        return None
    if f.is_number:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SfError(400, "JSON_PARSER_ERROR", f"Cannot deserialize instance of double from VALUE_STRING value "
                          f"{value} or request may be missing a required field at [line:1, column:1]")
        quant = Decimal(1).scaleb(-f.scale)
        rounded = Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP)
        if f.precision and abs(rounded) >= Decimal(10) ** (f.precision - f.scale):
            raise SfError(400, "NUMBER_OUTSIDE_VALID_RANGE",
                          f"{label}: value outside of valid range on numeric field: {value}", [f.name])
        return int(rounded) if f.scale == 0 else float(rounded)
    if f.type == "boolean":
        if not isinstance(value, bool):
            raise SfError(400, "JSON_PARSER_ERROR", f"Cannot deserialize instance of boolean from {value}")
        return value
    if not isinstance(value, str):
        raise SfError(400, "JSON_PARSER_ERROR", f"Cannot deserialize instance of string from {value}")
    if f.type == "picklist" and f.picklist and value not in f.picklist:
        raise SfError(400, "INVALID_OR_NULL_FOR_RESTRICTED_PICKLIST",
                      f"{label}: bad value for restricted picklist field: {value}", [f.name])
    if f.length and len(value) > f.length:
        raise SfError(400, "STRING_TOO_LONG", f"{label}: data value too large: {value} (max length={f.length})",
                      [f.name])
    return value


def patch_record_sync(request: Request, version: str, name: str, record_id: str, raw_body: bytes) -> Response:
    with connection(DB) as conn:
        call = begin(request, conn, version)
        obj = _object(call, name)
        rec_id = _record_id(record_id)
        try:
            payload = json.loads(raw_body or b"null")
        except json.JSONDecodeError as exc:
            raise SfError(400, "JSON_PARSER_ERROR", f"Unexpected character at [line:{exc.lineno}, "
                          f"column:{exc.colno}]") from None
        if not isinstance(payload, dict):
            raise SfError(400, "JSON_PARSER_ERROR", "Cannot deserialize instance of SObject from a non-object value")
        payload.pop("attributes", None)
        unknown = [k for k in payload if obj.field(k) is None]
        if unknown:
            raise SfError(400, "INVALID_FIELD", f"No such column '{unknown[0]}' on sobject of type {obj.name}")
        fields = [obj.field(k) for k in payload]
        locked = [f.name for f in fields if f is not None and not f.updateable]
        if locked:
            raise SfError(400, "INVALID_FIELD_FOR_INSERT_UPDATE",
                          f"Unable to create/update fields: {', '.join(locked)}. Please check the security settings "
                          "of this field and verify that it is read/write for your profile or permission set.", locked)
        updates = {f.name: _coerce(f, payload[k]) for k, f in zip(payload, fields, strict=True) if f is not None}
        with conn.transaction():
            data, _ = _load_row(call, obj, rec_id, lock=True)
            stamp = call.now
            data = {**data, **updates}
            user_id = _integration_user_id(conn)
            if user_id:
                data["LastModifiedById"] = user_id
            for key in ("LastModifiedDate", "SystemModstamp"):
                if key in data:
                    data[key] = sf_datetime(stamp)
            conn.execute(
                "UPDATE sfdc.record SET data = %s, last_modified_date = %s, system_modstamp = %s WHERE id = %s",
                [json.dumps(data), stamp, stamp, rec_id],
            )
    return Response(status_code=204)


@router.patch("/services/data/{version}/sobjects/{name}/{record_id}")
async def patch_record(request: Request, version: str, name: str, record_id: str) -> Response:
    raw = await request.body()
    return await run_in_threadpool(patch_record_sync, request, version, name, record_id, raw)


# --------------------------------------------------------------------------------------------------
# Limits
# --------------------------------------------------------------------------------------------------


@_route("/services/data/{version}/limits", ["GET"])
def limits(request: Request, version: str) -> dict[str, Any]:
    with connection(DB) as conn:
        begin(request, conn, version)
        usage = int(request.state.sf_usage)
        records = int(conn.execute("SELECT count(*) FROM sfdc.record WHERE NOT is_deleted").fetchone()[0])
        jobs = int(conn.execute("SELECT count(*) FROM sfdc.bulk_job WHERE created_date >= %s",
                                [_wall_now() - timedelta(days=1)]).fetchone()[0])
    data_max = 10_240
    full = {
        "ConcurrentAsyncGetReportInstances": (200, 200),
        "ConcurrentSyncReportRuns": (20, 20),
        "DailyApiRequests": (daily_limit(), max(0, daily_limit() - usage)),
        "DailyAsyncApexExecutions": (250_000, 250_000),
        "DailyBulkApiBatches": (15_000, 15_000),
        "DailyBulkV2QueryFileStorageMB": (976_562, 976_562),
        "DailyBulkV2QueryJobs": (10_000, max(0, 10_000 - jobs)),
        "DataStorageMB": (data_max, max(0, data_max - records * 2 // 1024)),
        "FileStorageMB": (10_240, 10_240),
        "HourlyODataCallout": (10_000, 10_000),
        "MassEmail": (5_000, 5_000),
        "SingleEmail": (5_000, 5_000),
        "StreamingApiConcurrentClients": (2_000, 2_000),
    }
    return {k: {"Max": m, "Remaining": r} for k, (m, r) in full.items()}


# --------------------------------------------------------------------------------------------------
# Bulk API 2.0 query jobs
# --------------------------------------------------------------------------------------------------

_JOB_STATES = ("UploadComplete", "InProgress", "JobComplete")


def _job_info(call: Call, row: dict[str, Any], detailed: bool) -> dict[str, Any]:
    opts = row.get("options") or {}
    info: dict[str, Any] = {
        "id": row["id"],
        "operation": row["operation"],
        "object": row["object"],
        "createdById": row["created_by"],
        "createdDate": sf_datetime(row["created_date"]),
        "systemModstamp": sf_datetime(row["system_modstamp"]),
        "state": row["state"],
        "concurrencyMode": "Parallel",
        "contentType": "CSV",
        "apiVersion": float(call.version[1:]),
        "lineEnding": opts.get("lineEnding", "LF"),
        "columnDelimiter": opts.get("columnDelimiter", "COMMA"),
    }
    if detailed:
        done = row["state"] == "JobComplete"
        info |= {"jobType": "V2Query", "numberRecordsProcessed": int(row["number_records"] or 0) if done else 0,
                 "retries": 0, "totalProcessingTime": 250 if done else 0, "isPkChunkingSupported": True}
    return info


def _job_row(call: Call, job_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9]{15}|[A-Za-z0-9]{18}", job_id):
        raise not_found()
    row = call.conn.execute(
        "SELECT j.id, j.operation, j.query, j.state, j.created_date, j.system_modstamp, j.result_path, "
        "j.number_records, c.options FROM sfdc.bulk_job j LEFT JOIN sfdc.query_cursor c ON c.id = j.result_path "
        "WHERE j.id = %s",
        [to_id18(job_id)],
    ).fetchone()
    if row is None:
        raise not_found()
    keys = ["id", "operation", "query", "state", "created_date", "system_modstamp", "result_path", "number_records",
            "options"]
    out = dict(zip(keys, row, strict=True))
    out["object"] = _query_object(out["query"])
    out["created_by"] = (out["options"] or {}).get("createdById")
    return out


def _query_object(text: str) -> str:
    m = re.search(r"\bFROM\s+([A-Za-z0-9_]+)", text, re.IGNORECASE)
    return m.group(1) if m else ""


@router.post("/services/data/{version}/jobs/query")
async def create_query_job(request: Request, version: str) -> Response:
    raw = await request.body()
    return await run_in_threadpool(_create_query_job, request, version, raw)


def _create_query_job(request: Request, version: str, raw: bytes) -> JSONResponse:
    with connection(DB) as conn:
        call = begin(request, conn, version)
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            raise SfError(400, "JSON_PARSER_ERROR", "Unexpected character in request body") from None
        operation = body.get("operation") if isinstance(body, dict) else None
        text = body.get("query") if isinstance(body, dict) else None
        if operation not in ("query", "queryAll"):
            raise SfError(400, "INVALIDJOB", "operation must be 'query' or 'queryAll'")
        if not isinstance(text, str) or not text.strip():
            raise SfError(400, "INVALIDJOB", "A query string has to be specified")
        delimiter = body.get("columnDelimiter", "COMMA")
        line_ending = body.get("lineEnding", "LF")
        if delimiter not in DELIMITERS or line_ending not in LINE_ENDINGS or body.get("contentType", "CSV") != "CSV":
            raise SfError(400, "INVALIDJOB", "Unsupported contentType, columnDelimiter or lineEnding")
        comp = soql.compile_soql(text, call.schema, call.now)
        if comp.count_only:
            raise SfError(400, "API_ERROR", "Aggregate queries are not supported by Bulk API 2.0")
        if comp.offset:
            raise SfError(400, "API_ERROR", "OFFSET is not supported by Bulk API 2.0")
        include_deleted = operation == "queryAll"
        sql, params = comp.ids_sql(include_deleted, _purge_before(call))
        ids = [r[0] for r in conn.execute(sql, params)]
        user_id = _integration_user_id(conn) or sf_id("005", 1)
        opts = {"columnDelimiter": delimiter, "lineEnding": line_ending, "createdById": user_id}
        with conn.transaction():
            cursor_id = _create_cursor(conn, "bulk", comp.soql, include_deleted, ids, opts)
            job_id = _new_id(conn, BULK_JOB_PREFIX)
            now = _wall_now()
            conn.execute(
                "INSERT INTO sfdc.bulk_job (id, operation, query, state, created_date, system_modstamp, result_path, "
                "number_records) VALUES (%s, %s, %s, 'UploadComplete', %s, %s, %s, %s)",
                [job_id, operation, comp.soql, now, now, cursor_id, len(ids)],
            )
        return JSONResponse(_job_info(call, _job_row(call, job_id), detailed=False))


@router.get("/services/data/{version}/jobs/query")
def list_query_jobs(request: Request, version: str) -> dict[str, Any]:
    with connection(DB) as conn:
        call = begin(request, conn, version)
        ids = [r[0] for r in conn.execute("SELECT id FROM sfdc.bulk_job ORDER BY created_date DESC LIMIT 1000")]
        return {"done": True, "records": [_job_info(call, _job_row(call, i), False) for i in ids],
                "nextRecordsUrl": None}


@router.get("/services/data/{version}/jobs/query/{job_id}")
def get_query_job(request: Request, version: str, job_id: str) -> dict[str, Any]:
    with connection(DB) as conn:
        call = begin(request, conn, version)
        row = _job_row(call, job_id)
        if row["state"] in _JOB_STATES[:-1]:  # each poll advances the job one step
            row["state"] = _JOB_STATES[_JOB_STATES.index(row["state"]) + 1]
            row["system_modstamp"] = _wall_now()
            conn.execute("UPDATE sfdc.bulk_job SET state = %s, system_modstamp = %s WHERE id = %s",
                         [row["state"], row["system_modstamp"], row["id"]])
        return _job_info(call, row, detailed=True)


@router.patch("/services/data/{version}/jobs/query/{job_id}")
async def abort_query_job(request: Request, version: str, job_id: str) -> Response:
    raw = await request.body()
    return await run_in_threadpool(_abort_query_job, request, version, job_id, raw)


def _abort_query_job(request: Request, version: str, job_id: str, raw: bytes) -> JSONResponse:
    with connection(DB) as conn:
        call = begin(request, conn, version)
        row = _job_row(call, job_id)
        try:
            state = json.loads(raw or b"{}").get("state")
        except (json.JSONDecodeError, AttributeError):
            state = None
        if state != "Aborted":
            raise SfError(400, "INVALIDJOBSTATE", "Only state 'Aborted' can be set on a query job")
        if row["state"] == "JobComplete":
            raise SfError(400, "INVALIDJOBSTATE", "Job is already complete")
        row["state"], row["system_modstamp"] = "Aborted", _wall_now()
        conn.execute("UPDATE sfdc.bulk_job SET state = 'Aborted', system_modstamp = %s WHERE id = %s",
                     [row["system_modstamp"], row["id"]])
        return JSONResponse(_job_info(call, row, detailed=False))


@router.delete("/services/data/{version}/jobs/query/{job_id}")
def delete_query_job(request: Request, version: str, job_id: str) -> Response:
    with connection(DB) as conn:
        call = begin(request, conn, version)
        row = _job_row(call, job_id)
        if row["state"] not in ("JobComplete", "Aborted", "Failed"):
            raise SfError(400, "INVALIDJOBSTATE", "Only completed or aborted jobs can be deleted")
        with conn.transaction():
            conn.execute("DELETE FROM sfdc.bulk_job WHERE id = %s", [row["id"]])
            conn.execute("DELETE FROM sfdc.query_cursor WHERE id = %s", [row["result_path"]])
    return Response(status_code=204)


def _decode_bulk_locator(locator: str | None) -> int:
    if not locator:
        return 0
    try:
        return int(base64.b64decode(locator + "=" * (-len(locator) % 4)).decode())
    except (ValueError, UnicodeDecodeError):
        raise SfError(400, "INVALIDLOCATOR", f"Invalid locator: {locator}") from None


def _encode_bulk_locator(offset: int) -> str:
    return base64.b64encode(str(offset).encode()).decode().rstrip("=")


@router.get("/services/data/{version}/jobs/query/{job_id}/results")
def query_job_results(request: Request, version: str, job_id: str, maxRecords: str | None = None,
                      locator: str | None = None) -> Response:
    with connection(DB) as conn:
        call = begin(request, conn, version)
        row = _job_row(call, job_id)
        if row["state"] != "JobComplete":
            raise SfError(400, "INVALIDJOBSTATE", f"Job is in state {row['state']}; results are available once the "
                          "job is JobComplete")
        if maxRecords is not None and (not maxRecords.isdigit() or int(maxRecords) < 1):
            raise SfError(400, "INVALIDJOB", "maxRecords must be a positive integer")
        size = int(maxRecords) if maxRecords else BULK_DEFAULT_MAX_RECORDS
        offset = _decode_bulk_locator(locator)
        cur = conn.execute(
            "SELECT soql, coalesce(array_length(record_ids, 1), 0), record_ids[%s:%s], options "
            "FROM sfdc.query_cursor WHERE id = %s",
            [offset + 1, offset + size, row["result_path"]],
        ).fetchone()
        if cur is None:
            raise not_found()
        text, total, ids, opts = cur[0], int(cur[1]), list(cur[2] or []), cur[3] or {}
        if offset > total:
            raise SfError(400, "INVALIDLOCATOR", f"Invalid locator: {locator}")
        comp = soql.compile_soql(text, call.schema, call.now)
        body = _csv(comp, call, ids, DELIMITERS[opts.get("columnDelimiter", "COMMA")],
                    LINE_ENDINGS[opts.get("lineEnding", "LF")])
        next_offset = offset + len(ids)
        headers = {
            "Sforce-Locator": _encode_bulk_locator(next_offset) if next_offset < total else "null",
            "Sforce-NumberOfRecords": str(len(ids)),
        }
        return Response(body, media_type="text/csv; charset=UTF-8", headers=headers)


def _csv(comp: Compiled, call: Call, ids: list[str], delimiter: str, line_ending: str) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=delimiter, lineterminator=line_ending, quoting=csv.QUOTE_ALL)
    writer.writerow(comp.headers)
    rows = call.conn.execute(comp.page_sql(), [ids]).fetchall() if ids else []
    by_id = {r[0]: r for r in rows}
    for rec_id in ids:
        r = by_id.get(rec_id)
        if r is None:
            continue
        out = []
        for col in comp.columns:
            parent_missing = col.join is not None and r[comp.parent_index[col.join.alias]] is None
            out.append("" if parent_missing else soql.csv_value(col.field, r[col.select_index]))
        writer.writerow(out)
    return buf.getvalue()
