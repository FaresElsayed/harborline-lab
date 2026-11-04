"""The Salesforce mock over records produced by the real CRM simulator module (finance-crm workstream).

Needs the test Postgres (HL_TEST_PG_PORT). Skips when the CRM module is not available.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from harborline.api import salesforce, soql
from harborline.db import get_db
from harborline.ids import sf_checksum
from harborline.settings import Settings
from harborline.sim import testing

V = "/sfdc/services/data/v67.0"


@pytest.fixture(scope="module")
def client(pg_settings: Settings) -> Iterator[tuple[TestClient, dict[str, str]]]:
    if importlib.util.find_spec("harborline.sim.crm") is None:
        pytest.skip("CRM module not available")
    ctx = testing.make_ctx("small", "build")
    try:
        testing.bootstrap_modules(ctx, ("crm",))
        testing.run_days(ctx, 2, modules=("crm",))
    finally:
        testing.close_ctx(ctx)
    app = FastAPI()
    app.include_router(salesforce.router)
    c = TestClient(app)
    token = c.post("/sfdc/services/oauth2/token", data={
        "grant_type": "client_credentials", "client_id": pg_settings.sf_client_id,
        "client_secret": pg_settings.sf_client_secret}).json()["access_token"]
    yield c, {"Authorization": f"Bearer {token}"}


def test_every_described_field_is_queryable_and_ids_are_valid(client: tuple[TestClient, dict[str, str]]) -> None:
    c, headers = client
    objects = [o["name"] for o in c.get(f"{V}/sobjects", headers=headers).json()["sobjects"]]
    total = 0
    for obj in objects:
        fields = [f["name"] for f in c.get(f"{V}/sobjects/{obj}/describe", headers=headers).json()["fields"]]
        r = c.get(f"{V}/queryAll", params={"q": f"SELECT {', '.join(fields)} FROM {obj}"}, headers=headers)
        assert r.status_code == 200, (obj, r.text[:300])
        for rec in r.json()["records"]:
            total += 1
            for name, value in rec.items():
                if name.endswith("Id") and isinstance(value, str) and len(value) == 18:
                    assert sf_checksum(value[:15]) == value[15:], (obj, name, value)
    assert total > 100


def test_stored_documents_only_use_described_fields(client: tuple[TestClient, dict[str, str]]) -> None:
    schema = soql.base_schema()
    rows = get_db().fetchall("sim_internal", "SELECT object, array_agg(DISTINCT k) FROM sfdc.record, "
                                             "jsonb_object_keys(data) AS k GROUP BY object")
    for obj, keys in rows:
        known = {f.name for f in schema.get(obj).fields}
        assert set(keys) <= known, (obj, sorted(set(keys) - known))


def test_relationship_and_typed_queries_on_real_data(client: tuple[TestClient, dict[str, str]]) -> None:
    c, headers = client
    queries = [
        "SELECT Name, Account.Name, Owner.Name FROM Opportunity WHERE IsClosed = false ORDER BY CloseDate LIMIT 5",
        "SELECT Name, Rate__c, Rate_Card__r.Effective_Date__c FROM Rate_Card_Line__c WHERE Rate__c > 0 LIMIT 5",
        "SELECT Subject, What.Name, Who.Name FROM Task WHERE CreatedDate < TODAY LIMIT 5",
        "SELECT CaseNumber, Account.WMS_Owner_Code__c FROM Case WHERE Status != 'Closed'",
    ]
    for q in queries:
        r = c.get(f"{V}/query", params={"q": q}, headers=headers)
        assert r.status_code == 200, (q, r.text[:300])
    count = c.get(f"{V}/query", params={"q": "SELECT COUNT() FROM Account"}, headers=headers).json()["totalSize"]
    assert count > 0
