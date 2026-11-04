"""Bootstrap idempotency, grants, publications, rebuild wipe, secondary indexes and copy_rows round trips."""

from __future__ import annotations

import time
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import psycopg
import pytest

from harborline.db import PUBLICATIONS, get_db, split_sql
from harborline.settings import Settings
from harborline.sim import testing
from harborline.sim.engine import EngineCtx

SOURCE_SCHEMAS = {"wms": "wms", "ax2012": "dbo", "iot": "telemetry"}


def _tables(db: str, schema: str) -> set[str]:
    return {r[0] for r in get_db().fetchall(db, "SELECT tablename FROM pg_tables WHERE schemaname = %s", (schema,))}


def test_bootstrap_is_idempotent(pg_settings: Settings) -> None:
    before = {db: _tables(db, schema) for db, schema in SOURCE_SCHEMAS.items()}
    get_db().bootstrap()
    get_db().bootstrap()
    after = {db: _tables(db, schema) for db, schema in SOURCE_SCHEMAS.items()}
    assert before == after
    assert {"order_header", "order_line", "inventory_txn", "billing_rate"} <= after["wms"]
    assert {"custtable", "custtrans", "generaljournalaccountentry"} <= after["ax2012"]
    assert {"sensor_reading", "dock_door_event"} <= after["iot"]
    assert {"record", "state", "run_log", "anomaly_log", "signal_cache"} <= _tables("sim_internal", "sfdc") | _tables(
        "sim_internal", "sim")


def test_every_source_table_has_a_primary_key(pg_settings: Settings) -> None:
    for db, schema in SOURCE_SCHEMAS.items():
        missing = get_db().fetchall(db, """
            SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relkind = 'r'
              AND NOT EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conrelid = c.oid AND k.contype = 'p')""", (schema,))
        assert missing == [], f"{db}: tables without a primary key (CDC deletes need one): {missing}"


def test_etl_reader_can_read_sources_but_not_the_simulator_store(pg_settings: Settings) -> None:
    db = get_db()
    for name, schema in SOURCE_SCHEMAS.items():
        with db.connect(name, "etl") as conn:
            table = min(_tables(name, schema))
            conn.execute(f'SELECT count(*) FROM {schema}."{table}"').fetchone()
            assert conn.execute("SELECT rolreplication FROM pg_roles WHERE rolname = current_user").fetchone()[0]
    with pytest.raises(psycopg.OperationalError):
        db.connect("sim_internal", "etl").close()


def test_uat_reviewer_reads_sources_read_only_and_nothing_else(pg_settings: Settings) -> None:
    db = get_db()
    for name, schema in SOURCE_SCHEMAS.items():
        with db.connect(name, "reviewer") as conn:
            table = min(_tables(name, schema))
            conn.execute(f'SELECT count(*) FROM {schema}."{table}"').fetchone()
            assert conn.execute("SELECT rolreplication FROM pg_roles WHERE rolname = current_user").fetchone()[0] is False
            assert conn.execute("SHOW default_transaction_read_only").fetchone()[0] == "on"
            assert conn.execute("SHOW statement_timeout").fetchone()[0] == "5min"
            with pytest.raises((psycopg.errors.ReadOnlySqlTransaction, psycopg.errors.InsufficientPrivilege)):
                conn.execute(f'DELETE FROM {schema}."{table}"')
            conn.rollback()
    with pytest.raises(psycopg.OperationalError):
        db.connect("sim_internal", "reviewer").close()
    with db.connect("tracker", "reviewer") as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("SELECT count(*) FROM tracker.issue")


def test_publications_cover_their_schemas_after_a_wipe(pg_settings: Settings) -> None:
    db = get_db()
    db.wipe_source_data()
    for name, (pub, schema) in PUBLICATIONS.items():
        rows = db.fetchall(name, "SELECT schemaname, count(*) FROM pg_publication_tables WHERE pubname = %s GROUP BY 1",
                           (pub,))
        assert rows and rows[0][0] == schema and rows[0][1] == len(_tables(name, schema))


