"""Salesforce REST mock contract tests: OAuth, query paging, queryAll, windows, describe, PATCH, limits, Bulk 2.0.

Needs the test Postgres (HL_TEST_PG_PORT, see harborline/sim/testing.py).
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from harborline.api import salesforce
from harborline.api.soql import sf_datetime
from harborline.db import get_db
from harborline.ids import sf_checksum, sf_id
from harborline.settings import Settings

SIM_NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
WALL_NOW = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)
V = "/sfdc/services/data/v67.0"
N_ACCOUNTS = 2500


@dataclass
class Env:
    client: TestClient
    headers: dict[str, str]
    conn: psycopg.Connection
    ids: dict[str, Any]

    def get(self, path: str, **params: Any) -> Any:
        return self.client.get(f"{V}{path}", params=params, headers=self.headers)

    def q(self, soql: str, all_rows: bool = False) -> dict[str, Any]:
        r = self.get("/queryAll" if all_rows else "/query", q=soql)
        assert r.status_code == 200, r.text
        return r.json()


def _record(obj: str, rid: str, fields: dict[str, Any], created: datetime, by: str,
            deleted: datetime | None = None) -> list[Any]:
    stamp = sf_datetime(created)
    data = {"Id": rid, "IsDeleted": deleted is not None, "CreatedDate": stamp, "CreatedById": by,
            "LastModifiedDate": stamp, "LastModifiedById": by, "SystemModstamp": stamp, **fields}
    modstamp = deleted or created
    return [rid, obj, json.dumps(data), created, created, modstamp, deleted is not None, deleted]


def _seed(conn: psycopg.Connection) -> dict[str, Any]:
    integ, ae = sf_id("005", 1), sf_id("005", 2)
    rows = [
        _record("User", integ, {"Name": "Integration User", "FirstName": "Integration", "LastName": "User",
                                "IsActive": True}, SIM_NOW - timedelta(days=900), integ),
        _record("User", ae, {"Name": "Jordan Ellis", "Email": "jordan.ellis@harborline-logistics.example",
                             "IsActive": True}, SIM_NOW - timedelta(days=900), integ),
    ]
    accounts = []
    for i in range(1, N_ACCOUNTS + 1):
        rid = sf_id("001", i)
        accounts.append(rid)
        rows.append(_record("Account", rid, {
            "Name": f"Client {i:04d}", "Industry": "Apparel" if i % 2 else "Retail", "OwnerId": ae,
            "AnnualRevenue": i * 1000, "Client_Since__c": "2024-03-01" if i <= 10 else None,
            "WMS_Owner_Code__c": f"C{i:04d}" if i % 7 else None, "Churn_Risk__c": "Low",
        }, SIM_NOW - timedelta(hours=i), integ))
    contact_live, contact_orphan = sf_id("003", 1), sf_id("003", 2)
    contact_deleted, contact_purged = sf_id("003", 3), sf_id("003", 4)
    rows += [
        _record("Contact", contact_live, {"LastName": "Smith", "AccountId": accounts[0], "Email": "a@x.example",
                                          "OwnerId": ae}, SIM_NOW - timedelta(days=3), integ),
        _record("Contact", contact_orphan, {"LastName": "Nobody", "AccountId": None, "OwnerId": ae},
                SIM_NOW - timedelta(days=4), integ),
        _record("Contact", contact_deleted, {"LastName": "Gone", "AccountId": accounts[1], "OwnerId": ae},
                SIM_NOW - timedelta(days=9), integ, deleted=SIM_NOW - timedelta(days=2)),
        _record("Contact", contact_purged, {"LastName": "Old", "AccountId": accounts[2], "OwnerId": ae},
                SIM_NOW - timedelta(days=40), integ, deleted=SIM_NOW - timedelta(days=20)),
    ]
    opps = []
    for i, (stage, amount, closed, close_date) in enumerate([
        ("Prospecting", 12000.5, False, "2026-11-15"), ("Closed Won", 99000, True, "2026-10-01"),
        ("Closed Lost", None, True, "2026-09-12"), ("Negotiation/Review", 45000, False, "2027-01-31"),
    ], start=1):
        rid = sf_id("006", i)
        opps.append(rid)
        rows.append(_record("Opportunity", rid, {
            "Name": f"Opp {i}", "AccountId": accounts[i], "StageName": stage, "Amount": amount, "IsClosed": closed,
            "CloseDate": close_date, "OwnerId": ae, "Warehouses_Requested__c": "ATL1;DFW1" if i == 1 else "TOR1",
        }, SIM_NOW - timedelta(days=i), integ))
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO sfdc.record (id, object, data, created_date, last_modified_date, system_modstamp, is_deleted, "
            "deleted_date) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", rows)
    return {"integ": integ, "ae": ae, "accounts": accounts, "contact_live": contact_live,
            "contact_orphan": contact_orphan, "contact_deleted": contact_deleted, "contact_purged": contact_purged,
            "opps": opps}


@pytest.fixture
def env(pg_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    monkeypatch.setattr(salesforce, "_wall_now", lambda: WALL_NOW)
    with get_db().connect("sim_internal", autocommit=True) as conn:
        conn.execute("TRUNCATE sfdc.record, sfdc.query_cursor, sfdc.bulk_job, sfdc.api_usage, sfdc.custom_field")
        conn.execute("DELETE FROM sfdc.id_counter WHERE prefix IN ('750', '01g')")
        conn.execute("DELETE FROM sim.state")
        conn.execute("INSERT INTO sim.state (id, sim_clock) VALUES (1, %s)", [SIM_NOW])
        ids = _seed(conn)
        app = FastAPI()
        app.include_router(salesforce.router)
        client = TestClient(app)
        token = client.post("/sfdc/services/oauth2/token", data={
            "grant_type": "client_credentials", "client_id": pg_settings.sf_client_id,
            "client_secret": pg_settings.sf_client_secret}).json()["access_token"]
        yield Env(client, {"Authorization": f"Bearer {token}"}, conn, ids)
        conn.execute("DELETE FROM sim.state")


def assert_valid_id(rid: str, prefix: str) -> None:
    assert len(rid) == 18 and rid.startswith(prefix), rid
    assert sf_checksum(rid[:15]) == rid[15:], f"bad checksum: {rid}"


# ------------------------------------------------------------------------------------------------ OAuth


def test_oauth_token_shape_and_errors(env: Env, pg_settings: Settings) -> None:
    r = env.client.post("/sfdc/services/oauth2/token", data={
        "grant_type": "client_credentials", "client_id": pg_settings.sf_client_id,
        "client_secret": pg_settings.sf_client_secret})
    body = r.json()
    assert r.status_code == 200
    assert set(body) == {"access_token", "signature", "scope", "instance_url", "id", "token_type", "issued_at"}
    assert body["access_token"].startswith("00D5f000004HbLq!AR")
    assert body["instance_url"].endswith("/sfdc") and body["token_type"] == "Bearer"
    assert body["id"].endswith(f"/id/00D5f000004HbLqEAK/{env.ids['integ']}")
    assert body["issued_at"] == str(int(WALL_NOW.timestamp() * 1000))
    bad = env.client.post("/sfdc/services/oauth2/token", data={
        "grant_type": "client_credentials", "client_id": pg_settings.sf_client_id, "client_secret": "nope"})
    assert bad.status_code == 400 and bad.json() == {"error": "invalid_client",
                                                     "error_description": "invalid client credentials"}
    grant = env.client.post("/sfdc/services/oauth2/token", data={"grant_type": "password"})
    assert grant.json()["error"] == "unsupported_grant_type"


def test_session_expires_after_two_hours(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    assert env.get("/limits").status_code == 200
    monkeypatch.setattr(salesforce, "_wall_now", lambda: WALL_NOW + timedelta(hours=2, seconds=1))
    r = env.get("/limits")
    assert r.status_code == 401
    assert r.json() == [{"message": "Session expired or invalid", "errorCode": "INVALID_SESSION_ID"}]
    tampered = env.client.get(f"{V}/limits", headers={"Authorization": env.headers["Authorization"][:-2] + "xx"})
    assert tampered.status_code == 401


def test_versions_and_unknown_version(env: Env) -> None:
    versions = env.client.get("/sfdc/services/data/").json()
    assert {"label": "Summer '26", "url": "/services/data/v67.0", "version": "67.0"} in versions
    r = env.client.get("/sfdc/services/data/v99.0/limits", headers=env.headers)
    assert r.status_code == 404 and r.json()[0]["errorCode"] == "NOT_FOUND"


# ------------------------------------------------------------------------------------------------ query


def test_query_pagination_round_trip(env: Env) -> None:
    first = env.q("SELECT Id, Name FROM Account ORDER BY Name")
    assert first["totalSize"] == N_ACCOUNTS and first["done"] is False and len(first["records"]) == 2000
    locator_url = first["nextRecordsUrl"]
    assert locator_url.startswith("/services/data/v67.0/query/01g") and locator_url.endswith("-2000")
    assert_valid_id(locator_url.rsplit("/", 1)[1].split("-")[0], "01g")
    second = env.client.get(f"/sfdc{locator_url}", headers=env.headers).json()
    assert second["done"] is True and "nextRecordsUrl" not in second and len(second["records"]) == 500
    names = [r["Name"] for r in first["records"] + second["records"]]
    assert names == sorted(names) and len(set(names)) == N_ACCOUNTS
    rec = first["records"][0]
    assert list(rec) == ["attributes", "Id", "Name"]
    assert rec["attributes"] == {"type": "Account", "url": f"/services/data/v67.0/sobjects/Account/{rec['Id']}"}
    for r in first["records"][:50]:
        assert_valid_id(r["Id"], "001")


def test_batch_size_header_and_locator_expiry(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    r = env.client.get(f"{V}/query", params={"q": "SELECT Id FROM Account"},
                       headers={**env.headers, "Sforce-Query-Options": "batchSize=500"}).json()
    pages, url = [r], r.get("nextRecordsUrl")
    while url:
        page = env.client.get(f"/sfdc{url}", headers={**env.headers, "Sforce-Query-Options": "batchSize=500"}).json()
        pages.append(page)
        url = page.get("nextRecordsUrl")
    assert [len(p["records"]) for p in pages] == [500] * 5
    again = env.q("SELECT Id FROM Account")
    monkeypatch.setattr(salesforce, "_wall_now", lambda: WALL_NOW + timedelta(minutes=16))
    expired = env.client.get(f"/sfdc{again['nextRecordsUrl']}", headers=env.headers)
    assert expired.status_code == 400
    assert expired.json() == [{"message": "invalid query locator", "errorCode": "INVALID_QUERY_LOCATOR"}]


def test_query_hides_deleted_and_query_all_shows_recycle_bin(env: Env) -> None:
    live = env.q("SELECT Id, LastName FROM Contact ORDER BY LastName")
    assert [r["LastName"] for r in live["records"]] == ["Nobody", "Smith"]
    everything = env.q("SELECT Id, LastName, IsDeleted FROM Contact ORDER BY LastName", all_rows=True)
    by_name = {r["LastName"]: r["IsDeleted"] for r in everything["records"]}
    assert by_name == {"Gone": True, "Nobody": False, "Smith": False}  # 'Old' left the recycle bin 20 days ago
    assert env.q("SELECT Id FROM Contact WHERE IsDeleted = true")["totalSize"] == 0


def test_parent_relationship_nesting_and_null_parent(env: Env) -> None:
    recs = env.q("SELECT LastName, Account.Name, Owner.Name FROM Contact ORDER BY LastName")["records"]
    nobody, smith = recs
    assert nobody["Account"] is None
    assert smith["Account"]["Name"] == "Client 0001"
    assert smith["Account"]["attributes"]["type"] == "Account"
    assert smith["Owner"] == {"attributes": {"type": "User", "url": f"/services/data/v67.0/sobjects/User/"
                                             f"{env.ids['ae']}"}, "Name": "Jordan Ellis"}


def test_typed_filters_against_jsonb(env: Env) -> None:
    def names(where: str) -> list[str]:
        return [r["Name"] for r in env.q(f"SELECT Name FROM Opportunity WHERE {where} ORDER BY Name")["records"]]

    assert names("Amount > 40000") == ["Opp 2", "Opp 4"]
    assert names("IsClosed = false") == ["Opp 1", "Opp 4"]
    assert names("CloseDate >= 2026-10-01 AND CloseDate < 2027-01-01") == ["Opp 1", "Opp 2"]
    assert names("StageName IN ('closed won', 'CLOSED LOST')") == ["Opp 2", "Opp 3"]
    assert names("StageName != 'Prospecting'") == ["Opp 2", "Opp 3", "Opp 4"]
    assert names("Amount = null") == ["Opp 3"]
    assert names("Name LIKE 'opp _'") == ["Opp 1", "Opp 2", "Opp 3", "Opp 4"]
    assert names("Warehouses_Requested__c INCLUDES ('DFW1')") == ["Opp 1"]
    assert names("CreatedDate = LAST_N_DAYS:2") == ["Opp 1", "Opp 2"]
    assert names(f"AccountId = '{env.ids['accounts'][1][:15]}'") == ["Opp 1"]
    assert names("Account.Industry = 'retail'") == ["Opp 1", "Opp 3"]
    since = env.q("SELECT Name FROM Account WHERE Client_Since__c = 2024-03-01")
    assert since["totalSize"] == 10


def test_count_order_limit_offset(env: Env) -> None:
    assert env.q("SELECT COUNT() FROM Account") == {"totalSize": N_ACCOUNTS, "done": True, "records": []}
    agg = env.q("SELECT COUNT(WMS_Owner_Code__c) FROM Account")
    assert agg["records"] == [{"attributes": {"type": "AggregateResult"}, "expr0": N_ACCOUNTS - N_ACCOUNTS // 7}]
    page = env.q("SELECT Name, AnnualRevenue FROM Account ORDER BY AnnualRevenue DESC LIMIT 3 OFFSET 2")
    assert [r["AnnualRevenue"] for r in page["records"]] == [2498000, 2497000, 2496000]
    nulls = env.q("SELECT WMS_Owner_Code__c FROM Account ORDER BY WMS_Owner_Code__c LIMIT 1")
    assert nulls["records"][0]["WMS_Owner_Code__c"] is None  # SOQL sorts nulls first by default


def test_query_errors_use_salesforce_shape(env: Env) -> None:
    r = env.get("/query", q="SELECT Nme FROM Account")
    assert r.status_code == 400
    [err] = r.json()
    assert err["errorCode"] == "INVALID_FIELD" and "ERROR at Row:1:Column:8" in err["message"]
    missing = env.get("/query")
    assert missing.json() == [{"message": "A query string has to be specified", "errorCode": "MALFORMED_QUERY"}]
    assert missing.headers["Sforce-Limit-Info"].startswith("api-usage=")


# ------------------------------------------------------------------------------------------------ windows


def test_get_updated_window_uses_sim_clock(env: Env) -> None:
    r = env.get("/sobjects/Contact/updated/", start="2026-10-20T00:00:00Z", end="2026-11-30T00:00:00+00:00").json()
    assert r == {"ids": [env.ids["contact_orphan"], env.ids["contact_live"]],
                 "latestDateCovered": "2026-10-30T12:00:00.000+0000"}
    too_old = env.get("/sobjects/Contact/updated/", start="2026-09-29T11:59:00Z", end="2026-10-01T00:00:00Z")
    assert too_old.status_code == 400 and too_old.json()[0]["errorCode"] == "INVALID_REPLICATION_DATE"
    unencoded_plus = env.get("/sobjects/Contact/updated", start="2026-10-26T00:00:59 00:00",
                             end="2026-10-27T00:00:00Z").json()
    assert unencoded_plus["ids"] == [env.ids["contact_orphan"]]  # seconds ignored; ' ' read as '+'


def test_get_deleted_window_and_recycle_bin_limit(env: Env) -> None:
    r = env.get("/sobjects/Contact/deleted/", start="2026-10-16T00:00:00Z", end="2026-10-31T00:00:00Z").json()
    assert r == {
        "deletedRecords": [{"id": env.ids["contact_deleted"], "deletedDate": "2026-10-28T12:00:00.000+0000"}],
        "earliestDateAvailable": "2026-10-15T12:00:00.000+0000",
        "latestDateCovered": "2026-10-30T12:00:00.000+0000",
    }
    old = env.get("/sobjects/Contact/deleted/", start="2026-10-01T00:00:00Z", end="2026-10-31T00:00:00Z")
    assert old.status_code == 400 and old.json()[0]["errorCode"] == "INVALID_REPLICATION_DATE"
    assert env.get("/sobjects/Nope/deleted/", start="x", end="y").status_code == 404


# ------------------------------------------------------------------------------------------------ describe


def test_describe_global_and_object(env: Env) -> None:
    glob = env.get("/sobjects").json()
    names = {o["name"] for o in glob["sobjects"]}
    assert {"Account", "Contact", "Opportunity", "Case", "Rate_Card__c", "OpportunityHistory"} <= names
    acct = next(o for o in glob["sobjects"] if o["name"] == "Account")
    assert acct["keyPrefix"] == "001" and acct["urls"]["describe"] == "/services/data/v67.0/sobjects/Account/describe"
    d = env.get("/sobjects/account/describe").json()
    fields = {f["name"]: f for f in d["fields"]}
    assert d["name"] == "Account" and d["custom"] is False
    churn = fields["Churn_Risk__c"]
    assert churn["type"] == "picklist" and churn["updateable"] and churn["custom"]
    assert [p["value"] for p in churn["picklistValues"]] == ["Low", "Medium", "High"]
    assert fields["Name"]["nillable"] is False and fields["Name"]["length"] == 255 and fields["Name"]["nameField"]
    assert fields["OwnerId"]["referenceTo"] == ["User"] and fields["OwnerId"]["relationshipName"] == "Owner"
    assert fields["Health_Score__c"]["soapType"] == "xsd:double" and fields["Health_Score__c"]["precision"] == 3
    assert {"childSObject": "Contact", "field": "AccountId", "relationshipName": "Contacts", "cascadeDelete": False,
            "deprecatedAndHidden": False} in d["childRelationships"]


def test_schema_drift_field_in_describe_and_soql(env: Env) -> None:
    env.conn.execute(
        "INSERT INTO sfdc.custom_field (object, api_name, definition, added_at) VALUES ('Account', "
        "'Credit_Hold__c', %s, %s)", [json.dumps({"name": "Credit_Hold__c", "type": "boolean", "custom": True,
                                                 "label": "Credit Hold"}), SIM_NOW])
    fields = {f["name"]: f for f in env.get("/sobjects/Account/describe").json()["fields"]}
    assert fields["Credit_Hold__c"]["type"] == "boolean" and fields["Credit_Hold__c"]["label"] == "Credit Hold"
    r = env.q("SELECT Name, Credit_Hold__c FROM Account WHERE Credit_Hold__c = false LIMIT 1")
    assert r["records"][0]["Credit_Hold__c"] is False


# ------------------------------------------------------------------------------------------------ record GET/PATCH


def test_get_record_and_errors(env: Env) -> None:
    acct = env.ids["accounts"][0]
    rec = env.get(f"/sobjects/Account/{acct[:15]}", fields="Name,AnnualRevenue").json()
    assert rec == {"attributes": {"type": "Account", "url": f"/services/data/v67.0/sobjects/Account/{acct}"},
                   "Name": "Client 0001", "AnnualRevenue": 1000}
    full = env.get(f"/sobjects/Account/{acct}").json()
    assert full["Id"] == acct and "Health_Score__c" in full and full["IsDeleted"] is False
    gone = env.get(f"/sobjects/Contact/{env.ids['contact_deleted']}")
    assert gone.status_code == 404 and gone.json()[0]["errorCode"] == "ENTITY_IS_DELETED"
    bad = env.get("/sobjects/Account/xyz")
    assert bad.status_code == 404 and "Provided external ID field does not exist" in bad.json()[0]["message"]
    assert env.get(f"/sobjects/Contact/{acct}").json()[0]["errorCode"] == "NOT_FOUND"


def test_patch_health_score_reverse_etl(env: Env) -> None:
    acct = env.ids["accounts"][0]
    r = env.client.patch(f"{V}/sobjects/Account/{acct}", json={"Health_Score__c": 72.6, "Churn_Risk__c": "High"},
                         headers=env.headers)
    assert r.status_code == 204 and r.content == b""
    rec = env.get(f"/sobjects/Account/{acct}", fields="Health_Score__c,Churn_Risk__c,LastModifiedById,"
                  "LastModifiedDate,SystemModstamp").json()
    assert rec["Health_Score__c"] == 73 and rec["Churn_Risk__c"] == "High"
    assert rec["LastModifiedById"] == env.ids["integ"]
    assert rec["LastModifiedDate"] == rec["SystemModstamp"] == "2026-10-30T12:00:00.000+0000"
    updated = env.get("/sobjects/Account/updated", start="2026-10-30T11:58:00Z", end="2026-10-30T12:01:00Z").json()
    assert updated["ids"] == []  # stamped at the sim clock, the window end is capped at it
    later = env.get("/sobjects/Account/updated", start="2026-10-30T11:00:00Z", end="2026-10-30T13:00:00Z").json()
    assert later["latestDateCovered"] == "2026-10-30T12:00:00.000+0000"


def test_patch_rejections(env: Env) -> None:
    acct = env.ids["accounts"][0]

    def patch(body: Any, obj: str = "Account", rid: str = acct) -> Any:
        return env.client.patch(f"{V}/sobjects/{obj}/{rid}", json=body, headers=env.headers)

    locked = patch({"Name": "Renamed", "Health_Score__c": 50})
    assert locked.status_code == 400
    assert locked.json() == [{"message": "Unable to create/update fields: Name. Please check the security settings "
                              "of this field and verify that it is read/write for your profile or permission set.",
                              "errorCode": "INVALID_FIELD_FOR_INSERT_UPDATE", "fields": ["Name"]}]
    assert patch({"Churn_Risk__c": "Extreme"}).json()[0]["errorCode"] == "INVALID_OR_NULL_FOR_RESTRICTED_PICKLIST"
    assert patch({"Health_Score__c": 1000}).json()[0]["errorCode"] == "NUMBER_OUTSIDE_VALID_RANGE"
    assert patch({"Health_Score__c": "high"}).json()[0]["errorCode"] == "JSON_PARSER_ERROR"
    assert patch({"Bogus__c": 1}).json()[0]["errorCode"] == "INVALID_FIELD"
    assert patch({"LastName": "X"}, "Contact", env.ids["contact_live"]).json()[0]["errorCode"] == \
        "INVALID_FIELD_FOR_INSERT_UPDATE"
    gone = patch({"Health_Score__c": 1}, "Contact", env.ids["contact_deleted"])
    assert gone.status_code == 400  # Contact has no Health_Score__c
    deleted_acct = sf_id("001", 999_999)
    env.conn.execute("INSERT INTO sfdc.record (id, object, data, created_date, last_modified_date, system_modstamp, "
                     "is_deleted, deleted_date) VALUES (%s, 'Account', '{}', %s, %s, %s, true, %s)",
                     [deleted_acct, SIM_NOW, SIM_NOW, SIM_NOW, SIM_NOW])
    assert patch({"Health_Score__c": 1}, rid=deleted_acct).json()[0]["errorCode"] == "ENTITY_IS_DELETED"
    raw = env.client.patch(f"{V}/sobjects/Account/{acct}", content=b"{nope", headers=env.headers)
    assert raw.json()[0]["errorCode"] == "JSON_PARSER_ERROR"


# ------------------------------------------------------------------------------------------------ limits


def test_daily_limit_header_and_request_limit_exceeded(env: Env) -> None:
    r = env.get("/limits")
    body = r.json()
    assert body["DailyApiRequests"]["Max"] == 15000
    used = int(r.headers["Sforce-Limit-Info"].split("=")[1].split("/")[0])
    assert body["DailyApiRequests"]["Remaining"] == 15000 - used
    env.conn.execute("UPDATE sfdc.api_usage SET calls = 15000")
    blocked = env.get("/query", q="SELECT Id FROM Account")
    assert blocked.status_code == 403
    assert blocked.json() == [{"message": "TotalRequests Limit exceeded.", "errorCode": "REQUEST_LIMIT_EXCEEDED"}]
    assert blocked.headers["Sforce-Limit-Info"] == "api-usage=15001/15000"


# ------------------------------------------------------------------------------------------------ Bulk API 2.0


def _bulk_rows(env: Env, job_id: str, max_records: int) -> tuple[list[dict[str, str]], list[str]]:
    rows, locators, locator = [], [], None
    while True:
        params: dict[str, Any] = {"maxRecords": max_records}
        if locator:
            params["locator"] = locator
        r = env.client.get(f"{V}/jobs/query/{job_id}/results", params=params, headers=env.headers)
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/csv")
        page = list(csv.DictReader(io.StringIO(r.text)))
        assert int(r.headers["Sforce-NumberOfRecords"]) == len(page)
        rows += page
        locator = r.headers["Sforce-Locator"]
        locators.append(locator)
        if locator == "null":
            return rows, locators


def test_bulk_query_job_lifecycle(env: Env) -> None:
    created = env.client.post(f"{V}/jobs/query", headers=env.headers, json={
        "operation": "query", "query": "SELECT Id, Name, Owner.Name, AnnualRevenue FROM Account"}).json()
    assert created["state"] == "UploadComplete" and created["object"] == "Account"
    assert created["contentType"] == "CSV" and created["lineEnding"] == "LF" and created["apiVersion"] == 67.0
    assert_valid_id(created["id"], "750")
    job = created["id"]
    early = env.client.get(f"{V}/jobs/query/{job}/results", headers=env.headers)
    assert early.status_code == 400 and early.json()[0]["errorCode"] == "INVALIDJOBSTATE"
    states = [env.client.get(f"{V}/jobs/query/{job}", headers=env.headers).json()["state"] for _ in range(3)]
    assert states == ["InProgress", "JobComplete", "JobComplete"]
    info = env.client.get(f"{V}/jobs/query/{job}", headers=env.headers).json()
    assert info["numberRecordsProcessed"] == N_ACCOUNTS and info["jobType"] == "V2Query"
    rows, locators = _bulk_rows(env, job, 1000)
    assert len(rows) == N_ACCOUNTS and len({r["Id"] for r in rows}) == N_ACCOUNTS
    assert locators == ["MTAwMA", "MjAwMA", "null"]  # base64 offsets, like Salesforce
    assert list(rows[0]) == ["Id", "Name", "Owner.Name", "AnnualRevenue"]
    assert rows[0]["Owner.Name"] == "Jordan Ellis"
    raw = env.client.get(f"{V}/jobs/query/{job}/results", params={"maxRecords": 1}, headers=env.headers).text
    assert raw.startswith('"Id","Name","Owner.Name","AnnualRevenue"\n"001')
    assert env.client.delete(f"{V}/jobs/query/{job}", headers=env.headers).status_code == 204
    assert env.client.get(f"{V}/jobs/query/{job}", headers=env.headers).status_code == 404


def test_bulk_query_all_includes_deleted_and_rejects_aggregates(env: Env) -> None:
    job = env.client.post(f"{V}/jobs/query", headers=env.headers, json={
        "operation": "queryAll", "query": "SELECT Id, IsDeleted, LastName FROM Contact",
        "columnDelimiter": "PIPE", "lineEnding": "CRLF"}).json()["id"]
    for _ in range(2):
        env.client.get(f"{V}/jobs/query/{job}", headers=env.headers)
    r = env.client.get(f"{V}/jobs/query/{job}/results", headers=env.headers)
    lines = r.text.split("\r\n")
    assert lines[0] == '"Id"|"IsDeleted"|"LastName"'
    assert sorted(line.split("|")[1] for line in lines[1:] if line) == ['"false"', '"false"', '"true"']
    bad = env.client.post(f"{V}/jobs/query", headers=env.headers,
                          json={"operation": "query", "query": "SELECT COUNT() FROM Contact"})
    assert bad.status_code == 400 and bad.json()[0]["errorCode"] == "API_ERROR"
    malformed = env.client.post(f"{V}/jobs/query", headers=env.headers,
                                json={"operation": "query", "query": "SELECT Nope FROM Contact"})
    assert malformed.json()[0]["errorCode"] == "INVALID_FIELD"
    listing = env.client.get(f"{V}/jobs/query", headers=env.headers).json()
    assert listing["done"] is True and [j["id"] for j in listing["records"]] == [job]


def test_all_returned_ids_have_valid_checksums(env: Env) -> None:
    prefixes = {"Account": "001", "Contact": "003", "Opportunity": "006", "User": "005"}
    for obj, prefix in prefixes.items():
        for rec in env.q(f"SELECT Id FROM {obj}", all_rows=True)["records"]:
            assert_valid_id(rec["Id"], prefix)
