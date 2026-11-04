"""Synthetic WMS + carrier stand-in for the finance-crm tests, and the shared small build they run against.

``FakeWms`` writes the WMS rows the finance-crm modules read (warehouse, owner, material, order_header,
billing_record, app_user, carrier.tracking_event) and publishes bus frames with the columns of build spec 4.1.
``fincrm_build()`` bootstraps and runs ~3.5 months of the Small preset once per test session.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

import numpy as np
import pandas as pd
import pytest

from harborline.core import Window
from harborline.sim import testing
from harborline.sim.edi import Edi
from harborline.sim.crm import (
    churn_of,
    dates_of,
    epoch_s,
    preset_warehouses,
    rate_on_date,
    rate_versions,
    sim_clients,
    to_local_naive,
)

DAYS = 107
MODULES = ("crm", "ax", "edi", "files")
_BUILD: dict[str, Any] = {}


def _naive(ts: pd.Series, tz: str) -> pd.Series:
    return ts.dt.tz_convert(tz).dt.tz_localize(None)


class FakeWms:
    name = "wms"

    def __init__(self, scale: float | None = None) -> None:
        self.scale = scale          # None: 4 orders/day per client; else volume_tiers x scale (performance runs)

    def bootstrap(self, ctx: Any) -> None:
        st = ctx.world.setdefault("fakewms", {"owners": {}, "ids": {}, "pending": [], "users": 0})
        whs = list(preset_warehouses(ctx).values())
        t0 = ctx.history_start.replace(tzinfo=None)
        ctx.copy_rows("wms", "warehouse", pd.DataFrame({
            "warehouse_id": range(1, len(whs) + 1), "warehouse_code": [w["code"] for w in whs],
            "warehouse_name": [w["name"] for w in whs], "country_code": [w["address"]["country"] for w in whs],
            "time_zone": [w["time_zone"] for w in whs], "weight_uom": [w["uom"]["weight"] for w in whs],
            "dim_uom": [w["uom"]["dim"] for w in whs], "create_ts": t0, "mod_ts": t0}))
        st["wh"] = {w["code"]: i for i, w in enumerate(whs, start=1)}
        st["tz"] = {w["code"]: w["time_zone"] for w in whs}
        for c in sim_clients(ctx):
            if dates_of(ctx, c).wms_created <= ctx.history_start.date():
                self.add_owner(ctx, c, ctx.history_start)
        users = []
        for code, wid in st["wh"].items():
            for k in range(6):
                st["users"] += 1
                users.append({"user_id": st["users"], "username": f"{code.lower()}u{k}", "first_name": f"Pat{k}",
                              "last_name": f"Lee{st['users']}", "personnel_nbr": f"{100000 + st['users']}", "warehouse_id": wid,
                              "user_role": "PICKER", "hire_date": (ctx.history_start - timedelta(days=400)).date(), "create_ts": t0,
                              "mod_ts": t0})
        ctx.copy_rows("wms", "app_user", pd.DataFrame(users))

    def nid(self, ctx: Any, name: str, n: int) -> np.ndarray:
        return ctx.next_ids(f"fakewms.{name}", n)

    def add_owner(self, ctx: Any, c: dict[str, Any], ts: Any) -> None:
        st = ctx.world["fakewms"]
        oid = int(self.nid(ctx, "owner", 1)[0])
        t = ts.replace(tzinfo=None)
        ctx.copy_rows("wms", "owner", pd.DataFrame([{
            "owner_id": oid, "owner_code": c["code"], "owner_name": c["name"], "owner_status": "ACTIVE", "vertical": c["vertical"],
            "billing_currency": c["currency"], "primary_warehouse_id": st["wh"][c["warehouses"][0]], "create_ts": t, "mod_ts": t}]))
        n = 25
        mids = self.nid(ctx, "material", n)
        rng = np.random.default_rng(oid)
        ctx.copy_rows("wms", "material", pd.DataFrame({
            "material_id": mids, "owner_id": oid, "sku": [f"{c['code'][:3]}-{1000 + i}" for i in range(n)],
            "description": [f"{c['name']} item {i} {'crème' if i == 3 else 'std'}" for i in range(n)],
            "upc": [f"{int(rng.integers(10**11, 10**12))}" for _ in range(n)], "unit_weight": rng.uniform(0.2, 9, n).round(3),
            "weight_uom": "LB", "case_qty": 12, "cases_per_pallet": 40, "is_lot_controlled": False, "create_ts": t, "mod_ts": t}))
        st["owners"][c["code"]] = {"id": oid, "skus": [f"{c['code'][:3]}-{1000 + i}" for i in range(n)], "mids": [int(x) for x in mids]}
        ctx.bus.publish("clients_created", pd.DataFrame([{"owner_id": oid, "owner_code": c["code"], "owner_name": c["name"],
                                                          "wms_created_date": ts.date(), "go_live": dates_of(ctx, c).go_live,
                                                          "primary_warehouse_code": c["warehouses"][0]}]))

    def step(self, ctx: Any, win: Window) -> None:
        st = ctx.world["fakewms"]
        rng = ctx.rng("fakewms", win)
        day = win.start.date()
        for c in sim_clients(ctx):
            if c["code"] not in st["owners"] and dates_of(ctx, c).wms_created <= day:
                self.add_owner(ctx, c, win.start)
        orders, lines, billing = [], [], []
        frac = win.hours / 24
        for c in sim_clients(ctx):
            o = st["owners"].get(c["code"])
            d = dates_of(ctx, c)
            churn = churn_of(ctx, c)
            if o is None or day < d.go_live or (churn and day > churn):
                continue
            v = rate_on_date(rate_versions(ctx, c), day)
            rates = v.rates if v else {}
            b2b = c["vertical"] in ("auto_parts", "b2b_general")
            wh = c["warehouses"][0]
            per_day = 4 if self.scale is None else ctx.rules["volume_tiers"][c["tier"]] * self.scale
            for _ in range(int(rng.poisson(per_day * frac))):
                oid = int(self.nid(ctx, "order", 1)[0])
                rec = win.start + timedelta(seconds=float(rng.uniform(0, win.hours * 3600 * 0.6)))
                nl = int(rng.integers(1, 5)) if not (b2b and self.scale) else int(rng.integers(5, 40))
                orders.append({"order_id": oid, "order_nbr": f"HL{oid:010d}", "owner_code": c["code"], "owner_id": o["id"],
                               "warehouse_code": wh, "warehouse_id": st["wh"][wh], "client_order_ref": f"{int(rng.integers(10**6, 10**7))}",
                               "order_type": "B2B" if b2b else "B2C", "channel": "EDI" if b2b else "API", "received_ts": rec,
                               "requested_ship_date": day + timedelta(days=1), "ship_to_name": "Jordan Smith",
                               "ship_to_company": "KROGER DC 014" if b2b else None, "ship_to_address1": "1600 Distribution Pkwy",
                               "ship_to_city": "Columbus", "ship_to_state": "OH", "ship_to_postal": "43228", "ship_to_country": "US",
                               "is_residential": not b2b, "scac": "ODFL" if b2b else ("CNPC" if wh == "TOR1" else "UPSN"),
                               "service_code": "LTL" if b2b else ("CEX" if wh == "TOR1" else "GND"), "total_lines": nl,
                               "total_units": 0})
                for ln in range(1, nl + 1):
                    k = int(rng.integers(0, len(o["skus"])))
                    q = int(rng.integers(1, 30 if b2b else 3))
                    orders[-1]["total_units"] += q
                    lines.append({"order_id": oid, "line_nbr": ln, "owner_code": c["code"], "sku": o["skus"][k], "upc": None,
                                  "description": f"item {k}", "ordered_qty": q, "uom": "EA", "unit_price": 9.99, "material_id": o["mids"][k]})
                code = "ORD-B2B" if b2b else "ORD-B2C"
                if code in rates:
                    billing.append((o["id"], st["wh"][wh], code, day, 1.0, rates[code], "ORDER", oid, c["currency"], st["tz"][wh]))
            if win.crosses_local_time("America/New_York", 2, 0) and "STOR-PLT" in rates:
                pallets = {"S": 25, "M": 80, "L": 260, "XL": 700}[c["tier"]]
                billing.append((o["id"], st["wh"][wh], "STOR-PLT", day, round(pallets / 30, 2), rates["STOR-PLT"], None, None,
                                c["currency"], st["tz"][wh]))
        if orders:
            self.write_orders(ctx, win, pd.DataFrame(orders), pd.DataFrame(lines), rng)
        if billing:
            df = pd.DataFrame(billing, columns=["owner_id", "warehouse_id", "charge_code", "activity_date", "qty", "rate", "ref_type",
                                                "ref_id", "currency", "tz"])
            df["billing_id"] = self.nid(ctx, "billing", len(df))
            df["amount"] = (df["qty"] * df["rate"]).round(2)
            df["billing_status"] = "ACCRUED"
            df["create_ts"] = df["mod_ts"] = win.start.replace(tzinfo=None)
            ctx.copy_rows("wms", "billing_record", df.drop(columns=["tz"]))
        self.deliveries(ctx, win, rng)
        if win.crosses_local_time("America/New_York", 23, 0):
            ctx.bus.publish("labor_hours", pd.DataFrame([{"warehouse_code": w, "work_date": day, "role": "PICKER", "hours": 160.0}
                                                         for w in st["wh"]]))
        self.asns(ctx, win, rng)
        if rng.random() < 0.1 * frac:
            st["users"] += 1
            ctx.bus.publish("workers_hired", pd.DataFrame([{"personnel_nbr": f"{100000 + st['users']}", "first_name": "Sam",
                                                            "last_name": f"Hire{st['users']}", "warehouse_code": "ATL1", "legal_entity": "us01",
                                                            "role": "PICKER", "hire_date": day}]))
        if rng.random() < 0.05 * frac:
            ctx.bus.publish("workers_terminated", pd.DataFrame([{"personnel_nbr": "100001", "first_name": "Pat0", "last_name": "Lee1",
                                                                 "warehouse_code": "ATL1", "legal_entity": "us01", "role": "PICKER",
                                                                 "term_date": day}]))

    def write_orders(self, ctx: Any, win: Window, orders: pd.DataFrame, lines: pd.DataFrame, rng: np.random.Generator) -> None:
        st = ctx.world["fakewms"]
        tz = orders["warehouse_code"].map(st["tz"])
        rec = pd.to_datetime(orders["received_ts"], utc=True)
        orders["received_ts_local"] = [to_local_naive(t, z) for t, z in zip(rec, tz, strict=True)]
        hdr = orders.rename(columns={})
        hdr = hdr.assign(order_status="SHIPPED", create_ts=hdr["received_ts_local"], mod_ts=hdr["received_ts_local"],
                         received_ts=hdr["received_ts_local"])
        cols = ["order_id", "order_nbr", "owner_id", "warehouse_id", "client_order_ref", "order_type", "channel", "order_status",
                "received_ts", "requested_ship_date", "ship_to_name", "ship_to_company", "ship_to_address1", "ship_to_city",
                "ship_to_state", "ship_to_postal", "ship_to_country", "is_residential", "total_lines", "total_units", "create_ts", "mod_ts"]
        ctx.copy_rows("wms", "order_header", hdr[cols])
        ctx.copy_rows("wms", "order_line", lines.assign(line_status="SHIPPED", shipped_qty=lines["ordered_qty"])[
            ["order_id", "line_nbr", "material_id", "ordered_qty", "shipped_qty", "uom", "line_status"]])
        ctx.bus.publish("orders_created", orders.drop(columns=["owner_id", "warehouse_id"]).assign(received_ts=rec))
        ctx.bus.publish("order_lines_created", lines.drop(columns=["material_id"]))
        n = len(orders)
        sid = self.nid(ctx, "shipment", n)
        ship_ts = rec + pd.to_timedelta(rng.uniform(2, 8, n), unit="h")
        ship_ts = ship_ts.where(ship_ts < win.end, win.end - timedelta(seconds=5))
        ltl = (orders["scac"] == "ODFL").to_numpy()
        trk = np.where(ltl, [f"{int(x)}" for x in rng.integers(10**9, 10**10, n)],
                       np.where(orders["scac"] == "CNPC", [f"{int(x):016d}" for x in rng.integers(10**15, 10**16 - 1, n)],
                                [f"1Z4R7X29{int(x):010d}" for x in rng.integers(0, 10**10, n)]))
        ships = pd.DataFrame({
            "shipment_id": sid, "shipment_nbr": [f"SH{int(x):09d}" for x in sid], "order_id": orders["order_id"],
            "order_nbr": orders["order_nbr"], "client_order_ref": orders["client_order_ref"], "owner_code": orders["owner_code"],
            "warehouse_code": orders["warehouse_code"], "channel": orders["channel"], "scac": orders["scac"],
            "service_code": orders["service_code"], "mode": np.where(ltl, "LTL", "PARCEL"), "tracking_nbr": trk,
            "bol_nbr": np.where(ltl, [f"BOL{int(x)}" for x in sid], None), "ship_ts": ship_ts,
            "ship_ts_local": [to_local_naive(t, z) for t, z in zip(ship_ts, tz, strict=True)],
            "total_weight": np.where(ltl, rng.uniform(300, 1800, n), rng.uniform(0.5, 30, n)).round(1), "weight_uom": "LB",
            "carton_count": np.where(ltl, 1, rng.integers(1, 3, n)),
            "billed_weight_lb": np.where(ltl, rng.uniform(300, 1800, n), rng.uniform(1, 60, n)).round(1),
            "zone": rng.integers(2, 9, n), "is_residential": orders["is_residential"], "dest_city": "Columbus", "dest_state": "OH",
            "dest_postal": [f"{int(x):05d}" for x in rng.integers(10000, 99999, n)], "dest_country": "US",
            "freight_charge_est": rng.uniform(8, 25, n).round(2), "currency": np.where(orders["warehouse_code"] == "TOR1", "CAD", "USD")})
        ctx.bus.publish("shipments_shipped", ships)
        sl = lines.merge(ships[["shipment_id", "order_id"]], on="order_id")
        short = rng.random(len(sl)) < 0.03
        sl = sl.assign(shipped_qty=np.where(short, np.maximum(0, sl["ordered_qty"] - 1), sl["ordered_qty"]), lot_nbr=None)
        ctx.bus.publish("shipment_lines", sl[["shipment_id", "order_id", "line_nbr", "sku", "upc", "shipped_qty", "ordered_qty", "lot_nbr"]])
        st["pending"].append(ships[["tracking_nbr", "order_nbr", "owner_code", "scac", "ship_ts"]].assign(
            due=epoch_s(ship_ts + pd.Timedelta(days=3))))

    def deliveries(self, ctx: Any, win: Window, rng: np.random.Generator) -> None:
        st = ctx.world["fakewms"]
        if not st["pending"]:
            return
        buf = pd.concat(st["pending"], ignore_index=True)
        due = buf[buf["due"] < win.end.timestamp()]
        rest = buf[buf["due"] >= win.end.timestamp()]
        st["pending"] = [rest] if len(rest) else []
        if due.empty:
            return
        n = len(due)
        late = rng.random(n) < 0.12
        exc = np.where(rng.random(n) < 0.05, "DAMAGED", None)
        delivered = pd.to_datetime(due["ship_ts"], utc=True) + pd.to_timedelta(np.where(late, 4.5, 2.5), unit="D")
        delivered = delivered.where(delivered < win.end, win.end - timedelta(seconds=1))
        ctx.bus.publish("delivery_outcomes", pd.DataFrame({
            "tracking_nbr": due["tracking_nbr"].to_numpy(), "order_nbr": due["order_nbr"].to_numpy(),
            "owner_code": due["owner_code"].to_numpy(), "scac": due["scac"].to_numpy(), "delivered_ts": delivered.to_numpy(),
            "promised_date": (pd.to_datetime(due["ship_ts"], utc=True) + pd.Timedelta(days=3)).dt.date.to_numpy(),
            "is_late": late, "exception_code": exc}))
        eid = self.nid(ctx, "event", n * 2)
        ev = pd.DataFrame({
            "event_id": eid, "tracking_nbr": np.repeat(due["tracking_nbr"].to_numpy(), 2), "scac": np.repeat(due["scac"].to_numpy(), 2),
            "event_code": np.tile(["PICKED_UP", "DELIVERED"], n),
            "event_ts": np.ravel(np.column_stack([pd.to_datetime(due["ship_ts"], utc=True).to_numpy(), delivered.to_numpy()])),
        })
        ev["event_ts"] = pd.to_datetime(ev["event_ts"], utc=True)
        ev["event_local_ts"] = ev["event_ts"].dt.strftime("%Y-%m-%dT%H:%M:%S-05:00")
        ev["received_at"] = ev["event_ts"] + pd.Timedelta(minutes=7)
        ctx.copy_rows("sim_internal", "carrier.tracking_event", ev)

    def asns(self, ctx: Any, win: Window, rng: np.random.Generator) -> None:
        st = ctx.world["fakewms"]
        o = st["owners"].get("APEXAUT")
        if o is None or rng.random() > 0.4 * win.hours / 24:
            return
        nbr = f"ASN-APEXAUT-{int(self.nid(ctx, 'asn', 1)[0]):06d}"
        lines = [{"sku": o["skus"][i], "upc": "012345678905", "qty": int(rng.integers(50, 400)), "uom": "EA", "lot": None, "expiry": None}
                 for i in range(3)]
        ctx.bus.publish("asns_created", pd.DataFrame([{"asn_id": 1, "asn_nbr": nbr, "owner_code": "APEXAUT", "warehouse_code": "ATL1",
                                                       "supplier_name": "Brakeline Mfg", "source": "EDI856",
                                                       "expected_date": win.start.date() + timedelta(days=4), "lines": lines}]))


def fincrm_build() -> dict[str, Any]:
    """Bootstrap + DAYS one-day windows of the Small preset (FakeWms + finance-crm). Run once per session."""
    if _BUILD:
        return _BUILD
    ctx = testing.make_ctx("small", "build")
    fake = FakeWms()
    t0 = time.perf_counter()
    testing.bootstrap_modules(ctx, [fake, *MODULES])
    t_boot = time.perf_counter() - t0
    timings = {m: 0.0 for m in MODULES}
    counts: dict[tuple[str, str], list[int]] = {}
    for day in range(DAYS):
        win = testing.day_window(ctx, day)
        testing.run_window(ctx, fake, win)
        for m in MODULES:
            t = time.perf_counter()
            changes = testing.run_window(ctx, m, win)
            timings[m] += time.perf_counter() - t
            for key, (i, u, d) in changes.rows.items():
                cur = counts.setdefault(key, [0, 0, 0])
                cur[0] += i
                cur[1] += u
                cur[2] += d
    Edi(ctx).flush_archives()   # a build writes these at each month end, and this short run stops before one
    _BUILD.update(ctx=ctx, timings=timings, bootstrap_s=t_boot, counts=counts, fake=fake)
    return _BUILD


@pytest.fixture(scope="session")
def build(pg_settings: Any) -> dict[str, Any]:
    return fincrm_build()


def test_fake_wms_feeds_the_bus(build: dict[str, Any]) -> None:
    ctx = build["ctx"]
    n = testing.fetch_df(ctx, "wms", "SELECT count(*) AS n FROM wms.order_header")["n"].iloc[0]
    assert n > 1000
    assert testing.fetch_df(ctx, "wms", "SELECT count(*) AS n FROM wms.billing_record")["n"].iloc[0] > n