def test_secondary_indexes_are_idempotent(pg_settings: Settings) -> None:
    db = get_db()
    first = db.create_secondary_indexes()
    assert first == db.create_secondary_indexes() > 40
    names = {r[0] for r in db.fetchall("wms", "SELECT indexname FROM pg_indexes WHERE schemaname = 'wms'")}
    assert {"hlx_order_header_mod_ts", "hlx_inventory_txn_txn_ts", "hlx_order_line_material_id"} <= names


def test_split_sql_respects_quotes_comments_and_dollar_quoting() -> None:
    text = """
    -- a comment; with a semicolon
    CREATE TABLE a (x int); COMMENT ON TABLE a IS 'it''s; fine';
    /* block; comment */ CREATE FUNCTION f() RETURNS int AS $$ SELECT 1; $$ LANGUAGE sql;
    SELECT ';' AS "semi;colon"
    """
    stmts = split_sql(text)
    assert len(stmts) == 4
    assert stmts[1].endswith("'it''s; fine'")
    assert "$$ SELECT 1; $$" in stmts[2]


# --------------------------------------------------------------------------- copy_rows
def test_copy_rows_round_trips_pandas3_dtypes(ctx: EngineCtx) -> None:
    ts = pd.Timestamp("2026-04-01 08:30:00")
    owner = pd.DataFrame({
        "owner_id": [1], "owner_code": ["ACME"], "owner_name": ["Acme"], "owner_status": ["ACTIVE"],
        "create_ts": [ts], "mod_ts": [ts],
    })
    material = pd.DataFrame({
        "material_id": np.array([1, 2, 3], dtype=np.int64),
        "owner_id": [1, 1, 1],
        "sku": pd.Series(["A-1", "B,2", 'C"3'], dtype="str"),
        "description": ["Plain", "Café crème, 12 × 330 ml", "line\nbreak"],
        "upc": pd.Series(["0123", None, ""], dtype="str"),          # NaN -> NULL, "" stays ""
        "unit_weight": [1.25, np.nan, 3.0],                          # numeric with NULL
        "case_qty": [12.0, np.nan, 6.0],                             # float -> integer column
        "cases_per_pallet": pd.array([40, None, 10], dtype="Int64"),
        "shelf_life_days": pd.Series([None, 365, None], dtype=object),
        "is_lot_controlled": [True, False, True],
        "create_ts": [ts, ts, pd.NaT],
        "mod_ts": [ts, ts, ts],
    })
    material.loc[2, "create_ts"] = ts
    assert ctx.copy_rows("wms", "owner", owner) == 1
    assert ctx.copy_rows("wms", "material", material) == 3
    assert material["case_qty"].dtype == np.float64            # the caller's frame is untouched
    got = ctx.read_df("wms", "SELECT * FROM material ORDER BY material_id")
    assert got["sku"].tolist() == ["A-1", "B,2", 'C"3']
    assert got["description"].tolist()[1:] == ["Café crème, 12 × 330 ml", "line\nbreak"]
    assert got["upc"].iloc[0] == "0123" and pd.isna(got["upc"].iloc[1]) and got["upc"].iloc[2] == ""
    assert got["case_qty"].tolist()[0] == 12 and pd.isna(got["case_qty"].iloc[1])
    assert got["cases_per_pallet"].iloc[0] == 40 and pd.isna(got["cases_per_pallet"].iloc[1])
    assert got["shelf_life_days"].iloc[1] == 365
    assert got["is_lot_controlled"].tolist() == [True, False, True]
    assert got["create_ts"].iloc[0] == datetime(2026, 4, 1, 8, 30)
    ctx.rollback()


