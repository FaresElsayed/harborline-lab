"""WMS / IoT / carrier modules against a real Postgres (HL_TEST_PG_PORT, see harborline/sim/testing.py).

One Small build (6 months, wms + iot + carrier) is shared by the module; later tests continue it in batch, stream
and scenario mode, so the order of the tests matters.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pandas as pd
import pytest

from harborline.core import ChangeCounter, Window
from harborline.sim import iot, scenarios, testing, wms
from harborline.sim.engine import EngineCtx

MODS = ("wms", "iot", "carrier")
pytestmark = pytest.mark.skipif(not testing.pg_configured(), reason="set HL_TEST_PG_PORT (see harborline/sim/testing.py)")


@pytest.fixture(scope="module")
def built() -> Iterator[EngineCtx]:
    testing.use_test_database()
    ctx = testing.make_ctx("small", "build")
    testing.bootstrap_modules(ctx, MODS)
    days = (ctx.history_end - ctx.history_start).days
    testing.run_days(ctx, days, modules=MODS)
    yield ctx
    testing.close_ctx(ctx)


def q(ctx: EngineCtx, sql: str, db: str = "wms", params: Any = None) -> pd.DataFrame:
    return testing.fetch_df(ctx, db, sql, params)


def logged_keys(ctx: EngineCtx, obj: str, categories: tuple[str, ...]) -> set[str]:
    df = q(ctx, "SELECT record_key FROM sim.anomaly_log WHERE object = %s AND category = ANY(%s)", "sim_internal",
           (obj, list(categories)))
    return set(df["record_key"])


class _Combo:
    """Runs all three domain modules (and optionally a scenario first) inside one engine window."""

    name = "wms_domain"

    def __init__(self, scenario: tuple[str, dict[str, Any]] | None = None) -> None:
        self.scenario = scenario
        self.notes: list[str] = []

    def step(self, ctx: EngineCtx, win: Window) -> None:
        mods = [testing.resolve_module(m) for m in MODS]
        if self.scenario:
            sid, params = self.scenario
            for m in mods:
                msg = m.apply_scenario(ctx, sid, params, win)
                if msg:
                    self.notes.append(msg)
        for m in mods:
            m.step(ctx, win)


def run(ctx: EngineCtx, hours: float, combo: _Combo | None = None) -> ChangeCounter:
    start = ctx.window.end
    win = Window(start, start + timedelta(hours=hours))
    return testing.run_window(ctx, combo or _Combo(), win)


# ------------------------------------------------------------------------------------------------ build integrity
def test_build_volumes(built: EngineCtx) -> None:
    counts = q(built, "SELECT (SELECT count(*) FROM wms.order_header) o, (SELECT count(*) FROM wms.order_line) l, "
                      "(SELECT count(*) FROM wms.inventory_txn) t, (SELECT count(*) FROM wms.shipment) s").iloc[0]
    assert counts["o"] > 20_000 and counts["l"] > 2 * counts["o"] and counts["t"] > 3 * counts["o"]
    assert counts["s"] > 0.85 * counts["o"]
    ev = q(built, "SELECT count(*) n FROM carrier.tracking_event", "sim_internal")["n"].iloc[0]
    assert ev > 4 * counts["s"]
    rd = q(built, "SELECT count(*) n, min(ingested_at) lo FROM telemetry.sensor_reading", "iot").iloc[0]
    assert rd["n"] > 100_000


def test_inventory_equals_ledger_except_logged(built: EngineCtx) -> None:
    diff = q(built, """
        WITH led AS (SELECT warehouse_id, material_id, coalesce(lot_nbr, '') lot, sum(qty) q FROM wms.inventory_txn GROUP BY 1, 2, 3),
             inv AS (SELECT warehouse_id, material_id, coalesce(lot_nbr, '') lot, sum(qty_on_hand) q FROM wms.inventory GROUP BY 1, 2, 3)
        SELECT warehouse_id, material_id, lot FROM led FULL JOIN inv USING (warehouse_id, material_id, lot)
        WHERE coalesce(led.q, 0) <> coalesce(inv.q, 0)""")
    bad_txn = logged_keys(built, "inventory_txn", ("duplicate_row", "negative_qty", "dst_duplicate"))
    keys = q(built, "SELECT DISTINCT warehouse_id, material_id, coalesce(lot_nbr, '') lot FROM wms.inventory_txn WHERE txn_id = ANY(%s)",
             params=([int(k) for k in bad_txn],))
    allowed = set(map(tuple, keys.to_numpy().tolist()))
    unexplained = [k for k in map(tuple, diff.to_numpy().tolist()) if k not in allowed]
    assert not unexplained, unexplained[:10]
    assert q(built, "SELECT count(*) n FROM wms.inventory WHERE qty_on_hand < 0 OR qty_allocated < 0 "
                    "OR qty_allocated > qty_on_hand")["n"].iloc[0] == 0


def test_every_shipment_has_an_order(built: EngineCtx) -> None:
    n = q(built, "SELECT count(*) n FROM wms.shipment s LEFT JOIN wms.order_header o USING (order_id) WHERE o.order_id IS NULL")
    assert n["n"].iloc[0] == 0
    assert q(built, "SELECT count(*) n FROM wms.shipment WHERE shipment_status IN ('SHIPPED', 'DELIVERED', 'EXCEPTION') "
                    "AND ship_ts IS NULL")["n"].iloc[0] == 0


def test_shipped_not_more_than_ordered_except_logged(built: EngineCtx) -> None:
    over = q(built, "SELECT order_id || '-' || line_nbr k FROM wms.order_line WHERE shipped_qty > ordered_qty")
    assert set(over["k"]) <= logged_keys(built, "order_line", ("shipped_gt_ordered",))


def test_order_lifecycle_is_consistent(built: EngineCtx) -> None:
    st = q(built, "SELECT order_status, count(*) n FROM wms.order_header GROUP BY 1").set_index("order_status")["n"]
    assert st["SHIPPED"] > 0.85 * st.sum()
    assert st.get("CANCELLED", 0) < 0.05 * st.sum()
    # shipped orders: ship time after receipt, never before the carrier pickup cutoff day
    bad = q(built, "SELECT count(*) n FROM wms.order_header o JOIN wms.shipment s USING (order_id) WHERE s.ship_ts < o.received_ts")
    assert bad["n"].iloc[0] == 0
    # B2C orders received before 10:00 on a business day usually ship the same day
    same = q(built, """SELECT avg((s.ship_ts::date = o.received_ts::date)::int) r FROM wms.order_header o JOIN wms.shipment s USING (order_id)
                       WHERE o.order_type = 'B2C' AND extract(hour FROM o.received_ts) < 10 AND extract(isodow FROM o.received_ts) < 5""")
    assert same["r"].iloc[0] > 0.6
    # local times: no WMS timestamp is in UTC (ATL1 orders peak in the evening local time, not after midnight UTC)
    hrs = q(built, "SELECT extract(hour FROM received_ts)::int h, count(*) n FROM wms.order_header o JOIN wms.warehouse w "
                   "USING (warehouse_id) WHERE w.warehouse_code = 'ATL1' AND order_type = 'B2C' GROUP BY 1").set_index("h")["n"]
    assert hrs.loc[19:21].sum() > hrs.loc[2:4].sum() * 3


def test_billing_uses_wms_rates(built: EngineCtx) -> None:
    df = q(built, """SELECT count(*) n, count(*) FILTER (WHERE b.rate <> r.rate) diff FROM wms.billing_record b
                     JOIN wms.billing_rate r USING (owner_id, charge_code)
                     WHERE b.charge_code IN ('ORD-B2C', 'ORD-B2B') AND b.activity_date >= r.effective_date
                       AND b.create_ts >= r.mod_ts""")
    assert df["n"].iloc[0] > 1000
    assert df["diff"].iloc[0] <= df["n"].iloc[0] * 0.01        # billing corrections re-rate a handful
    frt = q(built, "SELECT count(*) n FROM wms.billing_record WHERE charge_code = 'FRT-RCV' AND ref_type = 'SHIPMENT'")
    assert frt["n"].iloc[0] > 0


def test_planted_picker_outlier(built: EngineCtx) -> None:
    df = q(built, """SELECT t.assigned_user_id u, t.order_id, max(t.completed_ts) - min(t.completed_ts) spread, count(*) n
                     FROM wms.task t JOIN wms.warehouse w USING (warehouse_id) WHERE w.warehouse_code = 'ATL1' AND t.task_type = 'PICK'
                     GROUP BY 1, 2 HAVING count(*) >= 3""")
    df["spread_s"] = df["spread"].dt.total_seconds()
    per_user = df.groupby("u")["spread_s"].median()
    outlier = per_user.idxmin()
    assert per_user[outlier] <= 10 and per_user.drop(outlier).median() > 60
    picks = q(built, """SELECT assigned_user_id u, count(*) n FROM wms.task t JOIN wms.app_user a ON a.user_id = t.assigned_user_id
                        WHERE t.task_type = 'PICK' AND a.shift = 'B' AND t.warehouse_id = 1 GROUP BY 1""").set_index("u")["n"]
    assert picks[outlier] > 1.8 * picks.drop(outlier).median()
    txn = q(built, "SELECT count(*) n FROM wms.inventory_txn x JOIN wms.task t ON t.order_id = x.ref_id AND x.txn_type = 'PICK' "
                   "AND x.txn_ts = t.completed_ts AND x.user_id = t.assigned_user_id WHERE t.assigned_user_id = %s", params=(int(outlier),))
    assert txn["n"].iloc[0] > 0


def test_planted_cold_chain_and_recall(built: EngineCtx) -> None:
    holds = q(built, "SELECT reason_code, count(*) n FROM wms.inventory_txn WHERE txn_type = 'HOLD' GROUP BY 1").set_index("reason_code")["n"]
    assert holds.get("TEMP_EXCURSION", 0) > 0
    assert holds.get("RECALL", 0) > 0
    rc = q(built, "SELECT DISTINCT material_id, lot_nbr FROM wms.inventory_txn WHERE reason_code = 'RECALL'").iloc[0]
    shipped = q(built, "SELECT count(DISTINCT x.ref_id) n, count(DISTINCT o.ship_to_company || o.ship_to_city) shiptos "
                       "FROM wms.inventory_txn x JOIN wms.order_header o ON o.order_id = x.ref_id "
                       "WHERE x.txn_type = 'SHIP' AND x.material_id = %s AND x.lot_nbr = %s",
                params=(int(rc["material_id"]), rc["lot_nbr"])).iloc[0]
    assert shipped["n"] >= 20 and shipped["shiptos"] >= 10        # the recalled lot went to many orders and ship-tos
    door = q(built, "SELECT count(*) n FROM telemetry.dock_door_event WHERE warehouse_code = 'CHI1' AND door_nbr = 14", "iot")
    assert door["n"].iloc[0] > 0
    stuck = q(built, "SELECT count(*) n, count(DISTINCT value) v FROM telemetry.sensor_reading WHERE sensor_id = 'CHI1-CLR-03-T' "
                     "AND value = 4.0", "iot").iloc[0]
    assert stuck["n"] > 9 * 200 and stuck["v"] == 1
    assert iot.stuck_sensor_window(built) is not None


def test_carrier_events_and_delivery(built: EngineCtx) -> None:
    ev = q(built, """SELECT event_code, count(*) n, avg(extract(epoch FROM received_at - event_ts)) lag FROM carrier.tracking_event
                     GROUP BY 1""", "sim_internal").set_index("event_code")
    for code in ("LABEL_CREATED", "PICKED_UP", "IN_TRANSIT", "OUT_FOR_DELIVERY", "DELIVERED", "EXCEPTION"):
        assert ev.loc[code, "n"] > 0, code
    assert (ev["lag"] >= 0).all()
    assert q(built, "SELECT count(*) n FROM carrier.tracking_event WHERE event_local_ts !~ '^\\d{4}-\\d\\d-\\d\\dT\\d\\d:\\d\\d:\\d\\d[+-]\\d\\d:\\d\\d$'",
             "sim_internal")["n"].iloc[0] == 0
    st = q(built, "SELECT shipment_status, count(*) n FROM wms.shipment GROUP BY 1").set_index("shipment_status")["n"]
    assert st["DELIVERED"] > st.sum() * 0.8 and st.get("EXCEPTION", 0) > 0


def test_dq_injections_are_logged(built: EngineCtx) -> None:
    cats = set(q(built, "SELECT DISTINCT category FROM sim.anomaly_log WHERE system IN ('wms', 'iot', 'carrier')", "sim_internal")["category"])
    assert {"duplicate_row", "late_arriving"} <= cats
    orphans = q(built, "SELECT order_id || '-' || line_nbr k FROM wms.order_line l LEFT JOIN wms.material m USING (material_id) "
                       "WHERE m.material_id IS NULL")
    assert set(orphans["k"]) <= logged_keys(built, "order_line", ("orphan_fk",))


# ------------------------------------------------------------------------------------------------ batch, stream, scenarios
def test_orders_created_bus_has_every_new_order(built: EngineCtx) -> None:
    built.mode = "batch"
    before = int(q(built, "SELECT coalesce(max(order_id), 0) m FROM wms.order_header")["m"].iloc[0])
    run(built, 24)
    bus = built.bus.get("orders_created")
    lines = built.bus.get("order_lines_created")
    new = q(built, "SELECT order_id, channel FROM wms.order_header WHERE order_id > %s", params=(before,))
    o = built.world["wms"]["orders"]
    drafts = set(o["order_id"][o["is_draft"]].tolist())
    expected = set(new["order_id"].tolist()) - drafts
    assert len(expected) > 200
    assert set(bus["order_id"].tolist()) == expected
    assert bus["order_id"].is_unique
    assert (bus["channel"] == "EDI").sum() == (new.loc[new["order_id"].isin(expected), "channel"] == "EDI").sum() > 0
    assert set(lines["order_id"].tolist()) == expected
    db_lines = q(built, "SELECT count(*) n FROM wms.order_line WHERE order_id = ANY(%s)", params=(sorted(expected),))["n"].iloc[0]
    assert len(lines) >= db_lines        # lines cut later in the window are on the bus but deleted from the table


def test_batch_24h_changes_like_an_operational_db(built: EngineCtx) -> None:
    built.mode = "batch"
    total = ChangeCounter()
    for h in (12, 12):
        for k, (i, u, d) in run(built, h).rows.items():
            total.add(k[0], k[1], i, u, d)
    rows = {t: v for (s, t), v in total.rows.items() if s == "wms"}
    for table in ("order_header", "order_line", "inventory", "task", "shipment"):
        ins, upd, dele = rows.get(table, [0, 0, 0])
        assert ins > 0 and upd > 0 and dele > 0, (table, rows.get(table))
    assert rows["inventory_txn"][1] == 0 and rows["inventory_txn"][2] == 0     # the ledger is immutable


def test_stream_five_minute_windows(built: EngineCtx) -> None:
    built.mode = "stream"
    seen = ChangeCounter()
    for _ in range(24):
        for k, (i, u, d) in run(built, 5 / 60).rows.items():
            seen.add(k[0], k[1], i, u, d)
    rows = {f"{s}.{t}": v for (s, t), v in seen.rows.items()}
    assert rows["wms.order_header"][0] > 0 and rows["wms.order_header"][1] > 0
    assert rows["wms.task"][1] > 0
    assert rows["iot.sensor_reading"][0] > 0
    # a stream tick leaves partial states: some tasks in progress, some orders between steps
    st = q(built, "SELECT order_status, count(*) n FROM wms.order_header WHERE order_status NOT IN ('SHIPPED', 'CANCELLED') GROUP BY 1")
    assert len(st) >= 3


@pytest.mark.parametrize("sid,params", [
    ("peak_surge", {"multiplier": 3.0, "days": 2, "warehouse": "ALL"}),
    ("storm", {"warehouse": "CHI1", "days": 2, "severity": "severe"}),
    ("carrier_outage", {"scac": "UPSN", "hours": 6}),
    ("late_data_dump", {"source": "all", "hours_late": 2}),
    ("rate_increase", {"client": "NBOUT", "pct": 4.0, "keyed_in_wms": "on_time", "delay_days": 21}),
    ("schema_drift_release", {"systems": "all"}),
])
def test_scenarios(built: EngineCtx, sid: str, params: dict[str, Any]) -> None:
    built.mode = "scenario"
    combo = _Combo((sid, {**params, "effective_date": built.window.end.date().isoformat()}))
    run(built, 1, combo)
    assert combo.notes, sid
    if sid == "schema_drift_release":
        run(built, 6)
        n = q(built, "SELECT count(delivery_instructions) n FROM wms.order_header")["n"].iloc[0]
        assert n > 0 and q(built, "SELECT count(*) n FROM wms.sys_release")["n"].iloc[0] == 2
    if sid == "rate_increase":
        run(built, 1)
        assert q(built, "SELECT count(*) n FROM wms.billing_rate r JOIN wms.owner o USING (owner_id) WHERE o.owner_code = 'NBOUT' "
                        "AND r.mod_ts >= %s", params=(built.window.start.replace(tzinfo=None) - timedelta(days=3),))["n"].iloc[0] > 0


def test_onboard_and_offboard(built: EngineCtx) -> None:
    built.mode = "scenario"
    rng = built.rng("engine", built.window, "onboard")
    client = scenarios.make_client(built.rules, {"vertical": "pet", "tier": "M", "warehouse": "ATL1", "currency": "USD",
                                                 "go_live_days": 1, "name": ""}, built.window.end, built.history_end, rng)
    built.world.setdefault("dynamic_clients", []).append(client)
    built.rules["clients"].append(client)
    run(built, 1, _Combo(("onboard_client", {"client": client})))
    own = q(built, "SELECT owner_status, owner_id FROM wms.owner WHERE owner_code = %s", params=(client["code"],))
    assert own["owner_status"].iloc[0] == "ONBOARDING"
    end = (built.window.end + timedelta(days=2)).date().isoformat()
    run(built, 1, _Combo(("offboard_client", {"client": "PETALPN", "notice_days": 2, "end_date": end})))
    built.mode = "batch"
    for _ in range(18):
        run(built, 24)
    st = q(built, "SELECT owner_code, owner_status FROM wms.owner WHERE owner_code IN (%s, 'PETALPN')",
           params=(client["code"],)).set_index("owner_code")["owner_status"]
    assert st[client["code"]] == "ACTIVE"
    assert st["PETALPN"] == "INACTIVE"
    left = q(built, "SELECT coalesce(sum(qty_on_hand), 0) n FROM wms.inventory i JOIN wms.owner o USING (owner_id) WHERE o.owner_code = 'PETALPN'")
    assert left["n"].iloc[0] == 0
    assert q(built, "SELECT count(*) n FROM wms.order_header o JOIN wms.owner w USING (owner_id) WHERE w.owner_code = %s",
             params=(client["code"],))["n"].iloc[0] > 0
    assert wms.NAME == "wms"


def test_calibration_carries_on_through_batches_and_streams(built: EngineCtx) -> None:
    today = built.window.end.date()
    sensors = q(built, "SELECT calibration_due FROM telemetry.sensor WHERE sensor_type = 'TEMP'", "iot")
    overdue = (sensors["calibration_due"] < today).mean()
    assert overdue <= 0.25
    probes = built.world["iot"]["calibration"]["probes"]
    assert sum(p[1] for p in probes.values()) > 0
    assert all(p[2] >= int(built.window.end.timestamp()) for p in probes.values()), "a visit before the clock was not carried out"
