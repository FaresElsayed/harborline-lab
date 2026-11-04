"""UAT source extract (harborline/uat_extract.py, /api/uat): periods, scoping, manifest and the zip itself.

Unit tests run anywhere. Database tests share one real Small build (all modules, ~1 minute) against the throwaway
Postgres (HL_TEST_PG_PORT, see harborline/sim/testing.py); the stream test runs last because it moves the clock.
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from harborline import uat_extract as ux
from harborline.db import get_db
from harborline.settings import Settings
from harborline.sim import testing
from harborline.sim.engine import Engine, get_engine, reset_engine

UTC0 = datetime(2026, 4, 1, tzinfo=UTC)


# ================================================================================================== unit: periods
def test_closed_months_respect_every_sites_local_time() -> None:
    clock = datetime(2026, 10, 1, tzinfo=UTC)
    assert ux.closed_months(UTC0, clock, ["UTC"])[-1] == "2026-09"
    # September has not ended yet in New York or Los Angeles at midnight UTC.
    assert ux.closed_months(UTC0, clock, ["America/New_York", "America/Los_Angeles"])[-1] == "2026-08"
    assert ux.closed_months(UTC0, datetime(2026, 10, 1, 7, tzinfo=UTC), ["America/Los_Angeles"])[-1] == "2026-09"
    # A history that starts mid-month only offers the months after it.
    assert ux.closed_months(datetime(2026, 4, 15, tzinfo=UTC), clock, ["UTC"])[0] == "2026-05"
    assert ux.closed_months(UTC0, datetime(2026, 4, 20, tzinfo=UTC), ["UTC"]) == []


def test_default_periods() -> None:
    months = ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08"]
    assert ux.default_periods(months, "two") == ["2026-05", "2026-08"]
    assert ux.default_periods(["2026-07", "2026-08"], "two") == ["2026-07", "2026-08"]  # oldest when history is short
    assert ux.default_periods(["2026-08"], "two") == ["2026-08"]
    assert ux.default_periods(months, "latest") == ["2026-08"]
    assert ux.default_periods(months, "none") == []
    assert ux.default_periods([], "two") == []
    iot_from = datetime(2026, 6, 20, tzinfo=UTC)
    assert ux.default_periods(months, "iot", iot_from) == ["2026-06", "2026-08"]
    assert ux.default_periods(months, "iot", None) == ["2026-05", "2026-08"]  # no readings: like "two"


def test_validate_periods() -> None:
    months = ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08"]
    got = ux.validate_periods(["2026-08", "2026-05", "2026-08"], months, "two")
    assert [p.month for p in got] == ["2026-05", "2026-08"]
    assert got[0].as_json() == {
        "month": "2026-05",
        "start": "2026-05-01",
        "end": "2026-05-31",
        "end_exclusive": "2026-06-01",
    }
    assert [p.month for p in ux.validate_periods(None, months, "two")] == ["2026-05", "2026-08"]
    assert ux.validate_periods(["2026-01"], months, "none") == []
    for bad, msg in (
        (["2026-13"], "not a month"),
        (["Sept"], "not a month"),
        (["2026-09"], "has not closed"),
        (["2026-03"], "before the simulated history"),
        ([], "at least one"),
        (["2026-04", "2026-05", "2026-06", "2026-07"], "at most 3"),
    ):
        with pytest.raises(ValueError, match=msg):
            ux.validate_periods(bad, months, "two")


def test_scope_tokens_expand_to_literal_bounds() -> None:
    scope = ux.Scope([ux.period_of("2026-08")], datetime(2026, 10, 1, tzinfo=UTC), {"trk": ["A"]})
    assert scope.expand("{P:t.ship_ts}") == "((t.ship_ts >= '2026-08-01' AND t.ship_ts < '2026-09-01'))"
    assert "'2026-07-30'" in scope.expand("{PX:t.received_ts}") and "'2026-09-03'" in scope.expand("{PX:t.received_ts}")
    assert scope.expand("{RECENT:t.mod_ts}") == "(t.mod_ts >= '2026-09-22 00:00:00')"
    lo, hi = re.findall(r"\d{13}", scope.expand("{PMS:t.device_ts_ms}"))
    assert datetime.fromtimestamp(int(lo) / 1000, UTC) == datetime(2026, 7, 31, tzinfo=UTC)
    assert datetime.fromtimestamp(int(hi) / 1000, UTC) == datetime(2026, 9, 2, tzinfo=UTC)
    assert scope.bind("x = ANY(%(trk)s) AND y LIKE 'a%%'") == ("x = ANY(%(trk)s) AND y LIKE 'a%%'", {"trk": ["A"]})
    assert scope.bind("y LIKE 'a%%'") == ("y LIKE 'a%'", None)
    assert ux.Scope([], datetime(2026, 10, 1, tzinfo=UTC)).expand("{P:t.x}") == "FALSE"


# ================================================================================================== unit: helpers
def test_row_counter_ignores_newlines_inside_quotes() -> None:
    text = b'id,note\n1,"two\nlines"\n2,"say ""hi""\n"\n3,plain\n'
    counter = ux.RowCounter()
    for i in range(0, len(text), 5):  # arbitrary chunk boundaries, including inside quoted fields
        counter.feed(text[i : i + 5])
    assert counter.rows == 3


def test_parse_940_reads_orders_and_tolerates_bad_envelopes() -> None:
    segs = [
        "ISA*00*          *00*          *ZZ*APEXAUTOPARTS  *ZZ*HARBORLINE3PL  *260924*0333*U*00401*000117237*0*P*>",
        "GS*OW*APEX*HARBORLINE3PL*20260924*0333*9238*X*004010",
        "ST*940*0001",
        "W05*N*SO-1*APE1*",
        "N1*DE*APEX*ZZ*APEXAUT",
        "N1*WH*ATLANTA*ZZ*ATL1",
        "N1*ST*SOMEONE",
        "N9*PO*APE1",
        "G62*10*20260925",
        "W66*PP*M********ODFL",
        "LX*1",
        "W01*3*EA*0123*VN*SKU1",
        "LX*2",
        "W01*1*EA**VN*SKU2",
        "W76*4*10.5*LB",
        "SE*99*0001",  # wrong SE count
        "ST*940*0002",
        "W05*N*SO-2*APE2",
        "N1*WH*ATLANTA*ZZ*ATL1",
        "LX*1",
        "W76*7",  # missing SE
    ]
    rows = ux.parse_940(("~\r\n".join(segs) + "~").encode())
    assert [r["client_order_ref"] for r in rows] == ["APE1", "APE2"]
    first = rows[0]
    assert first["isa_sender_id"] == "APEXAUTOPARTS" and first["isa_control_nbr"] == "000117237"
    assert (first["depositor_code"], first["warehouse_code"], first["scac"], first["lines"]) == (
        "APEXAUT",
        "ATL1",
        "ODFL",
        2,
    )
    assert (first["units"], first["weight"], first["weight_uom"], first["requested_ship_date"]) == (
        "4",
        "10.5",
        "LB",
        "20260925",
    )
    assert "SOMEONE" not in json.dumps(rows)  # no consignee data
    assert rows[1]["units"] == "7" and rows[1]["lines"] == 1


def test_refusal_reasons() -> None:
    built = {
        "built_at": "2026-10-01T00:00:00+00:00",
        "sim_clock": "2026-10-01T00:00:00+00:00",
        "stream": {"running": False},
    }
    assert ux.ExtractService.refusal({**built, "state": "idle"}) is None
    assert ux.ExtractService.refusal({**built, "state": "error"}) is None  # still data, old error
    assert ux.ExtractService.refusal({**built, "state": "streaming", "stream": {"running": True}})[0] == "streaming"
    assert ux.ExtractService.refusal({**built, "state": "building"})[0] == "building"
    assert ux.ExtractService.refusal({**built, "state": "running_batch"})[0] == "busy"
    assert (
        ux.ExtractService.refusal({"state": "idle", "built_at": None, "sim_clock": None, "stream": {}})[0]
        == "not_built"
    )
    assert (
        "Stop the stream" in ux.ExtractService.refusal({**built, "state": "streaming", "stream": {"running": True}})[1]
    )


def test_every_project_is_mapped() -> None:
    assert list(ux.PROJECTS) == [f"P{i}" for i in range(1, 9)]
    dashboards = [d for p in ux.PROJECTS.values() for d, _ in p.dashboards]
    assert len(dashboards) == 15 and len(set(dashboards)) == 15
    for p in ux.PROJECTS.values():
        paths = [s.path for s in p.files]
        assert len(paths) == len(set(paths)), p.key
        for s in p.files:
            system, name = s.path.split("/")
            assert system in ("wms", "ax2012", "iot", "salesforce", "carrier") and name.endswith(".csv") and s.scope
        assert "Contact.csv" not in " ".join(paths)  # personal data stays out


# ================================================================================================== database tests
pytestmark_db = pytest.mark.skipif(
    not testing.pg_configured(), reason="set HL_TEST_PG_PORT (see harborline/sim/testing.py)"
)


@pytest.fixture(scope="module")
def engine(pg_settings: Settings) -> Iterator[Engine]:
    """A real Small build with every simulator module, shared by the database tests of this module."""
    with get_db().connect("sim_internal") as conn:
        conn.execute("DELETE FROM sim.state")
        conn.execute("TRUNCATE sim.run_log, sim.anomaly_log RESTART IDENTITY")
        conn.commit()
    reset_engine()
    eng = get_engine()
    eng.lock_wait_s = 5
    eng.load()
    eng.start_build("small", testing.TEST_SEED)
    assert eng._build_thread is not None
    eng._build_thread.join(900)
    assert eng.error is None and eng.built_at is not None, eng.error
    yield eng
    eng.shutdown()
    reset_engine()


@pytest.fixture(scope="module")
def service(engine: Engine, tmp_path_factory: pytest.TempPathFactory) -> ux.ExtractService:
    return ux.reset_service(tmp_path_factory.mktemp("uat-extracts"))


@pytest.fixture(scope="module")
def client(service: ux.ExtractService) -> Iterator[TestClient]:
    from harborline.api.main import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture(scope="module")
def extracts(engine: Engine, service: ux.ExtractService) -> dict[str, tuple[ux.ExtractInfo, zipfile.ZipFile]]:
    """One extract per project with the default periods (built directly through the service, read into memory
    because later builds prune old zips from the folder)."""
    out = {}
    for key in ux.PROJECTS:
        info = service.build(key, None, engine)
        out[key] = (info, zipfile.ZipFile(io.BytesIO(info.path.read_bytes())))
    return out


def read_csv(zf: zipfile.ZipFile, path: str) -> list[dict[str, str]]:
    with zf.open(path) as fh:
        return list(csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8", newline="")))


def manifest(zf: zipfile.ZipFile) -> dict[str, Any]:
    return json.loads(zf.read("manifest.json"))


def source(db: str, sql: str, params: Any = None) -> list[tuple]:
    with get_db().connect(db, "reviewer", autocommit=True) as conn:
        return conn.execute(sql, params).fetchall()


@pytestmark_db
def test_options_report_ready_months_and_defaults(client: TestClient, engine: Engine) -> None:
    opts = client.get("/api/uat/extract/options").json()
    assert opts["ready"] is True and opts["reason"] is None and opts["max_periods"] == 3
    assert opts["sim_clock"].startswith("2026-10-01") and opts["preset"] == "small"
    # Small history: April to September 2026; September has not closed in the US sites' local time.
    assert opts["months"] == ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08"]
    by_key = {p["key"]: p for p in opts["projects"]}
    assert list(by_key) == list(ux.PROJECTS)
    assert by_key["P2"]["default_periods"] == ["2026-05", "2026-08"]
    assert by_key["P8"]["default_periods"] == ["2026-08"]
    assert by_key["P6"]["default_periods"] == [] and by_key["P6"]["uses_periods"] is False
    assert by_key["P2"]["dashboards"] == [
        {"id": "P2-D1", "name": "Warehouse Ops Scorecard"},
        {"id": "P2-D2", "name": "Site Operations Detail"},
    ]
    assert all(p["not_in_extract"] for p in opts["projects"])
    assert opts["sensor_readings_from"] is not None


@pytestmark_db
@pytest.mark.parametrize("key", list(ux.PROJECTS))
def test_manifest_matches_files_and_row_counts(extracts: dict[str, Any], key: str) -> None:
    info, zf = extracts[key]
    assert zf.testzip() is None
    man = manifest(zf)
    assert man["extract_version"] == ux.EXTRACT_VERSION and man["project"]["key"] == key
    assert man["sim_clock"] == "2026-10-01T00:00:00Z" and man["preset"] == "small" and man["seed"] == testing.TEST_SEED
    assert [p["month"] for p in man["periods"]] == info.periods
    members = set(zf.namelist())
    assert members == {"manifest.json", *(f["path"] for f in man["files"])}
    assert [f["path"] for f in man["files"]] == [f["path"] for f in info.files]
    for f in man["files"]:
        assert {"path", "system", "object", "scope", "time_basis", "rows", "columns"} <= f.keys()
        assert f["bytes"] == zf.getinfo(f["path"]).file_size
        if not f["path"].endswith(".csv"):
            assert f["system"] == "files" and f["source"].startswith("landing/files/")
            continue
        with zf.open(f["path"]) as fh:
            rows = list(csv.reader(io.TextIOWrapper(fh, encoding="utf-8", newline="")))
        assert rows[0] == f["columns"], f["path"]
        assert len(rows) - 1 == f["rows"], f["path"]
        for omitted in f.get("columns_omitted", []):
            assert omitted not in rows[0]
    assert man["notes"] and man["not_in_extract"]


@pytestmark_db
def test_values_are_written_as_stored(extracts: dict[str, Any]) -> None:
    _, zf = extracts["P2"]
    order = read_csv(zf, "wms/order_header.csv")[0]
    stored = source(
        "wms",
        "SELECT received_ts::text, is_residential::text, mod_ts::text FROM wms.order_header WHERE order_id = %s",
        (int(order["order_id"]),),
    )[0]
    assert (order["received_ts"], order["mod_ts"]) == (stored[0], stored[2])  # local time, no zone, no rounding
    assert order["is_residential"] in ("t", "f", "")
    assert "ship_to_name" not in order and "ship_to_email" not in order and "ship_to_postal" in order
    assert "first_name" not in read_csv(zf, "wms/app_user.csv")[0]
    _, zf3 = extracts["P3"]
    rate = read_csv(zf3, "ax2012/exchangerate.csv")[0]
    assert re.fullmatch(r"-?\d+\.\d{16}", rate["exchangerate"])  # numeric(32,16) untouched
    acct = read_csv(zf3, "salesforce/Account.csv")[0]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", acct["CreatedDate"])
    assert acct["IsDeleted"] == "false"


@pytestmark_db
def test_child_rows_follow_their_parents(extracts: dict[str, Any]) -> None:
    _, zf = extracts["P2"]
    orders = {r["order_id"]: r for r in read_csv(zf, "wms/order_header.csv")}
    lines = read_csv(zf, "wms/order_line.csv")
    shipments = read_csv(zf, "wms/shipment.csv")
    cartons = read_csv(zf, "wms/shipment_carton.csv")
    receipts = {r["receipt_id"] for r in read_csv(zf, "wms/receipt_header.csv")}
    assert orders and lines and shipments and cartons and receipts
    assert {r["order_id"] for r in lines} <= set(orders)
    assert {r["order_id"] for r in shipments} <= set(orders)
    assert {r["shipment_id"] for r in cartons} <= {r["shipment_id"] for r in shipments}
    assert {r["receipt_id"] for r in read_csv(zf, "wms/receipt_line.csv")} <= receipts
    # Every line of an included order is there (children are complete, not sampled).
    n_lines = source(
        "wms", "SELECT count(*) FROM wms.order_line WHERE order_id = ANY(%s)", ([int(o) for o in orders],)
    )[0][0]
    assert n_lines == len(lines)
    # Every order received in a period is in the extract, and every included order meets the scope rule.
    expected = {
        str(r[0])
        for r in source(
            "wms",
            "SELECT order_id FROM wms.order_header WHERE (received_ts >= '2026-05-01' "
            "AND received_ts < '2026-06-01') OR (received_ts >= '2026-08-01' AND received_ts < '2026-09-01')",
        )
    }
    assert expected <= set(orders)
    shipped = {r["order_id"] for r in shipments if r["ship_ts"][:7] in ("2026-05", "2026-08")}
    for oid, o in orders.items():
        assert (
            o["received_ts"][:7] in ("2026-05", "2026-08")
            or (o["requested_ship_date"] or "")[:7] in ("2026-05", "2026-08")
            or o["order_status"] not in ("SHIPPED", "CANCELLED")
            or oid in shipped
        ), oid


@pytestmark_db
def test_cross_system_scope_freight(extracts: dict[str, Any]) -> None:
    _, zf = extracts["P4"]
    bills = read_csv(zf, "ax2012/vendinvoicejour.csv")
    lines = read_csv(zf, "ax2012/vendinvoicetrans.csv")
    shipments = read_csv(zf, "wms/shipment.csv")
    cartons = read_csv(zf, "wms/shipment_carton.csv")
    assert bills and all(b["invoicedate"][:7] in ("2026-05", "2026-08") for b in bills)
    assert {(r["dataareaid"], r["internalinvoiceid"]) for r in lines} <= {
        (b["dataareaid"], b["internalinvoiceid"]) for b in bills
    }
    known = (
        {s["tracking_nbr"] for s in shipments}
        | {s["shipment_nbr"] for s in shipments}
        | {c["tracking_nbr"] for c in cartons}
    )
    refs = [m.group(1) for r in lines if (m := re.search(r"(?:TRK|PRO) ([^ ]+)", r["name"]))]
    assert refs and all(ref in known for ref in refs)
    events = read_csv(zf, "carrier/tracking_event.csv")
    assert events and {e["tracking_nbr"] for e in events} <= known
    assert all(e["received_at"] <= "2026-10-01T00:00:00Z" and e["is_correction"] in ("true", "false") for e in events)
    assert list(events[0]) == [
        "event_id",
        "tracking_nbr",
        "scac",
        "event_code",
        "event_local_ts",
        "description",
        "exception_code",
        "city",
        "state_prov",
        "country_code",
        "received_at",
        "is_correction",
    ]
    assert any(f.startswith("files/") and f.endswith(".xlsx") and "_rates_" in f for f in zf.namelist())


@pytestmark_db
def test_salesforce_files_use_the_api_field_names(
    extracts: dict[str, Any], client: TestClient, pg_settings: Settings
) -> None:
    _, zf = extracts["P5"]
    token = client.post(
        "/sfdc/services/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": pg_settings.sf_client_id,
            "client_secret": pg_settings.sf_client_secret,
        },
    ).json()
    body = client.get(
        "/sfdc/services/data/v67.0/query",
        params={"q": "SELECT FIELDS(ALL) FROM Account ORDER BY Id LIMIT 200"},
        headers={"Authorization": f"Bearer {token['access_token']}"},
    ).json()
    api = body["records"][0]
    rows = read_csv(zf, "salesforce/Account.csv")
    assert list(rows[0]) == [k for k in api if k != "attributes"]
    first = next(r for r in rows if r["Id"] == api["Id"])
    assert first["Name"] == api["Name"] and first["WMS_Owner_Code__c"] == (api["WMS_Owner_Code__c"] or "")
    assert "SuppliedEmail" not in read_csv(zf, "salesforce/Case.csv")[0]
    n_api = client.get(
        "/sfdc/services/data/v67.0/query",
        params={"q": "SELECT COUNT() FROM Account"},
        headers={"Authorization": f"Bearer {token['access_token']}"},
    ).json()["totalSize"]
    assert n_api == len(rows)
    edi = read_csv(zf, "edi/inbound_940_orders.csv")
    assert edi and all(r["client_order_ref"] for r in edi)


@pytestmark_db
def test_project_specific_scopes(extracts: dict[str, Any]) -> None:
    _, p6 = extracts["P6"]
    man = manifest(p6)
    assert man["periods"] == [] and man["recent_window"]["end"] == "2026-10-01T00:00:00Z"
    assert {r["inv_status"] for r in read_csv(p6, "wms/inventory.csv")} <= {"HOLD"}
    _, p7 = extracts["P7"]
    sensors = {r["sensor_id"]: r for r in read_csv(p7, "iot/sensor.csv")}
    readings = read_csv(p7, "iot/sensor_reading.csv")
    assert all(sensors[r["sensor_id"]]["zone_code"].split("-")[1] in ("COOLER", "FREEZER") for r in readings)
    assert any("Sensor readings in the source run from" in n for n in manifest(p7)["notes"])
    _, p1 = extracts["P1"]
    stats = read_csv(p1, "platform/table_stats.csv")
    by_table = {(r["system"], r["table"]): r for r in stats}
    assert (
        int(by_table[("wms", "order_header")]["rows"]) == source("wms", "SELECT count(*) FROM wms.order_header")[0][0]
    )
    assert by_table[("wms", "order_header")]["audit_column"] == "mod_ts"
    assert ("salesforce", "Account") in by_table and ("carrier", "tracking_event") in by_table
    activity = read_csv(p1, "platform/daily_activity.csv")
    days = {r["day"][:7] for r in activity if r["table"] == "order_header"}
    assert {"2026-05", "2026-08", "2026-09"} <= days  # periods plus the weeks before the clock
    _, p3 = extracts["P3"]
    assert {"files/budget_FY2026.xlsx"} <= set(p3.namelist())


@pytestmark_db
def test_api_build_download_and_errors(client: TestClient) -> None:
    resp = client.post("/api/uat/extract", json={"project": "p3", "periods": ["2026-07"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["project"] == "P3" and body["periods"] == ["2026-07"] and body["sim_clock"] == "2026-10-01T00:00:00Z"
    assert re.fullmatch(r"[0-9a-f]{12}", body["id"]) and body["filename"].endswith(".zip")
    assert {"path", "rows", "bytes"} <= body["files"][0].keys()
    dl = client.get(body["download_url"])
    assert dl.status_code == 200 and dl.headers["content-type"] == "application/zip"
    assert body["filename"] in dl.headers["content-disposition"]
    zf = zipfile.ZipFile(io.BytesIO(dl.content))
    assert manifest(zf)["periods"][0]["month"] == "2026-07"
    assert any(r["id"] == body["id"] for r in client.get("/api/uat/extract/options").json()["recent"])

    assert client.post("/api/uat/extract", json={"project": "P9"}).status_code == 400
    bad = client.post("/api/uat/extract", json={"project": "P2", "periods": ["2026-09"]})
    assert bad.status_code == 400 and "has not closed" in bad.json()["detail"]
    assert (
        client.post(
            "/api/uat/extract", json={"project": "P2", "periods": ["2026-04", "2026-05", "2026-06", "2026-07"]}
        ).status_code
        == 400
    )
    assert client.get("/api/uat/extract/0123456789ab").status_code == 404
    assert client.get("/api/uat/extract/..%2Fsecret").status_code == 404


@pytestmark_db
def test_only_the_last_extracts_are_kept(
    engine: Engine, service: ux.ExtractService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ux, "KEEP_EXTRACTS", 2)
    built = [service.build("P1", ["2026-08"], engine) for _ in range(3)]
    kept = [e.id for e in service.list()]
    assert len(kept) == 2 and built[0].id not in kept and not built[0].path.exists()


@pytestmark_db
def test_one_extract_at_a_time(engine: Engine, service: ux.ExtractService) -> None:
    assert service._lock.acquire(blocking=False)
    try:
        with pytest.raises(ux.ExtractRefused) as exc:
            service.build("P1", None, engine)
        assert exc.value.code == "extracting"
    finally:
        service._lock.release()


@pytestmark_db
def test_sources_are_read_through_the_read_only_reviewer_login(engine: Engine, pg_settings: Settings) -> None:
    project = ux.PROJECTS["P2"]
    ex = ux.Extractor(get_db(), pg_settings, project, ux.Scope([], datetime(2026, 10, 1, tzinfo=UTC)), engine.status())
    try:
        for db in ux.SOURCE_DBS:
            conn = ex.source_conn(db)
            user, ro = conn.execute("SELECT current_user, current_setting('transaction_read_only')").fetchone()
            assert (user, ro) == (pg_settings.reviewer_user, "on")
        with pytest.raises(psycopg.Error):
            ex.source_conn("wms").execute("UPDATE wms.warehouse SET warehouse_name = warehouse_name")
        with pytest.raises(psycopg.Error):
            ex.source_conn("ax2012").execute("CREATE TEMP TABLE x (a int)")
        assert ex.internal_conn().execute("SELECT current_setting('transaction_read_only')").fetchone()[0] == "on"
    finally:
        ex.close()


@pytestmark_db
def test_zz_refuses_with_409_while_the_stream_runs(client: TestClient, engine: Engine) -> None:
    """Runs last in this module: the stream moves the clock (by a fraction of a second here)."""
    engine.start_stream(interval_s=300, speed=0.001)
    try:
        opts = client.get("/api/uat/extract/options").json()
        assert opts["ready"] is False and opts["reason_code"] == "streaming" and "Stop the stream" in opts["reason"]
        resp = client.post("/api/uat/extract", json={"project": "P2"})
        assert resp.status_code == 409 and resp.headers["x-reason-code"] == "streaming"
        assert "Stop the stream" in resp.json()["detail"]
    finally:
        engine.stop_stream()
    assert client.get("/api/uat/extract/options").json()["ready"] is True


def test_zip_path_layout_constants() -> None:
    assert ux.SCHEMA_OF == {"wms": "wms", "ax2012": "dbo", "iot": "telemetry"}
    assert Path(ux.ExtractService().folder).name == "harborline-uat-extracts"