def test_copy_rows_timestamptz_json_and_qualified_tables(ctx: EngineCtx) -> None:
    t = pd.Timestamp("2026-04-01 12:00:00", tz="UTC")
    readings = pd.DataFrame({
        "reading_id": [1, 2], "sensor_id": ["CHI1-CLR-03-T", "CHI1-CLR-03-T"],
        "device_ts_ms": [int(t.timestamp() * 1000)] * 2, "value": [4.0, np.nan], "battery_pct": [99, None],
        "ingested_at": [t, t.tz_convert("America/Chicago")],
    })
    assert ctx.copy_rows("iot", "sensor_reading", readings) == 2
    got = ctx.read_df("iot", "SELECT value, battery_pct, ingested_at FROM sensor_reading ORDER BY reading_id")
    assert got["ingested_at"].tolist() == [datetime(2026, 4, 1, 12, tzinfo=UTC)] * 2
    assert pd.isna(got["value"].iloc[1]) and pd.isna(got["battery_pct"].iloc[1])

    record = pd.DataFrame({
        "id": ["001000000000001AAA"], "object": ["Account"], "data": [{"Name": "Ünïcode, \"quoted\"", "n": [1, 2]}],
        "created_date": [t], "last_modified_date": [t], "system_modstamp": [t],
    })
    assert ctx.copy_rows("sim_internal", "sfdc.record", record) == 1
    data = ctx.read_df("sim_internal", "SELECT data FROM sfdc.record")["data"].iloc[0]
    assert data == {"Name": "Ünïcode, \"quoted\"", "n": [1, 2]}

    dates = pd.DataFrame({"id": ["us01"], "name": ["USA"], "recid": [5637144576]})
    assert ctx.copy_rows("ax2012", "dbo.dataarea", dates) == 1
    ctx.rollback()


def test_copy_rows_rejects_unknown_columns_and_ignores_empty_frames(ctx: EngineCtx) -> None:
    assert ctx.copy_rows("wms", "warehouse", pd.DataFrame()) == 0
    with pytest.raises(ValueError, match="no column"):
        ctx.copy_rows("wms", "warehouse", pd.DataFrame({"nope": [1]}))
    with pytest.raises(ValueError, match="does not exist"):
        ctx.copy_rows("wms", "no_such_table", pd.DataFrame({"x": [1]}))
    ctx.rollback()


def test_copy_rows_bulk_speed(ctx: EngineCtx) -> None:
    n = 200_000
    rng = np.random.default_rng(0)
    frame = pd.DataFrame({
        "txn_id": np.arange(1, n + 1), "txn_ts": pd.Timestamp("2026-04-01") + pd.to_timedelta(rng.integers(0, 86400, n), "s"),
        "warehouse_id": 1, "owner_id": rng.integers(1, 60, n), "material_id": rng.integers(1, 5000, n),
        "txn_type": rng.choice(["PICK", "RECEIVE", "MOVE"], n), "qty": rng.integers(-20, 20, n),
        "lot_nbr": pd.Series(rng.choice(["L1", None], n), dtype="str"), "create_ts": pd.Timestamp("2026-04-01"),
    })
    t = time.perf_counter()
    assert ctx.copy_rows("wms", "inventory_txn", frame) == n
    elapsed = time.perf_counter() - t
    ctx.commit()
    assert ctx.read_df("wms", "SELECT count(*) AS n FROM inventory_txn")["n"].iloc[0] == n
    assert elapsed < 10, f"COPY of {n} rows took {elapsed:.1f}s"


def test_run_window_measures_inserts_updates_deletes(ctx: EngineCtx, fake_module) -> None:
    boot = testing.bootstrap_modules(ctx, [fake_module])
    assert boot.as_list() == [{"system": "wms", "table": "warehouse", "ins": 1, "upd": 0, "del": 0}]
    changes = {(c["system"], c["table"]): c for c in testing.run_window(ctx, fake_module, testing.day_window(ctx)).as_list()}
    assert changes[("wms", "inventory_txn")] == {"system": "wms", "table": "inventory_txn", "ins": 96, "upd": 0, "del": 1}
    assert changes[("wms", "warehouse")]["upd"] == 1 and changes[("wms", "warehouse")]["ins"] == 0
    assert changes[("files", "fake_report.csv")]["ins"] == 1
    assert ctx.settings.anomalies_csv.read_text().count("negative_qty") == 1
