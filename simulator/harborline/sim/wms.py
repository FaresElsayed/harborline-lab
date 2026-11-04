"""Harborline WMS: the operational heart of the simulator.

State lives in ``ctx.world['wms']`` as plain dicts of NumPy arrays (picklable, bounded): master data, the inventory
index, and every *open* entity (orders, lines, allocations, tasks, receipts, returns, counts, shipments not yet
shipped). Each window advances every open entity whose next timestamp falls before the window end, then writes
the net effect to Postgres like an operational database would: COPY for new rows, bulk UPDATE in place (mod_ts set)
for changed rows and hard DELETE where the real system deletes. Rows that are created and finished inside one
window are inserted directly in their final state, so a 1-day build window and a 5-minute stream tick produce the
same history.

Times are kept as int64 *local wall-clock seconds* of the row's warehouse (the WMS stores local time without a
zone); days are int epoch days.
"""

from __future__ import annotations

import hashlib
import math
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

from ..commercial import ax_account_num, client_dates, contract_rates, month_start
from ..core import Ctx, Window, derive_seed
from . import carrier as carr
from . import geo

NAME = "wms"
DAY = 86400
T_NONE = np.int64(2**62)
D_NONE = np.int64(-(10**6))
ORDER_CAL = 0.42            # calibrates volume_tiers so Large ~ 2.5 M orders (measured, see tests)
SKUS_BY_TIER = {"S": 110, "M": 380, "L": 1150, "XL": 2900}
SKU_PRESET = {"small": 0.3, "medium": 0.6, "large": 1.0}
LOC_SCALE = {"small": 0.03, "medium": 0.08, "large": 0.14}
SHIPTOS_BY_TIER = {"S": 8, "M": 24, "L": 55, "XL": 110}
LB_PER_KG = 2.20462

# status codes (int8 internally, strings in the database)
ORD_ST = np.array(["NEW", "ON_HOLD", "ALLOCATED", "BACKORDERED", "RELEASED", "PICKING", "PACKED", "SHIPPED", "CANCELLED"], dtype=object)
O_NEW, O_HOLD, O_ALLOC, O_BACK, O_REL, O_PICK, O_PACK, O_SHIP, O_CANC = range(9)
LINE_ST = np.array(["OPEN", "ALLOCATED", "BACKORDERED", "PICKED", "SHIPPED"], dtype=object)
L_OPEN, L_ALLOC, L_BACK, L_PICKED, L_SHIP = range(5)
TASK_ST = np.array(["CREATED", "ASSIGNED", "IN_PROGRESS", "COMPLETE", "CANCELLED"], dtype=object)
TASK_TYPE = np.array(["PICK", "PUTAWAY", "REPLEN", "COUNT", "LOAD", "MOVE"], dtype=object)
K_PICK, K_PUT, K_REPLEN, K_COUNT, K_LOAD = range(5)
INV_ST = np.array(["AVAILABLE", "HOLD", "DAMAGED", "QC"], dtype=object)
I_AVAIL, I_HOLD, I_DMG, I_QC = range(4)
SHIP_ST = np.array(["CREATED", "MANIFESTED", "SHIPPED", "DELIVERED", "EXCEPTION"], dtype=object)
LOC_TYPES = np.array(["PICK", "RESERVE", "DOCK", "STAGE", "QC", "RETURN"], dtype=object)
LT_PICK, LT_RES, LT_DOCK, LT_STAGE, LT_QC, LT_RET = range(6)
ZONE_LETTER = {"AMBIENT": "A", "COOLER": "C", "FREEZER": "F", "HAZMAT": "H", "CAGE": "K", "BULK": "B", "RETURNS": "R"}
ZONE_WEIGHT = {"AMBIENT": 0.55, "COOLER": 0.32, "FREEZER": 0.2, "HAZMAT": 0.04, "CAGE": 0.05, "BULK": 0.22, "RETURNS": 0.04}
PROF_B2C = np.array([0.35, 0.25, 0.18, 0.15, 0.15, 0.25, 0.5, 0.8, 1.0, 1.15, 1.25, 1.3, 1.35, 1.3, 1.25, 1.2, 1.2, 1.25,
                     1.35, 1.5, 1.6, 1.5, 1.1, 0.65])
PROF_B2B = np.array([0.1, 0.05, 0.05, 0.05, 0.1, 0.4, 1.6, 1.4, 2.2, 2.0, 1.8, 1.6, 1.3, 1.9, 1.7, 1.5, 1.2, 0.8, 0.5,
                     0.4, 0.3, 0.25, 0.2, 0.15])
PROF_B2C = PROF_B2C / PROF_B2C.sum()
PROF_B2B = PROF_B2B / PROF_B2B.sum()
RET_REASONS = {"apparel": (["WRONG_SIZE", "NO_LONGER_NEEDED", "NOT_AS_DESCRIBED", "DAMAGED", "DEFECTIVE"], [0.46, 0.24, 0.18, 0.07, 0.05]),
               "electronics": (["DEFECTIVE", "NO_LONGER_NEEDED", "NOT_AS_DESCRIBED", "DAMAGED", "WRONG_SIZE"], [0.38, 0.27, 0.2, 0.13, 0.02])}
RET_DEFAULT = (["NO_LONGER_NEEDED", "DAMAGED", "NOT_AS_DESCRIBED", "DEFECTIVE", "WRONG_SIZE"], [0.4, 0.22, 0.2, 0.13, 0.05])
CANCEL_REASONS = np.array(["CUSTOMER_REQUEST", "ADDRESS_INVALID", "FRAUD_REVIEW", "DUPLICATE_ORDER", "CLIENT_REQUEST"], dtype=object)
HOLD_QA_REASONS = np.array(["QA_REVIEW", "DAMAGED_FOUND", "LABEL_ISSUE"], dtype=object)


# =============================================================================================== table helpers
def tnew(schema: dict[str, Any]) -> dict[str, np.ndarray]:
    return {k: np.zeros(0, dtype=v) for k, v in schema.items()}


def tlen(t: dict[str, np.ndarray]) -> int:
    return len(t["_st"]) if "_st" in t else len(next(iter(t.values())))


def ttake(t: dict[str, np.ndarray], sel: Any) -> dict[str, np.ndarray]:
    return {k: v[sel] for k, v in t.items()}


def tkeep(t: dict[str, np.ndarray], mask: np.ndarray) -> None:
    for k in list(t):
        t[k] = t[k][mask]


def tadd(t: dict[str, np.ndarray], n: int, **vals: Any) -> None:
    """Append n rows; missing columns get neutral defaults (0, None, T_NONE for t_* times)."""
    if n <= 0:
        return
    for k, arr in t.items():
        if k in vals:
            v = vals[k]
            v = np.full(n, v, dtype=arr.dtype) if np.ndim(v) == 0 else np.asarray(v).astype(arr.dtype, copy=False)
        elif arr.dtype == object:
            v = np.full(n, None, dtype=object)
        elif k.startswith("t_") or k.endswith("_ts"):
            v = np.full(n, T_NONE, dtype=arr.dtype)
        elif k.endswith(("_day", "_date")):
            v = np.full(n, D_NONE, dtype=arr.dtype)
        else:
            v = np.zeros(n, dtype=arr.dtype)
        t[k] = np.concatenate([arr, v])


def mark(t: dict[str, np.ndarray], mask: np.ndarray) -> None:
    st = t["_st"]
    st[mask & (st == 0)] = 2


def mark_pos(t: dict[str, np.ndarray], pos: np.ndarray) -> None:
    """Flag rows (by position) as changed unless they are new in this window."""
    st = t["_st"]
    pos = np.asarray(pos, dtype=np.int64)
    st[pos[st[pos] == 0]] = 2


def pos_of(sorted_ids: np.ndarray, ids: np.ndarray) -> np.ndarray:
    p = np.searchsorted(sorted_ids, ids)
    p = np.clip(p, 0, max(len(sorted_ids) - 1, 0))
    return p


def ts_col(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.int64)
    out = v.astype("datetime64[s]")
    out[v >= T_NONE] = np.datetime64("NaT", "s")
    return out


def day_col(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.int64)
    ok = v > D_NONE
    out = np.full(len(v), None, dtype=object)
    if ok.any():
        out[ok] = np.datetime_as_string(v[ok].astype("datetime64[D]"))
    return out


def nint(v: np.ndarray, null: int = 0) -> pd.arrays.IntegerArray:
    """Nullable integer column: ``null`` sentinel -> NULL."""
    v = np.asarray(v, dtype=np.int64)
    return pd.arrays.IntegerArray(np.where(v == null, 0, v), v == null)


def _frame(t: dict[str, np.ndarray], sel: Any, cols: list[str], tcols: set[str] = frozenset(), dcols: set[str] = frozenset(),
           codes: dict[str, np.ndarray] | None = None, nulls: dict[str, int] | None = None) -> pd.DataFrame:
    data: dict[str, Any] = {}
    for c in cols:
        v = t[c][sel]
        if c in tcols:
            v = ts_col(v)
        elif c in dcols:
            v = day_col(v)
        elif codes and c in codes:
            v = codes[c][v]
        elif nulls and c in nulls:
            v = nint(v, nulls[c])
        data[c] = v
    return pd.DataFrame(data)


def _hash_rng(*parts: Any) -> np.random.Generator:
    return np.random.default_rng(derive_seed(*parts))


# =============================================================================================== database flush
INS_ORDER = ("warehouse", "zone", "location", "carrier", "carrier_service", "owner", "owner_warehouse", "material",
             "material_uom", "app_user", "billing_rate", "asn_header", "asn_line", "receipt_header", "receipt_line",
             "license_plate", "inventory", "order_header", "order_line", "task", "shipment", "shipment_carton",
             "inventory_txn", "cycle_count", "return_header", "return_line", "billing_record", "sys_release")
DEL_ORDER = ("return_line", "shipment_carton", "order_line", "asn_line", "receipt_line", "task", "inventory",
             "shipment", "order_header", "asn_header", "material_uom", "material", "billing_record", "billing_rate")


class Flush:
    """Collects a window's inserts / updates / deletes and writes them in FK-safe order."""

    def __init__(self) -> None:
        self.ins: dict[str, list[pd.DataFrame]] = {}
        self.upd: list[tuple[str, tuple[str, ...], pd.DataFrame]] = []
        self.dels: dict[str, list[tuple[tuple[str, ...], dict[str, np.ndarray]]]] = {}
        self.sql: list[tuple[str, str, Any]] = []

    def insert(self, table: str, df: pd.DataFrame) -> None:
        if df is not None and len(df):
            self.ins.setdefault(table, []).append(df)

    def update(self, table: str, keys: tuple[str, ...], df: pd.DataFrame) -> None:
        if df is not None and len(df):
            self.upd.append((table, keys, df))

    def delete(self, table: str, keys: dict[str, np.ndarray]) -> None:
        if len(next(iter(keys.values()))):
            self.dels.setdefault(table, []).append((tuple(keys), keys))

    def drop_pending(self, table: str, col: str, ids: np.ndarray) -> None:
        """Forget queued inserts whose parent row was removed before it was ever written."""
        if table in self.ins:
            self.ins[table] = [df[~df[col].isin(ids)] for df in self.ins[table]]

    def execute(self, table: str, stmt: str, params: Any) -> None:
        self.sql.append((table, stmt, params))

    def run(self, ctx: Ctx) -> None:
        # Row changes are measured by the engine from Postgres statistics (no ctx.changes for wms).
        conn = ctx.conn("wms")
        for table in DEL_ORDER:
            for cols, keys in self.dels.get(table, []):
                _delete(conn, table, cols, keys)
        for table in INS_ORDER:
            frames = self.ins.get(table)
            if frames:
                df = frames[0] if len(frames) == 1 else pd.concat(frames, ignore_index=True)
                geo.copy_fast(ctx, "wms", f"wms.{table}", df)
        grouped: dict[tuple[str, tuple[str, ...], tuple[str, ...]], list[pd.DataFrame]] = {}
        for table, keys, df in self.upd:
            grouped.setdefault((table, keys, tuple(df.columns)), []).append(df)
        for (table, keys, _cols), frames in grouped.items():
            df = frames[0] if len(frames) == 1 else pd.concat(frames, ignore_index=True)
            df = df.drop_duplicates(list(keys), keep="last")
            _bulk_update(conn, table, keys, df)
        for _table, stmt, params in self.sql:
            conn.execute(stmt, params)


def _delete(conn: Any, table: str, cols: tuple[str, ...], keys: dict[str, np.ndarray]) -> int:
    if len(cols) == 1:
        c = cols[0]
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM wms.{table} WHERE {c} = ANY(%s)", (np.asarray(keys[c]).astype(np.int64).tolist(),))
            return cur.rowcount
    arrs = [np.asarray(keys[c]).astype(np.int64).tolist() for c in cols]
    types = ", ".join("%s::bigint[]" for _ in cols)
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM wms.{table} WHERE ({', '.join(cols)}) IN (SELECT * FROM unnest({types}))", arrs)
        return cur.rowcount


def _arrow_csv(df: pd.DataFrame) -> bytes:
    import io

    import pyarrow as pa
    import pyarrow.csv as pacsv

    buf = io.BytesIO()
    pacsv.write_csv(pa.Table.from_pandas(df, preserve_index=False), buf, pacsv.WriteOptions(include_header=False))
    return buf.getvalue()


def _bulk_update(conn: Any, table: str, keys: tuple[str, ...], df: pd.DataFrame) -> int:
    cols = list(df.columns)
    tmp = f"_u_{table}_{hashlib.md5(','.join(cols).encode()).hexdigest()[:8]}"
    collist = ", ".join(cols)
    conn.execute(f"CREATE TEMP TABLE IF NOT EXISTS {tmp} ON COMMIT DELETE ROWS AS SELECT {collist} FROM wms.{table} WITH NO DATA")
    sets = ", ".join(f"{c} = u.{c}" for c in cols if c not in keys)
    cond = " AND ".join(f"t.{k} = u.{k}" for k in keys)
    with conn.cursor() as cur:
        with cur.copy(f"COPY {tmp} ({collist}) FROM STDIN WITH (FORMAT csv)") as cp:
            cp.write(_arrow_csv(df))
        cur.execute(f"UPDATE wms.{table} t SET {sets} FROM {tmp} u WHERE {cond}")
        n = cur.rowcount
        cur.execute(f"DELETE FROM {tmp}")
    return n


# =============================================================================================== calendars
_CAL: dict[tuple, WorkCal] = {}


def _hhmm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 3600 + int(m) * 60


class WorkCal:
    """Working-time calendar of one warehouse: shift hours on business days, carrier pickup slots."""

    def __init__(self, ctx: Ctx, wh: dict[str, Any], d0: int, d1: int) -> None:
        self.d0, self.d1 = d0, d1
        days = np.arange(d0, d1, dtype=np.int64)
        dow = (days + 3) % 7
        country = wh["address"]["country"]
        hol = np.array([ctx.signals.is_holiday(country, geo.day_date(d)) for d in days])
        starts, ends = [], []
        for span in wh["shifts"].values():
            a, b = (_hhmm(x) for x in span.split("-"))
            if b <= a:
                b += DAY
            starts.append(a)
            ends.append(b)
        opn, cls = min(starts), min(max(ends), DAY)
        if len(wh["shifts"]) >= 3:
            opn, cls = 0, DAY
        md = pd.to_datetime(days * DAY, unit="s")
        mon, dom = md.month.to_numpy(), md.day.to_numpy()
        peak = ((mon == 11) & (dom >= 15)) | ((mon == 12) & (dom <= 22))
        work = (dow < 5) & ~hol
        sat = (dow == 5) & peak & (wh["profile"] == "ecommerce") & ~hol
        span_s = np.where(work, cls - opn, np.where(sat, starts and (min(ends) - opn), 0)).astype(np.int64)
        self.open = np.full(len(days), opn, dtype=np.int64)
        self.span = span_s
        self.cum = np.concatenate([[0], np.cumsum(span_s)[:-1]]).astype(np.int64)
        self.cum_end = self.cum + span_s
        rng = _hash_rng(ctx.seed, "pickups", wh["code"])
        cut_p, cut_l = _hhmm(wh["cutoffs"]["parcel"]), _hhmm(wh["cutoffs"]["ltl"])
        snow = np.array([geo.weather(ctx, wh["code"], wh["address"]["lat"], wh["address"]["lon"], geo.day_date(d))["snow_cm"]
                         if work[i] and mon[i] in (11, 12, 1, 2, 3, 4) else 0.0 for i, d in enumerate(days)])
        missed = rng.random(len(days)) < np.clip((snow - 4) / 20, 0, 0.95)
        self.snow = snow
        ok = work & ~missed
        self.pick_p = days[ok] * DAY + cut_p + 150 * 60
        self.pick_l = days[ok] * DAY + cut_l + 120 * 60
        self.work = work

    def to_work(self, t: np.ndarray) -> np.ndarray:
        d = np.clip(t // DAY - self.d0, 0, len(self.cum) - 1)
        s = t % DAY
        return self.cum[d] + np.clip(s - self.open[d], 0, self.span[d])

    def from_work(self, w: np.ndarray) -> np.ndarray:
        d = np.searchsorted(self.cum_end, w, side="right")
        d = np.clip(d, 0, len(self.cum) - 1)
        return (self.d0 + d) * DAY + self.open[d] + (w - self.cum[d])

    def add(self, t: np.ndarray, secs: np.ndarray) -> np.ndarray:
        return self.from_work(self.to_work(t) + np.asarray(secs, dtype=np.int64))

    def next_pickup(self, t: np.ndarray, ltl: np.ndarray) -> np.ndarray:
        p = np.where(ltl, self.pick_l[np.clip(np.searchsorted(self.pick_l, t), 0, len(self.pick_l) - 1)],
                     self.pick_p[np.clip(np.searchsorted(self.pick_p, t), 0, len(self.pick_p) - 1)])
        return np.maximum(p, t)


def _cal(ctx: Ctx, st: dict[str, Any], wi: int, need_day: int | None = None) -> WorkCal:
    wh = st["wh_rules"][wi]
    d0 = st["cal_d0"]
    d1 = st["cal_d1"]
    if need_day is not None and need_day + 120 >= d1:
        st["cal_d1"] = d1 = int(need_day + 400)
    key = (ctx.seed, wh["code"], d0, d1)
    cal = _CAL.get(key)
    if cal is None:
        for k in [k for k in _CAL if k[1] == wh["code"] and k[0] == ctx.seed]:
            del _CAL[k]
        cal = _CAL[key] = WorkCal(ctx, wh, d0, d1)
    return cal


def _cal_add(ctx: Ctx, st: dict[str, Any], wi: np.ndarray, t: np.ndarray, secs: np.ndarray) -> np.ndarray:
    out = np.empty(len(t), dtype=np.int64)
    secs = np.broadcast_to(np.asarray(secs, dtype=np.int64), t.shape)
    for w in np.unique(wi):
        m = wi == w
        out[m] = _cal(ctx, st, int(w), int(t[m].max() // DAY)).add(t[m], secs[m])
    return out


def _next_pickup(ctx: Ctx, st: dict[str, Any], wi: np.ndarray, t: np.ndarray, ltl: np.ndarray, scac: np.ndarray) -> np.ndarray:
    out = np.empty(len(t), dtype=np.int64)
    for w in np.unique(wi):
        m = wi == w
        cal = _cal(ctx, st, int(w), int(t[m].max() // DAY))
        out[m] = cal.next_pickup(t[m], ltl[m])
        for (bw, b0, b1) in st["blackouts"]:
            if bw != int(w):
                continue
            hit = (out[m] >= b0) & (out[m] < b1)
            if hit.any():
                sub = out[m]
                sub[hit] = cal.next_pickup(np.full(int(hit.sum()), b1), ltl[m][hit])
                out[m] = sub
    return out


# =============================================================================================== state
def _state(ctx: Ctx) -> dict[str, Any]:
    st = ctx.world.get(NAME)
    if st is None:
        raise RuntimeError("wms state missing: bootstrap() was not run")
    return st


ORDER_SCHEMA = {
    "order_id": np.int64, "order_nbr": object, "owner_id": np.int32, "warehouse_id": np.int16, "client_order_ref": object,
    "order_type": object, "channel": object, "order_status": np.int8, "priority": np.int16, "ordered_ts": np.int64,
    "received_ts": np.int64, "requested_ship_date": np.int64, "carrier_service_id": np.int16, "ship_to_name": object,
    "ship_to_company": object, "ship_to_address1": object, "ship_to_address2": object, "ship_to_city": object,
    "ship_to_state": object, "ship_to_postal": object, "ship_to_country": object, "ship_to_phone": object,
    "ship_to_email": object, "is_residential": bool, "total_lines": np.int32, "total_units": np.int32,
    "cancel_reason": object, "create_ts": np.int64, "mod_ts": np.int64, "create_user": object, "mod_user": object,
    "delivery_instructions": object,
    "_st": np.int8, "wi": np.int8, "ci": np.int32, "city": np.int32, "is_ltl": bool, "is_draft": bool, "rsd_day": np.int64,
    "t_ready": np.int64, "t_alloc": np.int64, "t_release": np.int64, "t_pick_start": np.int64, "t_pick_done": np.int64,
    "t_pack": np.int64, "t_ship": np.int64, "t_cancel": np.int64, "t_addr": np.int64, "t_date": np.int64, "t_lcancel": np.int64,
    "t_drop": np.int64, "new_rsd": np.int64, "bo_since": np.int64, "picker": np.int32, "kit": bool, "zone": np.int16,
    "shipment_id": np.int64,
}
LINE_SCHEMA = {
    "order_id": np.int64, "line_nbr": np.int32, "material_id": np.int32, "ordered_qty": np.int32, "allocated_qty": np.int32,
    "picked_qty": np.int32, "shipped_qty": np.int32, "uom": object, "unit_price": np.float64, "line_status": np.int8,
    "_st": np.int8, "wi": np.int8, "mat": np.int32, "open_alloc": np.int32,
}
ALLOC_SCHEMA = {"akey": np.int64, "order_id": np.int64, "line_nbr": np.int32, "inv_id": np.int64, "qty": np.int32,
                "mat": np.int32, "wi": np.int8, "loc": np.int32, "lot": object, "exp_day": np.int64, "task_id": np.int64,
                "picked": bool, "stage_inv": np.int64}
TASK_SCHEMA = {
    "task_id": np.int64, "warehouse_id": np.int16, "task_type": np.int8, "task_status": np.int8, "priority": np.int16,
    "order_id": np.int64, "order_line_nbr": np.int32, "receipt_id": np.int64, "material_id": np.int32, "qty": np.int32,
    "from_location_id": np.int32, "to_location_id": np.int32, "assigned_user_id": np.int32, "created_ts": np.int64,
    "assigned_ts": np.int64, "started_ts": np.int64, "completed_ts": np.int64, "mod_ts": np.int64,
    "_st": np.int8, "applied": bool, "ref": np.int64, "wi": np.int8,
}
INV_SCHEMA = {
    "inventory_id": np.int64, "warehouse_id": np.int16, "location_id": np.int32, "lpn_id": np.int64, "owner_id": np.int32,
    "material_id": np.int32, "lot_nbr": object, "expiry_date": np.int64, "qty_on_hand": np.int32, "qty_allocated": np.int32,
    "inv_status": np.int8, "hold_reason": object, "received_date": np.int64, "create_ts": np.int64, "mod_ts": np.int64,
    "_st": np.int8,
}
SHIP_SCHEMA = {
    "shipment_id": np.int64, "shipment_nbr": object, "order_id": np.int64, "warehouse_id": np.int16,
    "carrier_service_id": np.int16, "scac": object, "tracking_nbr": object, "bol_nbr": object, "ship_ts": np.int64,
    "shipment_status": np.int8, "total_weight": np.float64, "weight_uom": object, "carton_count": np.int32,
    "freight_charge_est": np.float64, "currency": object, "create_ts": np.int64, "mod_ts": np.int64,
    "_st": np.int8, "billed_lb": np.float64, "label_ts": np.int64, "t_void": np.int64, "t_reprint": np.int64,
}
CARTON_SCHEMA = {"carton_id": np.int64, "shipment_id": np.int64, "carton_nbr": object, "tracking_nbr": object,
                 "weight": np.float64, "length": np.float64, "width": np.float64, "height": np.float64, "dim_uom": object,
                 "create_ts": np.int64, "mod_ts": np.int64, "_st": np.int8}
ASN_SCHEMA = {"asn_id": np.int64, "asn_nbr": object, "owner_id": np.int32, "warehouse_id": np.int16, "supplier_name": object,
              "source": object, "expected_date": np.int64, "asn_status": object, "create_ts": np.int64, "mod_ts": np.int64,
              "_st": np.int8, "t_arrive": np.int64, "t_cancel": np.int64, "wi": np.int8, "receipt_id": np.int64,
              "transfer": bool}
ASNL_SCHEMA = {"asn_id": np.int64, "line_nbr": np.int32, "material_id": np.int32, "expected_qty": np.int32, "lot_nbr": object,
               "expiry_date": np.int64}
RCPT_SCHEMA = {"receipt_id": np.int64, "receipt_nbr": object, "asn_id": np.int64, "owner_id": np.int32, "warehouse_id": np.int16,
               "dock_door": object, "trailer_nbr": object, "arrived_ts": np.int64, "started_ts": np.int64,
               "completed_ts": np.int64, "receipt_status": object, "create_ts": np.int64, "mod_ts": np.int64, "_st": np.int8,
               "wi": np.int8, "lines_left": np.int32, "pallets": np.int32, "cartons": np.int32, "units": np.int32,
               "dts": np.float64}
RLINE_SCHEMA = {"receipt_id": np.int64, "line_nbr": np.int32, "material_id": np.int32, "received_qty": np.int32,
                "damaged_qty": np.int32, "lot_nbr": object, "expiry_date": np.int64, "received_ts": np.int64,
                "received_by": np.int32, "wi": np.int8, "owner_id": np.int32, "t_put": np.int64}
LPN_SCHEMA = {"lpn_id": np.int64, "lpn_nbr": object, "warehouse_id": np.int16, "location_id": np.int32, "lpn_type": object,
              "lpn_status": object, "create_ts": np.int64, "mod_ts": np.int64, "_st": np.int8}
COUNT_SCHEMA = {"count_id": np.int64, "warehouse_id": np.int16, "location_id": np.int32, "material_id": np.int32,
                "lot_nbr": object, "system_qty": np.int32, "counted_qty": np.int32, "variance_qty": np.int32,
                "count_ts": np.int64, "counted_by": np.int32, "count_status": object, "approved_by": np.int32,
                "create_ts": np.int64, "mod_ts": np.int64, "_st": np.int8, "inv_id": np.int64, "t_recount": np.int64,
                "t_approve": np.int64, "wi": np.int8, "owner_id": np.int32}
RET_SCHEMA = {"return_id": np.int64, "rma_nbr": object, "owner_id": np.int32, "warehouse_id": np.int16,
              "original_order_id": np.int64, "carrier_tracking_nbr": object, "received_ts": np.int64, "return_status": object,
              "create_ts": np.int64, "mod_ts": np.int64, "_st": np.int8, "t_rma": np.int64, "t_recv": np.int64,
              "t_proc": np.int64, "wi": np.int8, "order_nbr": object, "key": np.int64}
RETL_SCHEMA = {"key": np.int64, "line_nbr": np.int32, "material_id": np.int32, "qty": np.int32, "reason_code": object,
               "disposition": object, "lot": bool}
USER_SCHEMA = {"user_id": np.int32, "username": object, "first_name": object, "last_name": object, "personnel_nbr": object,
               "warehouse_id": np.int16, "user_role": object, "shift": object, "hire_date": np.int64, "term_date": np.int64,
               "is_active": bool, "create_ts": np.int64, "mod_ts": np.int64, "outlier": bool}
HOLD_SCHEMA = {"inv_id": np.int64, "t_due": np.int64, "destroy": bool, "reason": object, "t_hold": np.int64, "wi": np.int8}
RATE_SCHEMA = {"rate_id": np.int32, "owner_id": np.int32, "charge_code": object, "rate": np.float64, "rate_uom": object,
               "currency": object, "effective_date": np.int64, "mod_ts": np.int64, "mod_user": object}
PRET_SCHEMA = {"key": np.int64, "owner_id": np.int32, "wi": np.int8, "order_id": np.int64, "order_nbr": object,
               "t_rma": np.int64, "t_recv": np.int64, "t_proc": np.int64, "scac": object, "svc": object}


def _new_state(ctx: Ctx) -> dict[str, Any]:
    hs, he = ctx.history_start, ctx.history_end
    return {
        "version": 1,
        "orders": tnew(ORDER_SCHEMA), "lines": tnew(LINE_SCHEMA), "allocs": tnew(ALLOC_SCHEMA), "tasks": tnew(TASK_SCHEMA),
        "inv": tnew(INV_SCHEMA), "ships": tnew(SHIP_SCHEMA), "cartons": tnew(CARTON_SCHEMA), "asns": tnew(ASN_SCHEMA),
        "asn_lines": tnew(ASNL_SCHEMA), "rcpts": tnew(RCPT_SCHEMA), "rlines": tnew(RLINE_SCHEMA), "lpns": tnew(LPN_SCHEMA),
        "counts": tnew(COUNT_SCHEMA), "rets": tnew(RET_SCHEMA), "ret_lines": tnew(RETL_SCHEMA), "pret": tnew(PRET_SCHEMA),
        "holds": tnew(HOLD_SCHEMA), "users": tnew(USER_SCHEMA), "rates": tnew(RATE_SCHEMA),
        "cal_d0": geo.epoch_day(hs.date()) - 150, "cal_d1": geo.epoch_day(he.date()) + 500,
        "blackouts": [], "surges": [], "slowdowns": [], "rate_events": [], "dynamic_done": set(),
        "owners": {}, "wh_ready": set(), "pending_bus": [], "task_days": [], "purged_to": 0, "usernames": set(),
        "billing_recent": [], "door_log": [], "schema_v2": False, "story": {}, "dq_orphans": [],
    }


# =============================================================================================== reference data
def _preset_whs(ctx: Ctx) -> list[dict[str, Any]]:
    return [w for w in ctx.rules["warehouses"] if ctx.preset in w.get("presets", [ctx.preset])]


def _wh_opened(ctx: Ctx, wh: dict[str, Any]) -> date:
    if wh.get("opened"):
        return date.fromisoformat(str(wh["opened"]))
    return month_start(ctx.history_end.date(), int(wh.get("opened_month", -999)))


def _wh_index(ctx: Ctx) -> dict[str, int]:
    return {w["code"]: i for i, w in enumerate(ctx.rules["warehouses"])}


def _setup_reference(ctx: Ctx, st: dict[str, Any]) -> None:
    """Module-level lookups derived from the rules (rebuilt on every load; never stored in DB)."""
    rules = ctx.rules
    whs = rules["warehouses"]
    st["wh_rules"] = whs
    st["wh_code"] = np.array([w["code"] for w in whs], dtype=object)
    st["wh_tz"] = np.array([w["time_zone"] for w in whs], dtype=object)
    st["wh_country"] = np.array([w["address"]["country"] for w in whs], dtype=object)
    st["wh_state"] = np.array([w["address"]["state"] for w in whs], dtype=object)
    st["wh_lat"] = np.array([w["address"]["lat"] for w in whs])
    st["wh_lon"] = np.array([w["address"]["lon"] for w in whs])
    st["wh_kg"] = np.array([w["uom"]["weight"] == "KG" for w in whs])
    st["wh_entity"] = np.array([w["legal_entity"] for w in whs], dtype=object)
    cur = {e["dataareaid"]: e["currency"] for e in rules["legal_entities"]}
    st["wh_currency"] = np.array([cur[w["legal_entity"]] for w in whs], dtype=object)
    st["wh_city"] = np.array([geo.city_index(w["address"]["city"], w["address"]["state"], w["address"]["country"]) for w in whs])
    st["wh_open_day"] = np.array([geo.epoch_day(_wh_opened(ctx, w)) for w in whs], dtype=np.int64)
    st["wh_present"] = np.array([ctx.preset in w.get("presets", [ctx.preset]) for w in whs])
    st["svc"] = carr.services(rules)
    c = geo.cities()
    st["dist"] = geo.haversine_mi(c["lat"].to_numpy()[:, None], c["lon"].to_numpy()[:, None],
                                  st["wh_lat"][None, :], st["wh_lon"][None, :])
    st["vert"] = rules["verticals"]


def _boot_warehouse(ctx: Ctx, st: dict[str, Any], fl: Flush, wi: int, ts_local: int, now_local: int | None = None) -> None:
    """Warehouse row, zones (temperature ranges from cold_chain) and locations sized to the racking."""
    wh = st["wh_rules"][wi]
    wid = wi + 1
    opened = st["wh_open_day"][wi]
    rng = _hash_rng(ctx.seed, "locations", wh["code"])
    now_day = (now_local if now_local is not None else ts_local) // DAY
    fl.insert("warehouse", pd.DataFrame([{
        "warehouse_id": wid, "warehouse_code": wh["code"], "warehouse_name": wh["name"], "address1": wh["address"]["street"],
        "city": wh["address"]["city"], "state_prov": wh["address"]["state"], "postal_code": wh["address"]["postal"],
        "country_code": wh["address"]["country"], "time_zone": wh["time_zone"], "weight_uom": wh["uom"]["weight"],
        "dim_uom": wh["uom"]["dim"], "is_active": bool(opened <= now_day), "opened_date": geo.day_date(opened).isoformat(),
        "create_ts": ts_col(np.array([ts_local]))[0], "mod_ts": ts_col(np.array([ts_local]))[0]}]))
    cold = ctx.rules["cold_chain"]
    zones = list(wh["zones"]) + ["DOCK", "STAGING"]
    zrows, lrows = [], []
    loc_ids, loc_zone, loc_type, loc_aisle, loc_bay = [], [], [], [], []
    scale = LOC_SCALE.get(ctx.preset, 0.1)
    storage = [z for z in wh["zones"]]
    wsum = sum(ZONE_WEIGHT.get(z, 0.1) for z in storage)
    total = int((wh["pallet_positions"] + wh["pick_faces"]) * scale)
    pick_share = wh["pick_faces"] / (wh["pallet_positions"] + wh["pick_faces"])
    zone_ids = ctx.next_ids("wms.zone", len(zones))
    kg = wh["uom"]["weight"] == "KG"
    for zid, z in zip(zone_ids, zones):
        rng_c = cold.get(z, {}) if z in ("COOLER", "FREEZER") else {}
        zrows.append({"zone_id": int(zid), "warehouse_id": wid, "zone_code": f"{wh['code']}-{z}", "zone_type": z,
                      "temp_min_c": rng_c.get("min_c"), "temp_max_c": rng_c.get("max_c"), "is_active": True})
        if z == "DOCK":
            n = wh["dock_doors"]
            codes = [f"DOOR-{i:02d}" for i in range(1, n + 1)]
            parts = [("", "", "", "")] * n
            types = [LT_DOCK] * n
        elif z == "STAGING":
            n = max(4, wh["dock_doors"] // 3)
            codes = [f"STG-{i:02d}" for i in range(1, n + 1)] + [f"QC-{i:02d}" for i in range(1, 5)]
            parts = [("", "", "", "")] * len(codes)
            types = [LT_STAGE] * n + [LT_QC] * 4
        else:
            n = max(40, int(total * ZONE_WEIGHT.get(z, 0.1) / wsum))
            letter = ZONE_LETTER.get(z, "X")
            if z == "RETURNS":
                n_pick, n_res = n, 0
            elif z == "BULK":
                n_pick, n_res = 0, n
            else:
                n_pick = int(n * pick_share)
                n_res = n - n_pick
            codes, parts, types = [], [], []
            for count, levels, positions, lt in ((n_pick, "AB", 3, LT_RET if z == "RETURNS" else LT_PICK), (n_res, "CDEF", 2, LT_RES)):
                if count <= 0:
                    continue
                per_aisle = 30 * len(levels) * positions
                n_aisles = math.ceil(count / per_aisle)
                start_aisle = 1 if lt != LT_RES or n_pick == 0 else math.ceil(n_pick / (30 * 2 * 3)) + 1
                k = 0
                for a in range(start_aisle, start_aisle + n_aisles):
                    for bay in range(1, 31):
                        for lev in levels:
                            for p in range(1, positions + 1):
                                if k >= count:
                                    break
                                aisle = f"{letter}{a:02d}"
                                codes.append(f"{aisle}-{bay:02d}-{lev}-{p}")
                                parts.append((aisle, f"{bay:02d}", lev, str(p)))
                                types.append(lt)
                                k += 1
        ids = ctx.next_ids("wms.location", len(codes))
        for lid, code, part, lt in zip(ids, codes, parts, types):
            heavy = lt in (LT_RES, LT_DOCK, LT_STAGE)
            lrows.append({"location_id": int(lid), "warehouse_id": wid, "zone_id": int(zid), "location_code": code,
                          "aisle": part[0] or None, "bay": part[1] or None, "level": part[2] or None, "position": part[3] or None,
                          "location_type": LOC_TYPES[lt], "max_weight": (1134.0 if kg else 2500.0) if heavy else (227.0 if kg else 500.0),
                          "max_volume": (2.27 if kg else 80.0) if heavy else (0.85 if kg else 30.0), "is_active": True})
        loc_ids.append(np.asarray(ids))
        loc_zone.append(np.full(len(ids), z, dtype=object))
        loc_type.append(np.asarray(types, dtype=np.int8))
        loc_aisle.append(np.array([p[0] for p in parts], dtype=object))
        loc_bay.append(np.array([int(p[1]) if p[1] else 0 for p in parts], dtype=np.int16))
    zdf = pd.DataFrame(zrows)
    zdf["create_ts"] = zdf["mod_ts"] = ts_col(np.array([ts_local]))[0]
    ldf = pd.DataFrame(lrows)
    ldf["create_ts"] = ldf["mod_ts"] = ts_col(np.array([ts_local]))[0]
    fl.insert("zone", zdf)
    fl.insert("location", ldf)
    ids = np.concatenate(loc_ids)
    loc = st.setdefault("loc", {"id": np.zeros(0, np.int32), "wi": np.zeros(0, np.int8), "zone": np.zeros(0, object),
                                "type": np.zeros(0, np.int8), "aisle": np.zeros(0, object), "bay": np.zeros(0, np.int16)})
    loc["id"] = np.concatenate([loc["id"], ids.astype(np.int32)])
    loc["wi"] = np.concatenate([loc["wi"], np.full(len(ids), wi, np.int8)])
    loc["zone"] = np.concatenate([loc["zone"], np.concatenate(loc_zone)])
    loc["type"] = np.concatenate([loc["type"], np.concatenate(loc_type)])
    loc["aisle"] = np.concatenate([loc["aisle"], np.concatenate(loc_aisle)])
    loc["bay"] = np.concatenate([loc["bay"], np.concatenate(loc_bay)])
    del rng
    st["wh_ready"].add(wi)


def _loc_lookup(st: dict[str, Any]) -> dict[str, np.ndarray]:
    """Dense arrays indexed by location_id (rebuilt when locations are added)."""
    loc = st["loc"]
    key = len(loc["id"])
    cache = st.get("_loc_cache")
    if cache is not None and cache["n"] == key:
        return cache
    m = int(loc["id"].max()) + 1
    ztype = np.full(m, None, dtype=object)
    ltype = np.full(m, -1, dtype=np.int8)
    ztype[loc["id"]] = loc["zone"]
    ltype[loc["id"]] = loc["type"]
    pools: dict[tuple[int, str, int], np.ndarray] = {}
    df = pd.DataFrame({"id": loc["id"], "wi": loc["wi"], "zone": loc["zone"], "type": loc["type"]})
    for (w, z, t), g in df.groupby(["wi", "zone", "type"]):
        pools[(int(w), str(z), int(t))] = g["id"].to_numpy(np.int32)
    cache = {"n": key, "ztype": ztype, "ltype": ltype, "pools": pools}
    st["_loc_cache"] = cache
    return cache


def _pick_locations(st: dict[str, Any], rng: np.random.Generator, wi: np.ndarray, zone: np.ndarray, lt: np.ndarray) -> np.ndarray:
    lk = _loc_lookup(st)
    out = np.zeros(len(wi), dtype=np.int32)
    keys = pd.Series(list(zip(wi.tolist(), zone.tolist(), lt.tolist())))
    for k, idx in keys.groupby(keys).groups.items():
        w, z, t = k
        pool = lk["pools"].get((w, z, t))
        if pool is None:
            pool = lk["pools"].get((w, z, LT_PICK if t == LT_RES else LT_RES))
        if pool is None:
            pool = lk["pools"].get((w, "AMBIENT", t))
        if pool is None:
            pool = lk["pools"].get((w, "AMBIENT", LT_RES))
        if pool is None:
            pool = next(v for kk, v in lk["pools"].items() if kk[0] == w and kk[2] in (LT_PICK, LT_RES))
        idx = np.asarray(idx)
        out[idx] = pool[rng.integers(0, len(pool), len(idx))]
    return out


def _special_locs(st: dict[str, Any], wi: int, lt: int) -> np.ndarray:
    lk = _loc_lookup(st)
    zone = "DOCK" if lt == LT_DOCK else "STAGING"
    return lk["pools"].get((wi, zone, lt), np.zeros(0, np.int32))


def _boot_carriers(ctx: Ctx, st: dict[str, Any], fl: Flush, ts_local: int) -> None:
    svc = st["svc"]
    ts = ts_col(np.array([ts_local]))[0]
    car = svc.drop_duplicates("carrier_id")
    fl.insert("carrier", pd.DataFrame({"carrier_id": car["carrier_id"], "scac": car["scac"], "carrier_name": car["carrier_name"],
                                       "mode": car["mode"], "is_active": True, "mod_ts": ts}))
    fl.insert("carrier_service", pd.DataFrame({"service_id": svc["service_id"], "carrier_id": svc["carrier_id"],
                                               "service_code": svc["service_code"], "service_name": svc["service_name"],
                                               "transit_days_min": svc["tmin"], "transit_days_max": svc["tmax"],
                                               "is_active": True, "mod_ts": ts}))
    fl.insert("sys_release", pd.DataFrame([{"release_id": 1, "version": "7.4.2", "released_ts": ts_col(np.array([ts_local - 200 * DAY]))[0],
                                            "notes": "Wave planning engine 2.0; tote-level pick confirmation (RF guns)."}]))


# =============================================================================================== clients
def _sim_clients(ctx: Ctx) -> list[dict[str, Any]]:
    present = {w["code"] for w in _preset_whs(ctx)}
    out = []
    for i, c in enumerate(ctx.rules["clients"]):
        ws = [w for w in c.get("warehouses", []) if w in present]
        if ws:
            out.append({**c, "warehouses": ws, "_idx": i})
    return out


def _client_info(ctx: Ctx, st: dict[str, Any], c: dict[str, Any]) -> dict[str, Any]:
    he = ctx.history_end.date()
    dates = client_dates(c, ctx.seed, he)
    whi = _wh_index(ctx)
    vert = ctx.rules["verticals"][c["vertical"]]
    primary = whi[c["warehouses"][0]]
    churn = st.get("churn_override", {}).get(c["code"]) or dates.churn
    sig = (ctx.planted.get("client_behaviour", {}) or {}).get("churn_signals", {}).get(c["code"])
    rng = _hash_rng(ctx.seed, "client-info", c["code"])
    cat = geo.products()["client_category"].get(c["code"], geo.products()["vertical_default"][c["vertical"]])
    temp = c.get("temp", vert["temp"])
    zones = st["wh_rules"][primary]["zones"]
    if temp not in zones:
        temp = "AMBIENT"
    origins = geo.products()["categories"][cat].get("origin", ["US"])
    imported = sum(o not in ("US", "CA", "MX") for o in origins) / len(origins) > 0.5
    country = st["wh_country"][primary]
    lo, hi = vert["lines_b2c"]
    blo, bhi = vert["lines_b2b"]
    return {
        "code": c["code"], "name": c["name"], "owner_id": c["_idx"] + 1, "vertical": c["vertical"], "tier": c["tier"],
        "currency": c["currency"], "entity": c["entity"], "whs": [whi[w] for w in c["warehouses"]], "primary": primary,
        "go_live": geo.epoch_day(dates.go_live), "wms_created": geo.epoch_day(dates.wms_created),
        "churn": geo.epoch_day(churn) if churn else None, "sig": sig, "b2c": vert["b2c_share"], "edi": bool(vert.get("edi")),
        "lines_b2c": (lo, hi), "lines_b2b": (blo, bhi), "return_rate": vert["return_rate"], "peak": vert["peak_multiplier"],
        "lot": bool(vert["lot"]), "serial": bool(vert["serial"]), "temp": temp, "cat": cat, "imported": imported,
        "country": country, "kg": bool(st["wh_kg"][primary]), "bulky": bool(c.get("bulky")) or cat in ("furniture", "rugs", "tires"),
        "trend": float(rng.normal(0.06, 0.07)), "lead": int(rng.integers(24, 38) if imported else rng.integers(4, 10)),
        "dynamic": bool(c.get("dynamic")), "contact": None,
        "e_units": _expected_units(vert, c["vertical"], cat),
    }


def _expected_units(vert: dict[str, Any], vertical: str, cat: str) -> float:
    lo, hi = vert["lines_b2c"]
    blo, bhi = vert["lines_b2b"]
    e_b2c = (lo + (hi - lo + 1) / 3.5 - 0.5) * 1.28
    case = np.mean(geo.products()["categories"][cat]["case_qty"])
    e_b2b = (blo + (bhi - blo + 1) / 6.0 - 0.5) * (case * 2.5 if vertical != "auto_parts" else 6.0)
    return vert["b2c_share"] * e_b2c + (1 - vert["b2c_share"]) * e_b2b


def _daily_rates(ctx: Ctx, st: dict[str, Any], infos: list[dict[str, Any]], day: int) -> tuple[np.ndarray, np.ndarray]:
    """Expected B2C and B2B orders per client on a local day (before Poisson noise)."""
    rules = ctx.rules
    vs = float(rules["size_presets"][ctx.preset]["volume_scale"])
    d = geo.day_date(day)
    dow = d.weekday()
    shape_c = rules["calendars"]["weekday_shape_b2c"][dow]
    shape_b = rules["calendars"]["weekday_shape_b2b"][dow]
    season = _peak_shape(d)
    cyber = _is_cyber(d)
    boost = float(rules["calendars"]["peak_season"].get("cyber_week_boost", 1.6))
    n = len(infos)
    noise = np.exp(_hash_rng(ctx.seed, "wms-daily", day).normal(0, 0.12, max(n, 64))[:n])
    b2c = np.zeros(n)
    b2b = np.zeros(n)
    hs_day = geo.epoch_day(ctx.history_start.date())
    for i, c in enumerate(infos):
        if day < c["go_live"] or (c["churn"] is not None and day >= c["churn"]):
            continue
        base = rules["volume_tiers"][c["tier"]] * ORDER_CAL * vs
        ramp = min(1.0, 0.3 + 0.7 * (day - c["go_live"]) / 75.0)
        trend = 1.0 + c["trend"] * (day - hs_day) / 365.0
        decline = 1.0
        sig = c["sig"]
        if sig:
            start = geo.epoch_day(month_start(ctx.history_end.date(), int(sig["from_month"])))
            end = c["churn"] if c["churn"] else geo.epoch_day(ctx.history_end.date())
            if day >= start:
                frac = min(1.0, (day - start) / max(end - start, 1))
                decline = 1 - float(sig["volume_decline_pct"]) / 100 * frac
        if c["churn"] is not None and day >= c["churn"] - 21:
            decline *= max(0.05, (c["churn"] - day) / 21.0)
        country = c["country"]
        hol = ctx.signals.is_holiday(country, d)
        pm = c["peak"]
        sc = (1 + (pm - 1) * season) * (boost if cyber else 1.0)
        sb = 1 + (pm - 1) * 0.3 * season
        if d.month == 12 and d.day >= 25:
            sc *= 0.8
        lvl = base * ramp * trend * decline * noise[i] * _surge(st, c, day)
        b2c[i] = lvl * c["b2c"] * shape_c * sc * (0.55 if hol else 1.0)
        b2b[i] = lvl * (1 - c["b2c"]) * shape_b * sb * (0.05 if hol else 1.0)
    return b2c, b2b


def _surge(st: dict[str, Any], c: dict[str, Any], day: int) -> float:
    f = 1.0
    for (wh, d0, d1, mult) in st["surges"]:
        if d0 <= day < d1 and (wh is None or wh in c["whs"]):
            f *= mult
    return f


def _thanksgiving(year: int) -> date:
    d = date(year, 11, 1)
    first_thu = d + timedelta(days=(3 - d.weekday()) % 7)
    return first_thu + timedelta(days=21)


def _is_cyber(d: date) -> bool:
    tg = _thanksgiving(d.year)
    return tg <= d <= tg + timedelta(days=4)


def _peak_shape(d: date) -> float:
    if not ((d.month == 11 and d.day >= 15) or (d.month == 12 and d.day <= 24)):
        return 0.0
    tg = _thanksgiving(d.year)
    if d < tg:
        return 0.25 + 0.35 * (d - date(d.year, 11, 15)).days / max((tg - date(d.year, 11, 15)).days, 1)
    if d <= tg + timedelta(days=4):
        return 1.0
    if d <= date(d.year, 12, 15):
        return 0.75 - 0.2 * (d - tg - timedelta(days=4)).days / max((date(d.year, 12, 15) - tg - timedelta(days=4)).days, 1)
    return max(0.1, 0.45 - 0.04 * (d.day - 15))


def _route_share(st: dict[str, Any], info: dict[str, Any], whs: list[int]) -> np.ndarray:
    """Share of a client's orders each of its warehouses serves (nearest-site routing over population)."""
    c = geo.cities()
    mask = (c["country"] == info["country"]).to_numpy()
    w = c["pop_k"].to_numpy(float) ** 0.85 * mask
    d = st["dist"][:, whs]
    near = np.argmin(d, axis=1)
    share = np.array([w[near == k].sum() for k in range(len(whs))])
    share = 0.85 * share / max(share.sum(), 1e-9) + 0.15 / len(whs)
    return share


# =============================================================================================== owners & catalog
def _create_owner(ctx: Ctx, st: dict[str, Any], fl: Flush, info: dict[str, Any], ts_local: int, at_boot: bool) -> None:
    rng = _hash_rng(ctx.seed, "owner", info["code"])
    oid = info["owner_id"]
    first, last = geo.names(rng, np.array(["es" if info["country"] == "MX" else "fr" if info["code"] == "BRUNE" else "en"]))
    dom = "".join(ch for ch in info["name"].lower() if ch.isalnum()) + ".example"
    email = f"{geo._ascii(first[0]).lower()}.{geo._ascii(last[0]).lower().replace(' ', '')}@{dom}"
    blank_erp = info["code"] in ("FERNKID", "HARROW")
    today = ts_local // DAY
    status = "ACTIVE" if info["go_live"] <= today else "ONBOARDING"
    if info["churn"] is not None and today >= info["churn"]:
        status = "INACTIVE"
    erp = "" if blank_erp else ax_account_num(ctx.rules, info["code"])
    created_ts = (info["wms_created"] * DAY + 10 * 3600) if at_boot else ts_local
    fl.insert("owner", pd.DataFrame([{
        "owner_id": oid, "owner_code": info["code"], "owner_name": info["name"], "owner_status": status,
        "vertical": info["vertical"], "erp_account_nbr": erp or None, "billing_currency": info["currency"],
        "primary_warehouse_id": info["primary"] + 1, "contact_name": f"{first[0]} {last[0]}", "contact_email": email,
        "go_live_date": geo.day_date(info["go_live"]).isoformat(), "create_ts": ts_col(np.array([created_ts]))[0],
        "mod_ts": ts_col(np.array([max(created_ts, ts_local if at_boot else created_ts)]))[0], "create_user": "mbell",
        "mod_user": "mbell"}]))
    info["status"] = status
    info["ow_done"] = set()
    st["owners"][info["code"]] = info
    _owner_warehouses(ctx, st, fl, info, today)
    _create_catalog(ctx, st, fl, info, ts_local, at_boot)
    _key_initial_rates(ctx, st, fl, info, ts_local)
    _make_shiptos(ctx, st, info)
    st["pending_bus"].append(("clients_created", pd.DataFrame([{
        "owner_id": oid, "owner_code": info["code"], "owner_name": info["name"],
        "wms_created_date": geo.day_date(info["wms_created"]), "go_live": geo.day_date(info["go_live"]),
        "primary_warehouse_code": st["wh_code"][info["primary"]]}])))


def _owner_warehouses(ctx: Ctx, st: dict[str, Any], fl: Flush, info: dict[str, Any], today: int) -> None:
    rows = []
    for w in info["whs"]:
        if w in info["ow_done"] or w not in st["wh_ready"]:
            continue
        info["ow_done"].add(w)
        frm = max(info["go_live"], int(st["wh_open_day"][w]))
        rows.append({"owner_id": info["owner_id"], "warehouse_id": w + 1, "active_from": geo.day_date(frm).isoformat(),
                     "active_to": None})
    if rows:
        fl.insert("owner_warehouse", pd.DataFrame(rows))


def _upc_check(body: str) -> str:
    s = sum(int(ch) * (3 if i % 2 == 0 else 1) for i, ch in enumerate(body))
    return str((10 - s % 10) % 10)


def _ean_check(body: str) -> str:
    s = sum(int(ch) * (1 if i % 2 == 0 else 3) for i, ch in enumerate(body))
    return str((10 - s % 10) % 10)


MAT_SCHEMA = {"material_id": np.int32, "owner_id": np.int32, "sku": object, "upc": object, "description": object,
              "lb": np.float64, "l_in": np.float64, "w_in": np.float64, "h_in": np.float64, "case_qty": np.int32,
              "cpp": np.int32, "lot": bool, "shelf": np.int32, "temp": object, "value": np.float64, "status": np.int8,
              "pop": np.float64, "sellable": bool, "created_day": np.int64, "kg": bool, "zone": object, "hazmat": object,
              "item_seq": np.int32}


def _catalog_rows(ctx: Ctx, info: dict[str, Any], n: int, seq0: int, rng: np.random.Generator,
                  existing: set[str]) -> dict[str, np.ndarray]:
    prod = geo.products()
    cat = prod["categories"][info["cat"]]
    items = cat["items"]
    lang = cat.get("lang", "en")
    variants = [(k, prod["fitments"][lang] if v == "$fitments" else v) for k, v in cat["variants"].items()]
    mods = prod["modifier_groups"][cat["mods"]]
    single = len(variants) == 1 and len(variants[0][1]) == 1
    prefix = "".join(ch for ch in info["code"] if ch.isalpha())[:3]
    rows: dict[str, list] = {k: [] for k in ("sku", "desc", "lb", "l", "w", "h", "value", "hazmat", "seq")}
    seen: set[str] = set(existing)
    tries = 0
    seq = seq0
    while len(rows["sku"]) < n and tries < n * 20:
        tries += 1
        it = items[int(rng.integers(0, len(items)))]
        style = int(rng.integers(1000, 9999))
        model = "".join(ch for ch in it[0].upper() if ch.isalpha())[:2] + str(int(rng.integers(100, 990)))
        vals = [str(v[1][int(rng.integers(0, len(v[1])))]) for v in variants]
        v1 = vals[0] if vals else ""
        v2 = vals[1] if len(vals) > 1 else v1
        fit_idx = variants[0][1].index(v1) if variants and variants[0][0] == "fit" else 0
        tok = {"p": prefix, "n4": f"{style:04d}", "m": model, "c3": "".join(ch for ch in v1.upper() if ch.isalnum())[:3],
               "c2": "".join(ch for ch in v1.upper() if ch.isalnum())[:2], "s": v2.replace(" ", "")[:4].upper(),
               "s2": "".join(ch for ch in v2.upper() if ch.isalnum())[:2], "fit": "ABCDEFGHJKLMNPRSTUVW"[fit_idx % 20]}
        sku = cat["sku"].format(**tok)
        if sku in seen:
            continue
        seen.add(sku)
        name = it[0]
        if single or rng.random() < 0.35:
            mod = mods[int(rng.integers(0, len(mods)))]
            name = f"{name} {mod}" if lang == "es" else f"{mod} {name}"
        if variants and not single:
            sep = " - Fits " if variants[0][0] == "fit" and lang == "en" else (" - Para " if variants[0][0] == "fit" else " - ")
            name = f"{name}{sep}{v1}" + (f" / {v2}" if len(vals) > 1 else "")
        f = float(rng.uniform(0.85, 1.15))
        rows["sku"].append(sku)
        rows["desc"].append(name[:200])
        rows["lb"].append(round(it[1] * f, 3))
        rows["l"].append(round(it[2] * f ** 0.33, 2))
        rows["w"].append(round(it[3] * f ** 0.33, 2))
        rows["h"].append(round(it[4] * f ** 0.33, 2))
        rows["value"].append(round(it[5] * float(rng.uniform(0.85, 1.2)), 2))
        hz = None
        if cat.get("hazmat_share") and rng.random() < cat["hazmat_share"]:
            hz = "9"
        if "Dry Shampoo" in it[0]:
            hz = "2.1"
        rows["hazmat"].append(hz)
        rows["seq"].append(seq)
        seq += 1
    return {k: np.asarray(v) for k, v in rows.items()}


def _create_catalog(ctx: Ctx, st: dict[str, Any], fl: Flush, info: dict[str, Any], ts_local: int, at_boot: bool,
                    n: int | None = None, created_day: int | None = None) -> np.ndarray:
    """Item master rows (+ material_uom) for a client. Returns the new material ids."""
    rng = _hash_rng(ctx.seed, "catalog", info["code"], n or 0, created_day or 0)
    if n is None:
        n = max(25, int(SKUS_BY_TIER[info["tier"]] * SKU_PRESET.get(ctx.preset, 1.0)))
    seq0 = info.get("item_seq", 1)
    mat0 = st.get("mat")
    existing = set(mat0["sku"][mat0["owner_id"] == info["owner_id"]].tolist()) if mat0 is not None else set()
    r = _catalog_rows(ctx, info, n, seq0, rng, existing)
    n = len(r["sku"])
    if not n:
        return np.zeros(0, np.int32)
    info["item_seq"] = int(seq0 + n)
    cat = geo.products()["categories"][info["cat"]]
    ids = ctx.next_ids("wms.material", n).astype(np.int32)
    case = rng.integers(cat["case_qty"][0], cat["case_qty"][1] + 1, n)
    cpp = rng.integers(cat["cases_per_pallet"][0], cat["cases_per_pallet"][1] + 1, n)
    if info["bulky"]:
        case = np.ones(n, dtype=int)
    shelf_rng = cat.get("shelf_life") or ctx.rules["verticals"][info["vertical"]].get("shelf_life_days")
    lot = info["lot"]
    shelf = rng.integers(shelf_rng[0], shelf_rng[1] + 1, n) if (lot and shelf_rng) else np.zeros(n, int)
    upc = _upcs(info, r["seq"])
    pop = rng.pareto(1.1, n) + 0.05
    zone = np.full(n, info["temp"] if info["temp"] in ("COOLER", "FREEZER") else "AMBIENT", dtype=object)
    whz = set().union(*[set(st["wh_rules"][w]["zones"]) for w in info["whs"]])
    if info["bulky"] and "BULK" in whz:
        zone[:] = "BULK"
    if "CAGE" in whz:
        zone[(r["value"] > 40) & (zone == "AMBIENT") & (rng.random(n) < 0.6)] = "CAGE"
    if "HAZMAT" in whz:
        zone[np.array([h is not None for h in r["hazmat"]]) & (zone == "AMBIENT")] = "HAZMAT"
    day = created_day if created_day is not None else ts_local // DAY
    m = {
        "material_id": ids, "owner_id": np.full(n, info["owner_id"]), "sku": r["sku"], "upc": upc, "description": r["desc"],
        "lb": r["lb"], "l_in": r["l"], "w_in": r["w"], "h_in": r["h"], "case_qty": case, "cpp": cpp,
        "lot": np.full(n, lot), "shelf": shelf, "temp": np.full(n, info["temp"], dtype=object), "value": r["value"],
        "status": np.zeros(n, np.int8), "pop": pop, "sellable": np.zeros(n, bool), "created_day": np.full(n, day),
        "kg": np.full(n, info["kg"]), "zone": zone, "hazmat": r["hazmat"], "item_seq": r["seq"],
    }
    mat = st.setdefault("mat", tnew(MAT_SCHEMA))
    tadd(mat, n, **m)
    kg = info["kg"]
    wf = 1 / LB_PER_KG if kg else 1.0
    df_ = 2.54 if kg else 1.0
    if at_boot:
        cts = (info["wms_created"] * DAY + rng.integers(8 * 3600, 18 * 3600, n) + rng.integers(0, 12, n) * DAY)
    else:
        cts = np.full(n, ts_local) + rng.integers(0, 600, n)
    mdf = pd.DataFrame({
        "material_id": ids, "owner_id": info["owner_id"], "sku": r["sku"], "description": r["desc"], "upc": upc,
        "base_uom": "EA", "unit_weight": np.round(r["lb"] * wf, 3), "weight_uom": "KG" if kg else "LB",
        "unit_length": np.round(r["l"] * df_, 2), "unit_width": np.round(r["w"] * df_, 2), "unit_height": np.round(r["h"] * df_, 2),
        "dim_uom": "CM" if kg else "IN", "case_qty": case, "cases_per_pallet": cpp, "is_lot_controlled": lot,
        "is_serial_controlled": info["serial"] & (r["value"] > 20), "shelf_life_days": nint(shelf),
        "temp_class": info["temp"], "hazmat_class": r["hazmat"], "unit_value": r["value"],
        "country_of_origin": rng.choice(cat.get("origin", ["US"]), n), "hs_code": cat.get("hs"), "material_status": "ACTIVE",
        "create_ts": ts_col(cts), "mod_ts": ts_col(cts), "create_user": "svc_itemload", "mod_user": "svc_itemload",
    })
    _dq_material(ctx, rng, mdf, kg)
    fl.insert("material", mdf)
    uom = []
    for code, q in (("EA", np.ones(n, int)), ("CS", case), ("PL", case * cpp)):
        uom.append(pd.DataFrame({"material_id": ids, "uom_code": code, "qty_per_uom": q,
                                 "weight": np.round(r["lb"] * wf * q * (1.04 if code != "EA" else 1.0) + (0 if code != "PL" else (22 if kg else 48)), 3),
                                 "length": np.round((r["l"] if code == "EA" else np.where(code == "PL", 48, r["l"] * 2.2)) * df_, 2),
                                 "width": np.round((r["w"] if code == "EA" else np.where(code == "PL", 40, r["w"] * 2.0)) * df_, 2),
                                 "height": np.round((r["h"] if code == "EA" else np.where(code == "PL", 52, r["h"] * 1.8)) * df_, 2),
                                 "mod_ts": ts_col(cts)}))
    fl.insert("material_uom", pd.concat(uom, ignore_index=True))
    return ids


def _upcs(info: dict[str, Any], seq: np.ndarray) -> np.ndarray:
    h = int(hashlib.md5(info["code"].encode()).hexdigest()[:8], 16)
    out = []
    if info["cat"] == "books":
        pub = 7000 + h % 2900
        for s in seq:
            body = f"9781{pub:04d}{int(s) % 10000:04d}"
            out.append(body + _ean_check(body))
    elif info["country"] == "MX":
        comp = 1000 + h % 8999
        for s in seq:
            body = f"750{comp:04d}{int(s) % 100000:05d}"
            out.append(body + _ean_check(body))
    else:
        comp = (h % 89999) + 10000
        lead = "08" if info["country"] == "US" else "06"
        for s in seq:
            body = f"{lead[1]}{comp:05d}{int(s) % 100000:05d}"
            out.append(body + _upc_check(body))
    return np.asarray(out, dtype=object)


def _dq_material(ctx: Ctx, rng: np.random.Generator, mdf: pd.DataFrame, kg: bool) -> None:
    m = ctx.dq.mask("unit_mixup", len(mdf), rng)
    for i in np.flatnonzero(m):
        before = float(mdf.at[i, "unit_weight"])
        after = round(before * LB_PER_KG, 3) if kg else round(before / LB_PER_KG, 3)
        mdf.at[i, "unit_weight"] = after
        ctx.dq.log("unit_mixup", "wms", "material", str(mdf.at[i, "material_id"]), "unit_weight", before, after,
                   f"weight keyed in {'LB' if kg else 'KG'} but weight_uom says {mdf.at[i, 'weight_uom']}")
    m = ctx.dq.mask("null_required", len(mdf), rng)
    for i in np.flatnonzero(m):
        before = mdf.at[i, "upc"]
        mdf.at[i, "upc"] = None
        ctx.dq.log("null_required", "wms", "material", str(mdf.at[i, "material_id"]), "upc", before, None, "UPC missing on item master")
    m = ctx.dq.mask("mojibake", len(mdf), rng)
    for i in np.flatnonzero(m):
        before = mdf.at[i, "description"]
        if before.isascii():
            continue
        mdf.at[i, "description"] = geo.mojibake(before)[:200]
        ctx.dq.log("mojibake", "wms", "material", str(mdf.at[i, "material_id"]), "description", before, mdf.at[i, "description"])


def _key_initial_rates(ctx: Ctx, st: dict[str, Any], fl: Flush, info: dict[str, Any], ts_local: int) -> None:
    """Billing analysts key the contract into billing_rate. Later anniversaries are keyed by applying the % to the
    current WMS rate (planted: GLOWTH's month -10 increase is never keyed; LUMEN's RET-PROC typo at month -14)."""
    he = ctx.history_end.date()
    client = next(c for c in ctx.rules["clients"] if c["code"] == info["code"])
    versions = contract_rates(ctx.rules, client, ctx.seed, he, lambda d, c: ctx.signals.fx(d, c))
    info["versions"] = versions
    today = ts_local // DAY
    current = versions[0]
    for v in versions:
        if geo.epoch_day(v.effective_date) <= max(today, info["go_live"]):
            current = v
    rng = _hash_rng(ctx.seed, "rate-keying", info["code"])
    uoms = {c["code"]: c["uom"] for c in ctx.rules["charge_codes"]}
    codes = list(current.rates)
    ids = ctx.next_ids("wms.billing_rate", len(codes)).astype(np.int32)
    rates = np.array([current.monthly_minimum if c == "MIN-ADJ" else current.rates[c] for c in codes])
    eff = geo.epoch_day(current.effective_date)
    keyed = max(ts_local, (eff - int(rng.integers(3, 12))) * DAY + 14 * 3600) if eff * DAY > ts_local else ts_local
    keyed = min(keyed, ts_local) if eff * DAY <= ts_local else ts_local
    tadd(st["rates"], len(codes), rate_id=ids, owner_id=info["owner_id"], charge_code=np.array(codes, dtype=object),
         rate=rates, rate_uom=np.array([uoms[c] for c in codes], dtype=object), currency=info["currency"],
         effective_date=eff, mod_ts=keyed, mod_user="kgarza")
    sel = st["rates"]["owner_id"] == info["owner_id"]
    fl.insert("billing_rate", _rate_frame(st, sel))
    story = {s["id"]: s for s in ctx.planted.get("stories", [])}
    glowth = story.get("billing_leakage_unkeyed_increase", {})
    typo = story.get("billing_leakage_typo", {})
    skip = None
    if info["code"] == glowth.get("client") and len(versions) > 1:
        target = geo.epoch_day(month_start(he, int(glowth.get("month", -10))))
        skip = min(versions[1:], key=lambda v: abs(geo.epoch_day(v.effective_date) - target)).version
        info["unkeyed_version"] = skip
    for v in versions[1:]:
        e = geo.epoch_day(v.effective_date)
        if e <= today or v.version == skip:
            continue
        lag = int(rng.choice([0, 0, 0, 1, 2, 3, 5, 8]))
        st["rate_events"].append({"code": info["code"], "day": e + lag, "eff": e, "pct": v.increase_pct, "kind": "increase"})
    if info["code"] == typo.get("client"):
        d = geo.epoch_day(month_start(he, int(typo.get("month", -14)))) + 6
        st["rate_events"].append({"code": info["code"], "day": d, "eff": d, "kind": "typo", "charge": typo.get("charge_code", "RET-PROC"),
                                  "rate": float(typo.get("wrong_rate", 0.33))})


def _rate_frame(st: dict[str, Any], sel: np.ndarray) -> pd.DataFrame:
    r = st["rates"]
    return pd.DataFrame({"rate_id": r["rate_id"][sel], "owner_id": r["owner_id"][sel], "warehouse_id": pd.array([pd.NA] * int(sel.sum()), dtype="Int64"),
                         "charge_code": r["charge_code"][sel], "rate": np.round(r["rate"][sel], 4), "rate_uom": r["rate_uom"][sel],
                         "currency": r["currency"][sel], "effective_date": day_col(r["effective_date"][sel]),
                         "mod_ts": ts_col(r["mod_ts"][sel]), "mod_user": r["mod_user"][sel]})


def _make_shiptos(ctx: Ctx, st: dict[str, Any], info: dict[str, Any]) -> None:
    rng = _hash_rng(ctx.seed, "shiptos", info["code"])
    ret = geo.retailers()
    segs = set(ret["vertical_segments"][info["vertical"]])
    country = info["country"]
    pool = [r for r in ret["retailers"] if segs & set(r["segments"]) and r["country"] == country] or \
        [r for r in ret["retailers"] if segs & set(r["segments"])] or ret["retailers"]
    n = SHIPTOS_BY_TIER[info["tier"]]
    countries = np.where(rng.random(n) < (0.9 if country != "US" else 0.97), country, "US" if country != "US" else "CA")
    cities = geo.sample_cities(rng, countries)
    rr = [pool[int(i)] for i in rng.integers(0, len(pool), n)]
    loc = geo.cities()["locale"].to_numpy()[cities]
    a1, a2 = geo.street_lines(rng, cities, np.zeros(n, bool))
    names = []
    recv = ret["receiving_names"]
    for i, r in enumerate(rr):
        lab = ret["site_labels"].get(r["site"], "Store")
        names.append(f"{lab} {int(rng.integers(10, 9999)):04d}" if rng.random() < 0.7 else recv[loc[i]][int(rng.integers(0, len(recv[loc[i]])))])
    st.setdefault("shiptos", {})[info["code"]] = {
        "city": cities.astype(np.int32), "company": np.array([r["name"] for r in rr], dtype=object),
        "name": np.array(names, dtype=object), "a1": a1, "a2": a2, "postal": geo.postal_codes(rng, cities),
        "phone": geo.phones(rng, cities), "w": rng.pareto(1.3, n) + 0.2,
    }


# =============================================================================================== users
ROLE_SHIFT = ("PICKER", "PACKER", "RECEIVER", "FORKLIFT", "INV_CONTROL", "LEAD")


def _headcount(st: dict[str, Any], wi: int, lines_per_day: float) -> dict[tuple[str, str], int]:
    shifts = list(st["wh_rules"][wi]["shifts"])
    per_shift = max(lines_per_day, 50) / len(shifts)
    out = {}
    for s in shifts:
        out[("PICKER", s)] = int(np.clip(round(per_shift / (95 * 6.5) * 1.15), 2, 60))
        out[("PACKER", s)] = int(np.clip(round(per_shift / 2.6 / 260), 1, 30))
        out[("RECEIVER", s)] = int(np.clip(round(per_shift / 900), 1, 12))
        out[("FORKLIFT", s)] = int(np.clip(round(per_shift / 1200), 1, 10))
        out[("INV_CONTROL", s)] = 1
        out[("LEAD", s)] = 1
    out[("SUPERVISOR", shifts[0])] = 2 if lines_per_day > 4000 else 1
    out[("ADMIN", shifts[0])] = 1
    return out


def _hire(ctx: Ctx, st: dict[str, Any], fl: Flush, rng: np.random.Generator, wi: int, roles: list[tuple[str, str]],
          hire_day: int, ts_local: int, outlier: bool = False) -> None:
    n = len(roles)
    if not n:
        return
    loc = np.full(n, "es" if st["wh_country"][wi] == "MX" else "en", dtype=object)
    first, last = geo.names(rng, loc)
    ids = ctx.next_ids("wms.app_user", n, start=101).astype(np.int32)
    pers = ctx.next_ids("wms.personnel", n, start=1201)
    unames = []
    for f, ln in zip(first, last):
        base = (geo._ascii(f)[0] + geo._ascii(ln).replace(" ", "").replace("'", "").replace("-", "")).lower()[:24]
        u, k = base, 1
        while u in st["usernames"]:
            k += 1
            u = f"{base}{k}"
        st["usernames"].add(u)
        unames.append(u)
    role = np.array([r for r, _ in roles], dtype=object)
    shift = np.array([s for _, s in roles], dtype=object)
    out = np.zeros(n, bool)
    if outlier:
        out[0] = True
    tadd(st["users"], n, user_id=ids, username=np.array(unames, dtype=object), first_name=first, last_name=last,
         personnel_nbr=np.array([f"{p:06d}" for p in pers], dtype=object), warehouse_id=wi + 1, user_role=role, shift=shift,
         hire_date=hire_day, term_date=D_NONE, is_active=True, create_ts=ts_local, mod_ts=ts_local, outlier=out)
    sel = np.isin(st["users"]["user_id"], ids)
    fl.insert("app_user", _user_frame(st, sel))
    st["pending_bus"].append(("workers_hired", pd.DataFrame({
        "personnel_nbr": st["users"]["personnel_nbr"][sel], "first_name": first, "last_name": last,
        "warehouse_code": st["wh_code"][wi], "legal_entity": st["wh_entity"][wi], "role": role,
        "hire_date": geo.day_date(hire_day)})))


def _user_frame(st: dict[str, Any], sel: np.ndarray) -> pd.DataFrame:
    u = st["users"]
    return pd.DataFrame({
        "user_id": u["user_id"][sel], "username": u["username"][sel], "first_name": u["first_name"][sel],
        "last_name": u["last_name"][sel], "personnel_nbr": u["personnel_nbr"][sel],
        "warehouse_id": nint(u["warehouse_id"][sel]), "user_role": u["user_role"][sel], "shift": u["shift"][sel],
        "hire_date": day_col(u["hire_date"][sel]), "term_date": day_col(u["term_date"][sel]), "is_active": u["is_active"][sel],
        "create_ts": ts_col(u["create_ts"][sel]), "mod_ts": ts_col(u["mod_ts"][sel])})


def _boot_system_users(ctx: Ctx, st: dict[str, Any], fl: Flush, ts_local: int) -> None:
    rows = [("svc_edi", "EDI", "Service"), ("svc_api", "API", "Service"), ("svc_wms", "WMS", "Scheduler"),
            ("svc_billing", "Billing", "Service"), ("csv_import", "CSV", "Import"), ("svc_itemload", "Item", "Loader"),
            ("mbell", "Marcus", "Bell"), ("kgarza", "Kim", "Garza")]
    ids = ctx.next_ids("wms.app_user", len(rows), start=101).astype(np.int32)
    n = len(rows)
    tadd(st["users"], n, user_id=ids, username=np.array([r[0] for r in rows], dtype=object),
         first_name=np.array([r[1] for r in rows], dtype=object), last_name=np.array([r[2] for r in rows], dtype=object),
         personnel_nbr=np.array([None] * 6 + ["000417", "000562"], dtype=object), warehouse_id=0,
         user_role="ADMIN", shift=None, hire_date=ts_local // DAY - 900, term_date=D_NONE, is_active=True,
         create_ts=ts_local - 400 * DAY, mod_ts=ts_local - 400 * DAY)
    st["usernames"].update(r[0] for r in rows)
    fl.insert("app_user", _user_frame(st, np.isin(st["users"]["user_id"], ids)))


def _staff_warehouse(ctx: Ctx, st: dict[str, Any], fl: Flush, wi: int, day: int, ts_local: int, initial: bool) -> None:
    lines = _wh_lines_per_day(ctx, st, wi, day)
    target = _headcount(st, wi, lines)
    u = st["users"]
    act = u["is_active"] & (u["warehouse_id"] == wi + 1)
    have: dict[tuple[str, str], int] = {}
    for r, s in zip(u["user_role"][act], u["shift"][act]):
        have[(r, s)] = have.get((r, s), 0) + 1
    rng = _hash_rng(ctx.seed, "hire", wi, day)
    need = []
    for k, n in target.items():
        need += [k] * max(0, n - have.get(k, 0))
    outlier = False
    if initial and st["wh_code"][wi] == "ATL1" and not u["outlier"].any():
        need.insert(0, ("PICKER", "B"))
        outlier = True
    if need:
        hire_day = day - (rng.integers(30, 1500, len(need)) if initial else 0)
        if initial:
            for i, k in enumerate(need):
                _hire(ctx, st, fl, rng, wi, [k], int(hire_day[i]), ts_local, outlier=(outlier and i == 0))
        else:
            _hire(ctx, st, fl, rng, wi, need, day, ts_local)


def _wh_lines_per_day(ctx: Ctx, st: dict[str, Any], wi: int, day: int) -> float:
    ema = st.get("ema_lines")
    if ema is not None and ema[wi] > 0:
        return float(ema[wi])
    infos = [i for i in st["owners"].values() if wi in i["whs"]]
    if not infos:
        return 0.0
    b2c, b2b = _daily_rates(ctx, st, infos, max(day, min(i["go_live"] for i in infos) + 90))
    tot = 0.0
    for i, info in enumerate(infos):
        share = _route_share(st, info, info["whs"])[info["whs"].index(wi)]
        lo, hi = info["lines_b2c"]
        blo, bhi = info["lines_b2b"]
        tot += share * (b2c[i] * (lo + (hi - lo + 1) / 3.5 - 0.5) + b2b[i] * (blo + (bhi - blo + 1) / 6.0 - 0.5))
    return tot


# =============================================================================================== opening inventory
def _demand_units(st: dict[str, Any], owner_id: int, wi: int, mat_idx: np.ndarray) -> np.ndarray:
    mat = st["mat"]
    ema = st["ema_units"][owner_id, wi]
    own = (mat["owner_id"] == owner_id) & (mat["status"] == 0)
    tot = mat["pop"][own].sum()
    return ema * mat["pop"][mat_idx] / max(tot, 1e-9)


def _lot_numbers(ctx: Ctx, code: str, day: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    fmt = geo.products()["lot_formats"].get(code, geo.products()["lot_formats"]["default"])
    out = []
    for d in day:
        dd = geo.day_date(int(d))
        out.append(fmt.format(yy=f"{dd.year % 100:02d}", doy=f"{dd.timetuple().tm_yday:03d}", mm=f"{dd.month:02d}",
                              dd=f"{dd.day:02d}", letter="ABCDEFGH"[int(rng.integers(0, 8))], n2=f"{int(rng.integers(1, 99)):02d}",
                              n3=f"{int(rng.integers(1, 999)):03d}", n6=f"{int(rng.integers(100000, 999999))}"))
    return np.asarray(out, dtype=object)


def _opening_inventory(ctx: Ctx, st: dict[str, Any], fl: Flush, info: dict[str, Any], wi: int, ts_local: int) -> None:
    """Stock on hand at the history start, with balance-forward ledger rows (the WMS archives older history)."""
    rng = _hash_rng(ctx.seed, "opening", info["code"], wi)
    mat = st["mat"]
    idx = np.flatnonzero((mat["owner_id"] == info["owner_id"]) & (mat["status"] == 0))
    if not len(idx):
        return
    dem = _demand_units(st, info["owner_id"], wi, idx)
    days = info["lead"] + rng.uniform(15, 45, len(idx))
    shelf = mat["shelf"][idx]
    days = np.where(shelf > 0, np.minimum(days, np.maximum(7, shelf * 0.5)), days)
    qty = np.maximum(mat["case_qty"][idx], np.ceil(dem * days / mat["case_qty"][idx]) * mat["case_qty"][idx]).astype(np.int64)
    qty = np.where(rng.random(len(idx)) < 0.04, 0, qty)       # some SKUs are simply out of stock
    keep = qty > 0
    idx, qty = idx[keep], qty[keep]
    mat["sellable"][idx] = True
    # split into LPN rows: a pick-face row plus up to 4 reserve pallets
    per_pallet = np.maximum(mat["case_qty"][idx] * mat["cpp"][idx], 1).astype(np.int64)
    pick_q = np.minimum(np.maximum(mat["case_qty"][idx] * 2, 1), qty).astype(np.int64)
    rest = qty - pick_q
    n_pal = np.where(rest > 0, np.clip(np.ceil(rest / per_pallet), 1, 4), 0).astype(np.int64)
    rows_mat = np.repeat(idx, n_pal + 1)
    k = np.concatenate([np.arange(c + 1) for c in n_pal])
    q_each = np.where(n_pal > 0, np.ceil(rest / np.maximum(n_pal, 1)), 0).astype(np.int64)
    last_q = rest - q_each * np.maximum(n_pal - 1, 0)
    rn = np.repeat(n_pal, n_pal + 1)
    q = np.where(k == 0, np.repeat(pick_q, n_pal + 1), np.where(k == rn, np.repeat(last_q, n_pal + 1), np.repeat(q_each, n_pal + 1)))
    ok = q > 0
    rows_mat, k, q = rows_mat[ok], k[ok], q[ok]
    n = len(q)
    lt = np.where(k == 0, LT_PICK, LT_RES).astype(np.int8)
    zone = mat["zone"][rows_mat]
    loc = _pick_locations(st, rng, np.full(n, wi), zone, lt)
    shelf_r = mat["shelf"][rows_mat]
    age = np.where(shelf_r > 0, (rng.random(n) * np.maximum(shelf_r, 10) * 0.3).astype(np.int64) + 1, rng.integers(3, 160, n))
    rcv_day = ts_local // DAY - age
    lot = np.full(n, None, dtype=object)
    exp = np.full(n, D_NONE)
    lotm = mat["lot"][rows_mat]
    if lotm.any():
        mfg = rcv_day[lotm] - rng.integers(1, 6, int(lotm.sum()))
        lot[lotm] = _lot_numbers(ctx, info["code"], mfg, rng)
        shelf = mat["shelf"][rows_mat][lotm]
        exp[lotm] = mfg + np.maximum(shelf, 30)
        exp[lotm] = np.maximum(exp[lotm], ts_local // DAY + 5)
    lpn_ids = ctx.next_ids("wms.lpn", n)
    inv_ids = ctx.next_ids("wms.inventory", n)
    tadd(st["inv"], n, inventory_id=inv_ids, warehouse_id=wi + 1, location_id=loc, lpn_id=lpn_ids, owner_id=info["owner_id"],
         material_id=mat["material_id"][rows_mat], lot_nbr=lot, expiry_date=exp, qty_on_hand=q, qty_allocated=0,
         inv_status=I_AVAIL, hold_reason=None, received_date=rcv_day, create_ts=rcv_day * DAY + 11 * 3600,
         mod_ts=rcv_day * DAY + 15 * 3600, _st=1)
    fl.insert("license_plate", pd.DataFrame({
        "lpn_id": lpn_ids, "lpn_nbr": [f"LP{1_000_000_000 + int(x):010d}" for x in lpn_ids], "warehouse_id": wi + 1,
        "location_id": loc, "lpn_type": np.where(lt == LT_PICK, "CASE", "PALLET"), "lpn_status": "STORED",
        "create_ts": ts_col(rcv_day * DAY + 11 * 3600), "mod_ts": ts_col(rcv_day * DAY + 15 * 3600)}))
    bal_ts = ts_local - 60
    _txn(ctx, st, fl, rng, wi, ts=np.full(n, bal_ts), owner=np.full(n, info["owner_id"]), mat=mat["material_id"][rows_mat],
         kind="ADJUST", qty=q, to_loc=loc, lpn=lpn_ids, lot=lot, reason="BAL_FWD", ref_type=None, ref_id=0, user=0,
         dq=False)


# =============================================================================================== ledger
def _txn(ctx: Ctx, st: dict[str, Any], fl: Flush, rng: np.random.Generator, wi: Any, ts: np.ndarray, owner: np.ndarray,
         mat: np.ndarray, kind: str, qty: np.ndarray, from_loc: Any = 0, to_loc: Any = 0, lpn: Any = 0, lot: Any = None,
         reason: Any = None, ref_type: Any = None, ref_id: Any = 0, user: Any = 0, dq: bool = True) -> None:
    n = len(ts)
    if n == 0:
        return
    ids = ctx.next_ids("wms.inventory_txn", n)

    def arr(v: Any, dt: Any) -> np.ndarray:
        return np.full(n, v, dtype=dt) if np.ndim(v) == 0 else np.asarray(v).astype(dt, copy=False)

    qty = arr(qty, np.int64).copy()
    create = arr(ts, np.int64).copy()
    df = pd.DataFrame({
        "txn_id": ids, "txn_ts": ts_col(arr(ts, np.int64)), "warehouse_id": arr(wi, np.int64) + 1, "owner_id": arr(owner, np.int64),
        "material_id": arr(mat, np.int64), "txn_type": kind, "qty": qty, "from_location_id": nint(arr(from_loc, np.int64)),
        "to_location_id": nint(arr(to_loc, np.int64)), "lpn_id": nint(arr(lpn, np.int64)), "lot_nbr": arr(lot, object),
        "reason_code": arr(reason, object), "ref_type": arr(ref_type, object), "ref_id": nint(arr(ref_id, np.int64)),
        "user_id": nint(arr(user, np.int64)),
    })
    if dq:
        m = ctx.dq.mask("late_arriving", n, rng)
        if m.any():
            create[m] += rng.integers(2 * DAY, 9 * DAY, int(m.sum()))
            for i in np.flatnonzero(m):
                ctx.dq.log("late_arriving", "wms", "inventory_txn", str(ids[i]), "create_ts", None, None,
                           f"{kind} scan uploaded {(create[i] - int(arr(ts, np.int64)[i])) // DAY} days after txn_ts (RF gun offline)")
        m = ctx.dq.mask("negative_qty", n, rng) & (qty > 0)
        for i in np.flatnonzero(m):
            ctx.dq.log("negative_qty", "wms", "inventory_txn", str(ids[i]), "qty", int(qty[i]), int(-qty[i]), f"{kind} posted with flipped sign")
        qty[m] = -qty[m]
        df["qty"] = qty
    df["create_ts"] = ts_col(create)
    if dq:
        dup = ctx.dq.mask("duplicate_row", n, rng)
        dst = _dst_eligible(st, wi, arr(ts, np.int64)) & ctx.dq.mask("dst_duplicate", n, rng)
        extra = dup | dst
        if extra.any():
            d = df[extra].copy()
            new_ids = ctx.next_ids("wms.inventory_txn", len(d))
            for old, new, is_dst in zip(d["txn_id"], new_ids, dst[extra]):
                cat = "dst_duplicate" if is_dst else "duplicate_row"
                ctx.dq.log(cat, "wms", "inventory_txn", str(new), None, None, None,
                           f"duplicate of txn {old}" + (" (scanner replayed during DST fall-back hour)" if is_dst else ""))
            d["txn_id"] = new_ids
            df = pd.concat([df, d], ignore_index=True)
    fl.insert("inventory_txn", df)


def _dst_eligible(st: dict[str, Any], wi: Any, ts: np.ndarray) -> np.ndarray:
    """Local timestamps inside the repeated 01:00-02:00 hour of a DST fall-back day."""
    out = np.zeros(len(ts), dtype=bool)
    hour = (ts % DAY) // 3600
    cand = hour == 1
    if not cand.any():
        return out
    w = np.full(len(ts), wi) if np.ndim(wi) == 0 else np.asarray(wi)
    for i in np.flatnonzero(cand):
        tz = st["wh_tz"][int(w[i])]
        day0 = (ts[i] // DAY) * DAY
        u = geo.local_to_utc(tz, np.array([day0 + 3600, day0 + 3 * 3600]))
        out[i] = (u[1] - u[0]) == 3 * 3600
    return out


# =============================================================================================== module
class WmsModule:
    name = NAME

    # ------------------------------------------------------------------ bootstrap
    def bootstrap(self, ctx: Ctx) -> None:
        st = _new_state(ctx)
        ctx.world[NAME] = st
        _setup_reference(ctx, st)
        fl = Flush()
        hs_utc = int(ctx.history_start.timestamp())
        hs_day = geo.epoch_day(ctx.history_start.date())
        st["ema_units"] = np.zeros((len(ctx.rules["clients"]) + 200, len(st["wh_rules"])))
        st["ema_lines"] = np.zeros(len(st["wh_rules"]))
        first_local = int(geo.utc_to_local(st["wh_tz"][0], np.array([hs_utc]))[0])
        _boot_carriers(ctx, st, fl, first_local - 400 * DAY)
        _boot_system_users(ctx, st, fl, first_local)
        for wi, wh in enumerate(st["wh_rules"]):
            if not st["wh_present"][wi]:
                continue
            setup = int(st["wh_open_day"][wi]) - 45
            if setup <= hs_day:
                local = int(geo.utc_to_local(st["wh_tz"][wi], np.array([hs_utc]))[0])
                _boot_warehouse(ctx, st, fl, wi, min(local, (int(st["wh_open_day"][wi]) - 40) * DAY + 9 * 3600), now_local=local)
        for c in _sim_clients(ctx):
            info = _client_info(ctx, st, c)
            if info["wms_created"] <= hs_day and info["primary"] in st["wh_ready"]:
                local = int(geo.utc_to_local(st["wh_tz"][info["primary"]], np.array([hs_utc]))[0])
                _create_owner(ctx, st, fl, info, local, at_boot=True)
                _init_ema(ctx, st, info, hs_day)
        for c in list(st["owners"].values()):
            if c["go_live"] <= hs_day and (c["churn"] is None or c["churn"] > hs_day):
                for wi in c["whs"]:
                    if wi in st["wh_ready"] and st["wh_open_day"][wi] <= hs_day:
                        local = int(geo.utc_to_local(st["wh_tz"][wi], np.array([hs_utc]))[0])
                        _opening_inventory(ctx, st, fl, c, wi, local)
        for wi in sorted(st["wh_ready"]):
            local = int(geo.utc_to_local(st["wh_tz"][wi], np.array([hs_utc]))[0])
            if st["wh_open_day"][wi] <= hs_day:
                _staff_warehouse(ctx, st, fl, wi, hs_day, local, initial=True)
        _flush_open(ctx, st, fl, defer=ctx.mode == "build")
        fl.run(ctx)

    # ------------------------------------------------------------------ step
    def step(self, ctx: Ctx, win: Window) -> None:
        st = _state(ctx)
        if "wh_rules" not in st:
            _setup_reference(ctx, st)
        W = _WinCtx(ctx, st, win)
        fl = Flush()
        for key, df in st["pending_bus"]:
            ctx.bus.publish(key, df)
        st["pending_bus"] = []
        st["door_log"] = []
        decay = 1 - min(1.0, win.hours / 24 / 21)
        st["ema_units"] *= decay
        st["ema_lines"] *= decay
        _lifecycle(ctx, st, W, fl)
        _inbound(ctx, st, W, fl)
        _new_orders(ctx, st, W, fl)
        _order_flow(ctx, st, W, fl)
        _returns(ctx, st, W, fl)
        _inventory_control(ctx, st, W, fl)
        _nightly(ctx, st, W, fl)
        _flush_open(ctx, st, fl, defer=ctx.mode == "build" and win.end < ctx.history_end)
        fl.run(ctx)
        for key, df in st["pending_bus"]:
            ctx.bus.publish(key, df)
        st["pending_bus"] = []

    # ------------------------------------------------------------------ scenarios
    def apply_scenario(self, ctx: Ctx, scenario_id: str, params: dict[str, Any], win: Window) -> str | None:
        st = _state(ctx)
        if "wh_rules" not in st:
            _setup_reference(ctx, st)
        return _scenario(ctx, st, scenario_id, params, win)


class _WinCtx:
    """Per-window derived values: UTC bounds, local bounds per warehouse, random streams."""

    def __init__(self, ctx: Ctx, st: dict[str, Any], win: Window) -> None:
        self.win = win
        self.ws = int(win.start.timestamp())
        self.we = int(win.end.timestamp())
        nw = len(st["wh_rules"])
        self.ls = np.array([geo.utc_to_local(st["wh_tz"][i], np.array([self.ws]))[0] for i in range(nw)], dtype=np.int64)
        self.le = np.array([geo.utc_to_local(st["wh_tz"][i], np.array([self.we]))[0] for i in range(nw)], dtype=np.int64)
        self.ctx = ctx
        self._rng: dict[str, np.random.Generator] = {}

    def rng(self, salt: str) -> np.random.Generator:
        g = self._rng.get(salt)
        if g is None:
            g = self._rng[salt] = self.ctx.rng(NAME, self.win, salt)
        return g

    def crosses(self, st: dict[str, Any], wi: int, hh: int, mm: int = 0) -> list[int]:
        """Local timestamps (seconds) of hh:mm inside the window for warehouse wi."""
        out = []
        d = self.ls[wi] // DAY
        while d * DAY <= self.le[wi]:
            t = d * DAY + hh * 3600 + mm * 60
            if self.ls[wi] <= t < self.le[wi]:
                out.append(int(t))
            d += 1
        return out


def _active_whs(st: dict[str, Any]) -> list[int]:
    return sorted(st["wh_ready"])


# =============================================================================================== lifecycle (owners, sites, users, items, rates)
def _init_ema(ctx: Ctx, st: dict[str, Any], info: dict[str, Any], day: int) -> None:
    ref = max(day, info["go_live"] + 75)
    b2c, b2b = _daily_rates(ctx, st, [info], ref)
    orders = (b2c[0] + b2b[0])
    share = _route_share(st, info, info["whs"])
    lo, hi = info["lines_b2c"]
    blo, bhi = info["lines_b2b"]
    lines = b2c[0] * (lo + (hi - lo + 1) / 3.5 - 0.5) + b2b[0] * (blo + (bhi - blo + 1) / 6.0 - 0.5)
    for k, w in enumerate(info["whs"]):
        st["ema_units"][info["owner_id"], w] = orders * info["e_units"] * share[k]
        st["ema_lines"][w] += lines * share[k] * (1.0 if info["go_live"] <= day else 0.0)


def _lifecycle(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush) -> None:
    rng = W.rng("lifecycle")
    # warehouses that open during the history (LAX1): racking set up 45 days before opening
    for wi, wh in enumerate(st["wh_rules"]):
        if st["wh_present"][wi] and wi not in st["wh_ready"]:
            setup = int(st["wh_open_day"][wi]) - 45
            if setup * DAY < W.le[wi]:
                _boot_warehouse(ctx, st, fl, wi, max(int(W.ls[wi]), setup * DAY + 9 * 3600))
                for info in st["owners"].values():
                    if wi in info["whs"]:
                        _owner_warehouses(ctx, st, fl, info, int(W.le[wi] // DAY))
        if wi in st["wh_ready"]:
            for t in W.crosses(st, wi, 0, 5):
                if t // DAY == st["wh_open_day"][wi] and st["wh_open_day"][wi] > geo.epoch_day(ctx.history_start.date()):
                    fl.execute("warehouse", "UPDATE wms.warehouse SET is_active = true, mod_ts = %s WHERE warehouse_id = %s",
                               (ts_col(np.array([t]))[0].astype("datetime64[us]").item(), wi + 1))
                    _staff_warehouse(ctx, st, fl, wi, int(t // DAY), t, initial=False)
                    _stock_new_site(ctx, st, W, fl, wi, t)
    # new / dynamic clients
    for c in _sim_clients(ctx):
        if c["code"] in st["owners"]:
            continue
        info = _client_info(ctx, st, c)
        wi = info["primary"]
        if info["wms_created"] * DAY + 10 * 3600 < W.le[wi]:
            if wi not in st["wh_ready"]:
                continue
            t = max(int(W.ls[wi]), info["wms_created"] * DAY + 10 * 3600)
            _create_owner(ctx, st, fl, info, t, at_boot=False)
            _init_ema(ctx, st, info, int(t // DAY))
            st["onboarding"] = st.get("onboarding", []) + [info["code"]]
    # owner status transitions and pre-go-live stocking
    for info in list(st["owners"].values()):
        wi = info["primary"]
        for t in W.crosses(st, wi, 0, 1):
            day = int(t // DAY)
            if info["status"] == "ONBOARDING" and day >= info["go_live"]:
                _set_owner_status(fl, info, "ACTIVE", t)
            if info["status"] == "ONBOARDING" and day == info["go_live"] - 12 and not info.get("stocked"):
                info["stocked"] = True
                for w in info["whs"]:
                    if w in st["wh_ready"]:
                        _stocking_asns(ctx, st, W, fl, info, w, t, expected_day=info["go_live"] - 3)
            if info["churn"] is not None:
                if info["status"] == "ACTIVE" and day >= info["churn"] - 21:
                    _set_owner_status(fl, info, "OFFBOARDING", t)
                if info["status"] == "OFFBOARDING" and day >= info["churn"] and not info.get("shipped_out"):
                    info["shipped_out"] = True
                    _ship_out(ctx, st, W, fl, info, t)
                if info["status"] == "OFFBOARDING" and day >= info["churn"] + 12:
                    _deactivate_owner(ctx, st, fl, info, t)
        if info["status"] == "ONBOARDING" and not info.get("stocked") and info["go_live"] - 12 < W.le[wi] // DAY:
            info["stocked"] = True
            for w in info["whs"]:
                if w in st["wh_ready"]:
                    _stocking_asns(ctx, st, W, fl, info, w, int(W.ls[w]), expected_day=max(info["go_live"] - 3, int(W.le[w] // DAY) + 2))
    # rate keying events
    for ev in list(st["rate_events"]):
        info = st["owners"].get(ev["code"])
        if info is None:
            continue
        wi = info["primary"]
        t = ev.get("t") or ev["day"] * DAY + 15 * 3600
        if t < W.le[wi]:
            st["rate_events"].remove(ev)
            if info["status"] == "INACTIVE":
                continue
            _apply_rate_event(ctx, st, fl, info, ev, max(t, int(W.ls[wi])))
    # people and items: once a day per site at 02:00
    for wi in _active_whs(st):
        for t in W.crosses(st, wi, 2, 0):
            _people_day(ctx, st, W, fl, wi, t, rng)
    for info in list(st["owners"].values()):
        if info["status"] in ("ACTIVE", "ONBOARDING"):
            for t in W.crosses(st, info["primary"], 3, 30):
                _items_day(ctx, st, W, fl, info, t, rng)


def _set_owner_status(fl: Flush, info: dict[str, Any], status: str, t: int) -> None:
    info["status"] = status
    fl.execute("owner", "UPDATE wms.owner SET owner_status = %s, mod_ts = %s, mod_user = 'mbell' WHERE owner_id = %s",
               (status, ts_col(np.array([t]))[0].astype("datetime64[us]").item(), info["owner_id"]))


def _deactivate_owner(ctx: Ctx, st: dict[str, Any], fl: Flush, info: dict[str, Any], t: int) -> None:
    _set_owner_status(fl, info, "INACTIVE", t)
    inv = st["inv"]
    left = np.flatnonzero((inv["owner_id"] == info["owner_id"]) & (inv["qty_on_hand"] > 0) & (inv["qty_allocated"] == 0))
    if len(left):
        # stragglers (late returns, count gains) are written off to the client's final shipment
        q = inv["qty_on_hand"][left].astype(np.int64)
        wi = inv["warehouse_id"][left].astype(np.int64) - 1
        _txn(ctx, st, fl, np.random.default_rng(t % 2**31), wi, np.full(len(left), t), inv["owner_id"][left], inv["material_id"][left],
             "ADJUST", -q, from_loc=inv["location_id"][left], lot=inv["lot_nbr"][left], reason="OFFBOARD_RTV", ref_type="QA", ref_id=0,
             dq=False)
        inv["qty_on_hand"][left] = 0
        inv["mod_ts"][left] = t
        mark_pos(inv, left)
    ts = ts_col(np.array([t]))[0].astype("datetime64[us]").item()
    fl.execute("owner_warehouse", "UPDATE wms.owner_warehouse SET active_to = %s WHERE owner_id = %s AND active_to IS NULL",
               (geo.day_date(t // DAY), info["owner_id"]))
    fl.execute("material", "UPDATE wms.material SET material_status = 'INACTIVE', mod_ts = %s, mod_user = 'mbell' "
               "WHERE owner_id = %s AND material_status <> 'INACTIVE'", (ts, info["owner_id"]))
    st["mat"]["status"][(st["mat"]["owner_id"] == info["owner_id"]) & (st["mat"]["status"] == 0)] = 1
    r = st["rates"]
    sel = r["owner_id"] == info["owner_id"]
    if sel.any():
        fl.delete("billing_rate", {"rate_id": r["rate_id"][sel]})
        tkeep(r, ~sel)
    # open ASNs for an inactive client are cancelled and deleted
    a = st["asns"]
    sel = (a["owner_id"] == info["owner_id"]) & (a["receipt_id"] == 0)
    if sel.any():
        _delete_asns(st, fl, sel)


def _apply_rate_event(ctx: Ctx, st: dict[str, Any], fl: Flush, info: dict[str, Any], ev: dict[str, Any], t: int) -> None:
    r = st["rates"]
    sel = r["owner_id"] == info["owner_id"]
    if ev["kind"] == "increase":
        pct = float(ev["pct"])
        old = r["rate"][sel]
        is_flat = np.isin(r["charge_code"][sel], ["FRT-RCV"])
        new = np.where(is_flat, old, np.where(old < 2, np.round(old * (1 + pct / 100), 4), np.round(old * (1 + pct / 100), 2)))
        r["rate"][sel] = new
        r["effective_date"][sel] = ev["eff"]
    elif ev["kind"] == "typo":
        sel = sel & (r["charge_code"] == ev["charge"])
        if not sel.any():
            return
        r["rate"][sel] = ev["rate"]
        r["effective_date"][sel] = ev["eff"]
    r["mod_ts"][sel] = t
    r["mod_user"][sel] = np.array(["kgarza" if ev["kind"] != "typo" else "tnguyen"] * int(sel.sum()), dtype=object)
    df = _rate_frame(st, sel)[["rate_id", "rate", "effective_date", "mod_ts", "mod_user"]]
    fl.update("billing_rate", ("rate_id",), df)


def _people_day(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush, wi: int, t: int, rng: np.random.Generator) -> None:
    day = int(t // DAY)
    if day < st["wh_open_day"][wi] - 20:
        return
    u = st["users"]
    act = np.flatnonzero(u["is_active"] & (u["warehouse_id"] == wi + 1) & ~u["outlier"] & np.isin(u["user_role"], ROLE_SHIFT))
    quit_ = act[rng.random(len(act)) < 0.0011]
    if len(quit_):
        u["is_active"][quit_] = False
        u["term_date"][quit_] = day
        u["mod_ts"][quit_] = t
        fl.update("app_user", ("user_id",), pd.DataFrame({"user_id": u["user_id"][quit_], "is_active": False,
                                                          "term_date": day_col(u["term_date"][quit_]), "mod_ts": ts_col(u["mod_ts"][quit_])}))
        st["pending_bus"].append(("workers_terminated", pd.DataFrame({
            "personnel_nbr": u["personnel_nbr"][quit_], "first_name": u["first_name"][quit_], "last_name": u["last_name"][quit_],
            "warehouse_code": st["wh_code"][wi], "legal_entity": st["wh_entity"][wi], "role": u["user_role"][quit_],
            "term_date": geo.day_date(day)})))
    if (day + 3) % 7 == 0 or len(quit_):          # Mondays (or after a resignation) HR backfills to target
        _staff_warehouse(ctx, st, fl, wi, day, t, initial=False)
    # leads transfer to a new site the week before it opens
    for wj in range(len(st["wh_rules"])):
        if wj != wi and wj in st["wh_ready"] and day == st["wh_open_day"][wj] - 7 and st["wh_country"][wj] == st["wh_country"][wi]:
            leads = np.flatnonzero(u["is_active"] & (u["warehouse_id"] == wi + 1) & (u["user_role"] == "LEAD"))
            if len(leads) and rng.random() < 0.6:
                k = leads[:1]
                u["warehouse_id"][k] = wj + 1
                u["mod_ts"][k] = t
                fl.update("app_user", ("user_id",), pd.DataFrame({"user_id": u["user_id"][k], "warehouse_id": wj + 1,
                                                                  "mod_ts": ts_col(u["mod_ts"][k])}))
    # labour hours for the previous day
    act = u["is_active"] & (u["warehouse_id"] == wi + 1)
    if act.any():
        roles, counts = np.unique(u["user_role"][act], return_counts=True)
        prev = geo.day_date(day - 1)
        if prev.weekday() < 5 or (prev.weekday() == 5 and st["wh_rules"][wi]["profile"] == "ecommerce"):
            load = 1 + 0.4 * _peak_shape(prev)
            hrs = counts * 8.0 * (0.55 if prev.weekday() == 5 else 1.0) * load * rng.uniform(0.92, 1.05, len(counts))
            st["pending_bus"].append(("labor_hours", pd.DataFrame({"warehouse_code": st["wh_code"][wi], "work_date": prev,
                                                                   "role": roles, "hours": np.round(hrs, 1)})))


def _items_day(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush, info: dict[str, Any], t: int, rng: np.random.Generator) -> None:
    day = int(t // DAY)
    mat = st["mat"]
    own = np.flatnonzero((mat["owner_id"] == info["owner_id"]) & (mat["status"] == 0))
    if not len(own):
        return
    # SKUs created in error yesterday are deleted (only if nothing references them)
    err = st.setdefault("err_skus", [])
    for mid, d in list(err):
        if day >= d + 1:
            err.remove((mid, d))
            p = int(mid) - 1
            if mat["status"][p] == 0 and not mat["sellable"][p] and not (st["inv"]["material_id"] == mid).any():
                mat["status"][p] = 9
                fl.delete("material_uom", {"material_id": np.array([mid])})
                fl.delete("material", {"material_id": np.array([mid])})
                st["dq_orphans"].append(int(mid))
                st["dq_orphans"] = st["dq_orphans"][-50:]
    # new SKUs: seasonal drops for apparel, a trickle for everyone else
    d = geo.day_date(day)
    rate = len(own) * (0.004 if (info["vertical"] == "apparel" and d.month in (2, 8) and d.day <= 7) else 0.0006)
    n_new = int(rng.poisson(rate))
    if n_new:
        ids = _create_catalog(ctx, st, fl, info, t, at_boot=False, n=n_new, created_day=day)
        if len(ids) and rng.random() < 0.25:
            # a duplicate keyed in error (typo in the SKU), deleted the next day
            e_ids = _create_catalog(ctx, st, fl, info, t + 300, at_boot=False, n=1, created_day=day + 10_000)
            mat["pop"][e_ids - 1] = 0.0          # nobody orders or replenishes a SKU keyed by mistake
            for e in e_ids:
                err.append((int(e), day))
    # dimension / weight corrections overwrite the item master in place
    fix = own[rng.random(len(own)) < 0.0005]
    if len(fix):
        f = rng.uniform(0.8, 1.25, len(fix))
        mat["lb"][fix] = np.round(mat["lb"][fix] * f, 3)
        kg = info["kg"]
        df = pd.DataFrame({"material_id": mat["material_id"][fix],
                           "unit_weight": np.round(mat["lb"][fix] / (LB_PER_KG if kg else 1.0), 3),
                           "unit_length": np.round(mat["l_in"][fix] * (2.54 if kg else 1.0), 2),
                           "mod_ts": ts_col(np.full(len(fix), t)), "mod_user": "mbell"})
        fl.update("material", ("material_id",), df)
    disc = own[(rng.random(len(own)) < 0.0002)]
    if len(disc):
        mat["status"][disc] = 2
        fl.update("material", ("material_id",), pd.DataFrame({"material_id": mat["material_id"][disc], "material_status": "DISCONTINUED",
                                                              "mod_ts": ts_col(np.full(len(disc), t)), "mod_user": "svc_itemload"}))


# =============================================================================================== inbound: ASNs, receipts, putaway
def _stocking_asns(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush, info: dict[str, Any], wi: int, t: int,
                   expected_day: int, transfer_from: str | None = None) -> None:
    mat = st["mat"]
    idx = np.flatnonzero((mat["owner_id"] == info["owner_id"]) & (mat["status"] == 0))
    if not len(idx):
        return
    dem = _demand_units(st, info["owner_id"], wi, idx)
    rng = W.rng(f"stock-{info['code']}-{wi}")
    qty = np.ceil(dem * rng.uniform(25, 45, len(idx)) / mat["case_qty"][idx]) * mat["case_qty"][idx]
    qty = np.maximum(qty, mat["case_qty"][idx]).astype(np.int64)
    _create_asns(ctx, st, W, fl, info, wi, t, idx, qty, expected_day, rng, transfer_from=transfer_from)


def _stock_new_site(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush, wi: int, t: int) -> None:
    """A new site is stocked by inter-site transfers before it opens (planted lax1_ramp)."""
    for info in st["owners"].values():
        if wi in info["whs"] and info["status"] in ("ACTIVE", "ONBOARDING"):
            share = _route_share(st, info, info["whs"])
            tot = st["ema_units"][info["owner_id"], info["whs"]].sum()
            for k, w in enumerate(info["whs"]):
                st["ema_units"][info["owner_id"], w] = tot * share[k]
            src = next((st["wh_code"][w] for w in info["whs"] if w != wi), None)
            _stocking_asns(ctx, st, W, fl, info, wi, t, expected_day=int(t // DAY) + 2, transfer_from=src)


def _create_asns(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush, info: dict[str, Any], wi: int, t: int,
                 midx: np.ndarray, qty: np.ndarray, expected_day: Any, rng: np.random.Generator,
                 transfer_from: str | None = None, lots: np.ndarray | None = None) -> None:
    n = len(midx)
    if not n:
        return
    per = 35
    n_asn = math.ceil(n / per)
    asn_ids = ctx.next_ids("wms.asn", n_asn)
    seq = ctx.next_ids(f"wms.asn.{info['code']}", n_asn)
    group = np.arange(n) // per
    exp_day = np.full(n_asn, expected_day, dtype=np.int64) if np.ndim(expected_day) == 0 else np.asarray(expected_day)[:n_asn]
    sup = geo.products()["suppliers"].get(info["cat"], ["Supplier"])
    supplier = np.array([f"Harborline {transfer_from} (transfer)" if transfer_from else sup[int(rng.integers(0, len(sup)))]
                         for _ in range(n_asn)], dtype=object)
    src = np.where(rng.random(n_asn) < (0.75 if info["edi"] or info["tier"] in ("L", "XL") else 0.3), "EDI856",
                   np.where(rng.random(n_asn) < 0.7, "PORTAL", "EMAIL"))
    if transfer_from:
        src[:] = "PORTAL"
    arrive_off = np.round(rng.normal(0, 1.1, n_asn)).astype(np.int64)
    arrive = (exp_day + arrive_off) * DAY + rng.integers(6 * 3600, 15 * 3600, n_asn)
    arrive = np.maximum(arrive, t + 6 * 3600)
    arrive = np.where((arrive // DAY + 3) % 7 == 6, arrive + DAY, arrive)   # no Sunday deliveries
    for (bw, b0, b1) in st["blackouts"]:
        if bw == wi:
            arrive = np.where((arrive >= b0) & (arrive < b1), b1 + rng.integers(2 * 3600, 8 * 3600, n_asn), arrive)
    cancel = np.where(rng.random(n_asn) < 0.02, t + ((arrive - t) * rng.uniform(0.2, 0.8, n_asn)).astype(np.int64), T_NONE)
    if transfer_from:
        cancel[:] = T_NONE
    tadd(st["asns"], n_asn, asn_id=asn_ids, asn_nbr=np.array([f"ASN-{info['code']}-{int(s):06d}" for s in seq], dtype=object),
         owner_id=info["owner_id"], warehouse_id=wi + 1, supplier_name=supplier, source=src, expected_date=exp_day,
         asn_status="OPEN", create_ts=t, mod_ts=t, _st=1, t_arrive=arrive, t_cancel=cancel, wi=wi, receipt_id=0,
         transfer=bool(transfer_from))
    mat = st["mat"]
    lot = np.full(n, None, dtype=object)
    exp = np.full(n, D_NONE)
    lotm = mat["lot"][midx]
    if lotm.any():
        mfg = exp_day[group][lotm] - rng.integers(4, 25, int(lotm.sum()))
        lot[lotm] = _lot_numbers(ctx, info["code"], mfg, rng) if lots is None else lots[lotm]
        exp[lotm] = mfg + np.maximum(mat["shelf"][midx][lotm], 30)
    line_nbr = (np.arange(n) % per) + 1
    tadd(st["asn_lines"], n, asn_id=asn_ids[group], line_nbr=line_nbr, material_id=mat["material_id"][midx], expected_qty=qty,
         lot_nbr=lot, expiry_date=exp)
    fl.insert("asn_line", pd.DataFrame({"asn_id": asn_ids[group], "line_nbr": line_nbr, "material_id": mat["material_id"][midx],
                                        "expected_qty": qty, "uom": "EA", "lot_nbr": lot, "expiry_date": day_col(exp)}))
    lines = [[{"sku": mat["sku"][midx[j]], "upc": mat["upc"][midx[j]], "qty": int(qty[j]), "uom": "EA", "lot": lot[j],
               "expiry": None if exp[j] <= D_NONE else geo.day_date(int(exp[j])).isoformat()} for j in np.flatnonzero(group == g)]
             for g in range(n_asn)]
    st["pending_bus"].append(("asns_created", pd.DataFrame({
        "asn_id": asn_ids, "asn_nbr": st["asns"]["asn_nbr"][-n_asn:], "owner_code": info["code"],
        "warehouse_code": st["wh_code"][wi], "supplier_name": supplier, "source": src,
        "expected_date": [geo.day_date(int(d)) for d in exp_day], "lines": lines})))


def _delete_asns(st: dict[str, Any], fl: Flush, sel: np.ndarray) -> None:
    a = st["asns"]
    ids = a["asn_id"][sel]
    in_db = sel & (a["_st"] != 1)
    if in_db.any():
        fl.delete("asn_line", {"asn_id": a["asn_id"][in_db]})
        fl.delete("asn_header", {"asn_id": a["asn_id"][in_db]})
    fresh = sel & (a["_st"] == 1)
    if fresh.any():
        fl.drop_pending("asn_line", "asn_id", a["asn_id"][fresh])
    tkeep(a, ~sel)
    al = st["asn_lines"]
    tkeep(al, ~np.isin(al["asn_id"], ids))


def _reorder(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush, wi: int, t: int) -> None:
    """Daily replenishment review: clients send ASNs for SKUs whose projected stock is below the reorder point."""
    inv = st["inv"]
    mat = st["mat"]
    rng = W.rng(f"reorder-{wi}")
    here = inv["warehouse_id"] == wi + 1
    good = here & (inv["inv_status"] != I_DMG) & (inv["inv_status"] != I_HOLD)
    nmat = len(mat["material_id"])
    onh = np.bincount(inv["material_id"][good] - 1, weights=inv["qty_on_hand"][good] - inv["qty_allocated"][good], minlength=nmat)
    al = st["asn_lines"]
    a = st["asns"]
    open_asn = a["asn_id"][(a["wi"] == wi) & (a["receipt_id"] == 0)]
    inb_sel = np.isin(al["asn_id"], open_asn)
    inbound = np.bincount(al["material_id"][inb_sel] - 1, weights=al["expected_qty"][inb_sel], minlength=nmat)
    for info in st["owners"].values():
        if wi not in info["whs"] or info["status"] not in ("ACTIVE", "ONBOARDING") or info["owner_id"] in st.get("no_reorder", set()):
            continue
        idx = np.flatnonzero((mat["owner_id"] == info["owner_id"]) & (mat["status"] == 0))
        if not len(idx):
            continue
        dem = _demand_units(st, info["owner_id"], wi, idx)
        proj = onh[idx] + inbound[idx]
        lead = info["lead"] + 2
        rop = dem * (lead + 7)
        need = (proj < rop) & (dem > 0.02)
        if not need.any():
            continue
        sel = idx[need]
        cover = np.full(len(sel), lead + 7 + 24.0)
        shelf = mat["shelf"][sel]
        cover = np.where(shelf > 0, np.minimum(cover, np.maximum(lead + 3, shelf * 0.55)), cover)
        qty = dem[need] * cover - proj[need]
        cq = mat["case_qty"][sel]
        qty = (np.ceil(np.maximum(qty, cq) / cq) * cq).astype(np.int64)
        exp_day = int(t // DAY) + info["lead"] + int(rng.integers(-1, 2))
        _create_asns(ctx, st, W, fl, info, wi, t, sel, qty, exp_day, rng)
    _recall_receipt(ctx, st, W, fl, wi, t)


def _recall_receipt(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush, wi: int, t: int) -> None:
    """Planted recall_lot: one large Copper Kettle lot received ~9 months before the end, recalled ~2 months before."""
    story = next((s for s in ctx.planted.get("stories", []) if s["id"] == "recall_lot"), None)
    if not story:
        return
    info = st["owners"].get(story["client"])
    if info is None or wi != info["primary"] or info["status"] != "ACTIVE" or st["story"].get("recall_asn"):
        return
    hs = geo.epoch_day(ctx.history_start.date())
    he = geo.epoch_day(ctx.history_end.date())
    rday = max(hs + 25, geo.epoch_day(month_start(ctx.history_end.date(), -9)))
    if int(t // DAY) < rday:
        return
    mat = st["mat"]
    idx = np.flatnonzero((mat["owner_id"] == info["owner_id"]) & (mat["status"] == 0) & mat["sellable"])
    if not len(idx):
        return
    # the most popular long-life item: FEFO ships the new lot to many orders once the older stock is gone
    long_life = idx[mat["shelf"][idx] >= np.quantile(mat["shelf"][idx], 0.75)]
    inv = st["inv"]
    here = inv["warehouse_id"] == wi + 1
    onh = np.bincount(inv["material_id"][here] - 1, weights=inv["qty_on_hand"][here], minlength=len(mat["material_id"]))
    p = long_life[np.argsort(-mat["pop"][long_life])[:1]]
    rng = W.rng("recall")
    dem = max(float(_demand_units(st, info["owner_id"], wi, p)[0]), 1e-6)
    cover = float(onh[p[0]]) / dem
    shelf = int(mat["shelf"][p[0]])
    qty = (np.ceil(max(dem * min(50.0, shelf * 0.5), 300) / mat["case_qty"][p]) * mat["case_qty"][p]).astype(np.int64)
    day = int(t // DAY)
    lot = _lot_numbers(ctx, info["code"], np.array([day - 3]), rng)
    _create_asns(ctx, st, W, fl, info, wi, t, p, qty, day + 3, rng, lots=lot)
    al = st["asn_lines"]
    exp = int(al["expiry_date"][-1]) if len(al["expiry_date"]) else day + shelf
    recall_day = min(day + int(min(cover, 45)) + int(rng.integers(20, 31)), exp - 10, he - 4)
    st["story"]["recall_asn"] = True
    st["story"]["recall"] = {"lot": lot[0], "material_id": int(mat["material_id"][p[0]]), "day": recall_day, "wi": wi, "done": False}


def _inbound(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush) -> None:
    a = st["asns"]
    rng = W.rng("inbound")
    for wi in _active_whs(st):
        for t in W.crosses(st, wi, 9, 0):
            if (t // DAY + 3) % 7 < 5:
                _reorder(ctx, st, W, fl, wi, t)
    # ASN cancellations before arrival: hard delete
    le = W.le[a["wi"]] if len(a["wi"]) else np.zeros(0)
    canc = (a["t_cancel"] < le) & (a["receipt_id"] == 0) & (a["t_cancel"] < a["t_arrive"])
    if canc.any():
        _delete_asns(st, fl, canc)
        a = st["asns"]
        le = W.le[a["wi"]]
    # arrivals -> receipt headers
    arr = np.flatnonzero((a["t_arrive"] < le) & (a["receipt_id"] == 0))
    if len(arr):
        _arrive(ctx, st, W, fl, arr, rng)
    _receive_lines(ctx, st, W, fl, rng)
    a = st["asns"]
    done = (a["asn_status"] == "RECEIVED") & (a["_st"] == 0)
    if done.any():
        al = st["asn_lines"]
        tkeep(al, ~np.isin(al["asn_id"], a["asn_id"][done]))
        tkeep(a, ~done)


def _dts_factor(ctx: Ctx, st: dict[str, Any], wi: np.ndarray, t: np.ndarray) -> np.ndarray:
    """Dock-to-stock slowdowns: NJ1 staffing cut on receiving shift A (planted), LAX1 new-site ramp, storms."""
    f = np.ones(len(t))
    he = ctx.history_end.date()
    stories = {s["id"]: s for s in ctx.planted.get("stories", [])}
    reg = stories.get("dock_to_stock_regression")
    if reg:
        wj = _wh_index(ctx).get(reg["warehouse"])
        d0 = geo.epoch_day(month_start(he, int(reg["month"])))
        sh = st["wh_rules"][wj]["shifts"]["A"].split("-") if wj is not None else None
        if sh:
            a0, a1 = _hhmm(sh[0]), _hhmm(sh[1])
            tod = t % DAY
            f = np.where((wi == wj) & (t // DAY >= d0) & (tod >= a0) & (tod < a1), f * 2.15, f)
    lax = _wh_index(ctx).get("LAX1")
    if lax is not None:
        age = t // DAY - st["wh_open_day"][lax]
        f = np.where((wi == lax) & (age < 56), f * (1 + 1.6 * np.clip(1 - age / 56, 0, 1)), f)
    for (w, d0, d1, mult) in st["slowdowns"]:
        f = np.where((wi == w) & (t // DAY >= d0) & (t // DAY < d1), f * mult, f)
    return f


def _arrive(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush, arr: np.ndarray, rng: np.random.Generator) -> None:
    a = st["asns"]
    n = len(arr)
    rids = ctx.next_ids("wms.receipt", n)
    wi = a["wi"][arr].astype(np.int64)
    t_arr = a["t_arrive"][arr]
    dts = _dts_factor(ctx, st, wi, t_arr)
    start = _cal_add(ctx, st, wi, t_arr, (rng.uniform(0.3, 2.5, n) * 3600 * dts).astype(np.int64))
    doors = np.array([int(rng.integers(1, st["wh_rules"][int(w)]["dock_doors"] + 1)) for w in wi])
    trailer = np.array([f"{p}{int(x):06d}" for p, x in zip(rng.choice(["JBHU ", "SNLU ", "TRLU ", "HLXU ", "UMXU "], n),
                                                           rng.integers(100000, 999999, n))], dtype=object)
    a["receipt_id"][arr] = rids
    tadd(st["rcpts"], n, receipt_id=rids, receipt_nbr=np.array([f"RC{10_000_000 + int(r):08d}" for r in rids], dtype=object),
         asn_id=a["asn_id"][arr], owner_id=a["owner_id"][arr], warehouse_id=wi + 1,
         dock_door=np.array([f"D{d:02d}" for d in doors], dtype=object), trailer_nbr=trailer, arrived_ts=t_arr,
         started_ts=T_NONE, completed_ts=T_NONE, receipt_status="ARRIVED", create_ts=t_arr, mod_ts=t_arr, _st=1, wi=wi,
         dts=dts)
    # receipt lines: one per ASN line, received one after another once unloading starts
    al = st["asn_lines"]
    li = np.flatnonzero(np.isin(al["asn_id"], a["asn_id"][arr]))
    rmap = pd.Series(rids, index=a["asn_id"][arr])
    rid_l = rmap.reindex(al["asn_id"][li]).to_numpy(np.int64)
    smap = pd.Series(start, index=rids)
    t0 = smap.reindex(rid_l).to_numpy(np.int64)
    k = al["line_nbr"][li] - 1
    wl = pd.Series(wi, index=rids).reindex(rid_l).to_numpy(np.int64)
    dl = pd.Series(dts, index=rids).reindex(rid_l).to_numpy()
    rt = _cal_add(ctx, st, wl, t0, (k * rng.uniform(150, 420, len(li)) * dl + 120).astype(np.int64))
    exp_q = al["expected_qty"][li].astype(np.int64)
    var = rng.random(len(li))
    rq = np.where(var < 0.03, np.maximum(0, (exp_q * rng.uniform(0.85, 0.99, len(li))).astype(np.int64)),
                  np.where(var < 0.04, (exp_q * rng.uniform(1.01, 1.08, len(li))).astype(np.int64) + 1, exp_q))
    dmg = np.where(rng.random(len(li)) < 0.012, np.minimum(rq, rng.integers(1, 12, len(li))), 0)
    owner = pd.Series(a["owner_id"][arr], index=rids).reindex(rid_l).to_numpy()
    users = _pick_users(st, rng, wl, rt, "RECEIVER")
    tadd(st["rlines"], len(li), receipt_id=rid_l, line_nbr=al["line_nbr"][li], material_id=al["material_id"][li],
         received_qty=rq - dmg, damaged_qty=dmg, lot_nbr=al["lot_nbr"][li], expiry_date=al["expiry_date"][li], received_ts=rt,
         received_by=users, wi=wl, owner_id=owner, t_put=T_NONE)
    rc = st["rcpts"]
    nl = pd.Series(rid_l).value_counts()
    p = pos_of(rc["receipt_id"], rids)
    rc["lines_left"][p] = nl.reindex(rids).fillna(0).to_numpy(np.int32)
    lastl = pd.Series(rt, index=rid_l).groupby(level=0).max().reindex(rids).to_numpy()
    rc["completed_ts"][p] = np.where(np.isnan(lastl), start + 600, lastl).astype(np.int64) + rng.integers(300, 1800, n)
    rc["started_ts"][p] = start
    for w, door, tr, ta, tc in zip(wi, doors, trailer, t_arr, rc["completed_ts"][p]):
        st["door_log"].append((int(w), int(door), tr, int(ta), int(tc)))
    # ASN status
    a["asn_status"][arr] = "IN_RECEIVING"
    a["mod_ts"][arr] = start
    mark_pos(a, arr)


def _receive_lines(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush, rng: np.random.Generator) -> None:
    rc = st["rcpts"]
    rl = st["rlines"]
    if not len(rc["receipt_id"]):
        return
    le_r = W.le[rc["wi"]]
    started = (rc["receipt_status"] == "ARRIVED") & (rc["started_ts"] < le_r)
    if started.any():
        rc["receipt_status"][started] = "RECEIVING"
        rc["mod_ts"][started] = rc["started_ts"][started]
        mark(rc, started)
    due = np.flatnonzero((rl["received_ts"] < W.le[rl["wi"]]) & (rl["t_put"] == T_NONE))
    if len(due):
        _post_receipt_lines(ctx, st, W, fl, due, rng)
    rl = st["rlines"]
    # completion: all lines received
    comp = (rc["receipt_status"] == "RECEIVING") & (rc["completed_ts"] < le_r) & (rc["lines_left"] <= 0)
    if comp.any():
        rc["receipt_status"][comp] = "COMPLETE"
        rc["mod_ts"][comp] = rc["completed_ts"][comp]
        mark(rc, comp)
        a = st["asns"]
        ap = np.flatnonzero(np.isin(a["receipt_id"], rc["receipt_id"][comp]))
        a["asn_status"][ap] = "RECEIVED"
        a["mod_ts"][ap] = pd.Series(rc["completed_ts"][comp], index=rc["receipt_id"][comp]).reindex(a["receipt_id"][ap]).to_numpy(np.int64)
        mark_pos(a, ap)
        _receipt_complete_effects(ctx, st, W, fl, np.flatnonzero(comp), rng)


def _post_receipt_lines(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush, due: np.ndarray, rng: np.random.Generator) -> None:
    """Lines scanned at the dock: receipt_line rows, LPNs and QC inventory at the door, RECEIVE ledger rows."""
    rl = st["rlines"]
    mat = st["mat"]
    rc = st["rcpts"]
    n = len(due)
    mi = rl["material_id"][due] - 1
    qty = rl["received_qty"][due].astype(np.int64)
    per_pal = np.maximum(mat["case_qty"][mi] * mat["cpp"][mi], 1)
    n_lpn = np.clip(np.ceil(qty / per_pal), 1, 6).astype(int)
    n_lpn = np.where(qty <= 0, 0, n_lpn)
    rep = np.repeat(np.arange(n), n_lpn)
    k = np.concatenate([np.arange(c) for c in n_lpn]) if n_lpn.sum() else np.zeros(0, int)
    q_each = np.ceil(qty / np.maximum(n_lpn, 1)).astype(np.int64)
    q = np.where(k == n_lpn[rep] - 1, qty[rep] - q_each[rep] * (n_lpn[rep] - 1), q_each[rep])
    ok = q > 0
    rep, q = rep[ok], q[ok]
    m = len(rep)
    wi = rl["wi"][due][rep].astype(np.int64)
    rcp = pos_of(rc["receipt_id"], rl["receipt_id"][due][rep])
    doors = rc["dock_door"][rcp]
    door_loc = np.zeros(m, dtype=np.int32)
    for w in np.unique(wi):
        pool = _special_locs(st, int(w), LT_DOCK)
        mm = wi == w
        dn = np.array([int(d[1:]) for d in doors[mm]])
        door_loc[mm] = pool[np.clip(dn - 1, 0, len(pool) - 1)]
    lpn = ctx.next_ids("wms.lpn", m)
    inv_ids = ctx.next_ids("wms.inventory", m)
    ts = rl["received_ts"][due][rep]
    owner = rl["owner_id"][due][rep]
    mids = rl["material_id"][due][rep]
    lot = rl["lot_nbr"][due][rep]
    exp = rl["expiry_date"][due][rep]
    tadd(st["inv"], m, inventory_id=inv_ids, warehouse_id=wi + 1, location_id=door_loc, lpn_id=lpn, owner_id=owner,
         material_id=mids, lot_nbr=lot, expiry_date=exp, qty_on_hand=q, qty_allocated=0, inv_status=I_QC,
         hold_reason=None, received_date=ts // DAY, create_ts=ts, mod_ts=ts, _st=1)
    small = q <= mat["case_qty"][mids - 1] * 2
    tadd(st["lpns"], m, lpn_id=lpn, lpn_nbr=np.array([f"LP{1_000_000_000 + int(x):010d}" for x in lpn], dtype=object),
         warehouse_id=wi + 1, location_id=door_loc, lpn_type=np.where(small, "CASE", "PALLET").astype(object),
         lpn_status="RECEIVED", create_ts=ts, mod_ts=ts, _st=1)
    users = rl["received_by"][due][rep]
    _txn(ctx, st, fl, rng, wi, ts, owner, mids, "RECEIVE", q, to_loc=door_loc, lpn=lpn, lot=lot, ref_type="RECEIPT",
         ref_id=rl["receipt_id"][due][rep], user=users)
    first_lpn = pd.Series(lpn, index=rep).groupby(level=0).first().reindex(np.arange(n)).to_numpy()
    df = pd.DataFrame({"receipt_id": rl["receipt_id"][due], "line_nbr": rl["line_nbr"][due], "material_id": rl["material_id"][due],
                       "lpn_id": nint(np.nan_to_num(first_lpn, nan=0).astype(np.int64)),
                       "received_qty": rl["received_qty"][due], "damaged_qty": rl["damaged_qty"][due], "uom": "EA",
                       "lot_nbr": rl["lot_nbr"][due], "expiry_date": day_col(rl["expiry_date"][due]),
                       "received_ts": ts_col(rl["received_ts"][due]), "received_by": nint(rl["received_by"][due])})
    neg = ctx.dq.mask("negative_qty", n, rng) & (df["received_qty"].to_numpy() > 0)
    for i in np.flatnonzero(neg):
        before = int(df.at[i, "received_qty"])
        df.at[i, "received_qty"] = -before
        ctx.dq.log("negative_qty", "wms", "receipt_line", f"{df.at[i, 'receipt_id']}-{df.at[i, 'line_nbr']}", "received_qty", before, -before)
    fl.insert("receipt_line", df)
    # putaway tasks, one per LPN, after the receipt completes
    rcp_line = pos_of(rc["receipt_id"], rl["receipt_id"][due])
    rl["t_put"][due] = rc["completed_ts"][rcp_line]
    np.subtract.at(rc["lines_left"], rcp_line, 1)
    dts = rc["dts"][rcp]
    done_t = rc["completed_ts"][rcp]
    put_start = _cal_add(ctx, st, wi, done_t, (rng.uniform(0.3, 5.0, m) * 3600 * dts).astype(np.int64))
    put_done = put_start + rng.integers(180, 900, m)
    mzone = mat["zone"][mids - 1]
    lt = np.where(small, LT_PICK, LT_RES).astype(np.int8)
    to_loc = _pick_locations(st, rng, wi, mzone, lt)
    tids = ctx.next_ids("wms.task", m)
    fork = _pick_users(st, rng, wi, put_start, "FORKLIFT")
    tadd(st["tasks"], m, task_id=tids, warehouse_id=wi + 1, task_type=K_PUT, task_status=0, priority=5,
         receipt_id=rl["receipt_id"][due][rep], material_id=mids, qty=q, from_location_id=door_loc, to_location_id=to_loc,
         assigned_user_id=fork, created_ts=done_t, assigned_ts=put_start - rng.integers(30, 600, m), started_ts=put_start,
         completed_ts=put_done, mod_ts=done_t, _st=1, applied=False, ref=inv_ids, wi=wi)


def _receipt_complete_effects(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush, cp: np.ndarray, rng: np.random.Generator) -> None:
    rc = st["rcpts"]
    rl = st["rlines"]
    mat = st["mat"]
    rids = rc["receipt_id"][cp]
    sel = np.isin(rl["receipt_id"], rids)
    mi = rl["material_id"][sel] - 1
    q = rl["received_qty"][sel].astype(np.int64)
    pallets = pd.Series(np.ceil(q / np.maximum(mat["case_qty"][mi] * mat["cpp"][mi], 1)), index=rl["receipt_id"][sel]).groupby(level=0).sum()
    cartons = pd.Series(np.ceil(q / np.maximum(mat["case_qty"][mi], 1)), index=rl["receipt_id"][sel]).groupby(level=0).sum()
    units = pd.Series(q, index=rl["receipt_id"][sel]).groupby(level=0).sum()
    pal = pallets.reindex(rids).fillna(0).to_numpy(np.int64)
    ctn = cartons.reindex(rids).fillna(0).to_numpy(np.int64)
    un = units.reindex(rids).fillna(0).to_numpy(np.int64)
    wi = rc["wi"][cp].astype(np.int64)
    a = st["asns"]
    ap = pos_of(a["asn_id"], rc["asn_id"][cp])
    transfer = a["transfer"][ap]
    owners = rc["owner_id"][cp]
    t = rc["completed_ts"][cp]
    floor_loaded = np.array([st["owners_by_id"].get(int(o), {}).get("imported", False) for o in owners]) if "owners_by_id" in st else \
        np.array([_owner_by_id(st, int(o))["imported"] for o in owners])
    code = np.where(floor_loaded, "RCV-CTN", "RCV-PLT")
    qty = np.where(floor_loaded, ctn, pal)
    _accrue(ctx, st, fl, rng, owners[~transfer], wi[~transfer], code[~transfer], t[~transfer], qty[~transfer],
            "RECEIPT", rids[~transfer])
    a_nbr = a["asn_nbr"][ap]
    st["pending_bus"].append(("receipts_completed", pd.DataFrame({
        "receipt_id": rids, "receipt_nbr": rc["receipt_nbr"][cp], "asn_nbr": a_nbr,
        "owner_code": [_owner_by_id(st, int(o))["code"] for o in owners], "warehouse_code": st["wh_code"][wi],
        "completed_ts": _utc_series(st, wi, t), "cartons": ctn, "pallets": pal, "units": un})))
    tkeep(rl, ~sel)


def _owner_by_id(st: dict[str, Any], oid: int) -> dict[str, Any]:
    by = st.get("_owner_by_id")
    if by is None or len(by) != len(st["owners"]):
        by = {i["owner_id"]: i for i in st["owners"].values()}
        st["_owner_by_id"] = by
    return by[oid]


def _utc_series(st: dict[str, Any], wi: np.ndarray, local: np.ndarray) -> pd.Series:
    tz = st["wh_tz"][np.asarray(wi, dtype=np.int64)]
    u = geo.local_to_utc_many(tz, np.asarray(local, dtype=np.int64))
    return pd.Series(pd.to_datetime(u, unit="s", utc=True))


def _pick_users(st: dict[str, Any], rng: np.random.Generator, wi: np.ndarray, t: np.ndarray, role: str) -> np.ndarray:
    """A user of ``role`` on shift at time t (per row)."""
    u = st["users"]
    out = np.zeros(len(t), dtype=np.int32)
    tod = t % DAY
    for w in np.unique(wi):
        m = wi == w
        shifts = st["wh_rules"][int(w)]["shifts"]
        cand = u["is_active"] & (u["warehouse_id"] == w + 1) & (u["user_role"] == role)
        if not cand.any():
            cand = u["is_active"] & (u["warehouse_id"] == w + 1)
            if not cand.any():
                continue
        ids = u["user_id"][cand]
        sh = u["shift"][cand]
        cur = np.full(int(m.sum()), next(iter(shifts)), dtype=object)
        for s, span in shifts.items():
            a0, a1 = (_hhmm(x) for x in span.split("-"))
            if a1 <= a0:
                in_s = (tod[m] >= a0) | (tod[m] < a1)
            else:
                in_s = (tod[m] >= a0) & (tod[m] < a1)
            cur[in_s] = s
        res = np.zeros(int(m.sum()), dtype=np.int32)
        for s in np.unique(cur):
            pool = ids[sh == s]
            if not len(pool):
                pool = ids
            mm = cur == s
            res[mm] = pool[rng.integers(0, len(pool), int(mm.sum()))]
        out[m] = res
    return out


# =============================================================================================== orders: creation
def _new_orders(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush) -> None:
    infos = [i for i in st["owners"].values() if i["status"] in ("ACTIVE", "OFFBOARDING")
             and any(w in st["wh_ready"] and st["wh_open_day"][w] * DAY <= W.le[w] for w in i["whs"])]
    if not infos:
        return
    rng = W.rng("orders")
    edges = np.arange((W.ws // 900) * 900, W.we, 900, dtype=np.int64)
    b_start = np.maximum(edges, W.ws)
    b_end = np.minimum(edges + 900, W.we)
    frac = (b_end - b_start) / 900.0
    n_cl = len(infos)
    lam_c = np.zeros((n_cl, len(edges)))
    lam_b = np.zeros((n_cl, len(edges)))
    cache: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    for i, info in enumerate(infos):
        loc = geo.utc_to_local(st["wh_tz"][info["primary"]], b_start)
        hour = (loc % DAY) // 3600
        day = loc // DAY
        for d in np.unique(day):
            if d not in cache:
                cache[int(d)] = _daily_rates(ctx, st, infos, int(d))
            rc, rb = cache[int(d)]
            m = day == d
            lam_c[i, m] = rc[i] * PROF_B2C[hour[m]] / 4 * frac[m]
            lam_b[i, m] = rb[i] * PROF_B2B[hour[m]] / 4 * frac[m]
    nc = rng.poisson(lam_c)
    nb = rng.poisson(lam_b)
    ci_c, bi_c = np.nonzero(nc)
    ci_b, bi_b = np.nonzero(nb)
    cli = np.concatenate([np.repeat(ci_c, nc[ci_c, bi_c]), np.repeat(ci_b, nb[ci_b, bi_b])])
    bkt = np.concatenate([np.repeat(bi_c, nc[ci_c, bi_c]), np.repeat(bi_b, nb[ci_b, bi_b])])
    is_b2b = np.concatenate([np.zeros(nc.sum(), bool), np.ones(nb.sum(), bool)])
    n = len(cli)
    if n == 0:
        return
    utc = b_start[bkt] + (rng.random(n) * (b_end[bkt] - b_start[bkt])).astype(np.int64)
    order = np.argsort(utc, kind="stable")
    cli, bkt, is_b2b, utc = cli[order], bkt[order], is_b2b[order], utc[order]
    _build_orders(ctx, st, W, fl, rng, [infos[i] for i in range(n_cl)], cli, is_b2b, utc)


def _build_orders(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush, rng: np.random.Generator, infos: list[dict[str, Any]],
                  cli: np.ndarray, is_b2b: np.ndarray, utc: np.ndarray, transfer: dict[str, Any] | None = None) -> None:
    n = len(cli)
    c = geo.cities()
    mat = st["mat"]
    countries = np.array([infos[i]["country"] for i in cli], dtype=object)
    ctry_mix = {"US": ("CA", 0.03), "CA": ("US", 0.18), "MX": ("US", 0.08)}
    dest_ctry = countries.copy()
    for k, (other, p) in ctry_mix.items():
        m = (countries == k) & (rng.random(n) < p)
        dest_ctry[m] = other
    city = geo.sample_cities(rng, dest_ctry)
    name = np.full(n, None, dtype=object)
    company = np.full(n, None, dtype=object)
    a1, a2 = geo.street_lines(rng, city, ~is_b2b)
    postal = geo.postal_codes(rng, city)
    phone = geo.phones(rng, city)
    locale = c["locale"].to_numpy()[city]
    first, last = geo.names(rng, locale)
    name[:] = [f"{f} {ln}" for f, ln in zip(first, last)]
    email = geo.emails(rng, first, last)
    # B2B orders go to the client's retail ship-to accounts
    for i in np.unique(cli[is_b2b]):
        info = infos[int(i)]
        stt = st["shiptos"][info["code"]]
        m = is_b2b & (cli == i)
        k = rng.choice(len(stt["w"]), size=int(m.sum()), p=stt["w"] / stt["w"].sum())
        city[m] = stt["city"][k]
        company[m] = stt["company"][k]
        name[m] = stt["name"][k]
        a1[m] = stt["a1"][k]
        a2[m] = stt["a2"][k]
        postal[m] = stt["postal"][k]
        phone[m] = stt["phone"][k]
        email[m] = None
    if transfer:
        city[:] = transfer["city"]
        company[:] = transfer["company"]
        name[:] = transfer["name"]
        a1[:] = transfer["a1"]
        a2[:] = None
        postal[:] = transfer["postal"]
        phone[:] = None
        email[:] = None
    # route to the nearest active warehouse of the client (15% balancing to another site)
    wi = np.zeros(n, dtype=np.int64)
    for i in np.unique(cli):
        info = infos[int(i)]
        m = cli == i
        today = int(geo.utc_to_local(st["wh_tz"][info["primary"]], np.array([W.ws]))[0] // DAY)
        cand = [w for w in info["whs"] if w in st["wh_ready"] and st["wh_open_day"][w] <= today]
        if not cand:
            cand = [info["primary"]]
        if len(cand) == 1:
            wi[m] = cand[0]
            continue
        d = st["dist"][city[m]][:, cand]
        best = np.array(cand)[np.argmin(d, axis=1)]
        alt = np.array(cand)[rng.integers(0, len(cand), int(m.sum()))]
        wi[m] = np.where(rng.random(int(m.sum())) < 0.15, alt, best)
    if transfer:
        wi[:] = transfer["wi"]
    local = geo.utc_to_local_many(st["wh_tz"][wi], utc)
    # lines
    lo = np.array([infos[i]["lines_b2b" if b else "lines_b2c"][0] for i, b in zip(cli, is_b2b)])
    hi = np.array([infos[i]["lines_b2b" if b else "lines_b2c"][1] for i, b in zip(cli, is_b2b)])
    u = rng.random(n)
    nl = lo + np.floor((hi - lo + 1) * np.where(is_b2b, u ** 5.0, u ** 2.5)).astype(int)
    nl = np.minimum(nl, hi)
    if transfer:
        nl[:] = len(transfer["mat_idx"])
    line_o = np.repeat(np.arange(n), nl)
    line_mat = np.zeros(len(line_o), dtype=np.int64)
    for i in np.unique(cli):
        info = infos[int(i)]
        m = cli[line_o] == i
        pool = np.flatnonzero((mat["owner_id"] == info["owner_id"]) & (mat["status"] == 0) & mat["sellable"])
        if transfer:
            line_mat[m] = transfer["mat_idx"]
            continue
        if not len(pool):
            line_mat[m] = -1
            continue
        w = mat["pop"][pool]
        line_mat[m] = pool[rng.choice(len(pool), size=int(m.sum()), p=w / w.sum())]
    ok = line_mat >= 0
    key = line_o.astype(np.int64) * 10_000_000 + line_mat
    _, first_idx = np.unique(key, return_index=True)
    uniq = np.zeros(len(key), bool)
    uniq[first_idx] = True
    keep = ok & uniq
    line_o, line_mat = line_o[keep], line_mat[keep]
    has = np.bincount(line_o, minlength=n) > 0
    if not has.any():
        return
    # drop orders without lines
    remap = np.cumsum(has) - 1
    sel = np.flatnonzero(has)
    cli, is_b2b, utc, local, wi, city = cli[sel], is_b2b[sel], utc[sel], local[sel], wi[sel], city[sel]
    name, company, a1, a2, postal, phone, email = name[sel], company[sel], a1[sel], a2[sel], postal[sel], phone[sel], email[sel]
    line_o = remap[line_o]
    n = len(sel)
    line_nbr = np.ones(len(line_o), dtype=np.int64)
    if len(line_o):
        starts = np.r_[0, np.flatnonzero(np.diff(line_o)) + 1]
        line_nbr = np.arange(len(line_o)) - np.repeat(starts, np.diff(np.r_[starts, len(line_o)])) + 1
    cq = mat["case_qty"][line_mat].astype(np.int64)
    b2b_l = is_b2b[line_o]
    q_b2c = rng.geometric(0.78, len(line_o))
    vert_auto = np.array([infos[cli[o]]["vertical"] == "auto_parts" for o in line_o])
    q_b2b = np.where(vert_auto, rng.integers(1, 12, len(line_o)), cq * rng.geometric(0.4, len(line_o)))
    qty = np.where(b2b_l, q_b2b, q_b2c).astype(np.int64)
    if transfer:
        qty = transfer["qty"].astype(np.int64)
    value = mat["value"][line_mat]
    price = np.where(b2b_l, np.round(value * 1.35, 2), np.floor(value * rng.uniform(1.8, 2.4, len(line_o))) + 0.99)
    units = np.bincount(line_o, weights=qty, minlength=n).astype(np.int64)
    lines_n = np.bincount(line_o, minlength=n)
    est_lb = np.bincount(line_o, weights=qty * mat["lb"][line_mat], minlength=n)
    cartons_est = np.bincount(line_o, weights=np.ceil(qty / np.maximum(cq, 1)), minlength=n)
    is_xfer = transfer is not None
    is_ltl = (is_b2b & ((est_lb > 150) | (cartons_est > 8))) | is_xfer
    svc = st["svc"]
    sid = carr.choose_services(rng, svc, st["wh_country"][wi], st["wh_state"][wi], c["country"].to_numpy()[city],
                               c["state"].to_numpy()[city], is_ltl, est_lb, is_b2b)
    srow = svc.set_index("service_id").loc[sid]
    # header fields
    oids = ctx.next_ids("wms.order", n)
    order_nbr = np.array([f"HL{2_041_000_000 + int(x):010d}" for x in oids], dtype=object)
    edi = np.array([infos[i]["edi"] for i in cli])
    chan_u = rng.random(n)
    channel = np.where(~is_b2b, np.where(chan_u < 0.95, "API", np.where(chan_u < 0.98, "PORTAL", "CSV")),
                       np.where(edi, np.where(chan_u < 0.9, "EDI", "PORTAL"),
                                np.where(chan_u < 0.4, "EDI", np.where(chan_u < 0.75, "PORTAL", "CSV")))).astype(object)
    if transfer:
        channel[:] = "PORTAL"
    otype = np.where(is_b2b, "B2B", "B2C").astype(object)
    if transfer:
        otype[:] = "TRANSFER"
    refs = _client_refs(ctx, infos, cli, is_b2b, local)
    ordered = np.where(channel == "API", utc - rng.integers(10, 900, n),
                       np.where(channel == "EDI", local - rng.integers(3600, 30 * 3600, n), local - rng.integers(0, 7200, n)))
    day = local // DAY
    rsd = np.where(is_b2b, day + rng.integers(1, 7, n), day)
    dow = (rsd + 3) % 7
    rsd = np.where(dow == 5, rsd + 2, np.where(dow == 6, rsd + 1, rsd))
    svc_code = srow["service_code"].to_numpy(object)
    prio = np.where(np.isin(svc_code, ["NDA", "FPO", "PEX", "EDS"]), 1, np.where(np.isin(svc_code, ["2DA", "F2D", "CXP", "UPM"]), 3, 5))
    create_user = np.select([channel == "EDI", channel == "API", channel == "CSV"], ["svc_edi", "svc_api", "csv_import"],
                            "portal_user").astype(object)
    portal = channel == "PORTAL"
    if portal.any():
        create_user[portal] = [f"{infos[i]['code'].lower()}.portal" for i in cli[portal]]
    # drafts, holds, cancellations, edits
    draft = portal & (rng.random(n) < 0.10) & (not is_xfer)
    hold = (rng.random(n) < 0.006) & ~draft
    t_ready = local + np.where(hold, rng.integers(2 * 3600, 30 * 3600, n), 0)
    t_alloc = np.where(draft, T_NONE, t_ready + rng.integers(120, 900, n))
    exp_span = np.where(is_b2b, (rsd * DAY + 12 * 3600 - local).clip(4 * 3600), 5 * 3600)
    t_cancel = np.where((rng.random(n) < np.where(is_b2b, 0.008, 0.012)) & ~draft & (not is_xfer),
                        local + (exp_span * rng.uniform(0.02, 0.9, n)).astype(np.int64), T_NONE)
    t_addr = np.where((~is_b2b) & (rng.random(n) < 0.01) & ~draft, local + (exp_span * rng.uniform(0.05, 0.6, n)).astype(np.int64), T_NONE)
    t_date = np.where(is_b2b & (rng.random(n) < 0.04) & ~draft, local + (exp_span * rng.uniform(0.05, 0.5, n)).astype(np.int64), T_NONE)
    new_rsd = np.where(t_date < T_NONE, rsd + rng.integers(1, 6, n), D_NONE)
    new_rsd = np.where(((new_rsd + 3) % 7) >= 5, new_rsd + 2, new_rsd)
    t_lcancel = np.where(is_b2b & (lines_n > 1) & (rng.random(n) < 0.03) & ~draft,
                         local + (exp_span * rng.uniform(0.05, 0.4, n)).astype(np.int64), T_NONE)
    t_drop = np.where(draft, local + rng.integers(6 * 3600, 48 * 3600, n), T_NONE)
    kit = (~is_b2b) & (rng.random(n) < 0.05)
    status = np.where(hold, O_HOLD, O_NEW).astype(np.int8)
    zone = geo.parcel_zone(st["dist"][city, wi])
    owner_id = np.array([infos[i]["owner_id"] for i in cli])
    deliv = np.full(n, None, dtype=object)
    if st["schema_v2"]:
        notes = np.array(["Leave at back door", "Call on arrival", "Gate code 4512", "Deliver to front desk",
                          "Do not leave unattended", "Use side entrance", "Receiving hours 7am-3pm"], dtype=object)
        m = rng.random(n) < np.where(is_b2b, 0.3, 0.14)
        deliv[m] = notes[rng.integers(0, len(notes), int(m.sum()))]
    o = st["orders"]
    base = len(o["order_id"])
    tadd(o, n, order_id=oids, order_nbr=order_nbr, owner_id=owner_id, warehouse_id=wi + 1, client_order_ref=refs,
         order_type=otype, channel=channel, order_status=status, priority=prio, ordered_ts=ordered, received_ts=local,
         requested_ship_date=rsd, carrier_service_id=sid, ship_to_name=name, ship_to_company=company, ship_to_address1=a1,
         ship_to_address2=a2, ship_to_city=c["city"].to_numpy()[city], ship_to_state=c["state"].to_numpy()[city],
         ship_to_postal=postal, ship_to_country=c["country"].to_numpy()[city], ship_to_phone=phone, ship_to_email=email,
         is_residential=~is_b2b, total_lines=lines_n, total_units=units, create_ts=local, mod_ts=local, create_user=create_user,
         mod_user=create_user, delivery_instructions=deliv, _st=1, wi=wi, ci=cli, city=city, is_ltl=is_ltl, is_draft=draft,
         rsd_day=rsd, t_ready=t_ready, t_alloc=t_alloc, t_cancel=t_cancel, t_addr=t_addr, t_date=t_date, t_lcancel=t_lcancel,
         t_drop=t_drop, new_rsd=new_rsd, bo_since=T_NONE, kit=kit, zone=zone)
    tadd(st["lines"], len(line_o), order_id=oids[line_o], line_nbr=line_nbr, material_id=mat["material_id"][line_mat],
         ordered_qty=qty, uom="EA", unit_price=price, line_status=L_OPEN, _st=1, wi=wi[line_o], mat=line_mat)
    _dq_orders(ctx, st, rng, base, n)
    if transfer:
        return
    pub = ~draft
    owner_code = np.array([infos[i]["code"] for i in cli], dtype=object)
    oc = st["orders"]
    sl = slice(base, base + n)
    st["pending_bus"].append(("orders_created", pd.DataFrame({
        "order_id": oids[pub], "order_nbr": order_nbr[pub], "owner_code": owner_code[pub], "warehouse_code": st["wh_code"][wi[pub]],
        "client_order_ref": oc["client_order_ref"][sl][pub], "order_type": otype[pub], "channel": channel[pub],
        "received_ts": pd.to_datetime(utc[pub], unit="s", utc=True), "received_ts_local": ts_col(local[pub]),
        "requested_ship_date": pd.to_datetime(rsd[pub] * DAY, unit="s").date, "ship_to_name": name[pub],
        "ship_to_company": company[pub], "ship_to_address1": a1[pub], "ship_to_city": oc["ship_to_city"][sl][pub],
        "ship_to_state": oc["ship_to_state"][sl][pub], "ship_to_postal": oc["ship_to_postal"][sl][pub],
        "ship_to_country": oc["ship_to_country"][sl][pub], "is_residential": ~is_b2b[pub], "scac": srow["scac"].to_numpy(object)[pub],
        "service_code": svc_code[pub], "total_lines": oc["total_lines"][sl][pub], "total_units": oc["total_units"][sl][pub]})))
    lp = pub[line_o]
    st["pending_bus"].append(("order_lines_created", pd.DataFrame({
        "order_id": oids[line_o][lp], "line_nbr": line_nbr[lp], "owner_code": owner_code[line_o][lp], "sku": mat["sku"][line_mat][lp],
        "upc": mat["upc"][line_mat][lp], "description": mat["description"][line_mat][lp], "ordered_qty": qty[lp], "uom": "EA",
        "unit_price": price[lp]})))
    # demand memory for replenishment and staffing
    np.add.at(st["ema_units"], (owner_id[line_o], wi[line_o]), qty / 21.0)
    np.add.at(st["ema_lines"], wi[line_o], 1 / 21.0)


def _ref_format(code: str, b2b: bool, style: int, s: int, y: int) -> str:
    """Client order reference: PO numbers for B2B, web-store order numbers for B2C (one house style per client)."""
    pre = "".join(ch for ch in code if ch.isalpha())[:2]
    if b2b:
        return (f"{4_400_000 + s}", f"PO-{y:02d}-{s:05d}", f"{code[:3]}{s + 10_000_000:08d}", f"{s + 300_000_000:09d}")[style]
    return (f"#{pre}{1_000_000 + s}", f"SO-{700_000 + s}", f"W{100_000_000 + s}", f"{pre}-{y:02d}{s:06d}")[style]


def _client_refs(ctx: Ctx, infos: list[dict[str, Any]], cli: np.ndarray, is_b2b: np.ndarray, local: np.ndarray) -> np.ndarray:
    out = np.empty(len(cli), dtype=object)
    for i in np.unique(cli):
        info = infos[int(i)]
        for b in (False, True):
            m = (cli == i) & (is_b2b == b)
            k = int(m.sum())
            if not k:
                continue
            seq = ctx.next_ids(f"wms.ref.{info['code']}.{int(b)}", k, start=int(_hash_rng(info["code"], b).integers(10_000, 90_000)))
            style = derive_seed(info["code"], b) % 4
            yy = (local[m] // DAY // 365 + 70) % 100
            out[m] = [_ref_format(info["code"], b, style, int(s), int(y)) for s, y in zip(seq, yy)]
    return out


def _dq_orders(ctx: Ctx, st: dict[str, Any], rng: np.random.Generator, base: int, n: int) -> None:
    o = st["orders"]
    sl = np.arange(base, base + n)

    def log(cat: str, i: int, col: str, before: Any, after: Any, note: str | None = None) -> None:
        ctx.dq.log(cat, "wms", "order_header", str(o["order_id"][i]), col, before, after, note)

    for i in sl[ctx.dq.mask("null_required", n, rng)]:
        log("null_required", i, "ship_to_postal", o["ship_to_postal"][i], None)
        o["ship_to_postal"][i] = None
    for i in sl[ctx.dq.mask("sentinel_date", n, rng)]:
        before = geo.day_date(int(o["requested_ship_date"][i])).isoformat()
        o["requested_ship_date"][i] = geo.epoch_day(date(1900, 1, 1))
        log("sentinel_date", i, "requested_ship_date", before, "1900-01-01")
    for i in sl[ctx.dq.mask("future_date", n, rng)]:
        before = o["ordered_ts"][i]
        o["ordered_ts"][i] = before + 366 * DAY
        log("future_date", i, "ordered_ts", str(ts_col(np.array([before]))[0]), str(ts_col(np.array([o['ordered_ts'][i]]))[0]),
            "client clock a year ahead")
    for i in sl[ctx.dq.mask("key_whitespace_case", n, rng)]:
        before = o["client_order_ref"][i]
        after = before.lower() + " " if before.lower() != before else before + "  "
        o["client_order_ref"][i] = after
        log("key_whitespace_case", i, "client_order_ref", before, after)
    for i in sl[ctx.dq.mask("mojibake", n, rng)]:
        for col in ("ship_to_city", "ship_to_name", "ship_to_address1"):
            v = o[col][i]
            if v and not str(v).isascii():
                o[col][i] = geo.mojibake(str(v))[:100]
                log("mojibake", i, col, v, o[col][i])
                break
    for i in sl[ctx.dq.mask("header_line_mismatch", n, rng)]:
        before = int(o["total_units"][i])
        o["total_units"][i] = before + int(rng.integers(1, 6))
        log("header_line_mismatch", i, "total_units", before, int(o["total_units"][i]), "header total_units != sum(order_line.ordered_qty)")


# =============================================================================================== orders: fulfilment
def _order_flow(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush) -> None:
    rng = W.rng("flow")
    o = st["orders"]
    if not len(o["order_id"]):
        return
    _drafts(ctx, st, W, fl)
    o = st["orders"]
    le = W.le[o["wi"]]
    m = (o["order_status"] == O_HOLD) & (o["t_ready"] < le)
    if m.any():
        o["order_status"][m] = O_NEW
        o["mod_ts"][m] = o["t_ready"][m]
        o["mod_user"][m] = "csr_review"
        mark(o, m)
    _edits(ctx, st, W, fl, rng)
    _line_cancels(ctx, st, W, fl, rng)
    _cancellations(ctx, st, W, fl, rng)
    _allocation(ctx, st, W, fl, rng)
    _release(ctx, st, W, fl, rng)
    _tasks_progress(ctx, st, W, fl, rng)
    _pack(ctx, st, W, fl, rng)
    _label_events(ctx, st, W, fl, rng)
    _ship(ctx, st, W, fl, rng)


def _drafts(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush) -> None:
    o = st["orders"]
    m = o["is_draft"] & (o["t_drop"] < W.le[o["wi"]])
    if not m.any():
        return
    ids = o["order_id"][m]
    in_db = m & (o["_st"] != 1)
    L = st["lines"]
    if in_db.any():
        fl.delete("order_line", {"order_id": L["order_id"][np.isin(L["order_id"], o["order_id"][in_db])],
                                 "line_nbr": L["line_nbr"][np.isin(L["order_id"], o["order_id"][in_db])]})
        fl.delete("order_header", {"order_id": o["order_id"][in_db]})
    tkeep(L, ~np.isin(L["order_id"], ids))
    tkeep(o, ~m)


def _edits(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush, rng: np.random.Generator) -> None:
    o = st["orders"]
    le = W.le[o["wi"]]
    m = (o["t_addr"] < le) & (o["order_status"] < O_PACK)
    if m.any():
        idx = np.flatnonzero(m)
        a1, a2 = geo.street_lines(rng, o["city"][idx], np.ones(len(idx), bool))
        o["ship_to_address1"][idx] = a1
        o["ship_to_address2"][idx] = a2
        o["ship_to_postal"][idx] = geo.postal_codes(rng, o["city"][idx])
        o["mod_ts"][idx] = o["t_addr"][idx]
        o["mod_user"][idx] = "csr_" + np.array(["amartin", "jlee", "rpatel", "dgomez"], dtype=object)[rng.integers(0, 4, len(idx))]
        o["t_addr"][idx] = T_NONE
        mark(o, m)
    m = (o["t_date"] < le) & (o["order_status"] < O_REL)
    if m.any():
        o["requested_ship_date"][m] = o["new_rsd"][m]
        o["rsd_day"][m] = o["new_rsd"][m]
        o["mod_ts"][m] = o["t_date"][m]
        o["mod_user"][m] = "svc_edi"
        o["t_date"][m] = T_NONE
        mark(o, m)
    o["t_addr"][(o["t_addr"] < le) & (o["order_status"] >= O_PACK)] = T_NONE
    o["t_date"][(o["t_date"] < le) & (o["order_status"] >= O_REL)] = T_NONE


def _line_cancels(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush, rng: np.random.Generator) -> None:
    o = st["orders"]
    le = W.le[o["wi"]]
    m = (o["t_lcancel"] < le) & (o["order_status"] < O_REL)
    o["t_lcancel"][(o["t_lcancel"] < le) & ~m] = T_NONE
    if not m.any():
        return
    L = st["lines"]
    ids = o["order_id"][m]
    lp = np.flatnonzero(np.isin(L["order_id"], ids))
    # the last line of each affected order is cut by the client
    last = pd.Series(lp, index=L["order_id"][lp]).groupby(level=0).max().to_numpy()
    _release_line_allocs(st, fl, last)
    in_db = L["_st"][last] != 1
    if in_db.any():
        fl.delete("order_line", {"order_id": L["order_id"][last][in_db], "line_nbr": L["line_nbr"][last][in_db]})
    oq = pd.Series(L["ordered_qty"][last], index=L["order_id"][last])
    keep = np.ones(len(L["order_id"]), bool)
    keep[last] = False
    tkeep(L, keep)
    op = np.flatnonzero(m)
    o["total_lines"][op] -= 1
    o["total_units"][op] -= oq.reindex(o["order_id"][op]).fillna(0).to_numpy(np.int32)
    o["mod_ts"][op] = o["t_lcancel"][op]
    o["mod_user"][op] = "svc_edi"
    o["t_lcancel"][op] = T_NONE
    mark(o, m)
    _recheck_alloc_status(st, op)


def _release_line_allocs(st: dict[str, Any], fl: Flush, lpos: np.ndarray) -> None:
    """Undo the open allocations of order lines (inventory qty_allocated given back)."""
    L = st["lines"]
    A = st["allocs"]
    if not len(A["akey"]):
        return
    key = L["order_id"][lpos].astype(np.int64) * 1000 + L["line_nbr"][lpos]
    akey = A["order_id"].astype(np.int64) * 1000 + A["line_nbr"]
    am = np.isin(akey, key) & ~A["picked"]
    if not am.any():
        return
    inv = st["inv"]
    ip = pos_of(inv["inventory_id"], A["inv_id"][am])
    np.subtract.at(inv["qty_allocated"], ip, A["qty"][am])
    inv["qty_allocated"][ip] = np.maximum(inv["qty_allocated"][ip], 0)
    mark_pos(inv, ip)
    lk = pd.Series(A["qty"][am], index=akey[am]).groupby(level=0).sum()
    lkey_all = L["order_id"].astype(np.int64) * 1000 + L["line_nbr"]
    hit = np.isin(lkey_all, lk.index.to_numpy())
    L["allocated_qty"][hit] -= lk.reindex(lkey_all[hit]).to_numpy(np.int32)
    L["open_alloc"][hit] -= lk.reindex(lkey_all[hit]).to_numpy(np.int32)
    mark(L, hit)
    # tasks not yet completed for those allocations are cancelled and deleted
    tk = st["tasks"]
    tasks = A["task_id"][am]
    tasks = tasks[tasks > 0]
    if len(tasks):
        tm = np.isin(tk["task_id"], tasks) & ~tk["applied"]
        if tm.any():
            in_db = tm & (tk["_st"] != 1)
            if in_db.any():
                fl.delete("task", {"task_id": tk["task_id"][in_db]})
            tkeep(tk, ~tm)
    tkeep(A, ~am)


def _recheck_alloc_status(st: dict[str, Any], op: np.ndarray) -> None:
    o = st["orders"]
    L = st["lines"]
    for i in op:
        if o["order_status"][i] != O_BACK:
            continue
        lm = L["order_id"] == o["order_id"][i]
        if (L["allocated_qty"][lm] >= L["ordered_qty"][lm]).all():
            o["order_status"][i] = O_ALLOC


def _cancellations(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush, rng: np.random.Generator) -> None:
    o = st["orders"]
    le = W.le[o["wi"]]
    m = (o["t_cancel"] < le) & (o["order_status"] < O_PACK)
    o["t_cancel"][(o["t_cancel"] < le) & ~m] = T_NONE
    # backorders with nothing allocated are cancelled: B2C after a week, B2B three days after the requested ship date
    stale = (o["order_status"] == O_BACK) & (o["bo_since"] < le - 7 * DAY)
    if stale.any():
        L = st["lines"]
        has_alloc = np.isin(o["order_id"], L["order_id"][L["allocated_qty"] > 0])
        b2b = o["order_type"] != "B2C"
        stale &= ~has_alloc & (~b2b | (le > (o["rsd_day"] + 3) * DAY))
    reason_oos = stale & ~m
    m = m | stale
    if not m.any():
        return
    op = np.flatnonzero(m)
    L = st["lines"]
    lp = np.flatnonzero(np.isin(L["order_id"], o["order_id"][op]))
    _release_line_allocs(st, fl, lp)
    _unpick(ctx, st, W, fl, rng, o["order_id"][op])
    in_db = L["_st"][lp] != 1
    if in_db.any():
        fl.delete("order_line", {"order_id": L["order_id"][lp][in_db], "line_nbr": L["line_nbr"][lp][in_db]})
    keep = np.ones(len(L["order_id"]), bool)
    keep[lp] = False
    tkeep(L, keep)
    when = np.where(o["t_cancel"][op] < T_NONE, o["t_cancel"][op], np.maximum(W.ls[o["wi"][op]], o["bo_since"][op] + 7 * DAY))
    o["order_status"][op] = O_CANC
    o["cancel_reason"][op] = np.where(reason_oos[op], "OUT_OF_STOCK", CANCEL_REASONS[rng.integers(0, len(CANCEL_REASONS), len(op))])
    o["mod_ts"][op] = when
    o["mod_user"][op] = "csr_" + np.array(["amartin", "jlee", "rpatel", "dgomez"], dtype=object)[rng.integers(0, 4, len(op))]
    o["t_cancel"][op] = T_NONE
    mark(o, m)
    info = [_owner_by_id(st, int(x))["code"] for x in o["owner_id"][op]]
    st["pending_bus"].append(("orders_cancelled", pd.DataFrame({
        "order_id": o["order_id"][op], "order_nbr": o["order_nbr"][op], "owner_code": info,
        "cancelled_ts": _utc_series(st, o["wi"][op], when), "reason": o["cancel_reason"][op]})))


def _unpick(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush, rng: np.random.Generator, order_ids: np.ndarray) -> None:
    """Picked-but-not-shipped units of cancelled orders go back to their pick location (MOVE, qty 0)."""
    A = st["allocs"]
    am = np.isin(A["order_id"], order_ids) & A["picked"]
    if not am.any():
        return
    inv = st["inv"]
    sp = pos_of(inv["inventory_id"], A["stage_inv"][am])
    wi = A["wi"][am].astype(np.int64)
    t = W.ls[wi] + 60
    stage_loc = inv["location_id"][sp].copy()
    inv["location_id"][sp] = A["loc"][am]
    inv["qty_allocated"][sp] = 0
    inv["mod_ts"][sp] = t
    mark_pos(inv, sp)
    _txn(ctx, st, fl, rng, wi, t, inv["owner_id"][sp], A["mat"][am] + 1, "MOVE", 0, from_loc=stage_loc, to_loc=A["loc"][am],
         lot=A["lot"][am], reason="ORDER_CANCEL", ref_type="ORDER", ref_id=A["order_id"][am])
    tkeep(A, ~am)


def _allocation(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush, rng: np.random.Generator) -> None:
    o = st["orders"]
    le = W.le[o["wi"]]
    due = ((o["order_status"] == O_NEW) | (o["order_status"] == O_BACK)) & (o["t_alloc"] < le) & ~o["is_draft"]
    if not due.any():
        return
    op = np.flatnonzero(due)
    op = op[np.argsort(o["t_alloc"][op], kind="stable")]
    L = st["lines"]
    inv = st["inv"]
    lpos_all = np.flatnonzero(np.isin(L["order_id"], o["order_id"][op]))
    need = (L["ordered_qty"][lpos_all] - L["allocated_qty"][lpos_all]).astype(np.int64)
    lpos = lpos_all[need > 0]
    need = need[need > 0]
    if len(lpos):
        opos = pos_of(o["order_id"], L["order_id"][lpos])
        prio = o["t_alloc"][opos]
        # supply: available stock in storage locations
        lk = _loc_lookup(st)
        avail = (inv["qty_on_hand"] - inv["qty_allocated"]).astype(np.int64)
        ltype = lk["ltype"][inv["location_id"]]
        ok = (inv["inv_status"] == I_AVAIL) & (avail > 0) & ((ltype == LT_PICK) | (ltype == LT_RES) | (ltype == LT_RET))
        sidx = np.flatnonzero(ok)
        skey = inv["warehouse_id"][sidx].astype(np.int64) * 10_000_000 + inv["material_id"][sidx]
        srt = np.lexsort((inv["inventory_id"][sidx], inv["received_date"][sidx], np.where(inv["expiry_date"][sidx] > D_NONE,
                                                                                          inv["expiry_date"][sidx], 10**7), skey))
        sidx, skey = sidx[srt], skey[srt]
        sav = avail[sidx]
        scum = np.cumsum(sav)
        sstart = scum - sav
        dkey = L["wi"][lpos].astype(np.int64) + 1
        dkey = dkey * 10_000_000 + L["material_id"][lpos]
        dsrt = np.lexsort((L["line_nbr"][lpos], L["order_id"][lpos], prio, dkey))
        lpos, need, dkey = lpos[dsrt], need[dsrt], dkey[dsrt]
        ukeys, kfirst = np.unique(skey, return_index=True)
        klast = np.r_[kfirst[1:], len(skey)] - 1
        kstart = sstart[kfirst] if len(kfirst) else np.zeros(0, np.int64)
        kend = scum[klast] if len(klast) else np.zeros(0, np.int64)
        kp = np.searchsorted(ukeys, dkey)
        has = (kp < len(ukeys)) & (ukeys[np.clip(kp, 0, max(len(ukeys) - 1, 0))] == dkey) if len(ukeys) else np.zeros(len(dkey), bool)
        dc = np.cumsum(need)
        gstart = np.r_[0, np.flatnonzero(np.diff(dkey)) + 1]
        glen = np.diff(np.r_[gstart, len(dkey)])
        before = np.repeat(dc[gstart] - need[gstart], glen)
        within = dc - before
        kpc = np.clip(kp, 0, max(len(ukeys) - 1, 0))
        base = np.where(has, kstart[kpc] if len(kstart) else 0, 0)
        cap = np.where(has, kend[kpc] if len(kend) else 0, 0)
        lo = np.minimum(base + within - need, cap)
        hi = np.minimum(base + within, cap)
        got = np.where(has, hi - lo, 0)
        g = np.flatnonzero(got > 0)
        if len(g):
            i0 = np.searchsorted(scum, lo[g], side="right")
            i1 = np.searchsorted(sstart, hi[g], side="left") - 1
            cnt = np.maximum(i1 - i0 + 1, 0)
            rep = np.repeat(np.arange(len(g)), cnt)
            off = np.arange(cnt.sum()) - np.repeat(np.cumsum(cnt) - cnt, cnt)
            si = i0[rep] + off
            q = np.minimum(hi[g][rep], scum[si]) - np.maximum(lo[g][rep], sstart[si])
            okq = q > 0
            rep, si, q = rep[okq], si[okq], q[okq]
            ip = sidx[si]
            np.add.at(inv["qty_allocated"], ip, q)
            mark_pos(inv, ip)
            lsel = lpos[g][rep]
            akeys = ctx.next_ids("wms.alloc", len(q))
            tadd(st["allocs"], len(q), akey=akeys, order_id=L["order_id"][lsel], line_nbr=L["line_nbr"][lsel],
                 inv_id=inv["inventory_id"][ip], qty=q, mat=L["mat"][lsel], wi=L["wi"][lsel], loc=inv["location_id"][ip],
                 lot=inv["lot_nbr"][ip], exp_day=inv["expiry_date"][ip], task_id=0, picked=False, stage_inv=0)
            np.add.at(L["allocated_qty"], lpos[g], got[g].astype(np.int32))
            np.add.at(L["open_alloc"], lpos[g], got[g].astype(np.int32))
        full = L["allocated_qty"][lpos] >= L["ordered_qty"][lpos]
        L["line_status"][lpos] = np.where(full, L_ALLOC, np.where(L["allocated_qty"][lpos] > 0, L_BACK, L_BACK))
        mark_pos(L, lpos)
    # order level
    lo_all = np.flatnonzero(np.isin(L["order_id"], o["order_id"][op]))
    short = pd.Series(L["allocated_qty"][lo_all] < L["ordered_qty"][lo_all], index=L["order_id"][lo_all]).groupby(level=0).any()
    some = pd.Series(L["allocated_qty"][lo_all] > 0, index=L["order_id"][lo_all]).groupby(level=0).any()
    is_short = short.reindex(o["order_id"][op]).fillna(False).to_numpy(bool)
    any_alloc = some.reindex(o["order_id"][op]).fillna(False).to_numpy(bool)
    now = np.maximum(o["t_alloc"][op], W.ls[o["wi"][op]])
    first_bo = is_short & (o["bo_since"][op] >= T_NONE)
    o["bo_since"][op[first_bo]] = now[first_bo]
    b2b = o["order_type"][op] != "B2C"
    age = now - np.where(o["bo_since"][op] < T_NONE, o["bo_since"][op], now)
    ship_partial = is_short & any_alloc & ((b2b & (now >= o["rsd_day"][op] * DAY)) | (~b2b & (age >= 3 * DAY)))
    go = ~is_short | ship_partial
    stay = is_short & ~ship_partial
    o["order_status"][op[stay]] = O_BACK
    nxt = (now[stay] // DAY) * DAY + np.where((now[stay] % DAY) < 6 * 3600, 6 * 3600, np.where((now[stay] % DAY) < 14 * 3600, 14 * 3600, DAY + 6 * 3600))
    o["t_alloc"][op[stay]] = nxt
    gp = op[go]
    o["order_status"][gp] = O_ALLOC
    o["t_alloc"][gp] = T_NONE
    o["mod_ts"][op] = now
    o["mod_user"][op] = "svc_wave"
    mark_pos(o, op)
    if len(gp):
        _plan(ctx, st, W, gp, now[go], rng)


def _slow(ctx: Ctx, st: dict[str, Any], wi: np.ndarray, t: np.ndarray) -> np.ndarray:
    """Cycle-time multiplier: peak load, snow days (weather_delays), LAX1 ramp, storms, surges."""
    day = t // DAY
    f = np.ones(len(t))
    for d in np.unique(day):
        pk = _peak_shape(geo.day_date(int(d)))
        if pk:
            f[day == d] *= 1 + 0.9 * pk
    for w in np.unique(wi):
        m = wi == w
        cal = _cal(ctx, st, int(w), int(day[m].max()))
        di = np.clip(day[m] - cal.d0, 0, len(cal.snow) - 1)
        snow = cal.snow[di]
        f[m] *= np.where(snow >= 3, 1 + np.minimum(snow, 30) / 25, 1.0)
    lax = _wh_index(ctx).get("LAX1")
    if lax is not None:
        age = day - st["wh_open_day"][lax]
        f = np.where((wi == lax) & (age < 56), f * (1 + 1.5 * np.clip(1 - age / 56, 0, 1)), f)
    for (w, d0, d1, mult) in st["slowdowns"]:
        f = np.where((wi == w) & (day >= d0) & (day < d1), f * mult, f)
    for (w, d0, d1, mult) in st["surges"]:
        f = np.where(((w is None) | (wi == (w if w is not None else -1))) & (day >= d0) & (day < d1), f * (1 + 0.35 * (mult - 1)), f)
    return f


def _plan(ctx: Ctx, st: dict[str, Any], W: _WinCtx, op: np.ndarray, now: np.ndarray, rng: np.random.Generator) -> None:
    """Timeline after allocation: release -> pick -> pack -> carrier pickup (respecting shifts, cutoffs, business days)."""
    o = st["orders"]
    L = st["lines"]
    n = len(op)
    wi = o["wi"][op].astype(np.int64)
    b2b = o["order_type"][op] != "B2C"
    slow = _slow(ctx, st, wi, now)
    rel0 = np.where(b2b, np.maximum(now, (o["rsd_day"][op] - 1) * DAY + 6 * 3600), now)
    rel = _cal_add(ctx, st, wi, rel0, (rng.uniform(600, 2700, n) * slow).astype(np.int64))
    ps = _cal_add(ctx, st, wi, rel, (rng.uniform(300, 2400, n) * slow).astype(np.int64))
    nlines = pd.Series(1, index=L["order_id"][np.isin(L["order_id"], o["order_id"][op])]).groupby(level=0).sum()
    nl = nlines.reindex(o["order_id"][op]).fillna(1).to_numpy()
    pph = np.where(b2b, 140.0, 95.0)
    units = o["total_units"][op].astype(float)
    pick_s = nl * 3600 / pph * rng.uniform(0.8, 1.25, n) * slow + np.where(b2b, np.minimum(units, 4000) * 2.0, 0)
    pd_ = _cal_add(ctx, st, wi, ps, pick_s.astype(np.int64))
    pack = _cal_add(ctx, st, wi, pd_, (rng.uniform(240, 1500, n) * slow + np.where(b2b, np.minimum(units, 2000) * 1.5, 0)).astype(np.int64))
    scac = st["svc"].set_index("service_id").loc[o["carrier_service_id"][op], "scac"].to_numpy(object)
    ship = _next_pickup(ctx, st, wi, pack, o["is_ltl"][op], scac)
    floor = np.where(b2b, o["rsd_day"][op] * DAY, 0)
    late_b2b = b2b & (ship < floor)
    if late_b2b.any():
        ship[late_b2b] = _next_pickup(ctx, st, wi[late_b2b], floor[late_b2b], o["is_ltl"][op][late_b2b], scac[late_b2b])
    o["t_release"][op] = rel
    o["t_pick_start"][op] = ps
    o["t_pick_done"][op] = pd_
    o["t_pack"][op] = pack
    o["t_ship"][op] = ship


def _release(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush, rng: np.random.Generator) -> None:
    o = st["orders"]
    le = W.le[o["wi"]]
    m = (o["order_status"] == O_ALLOC) & (o["t_release"] < le)
    if not m.any():
        return
    op = np.flatnonzero(m)
    o["order_status"][op] = O_REL
    o["mod_ts"][op] = o["t_release"][op]
    o["mod_user"][op] = "svc_wave"
    mark(o, m)
    A = st["allocs"]
    am = np.flatnonzero(np.isin(A["order_id"], o["order_id"][op]) & (A["task_id"] == 0))
    if not len(am):
        return
    am = am[np.lexsort((A["line_nbr"][am], A["order_id"][am]))]
    oid = A["order_id"][am]
    opos = pos_of(o["order_id"], oid)
    starts = np.r_[0, np.flatnonzero(np.diff(oid)) + 1]
    cnt = np.diff(np.r_[starts, len(oid)])
    k = np.arange(len(oid)) - np.repeat(starts, cnt)
    per = (o["t_pick_done"][opos] - o["t_pick_start"][opos]) / np.repeat(cnt, cnt)
    t_start = o["t_pick_start"][opos] + (k * per).astype(np.int64)
    t_done = t_start + (per * rng.uniform(0.7, 1.0, len(oid))).astype(np.int64)
    wi = A["wi"][am].astype(np.int64)
    # one picker per order (a tote)
    first = np.zeros(len(oid), bool)
    first[starts] = True
    pick_user = _pick_users(st, rng, wi[first], t_start[first], "PICKER")
    u = st["users"]
    outl = u["user_id"][u["outlier"] & u["is_active"]]
    if len(outl):
        pick_user = _outlier_assign(st, rng, wi[first], t_start[first], pick_user, int(outl[0]))
    picker = np.repeat(pick_user, cnt)
    if len(outl):
        # planted picker_outlier: the whole tote is confirmed at once. started_ts stays realistic; completed_ts of every
        # task in the tote is bunched within a few seconds of the tote's last pick (the PICK ledger rows share them).
        om = picker == outl[0]
        if om.any():
            last = np.r_[starts[1:], len(oid)] - 1
            tote_end = np.repeat(t_done[last], cnt)
            bunched = tote_end + k * rng.integers(1, 3, len(oid))
            limit = o["t_pack"][opos] - 60
            t_done = np.where(om, np.minimum(bunched, np.maximum(limit, tote_end)), t_done)
    tids = ctx.next_ids("wms.task", len(am))
    A["task_id"][am] = tids
    stage = np.zeros(len(am), dtype=np.int32)
    for w in np.unique(wi):
        pool = _special_locs(st, int(w), LT_STAGE)
        mm = wi == w
        stage[mm] = pool[rng.integers(0, len(pool), int(mm.sum()))]
    created = o["t_release"][opos]
    tadd(st["tasks"], len(am), task_id=tids, warehouse_id=wi + 1, task_type=K_PICK, task_status=0,
         priority=o["priority"][opos], order_id=oid, order_line_nbr=A["line_nbr"][am], material_id=A["mat"][am] + 1,
         qty=A["qty"][am], from_location_id=A["loc"][am], to_location_id=stage, assigned_user_id=picker, created_ts=created,
         assigned_ts=np.minimum(t_start - rng.integers(5, 240, len(am)), np.maximum(created, t_start - 1)),
         started_ts=t_start, completed_ts=t_done, mod_ts=created, _st=1, applied=False, ref=A["akey"][am], wi=wi)
    o["picker"][op] = pd.Series(picker, index=oid).groupby(level=0).first().reindex(o["order_id"][op]).fillna(0).to_numpy(np.int32)


def _outlier_assign(st: dict[str, Any], rng: np.random.Generator, wi: np.ndarray, t: np.ndarray, picker: np.ndarray,
                    outlier: int) -> np.ndarray:
    """The outlier's RF gun pulls the next tote as soon as one is confirmed: ~2.3x the median share of shift-B totes."""
    u = st["users"]
    p = np.flatnonzero(u["user_id"] == outlier)
    if not len(p):
        return picker
    ow = int(u["warehouse_id"][p[0]]) - 1
    shift = u["shift"][p[0]]
    span = st["wh_rules"][ow]["shifts"].get(shift)
    if not span:
        return picker
    a0, a1 = (_hhmm(x) for x in span.split("-"))
    tod = t % DAY
    on = (wi == ow) & ((tod >= a0) & (tod < a1) if a1 > a0 else (tod >= a0) | (tod < a1))
    if not on.any():
        return picker
    k = int((u["is_active"] & (u["warehouse_id"] == ow + 1) & (u["user_role"] == "PICKER") & (u["shift"] == shift)).sum())
    k = max(k, 2)
    target = 2.3 / (k - 1 + 2.3)
    r = max(0.0, (target - 1 / k) / (1 - 1 / k))
    out = picker.copy()
    out[on & (rng.random(len(t)) < r)] = outlier
    return out


def _tasks_progress(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush, rng: np.random.Generator) -> None:
    tk = st["tasks"]
    if not len(tk["task_id"]):
        return
    le = W.le[tk["wi"]]
    status = np.where(tk["completed_ts"] < le, 3, np.where(tk["started_ts"] < le, 2, np.where(tk["assigned_ts"] < le, 1, 0))).astype(np.int8)
    changed = status != tk["task_status"]
    if changed.any():
        tk["task_status"] = status
        tk["mod_ts"][changed] = np.select([status[changed] == 3, status[changed] == 2, status[changed] == 1],
                                          [tk["completed_ts"][changed], tk["started_ts"][changed], tk["assigned_ts"][changed]],
                                          tk["created_ts"][changed])
        mark(tk, changed)
    do = (status == 3) & ~tk["applied"]
    if not do.any():
        return
    for kind, fn in ((K_PICK, _apply_picks), (K_PUT, _apply_putaways), (K_REPLEN, _apply_replens), (K_COUNT, _apply_counts)):
        m = do & (tk["task_type"] == kind)
        if m.any():
            fn(ctx, st, W, fl, rng, np.flatnonzero(m))
    tk["applied"][do] = True
    # order status PICKING once any pick has started
    o = st["orders"]
    pk = (tk["task_type"] == K_PICK) & (status >= 2)
    if pk.any():
        om = np.isin(o["order_id"], tk["order_id"][pk]) & (o["order_status"] == O_REL)
        if om.any():
            first_start = pd.Series(tk["started_ts"][pk], index=tk["order_id"][pk]).groupby(level=0).min()
            o["order_status"][om] = O_PICK
            o["mod_ts"][om] = first_start.reindex(o["order_id"][om]).to_numpy(np.int64)
            o["mod_user"][om] = "rf_pick"
            mark(o, om)


def _apply_picks(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush, rng: np.random.Generator, tp: np.ndarray) -> None:
    """Units move from the storage row to a staging row (PICK ledger row, qty 0: a move)."""
    tk = st["tasks"]
    A = st["allocs"]
    inv = st["inv"]
    ap = pos_of(A["akey"], tk["ref"][tp])
    valid = A["akey"][ap] == tk["ref"][tp]
    tp, ap = tp[valid], ap[valid]
    if not len(tp):
        return
    q = A["qty"][ap].astype(np.int64)
    ip = pos_of(inv["inventory_id"], A["inv_id"][ap])
    np.subtract.at(inv["qty_on_hand"], ip, q)
    np.subtract.at(inv["qty_allocated"], ip, q)
    t = tk["completed_ts"][tp]
    inv["mod_ts"][ip] = t
    mark_pos(inv, ip)
    n = len(tp)
    sids = ctx.next_ids("wms.inventory", n)
    tadd(inv, n, inventory_id=sids, warehouse_id=tk["warehouse_id"][tp], location_id=tk["to_location_id"][tp], lpn_id=0,
         owner_id=inv["owner_id"][ip], material_id=inv["material_id"][ip], lot_nbr=inv["lot_nbr"][ip],
         expiry_date=inv["expiry_date"][ip], qty_on_hand=q, qty_allocated=q, inv_status=I_AVAIL, hold_reason=None,
         received_date=inv["received_date"][ip], create_ts=t, mod_ts=t, _st=1)
    A["picked"][ap] = True
    A["stage_inv"][ap] = sids
    L = st["lines"]
    lkey = L["order_id"].astype(np.int64) * 1000 + L["line_nbr"]
    akey = A["order_id"][ap].astype(np.int64) * 1000 + A["line_nbr"][ap]
    srt = np.argsort(lkey)
    lp = srt[np.searchsorted(lkey[srt], akey)]
    np.add.at(L["picked_qty"], lp, q.astype(np.int32))
    np.subtract.at(L["open_alloc"], lp, q.astype(np.int32))
    L["line_status"][lp] = np.where(L["picked_qty"][lp] >= L["ordered_qty"][lp], L_PICKED, L["line_status"][lp])
    mark_pos(L, lp)
    wi = tk["wi"][tp].astype(np.int64)
    _txn(ctx, st, fl, rng, wi, t, inv["owner_id"][ip], inv["material_id"][ip], "PICK", 0, from_loc=tk["from_location_id"][tp],
         to_loc=tk["to_location_id"][tp], lpn=inv["lpn_id"][ip], lot=inv["lot_nbr"][ip], ref_type="ORDER",
         ref_id=tk["order_id"][tp], user=tk["assigned_user_id"][tp])


def _apply_putaways(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush, rng: np.random.Generator, tp: np.ndarray) -> None:
    tk = st["tasks"]
    inv = st["inv"]
    ip = pos_of(inv["inventory_id"], tk["ref"][tp])
    ok = inv["inventory_id"][ip] == tk["ref"][tp]
    tp, ip = tp[ok], ip[ok]
    if not len(tp):
        return
    t = tk["completed_ts"][tp]
    inv["location_id"][ip] = tk["to_location_id"][tp]
    inv["inv_status"][ip] = np.where(inv["inv_status"][ip] == I_QC, I_AVAIL, inv["inv_status"][ip])
    inv["mod_ts"][ip] = t
    mark_pos(inv, ip)
    st["mat"]["sellable"][inv["material_id"][ip] - 1] = True
    lp = st["lpns"]
    lpos = np.flatnonzero(np.isin(lp["lpn_id"], inv["lpn_id"][ip]))
    if len(lpos):
        mp = pd.Series(np.arange(len(ip)), index=inv["lpn_id"][ip]).reindex(lp["lpn_id"][lpos]).to_numpy(np.int64)
        lp["lpn_status"][lpos] = "STORED"
        lp["location_id"][lpos] = tk["to_location_id"][tp][mp]
        lp["mod_ts"][lpos] = t[mp]
        mark_pos(lp, lpos)
    wi = tk["wi"][tp].astype(np.int64)
    _txn(ctx, st, fl, rng, wi, t, inv["owner_id"][ip], inv["material_id"][ip], "PUTAWAY", 0, from_loc=tk["from_location_id"][tp],
         to_loc=tk["to_location_id"][tp], lpn=inv["lpn_id"][ip], lot=inv["lot_nbr"][ip], ref_type="RECEIPT",
         ref_id=tk["receipt_id"][tp], user=tk["assigned_user_id"][tp])


def _apply_replens(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush, rng: np.random.Generator, tp: np.ndarray) -> None:
    tk = st["tasks"]
    inv = st["inv"]
    ip = pos_of(inv["inventory_id"], tk["ref"][tp])
    ok = (inv["inventory_id"][ip] == tk["ref"][tp]) & (inv["qty_on_hand"][ip] > 0)
    tp, ip = tp[ok], ip[ok]
    if not len(tp):
        return
    t = tk["completed_ts"][tp]
    inv["location_id"][ip] = tk["to_location_id"][tp]
    inv["mod_ts"][ip] = t
    mark_pos(inv, ip)
    A = st["allocs"]
    am = np.isin(A["inv_id"], inv["inventory_id"][ip]) & ~A["picked"]
    if am.any():
        A["loc"][am] = pd.Series(inv["location_id"][ip], index=inv["inventory_id"][ip]).reindex(A["inv_id"][am]).to_numpy(np.int32)
    wi = tk["wi"][tp].astype(np.int64)
    _txn(ctx, st, fl, rng, wi, t, inv["owner_id"][ip], inv["material_id"][ip], "REPLEN", 0, from_loc=tk["from_location_id"][tp],
         to_loc=tk["to_location_id"][tp], lpn=inv["lpn_id"][ip], lot=inv["lot_nbr"][ip], ref_type="TASK",
         ref_id=tk["task_id"][tp], user=tk["assigned_user_id"][tp])


def _pack(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush, rng: np.random.Generator) -> None:
    o = st["orders"]
    le = W.le[o["wi"]]
    m = ((o["order_status"] == O_PICK) | (o["order_status"] == O_REL)) & (o["t_pack"] < le)
    if not m.any():
        return
    tk = st["tasks"]
    open_pick = (tk["task_type"] == K_PICK) & ~tk["applied"]
    pending = np.isin(o["order_id"], tk["order_id"][open_pick])
    m &= ~pending
    if not m.any():
        return
    op = np.flatnonzero(m)
    A = st["allocs"]
    am = np.isin(A["order_id"], o["order_id"][op]) & A["picked"]
    mat = st["mat"]
    mi = A["mat"][am]
    q = A["qty"][am].astype(np.int64)
    oid = A["order_id"][am]
    lb = pd.Series(q * mat["lb"][mi], index=oid).groupby(level=0).sum().reindex(o["order_id"][op]).fillna(0).to_numpy()
    vol = pd.Series(q * mat["l_in"][mi] * mat["w_in"][mi] * mat["h_in"][mi], index=oid).groupby(level=0).sum().reindex(o["order_id"][op]).fillna(0).to_numpy()
    ctn_b2b = pd.Series(np.ceil(q / np.maximum(mat["case_qty"][mi], 1)), index=oid).groupby(level=0).sum().reindex(o["order_id"][op]).fillna(1).to_numpy()
    units = pd.Series(q, index=oid).groupby(level=0).sum().reindex(o["order_id"][op]).fillna(0).to_numpy()
    has_units = units > 0
    if not has_units.all():
        # nothing was picked (all lines short): such orders are cancelled out of stock later
        op, lb, vol, ctn_b2b, units = op[has_units], lb[has_units], vol[has_units], ctn_b2b[has_units], units[has_units]
        if not len(op):
            return
    n = len(op)
    b2b = o["order_type"][op] != "B2C"
    ltl = o["is_ltl"][op]
    box_vol = 1.6 * 1728
    cartons = np.where(b2b, np.clip(ctn_b2b, 1, 400), np.clip(np.ceil(vol * 1.25 / box_vol), 1, 6)).astype(np.int64)
    pallets = np.where(ltl, np.clip(np.ceil(cartons / 30), 1, 26), 0).astype(np.int64)
    n_rows = np.where(ltl, pallets, np.where(b2b, np.minimum(cartons, 40), cartons))
    wi = o["wi"][op].astype(np.int64)
    kg = st["wh_kg"][wi]
    pack_lb = lb + np.where(ltl, pallets * 45.0, cartons * 0.6)
    t = o["t_pack"][op]
    sids = ctx.next_ids("wms.shipment", n)
    svc = st["svc"].set_index("service_id").loc[o["carrier_service_id"][op]]
    scac = svc["scac"].to_numpy(object)
    fmt = svc["fmt"].to_numpy(object)
    code = svc["service_code"].to_numpy(object)
    trk = carr.tracking_numbers(fmt, code, st["wh_code"][wi], ctx.next_ids("wms.tracking", n, start=50_000))
    # cartons
    rep = np.repeat(np.arange(n), n_rows)
    k = np.concatenate([np.arange(c) for c in n_rows])
    cids = ctx.next_ids("wms.carton", len(rep))
    side = np.cbrt(np.maximum(vol[rep] * 1.25 / np.maximum(cartons[rep], 1), 200))
    L_ = np.where(ltl[rep], 48.0, np.round(side * rng.uniform(1.1, 1.4, len(rep)), 0))
    W_ = np.where(ltl[rep], 40.0, np.round(side * rng.uniform(0.8, 1.0, len(rep)), 0))
    H_ = np.where(ltl[rep], np.round(rng.uniform(30, 66, len(rep)), 0), np.round(side * rng.uniform(0.6, 0.9, len(rep)), 0))
    c_lb = pack_lb[rep] / n_rows[rep]
    child = np.where(ltl[rep] | (k == 0), trk[rep], None)
    multi = (~ltl[rep]) & (k > 0)
    if multi.any():
        child[multi] = carr.tracking_numbers(fmt[rep][multi], code[rep][multi], st["wh_code"][wi[rep][multi]],
                                             ctx.next_ids("wms.tracking", int(multi.sum()), start=50_000))
    shipment_nbr = np.array([f"SH{400_000_000 + int(x):09d}" for x in sids], dtype=object)
    tadd(st["cartons"], len(rep), carton_id=cids, shipment_id=sids[rep],
         carton_nbr=np.array([f"{s}-{int(j) + 1:03d}" for s, j in zip(shipment_nbr[rep], k)], dtype=object), tracking_nbr=child,
         weight=np.round(np.where(kg[rep], c_lb / LB_PER_KG, c_lb), 2), length=np.where(kg[rep], np.round(L_ * 2.54, 1), L_),
         width=np.where(kg[rep], np.round(W_ * 2.54, 1), W_), height=np.where(kg[rep], np.round(H_ * 2.54, 1), H_),
         dim_uom=np.where(kg[rep], "CM", "IN").astype(object), create_ts=t[rep], mod_ts=t[rep], _st=1)
    dims = np.stack([L_, W_, H_], axis=1)
    billed_c = carr.billed_weight_lb(c_lb, dims, ltl[rep])
    billed = pd.Series(billed_c, index=rep).groupby(level=0).sum().reindex(np.arange(n)).to_numpy()
    diesel = float(ctx.signals.diesel(geo.day_date(int(t[0] // DAY))))
    usd = carr.freight_estimate_usd(code, ltl, billed, o["zone"][op], o["is_residential"][op], diesel)
    ccy = st["wh_currency"][wi]
    fx = np.array([ctx.signals.fx(geo.day_date(int(tt // DAY)), c) if c != "USD" else 1.0 for tt, c in zip(t, ccy)])
    bol = np.where(ltl, np.array([f"BOL{7_000_000 + int(x):08d}" for x in sids], dtype=object), None)
    tot_w = np.round(np.where(kg, pack_lb / LB_PER_KG, pack_lb), 2)
    window = np.maximum(o["t_ship"][op] - t, 600)
    void = np.where(rng.random(n) < 0.008, t + (window * rng.uniform(0.1, 0.8, n)).astype(np.int64), T_NONE)
    reprint = np.where((~ltl) & (rng.random(n) < 0.015) & (void == T_NONE), t + (window * rng.uniform(0.1, 0.8, n)).astype(np.int64), T_NONE)
    tadd(st["ships"], n, shipment_id=sids, shipment_nbr=shipment_nbr, order_id=o["order_id"][op], warehouse_id=wi + 1,
         carrier_service_id=o["carrier_service_id"][op], scac=scac, tracking_nbr=trk, bol_nbr=bol, ship_ts=T_NONE,
         shipment_status=1, total_weight=tot_w, weight_uom=np.where(kg, "KG", "LB").astype(object), carton_count=cartons,
         freight_charge_est=np.round(usd * fx, 2), currency=ccy, create_ts=t, mod_ts=t, _st=1, billed_lb=billed, label_ts=t,
         t_void=void, t_reprint=reprint)
    o["shipment_id"][op] = sids
    o["order_status"][op] = O_PACK
    o["mod_ts"][op] = t
    o["mod_user"][op] = "pack_station"
    mark_pos(o, op)
    # LTL loads use a dock door
    for i in np.flatnonzero(ltl):
        st["door_log"].append((int(wi[i]), int(rng.integers(1, st["wh_rules"][int(wi[i])]["dock_doors"] + 1)),
                               f"{scac[i]} {int(rng.integers(10000, 99999))}", int(o["t_ship"][op][i] - 5400), int(o["t_ship"][op][i] - 600)))


def _label_events(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush, rng: np.random.Generator) -> None:
    """Label reprints (carton deleted, new tracking number) and whole-shipment voids before carrier pickup."""
    s = st["ships"]
    if not len(s["shipment_id"]):
        return
    le = W.le[s["warehouse_id"].astype(np.int64) - 1]
    voided = []
    m = (s["t_reprint"] < le) & (s["shipment_status"] == 1)
    if m.any():
        sp = np.flatnonzero(m)
        c = st["cartons"]
        cp = np.flatnonzero(np.isin(c["shipment_id"], s["shipment_id"][sp]))
        cp = cp[pd.Series(cp, index=c["shipment_id"][cp]).groupby(level=0).cumcount().to_numpy() == 0]
        old = c["tracking_nbr"][cp].copy()
        svc = st["svc"].set_index("service_id").loc[s["carrier_service_id"][pos_of(s["shipment_id"], c["shipment_id"][cp])]]
        sp_c = pos_of(s["shipment_id"], c["shipment_id"][cp])
        new_trk = carr.tracking_numbers(svc["fmt"].to_numpy(object), svc["service_code"].to_numpy(object),
                                        st["wh_code"][s["warehouse_id"][sp_c].astype(np.int64) - 1],
                                        ctx.next_ids("wms.tracking", len(cp), start=50_000))
        in_db = c["_st"][cp] != 1
        if in_db.any():
            fl.delete("shipment_carton", {"carton_id": c["carton_id"][cp][in_db]})
        new_ids = ctx.next_ids("wms.carton", len(cp))
        t = s["t_reprint"][sp_c]
        add = ttake(c, cp)
        add.update(carton_id=new_ids, tracking_nbr=new_trk, create_ts=t, mod_ts=t, _st=np.ones(len(cp), np.int8))
        keep = np.ones(len(c["carton_id"]), bool)
        keep[cp] = False
        tkeep(c, keep)
        tadd(c, len(cp), **add)
        s["tracking_nbr"][sp_c] = new_trk
        s["mod_ts"][sp_c] = t
        s["label_ts"][sp_c] = t
        mark_pos(s, sp_c)
        s["t_reprint"][sp] = T_NONE
        voided.append(pd.DataFrame({"tracking_nbr": old, "scac": s["scac"][sp_c],
                                    "origin_city_idx": st["wh_city"][s["warehouse_id"][sp_c].astype(np.int64) - 1],
                                    "label_utc": geo.local_to_utc_many(st["wh_tz"][s["warehouse_id"][sp_c].astype(np.int64) - 1], t - 600)}))
    m = (s["t_void"] < le) & (s["shipment_status"] == 1)
    if m.any():
        sp = np.flatnonzero(m)
        c = st["cartons"]
        cm = np.isin(c["shipment_id"], s["shipment_id"][sp])
        in_db_c = cm & (c["_st"] != 1)
        if in_db_c.any():
            fl.delete("shipment_carton", {"carton_id": c["carton_id"][in_db_c]})
        in_db_s = m & (s["_st"] != 1)
        if in_db_s.any():
            fl.delete("shipment", {"shipment_id": s["shipment_id"][in_db_s]})
        wi = s["warehouse_id"][sp].astype(np.int64) - 1
        voided.append(pd.DataFrame({"tracking_nbr": s["tracking_nbr"][sp], "scac": s["scac"][sp], "origin_city_idx": st["wh_city"][wi],
                                    "label_utc": geo.local_to_utc_many(st["wh_tz"][wi], s["label_ts"][sp])}))
        # re-label with a new shipment (e.g. service upgrade after a missed cutoff)
        o = st["orders"]
        op = pos_of(o["order_id"], s["order_id"][sp])
        tkeep(c, ~cm)
        old = ttake(s, sp)
        tkeep(s, ~m)
        s_t = old["t_void"] + rng.integers(120, 900, len(sp))
        o["t_pack"][op] = s_t
        o["t_ship"][op] = _next_pickup(ctx, st, o["wi"][op].astype(np.int64), s_t, o["is_ltl"][op], old["scac"])
        o["order_status"][op] = O_PICK
        o["mod_ts"][op] = s_t
        mark_pos(o, op)
    if voided:
        ctx.bus.publish("wms_voided_labels", pd.concat(voided, ignore_index=True))


def _ship(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush, rng: np.random.Generator) -> None:
    o = st["orders"]
    le = W.le[o["wi"]]
    m = (o["order_status"] == O_PACK) & (o["t_ship"] < le)
    if not m.any():
        return
    op = np.flatnonzero(m)
    s = st["ships"]
    spos = pos_of(s["shipment_id"], o["shipment_id"][op])
    valid = s["shipment_id"][spos] == o["shipment_id"][op]
    op, spos = op[valid], spos[valid]
    t = o["t_ship"][op]
    n = len(op)
    wi = o["wi"][op].astype(np.int64)
    A = st["allocs"]
    am = np.flatnonzero(np.isin(A["order_id"], o["order_id"][op]) & A["picked"])
    inv = st["inv"]
    sp = pos_of(inv["inventory_id"], A["stage_inv"][am])
    q = A["qty"][am].astype(np.int64)
    np.subtract.at(inv["qty_on_hand"], sp, q)
    np.subtract.at(inv["qty_allocated"], sp, q)
    inv["qty_allocated"][sp] = np.maximum(inv["qty_allocated"][sp], 0)
    t_a = pd.Series(t, index=o["order_id"][op]).reindex(A["order_id"][am]).to_numpy(np.int64)
    inv["mod_ts"][sp] = t_a
    mark_pos(inv, sp)
    _txn(ctx, st, fl, rng, A["wi"][am].astype(np.int64), t_a, inv["owner_id"][sp], A["mat"][am] + 1, "SHIP", -q,
         from_loc=inv["location_id"][sp], lot=A["lot"][am], ref_type="ORDER", ref_id=A["order_id"][am], user=0)
    # lines
    L = st["lines"]
    lsel = np.flatnonzero(np.isin(L["order_id"], o["order_id"][op]))
    L["shipped_qty"][lsel] = L["picked_qty"][lsel]
    L["line_status"][lsel] = np.where(L["shipped_qty"][lsel] > 0, L_SHIP, L_BACK)
    over = ctx.dq.mask("shipped_gt_ordered", len(lsel), rng) & (L["shipped_qty"][lsel] > 0)
    for i in lsel[over]:
        before = int(L["shipped_qty"][i])
        L["shipped_qty"][i] = L["ordered_qty"][i] + int(rng.integers(1, 4))
        ctx.dq.log("shipped_gt_ordered", "wms", "order_line", f"{L['order_id'][i]}-{L['line_nbr'][i]}", "shipped_qty", before,
                   int(L["shipped_qty"][i]), "RF over-scan at ship confirm; ledger shipped the picked qty")
    mark_pos(L, lsel)
    o["order_status"][op] = O_SHIP
    o["mod_ts"][op] = t
    o["mod_user"][op] = "svc_ship"
    mark_pos(o, op)
    s["shipment_status"][spos] = 2
    s["ship_ts"][spos] = t
    s["mod_ts"][spos] = t
    # re-weigh at the dock: some carton weights are corrected in place
    c = st["cartons"]
    cm = np.flatnonzero(np.isin(c["shipment_id"], s["shipment_id"][spos]) & (rng.random(len(c["carton_id"])) < 0.02))
    if len(cm):
        c["weight"][cm] = np.round(c["weight"][cm] * rng.uniform(0.9, 1.15, len(cm)), 2)
        c["mod_ts"][cm] = pd.Series(t, index=s["shipment_id"][spos]).reindex(c["shipment_id"][cm]).to_numpy(np.int64)
        mark_pos(c, cm)
    mix = ctx.dq.mask("unit_mixup", n, rng) & (s["weight_uom"][spos] == "LB")
    for i in spos[mix]:
        before = float(s["total_weight"][i])
        s["total_weight"][i] = round(before / LB_PER_KG, 2)
        ctx.dq.log("unit_mixup", "wms", "shipment", str(s["shipment_id"][i]), "total_weight", before, s["total_weight"][i],
                   "scale reported KG; weight_uom left as LB")
    mark_pos(s, spos)
    # LOAD tasks for LTL trailers
    ltl = o["is_ltl"][op]
    if ltl.any():
        lt = np.flatnonzero(ltl)
        tids = ctx.next_ids("wms.task", len(lt))
        fork = _pick_users(st, rng, wi[lt], t[lt] - 3600, "FORKLIFT")
        stage = np.array([_special_locs(st, int(w), LT_STAGE)[0] for w in wi[lt]])
        doors = np.array([_special_locs(st, int(w), LT_DOCK)[int(rng.integers(0, 10))] for w in wi[lt]])
        tadd(st["tasks"], len(lt), task_id=tids, warehouse_id=wi[lt] + 1, task_type=K_LOAD, task_status=3, priority=4,
             order_id=o["order_id"][op][lt], material_id=0, qty=s["carton_count"][spos][lt], from_location_id=stage,
             to_location_id=doors, assigned_user_id=fork, created_ts=o["t_pack"][op][lt], assigned_ts=t[lt] - 3900,
             started_ts=t[lt] - 3600, completed_ts=t[lt] - 900, mod_ts=t[lt] - 900, _st=1, applied=True, wi=wi[lt])
    _publish_shipments(ctx, st, op, spos, am, t)
    _accrue_orders(ctx, st, fl, rng, op, spos)
    _schedule_returns(ctx, st, W, rng, op, spos, am)
    # close the allocations of shipped orders
    keep = np.ones(len(A["akey"]), bool)
    keep[am] = False
    tkeep(A, keep)


def _publish_shipments(ctx: Ctx, st: dict[str, Any], op: np.ndarray, spos: np.ndarray, am: np.ndarray, t: np.ndarray) -> None:
    o = st["orders"]
    s = st["ships"]
    wi = o["wi"][op].astype(np.int64)
    svc = st["svc"].set_index("service_id").loc[s["carrier_service_id"][spos]]
    utc = geo.local_to_utc_many(st["wh_tz"][wi], t)
    label_utc = geo.local_to_utc_many(st["wh_tz"][wi], s["label_ts"][spos])
    owner_code = np.array([_owner_by_id(st, int(x))["code"] for x in o["owner_id"][op]], dtype=object)
    city = o["city"][op]
    ltl = o["is_ltl"][op]
    tz = np.array([st["wh_tz"][w] for w in wi], dtype=object)
    df = pd.DataFrame({
        "shipment_id": s["shipment_id"][spos], "shipment_nbr": s["shipment_nbr"][spos], "order_id": o["order_id"][op],
        "order_nbr": o["order_nbr"][op], "client_order_ref": o["client_order_ref"][op], "owner_code": owner_code,
        "warehouse_code": st["wh_code"][wi], "channel": o["channel"][op], "scac": s["scac"][spos],
        "service_code": svc["service_code"].to_numpy(object), "mode": svc["mode"].to_numpy(object),
        "tracking_nbr": s["tracking_nbr"][spos], "bol_nbr": s["bol_nbr"][spos], "ship_ts": pd.to_datetime(utc, unit="s", utc=True),
        "ship_ts_local": ts_col(t), "total_weight": s["total_weight"][spos], "weight_uom": s["weight_uom"][spos],
        "carton_count": s["carton_count"][spos], "billed_weight_lb": s["billed_lb"][spos], "zone": o["zone"][op],
        "is_residential": o["is_residential"][op], "dest_city": o["ship_to_city"][op], "dest_state": o["ship_to_state"][op],
        "dest_postal": o["ship_to_postal"][op], "dest_country": o["ship_to_country"][op],
        "freight_charge_est": s["freight_charge_est"][spos], "currency": s["currency"][spos]})
    st["pending_bus"].append(("shipments_shipped", df))
    rng = np.random.default_rng(int(t[0]) % 2**31)
    transit = carr.transit_plan(rng, svc["tmin"].to_numpy(), svc["tmax"].to_numpy(), o["zone"][op])
    ctx.bus.publish("wms_ship_ctx", pd.DataFrame({
        "shipment_id": s["shipment_id"][spos], "tracking_nbr": s["tracking_nbr"][spos], "scac": s["scac"][spos],
        "service_code": svc["service_code"].to_numpy(object), "is_ltl": ltl, "origin_city_idx": st["wh_city"][wi],
        "dest_city_idx": city, "ship_utc": utc, "label_utc": label_utc, "transit_days": transit, "zone": o["zone"][op],
        "warehouse_code": st["wh_code"][wi], "order_nbr": o["order_nbr"][op], "owner_code": owner_code, "wh_tz": tz}))
    A = st["allocs"]
    mat = st["mat"]
    L = st["lines"]
    lkey = L["order_id"].astype(np.int64) * 1000 + L["line_nbr"]
    oq = pd.Series(L["ordered_qty"], index=lkey)
    oq = oq[~oq.index.duplicated()]
    sl = pd.DataFrame({"order_id": A["order_id"][am], "line_nbr": A["line_nbr"][am], "lot_nbr": A["lot"][am],
                       "shipped_qty": A["qty"][am], "mat": A["mat"][am]})
    sl = sl.groupby(["order_id", "line_nbr", "mat", sl["lot_nbr"].fillna("")], as_index=False, sort=False)["shipped_qty"].sum()
    sl["lot_nbr"] = sl["lot_nbr"].replace("", None)
    sid = pd.Series(s["shipment_id"][spos], index=o["order_id"][op])
    st["pending_bus"].append(("shipment_lines", pd.DataFrame({
        "shipment_id": sid.reindex(sl["order_id"]).to_numpy(np.int64), "order_id": sl["order_id"], "line_nbr": sl["line_nbr"],
        "sku": mat["sku"][sl["mat"].to_numpy()], "upc": mat["upc"][sl["mat"].to_numpy()], "shipped_qty": sl["shipped_qty"],
        "ordered_qty": oq.reindex(sl["order_id"].to_numpy(np.int64) * 1000 + sl["line_nbr"].to_numpy()).to_numpy(np.int64),
        "lot_nbr": sl["lot_nbr"]})))


# =============================================================================================== returns
def _schedule_returns(ctx: Ctx, st: dict[str, Any], W: _WinCtx, rng: np.random.Generator, op: np.ndarray, spos: np.ndarray,
                      am: np.ndarray) -> None:
    o = st["orders"]
    rr = np.array([_owner_by_id(st, int(x))["return_rate"] for x in o["owner_id"][op]])
    b2c = o["order_type"][op] == "B2C"
    t = o["t_ship"][op]
    day = t // DAY
    peak = np.array([_peak_shape(geo.day_date(int(d))) > 0 for d in day])
    p = np.where(b2c, rr * np.where(peak, 1.3, 1.0), 0.004)
    ret = rng.random(len(op)) < p
    if not ret.any():
        return
    r = np.flatnonzero(ret)
    n = len(r)
    wi = o["wi"][op][r].astype(np.int64)
    t_rma = t[r] + rng.integers(4 * DAY, 30 * DAY, n)
    t_recv = t_rma + rng.integers(3 * DAY, 9 * DAY, n)
    t_recv = (t_recv // DAY) * DAY + rng.integers(8 * 3600, 16 * 3600, n)
    t_proc = _cal_add(ctx, st, wi, t_recv, rng.integers(2 * 3600, 40 * 3600, n))
    keys = ctx.next_ids("wms.retkey", n)
    scac = np.where(st["wh_country"][wi] == "US", np.where(rng.random(n) < 0.6, "USPS", "UPSN"),
                    np.where(st["wh_country"][wi] == "CA", "CNPC", "ESTF")).astype(object)
    tadd(st["pret"], n, key=keys, owner_id=o["owner_id"][op][r], wi=wi, order_id=o["order_id"][op][r], order_nbr=o["order_nbr"][op][r],
         t_rma=t_rma, t_recv=t_recv, t_proc=t_proc, scac=scac, svc=np.where(scac == "UPSN", "GND", "UGA").astype(object))
    # lines: one or more shipped lines, part or all of the qty
    A = st["allocs"]
    oids = o["order_id"][op][r]
    sel = np.isin(A["order_id"][am], oids)
    la = am[sel]
    df = pd.DataFrame({"order_id": A["order_id"][la], "line_nbr": A["line_nbr"][la], "mat": A["mat"][la], "qty": A["qty"][la],
                       "lot": st["mat"]["lot"][A["mat"][la]]})
    df = df.groupby(["order_id", "line_nbr", "mat", "lot"], as_index=False)["qty"].sum()
    keep = rng.random(len(df)) < 0.75
    first = ~df["order_id"].duplicated()
    df = df[keep | first.to_numpy()]
    df["qty"] = np.maximum(1, np.floor(df["qty"].to_numpy() * rng.uniform(0.5, 1.0, len(df)))).astype(np.int64)
    kmap = pd.Series(keys, index=oids)
    vert = np.array([_owner_by_id(st, int(x))["vertical"] for x in o["owner_id"][pos_of(o["order_id"], df["order_id"].to_numpy())]])
    reasons = np.empty(len(df), dtype=object)
    for v in np.unique(vert):
        opts, w = RET_REASONS.get(v, RET_DEFAULT)
        m = vert == v
        reasons[m] = rng.choice(opts, int(m.sum()), p=w)
    ln = df.groupby("order_id").cumcount().to_numpy() + 1
    tadd(st["ret_lines"], len(df), key=kmap.reindex(df["order_id"]).to_numpy(np.int64), line_nbr=ln, material_id=df["mat"].to_numpy() + 1,
         qty=df["qty"].to_numpy(), reason_code=reasons, disposition=None, lot=df["lot"].to_numpy(bool))


def _returns(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush) -> None:
    rng = W.rng("returns")
    p = st["pret"]
    if len(p["key"]):
        le = W.le[p["wi"]]
        due = p["t_rma"] < le
        if due.any():
            d = np.flatnonzero(due)
            n = len(d)
            rids = ctx.next_ids("wms.return", n)
            svc = st["svc"]
            fmt = np.array([svc.loc[svc["scac"] == s, "fmt"].iloc[0] for s in p["scac"][d]], dtype=object)
            trk = carr.tracking_numbers(fmt, p["svc"][d], st["wh_code"][p["wi"][d]], ctx.next_ids("wms.tracking", n, start=50_000))
            tadd(st["rets"], n, return_id=rids, rma_nbr=np.array([f"RMA{30_000_000 + int(x):08d}" for x in rids], dtype=object),
                 owner_id=p["owner_id"][d], warehouse_id=p["wi"][d] + 1, original_order_id=p["order_id"][d], carrier_tracking_nbr=trk,
                 received_ts=T_NONE, return_status="EXPECTED", create_ts=p["t_rma"][d], mod_ts=p["t_rma"][d], _st=1,
                 t_rma=p["t_rma"][d], t_recv=p["t_recv"][d], t_proc=p["t_proc"][d], wi=p["wi"][d], order_nbr=p["order_nbr"][d],
                 key=p["key"][d])
            rl = st["ret_lines"]
            lm = np.isin(rl["key"], p["key"][d])
            kmap = pd.Series(rids, index=p["key"][d])
            fl.insert("return_line", pd.DataFrame({"return_id": kmap.reindex(rl["key"][lm]).to_numpy(np.int64), "line_nbr": rl["line_nbr"][lm],
                                                   "material_id": rl["material_id"][lm], "qty": rl["qty"][lm],
                                                   "reason_code": rl["reason_code"][lm], "disposition": None}))
            tkeep(p, ~due)
    r = st["rets"]
    if not len(r["return_id"]):
        return
    le = W.le[r["wi"]]
    recv = (r["return_status"] == "EXPECTED") & (r["t_recv"] < le)
    if recv.any():
        rp = np.flatnonzero(recv)
        r["return_status"][rp] = "RECEIVED"
        r["received_ts"][rp] = r["t_recv"][rp]
        r["mod_ts"][rp] = r["t_recv"][rp]
        mark(r, recv)
        rl = st["ret_lines"]
        lm = np.isin(rl["key"], r["key"][rp])
        units = pd.Series(rl["qty"][lm], index=rl["key"][lm]).groupby(level=0).sum().reindex(r["key"][rp]).fillna(0).to_numpy(np.int64)
        reason = pd.Series(rl["reason_code"][lm], index=rl["key"][lm]).groupby(level=0).first().reindex(r["key"][rp]).to_numpy(object)
        st["pending_bus"].append(("returns_received", pd.DataFrame({
            "return_id": r["return_id"][rp], "rma_nbr": r["rma_nbr"][rp],
            "owner_code": [_owner_by_id(st, int(x))["code"] for x in r["owner_id"][rp]], "warehouse_code": st["wh_code"][r["wi"][rp]],
            "original_order_nbr": r["order_nbr"][rp], "received_ts": _utc_series(st, r["wi"][rp], r["t_recv"][rp]), "units": units,
            "reason_code": reason})))
    proc = (r["return_status"] == "RECEIVED") & (r["t_proc"] < le)
    if proc.any():
        _process_returns(ctx, st, W, fl, rng, np.flatnonzero(proc))
    done = (r["return_status"] == "PROCESSED") & (r["_st"] == 0)
    if done.any():
        rl = st["ret_lines"]
        tkeep(rl, ~np.isin(rl["key"], r["key"][done]))
        tkeep(r, ~done)


def _process_returns(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush, rng: np.random.Generator, rp: np.ndarray) -> None:
    r = st["rets"]
    rl = st["ret_lines"]
    t = r["t_proc"][rp]
    r["return_status"][rp] = "PROCESSED"
    r["mod_ts"][rp] = t
    mark_pos(r, rp)
    lm = np.flatnonzero(np.isin(rl["key"], r["key"][rp]))
    reason = rl["reason_code"][lm]
    u = rng.random(len(lm))
    disp = np.select([np.isin(reason, ["WRONG_SIZE", "NO_LONGER_NEEDED"]) & (u < 0.92),
                      (reason == "NOT_AS_DESCRIBED") & (u < 0.7), reason == "DAMAGED", reason == "DEFECTIVE"],
                     ["RESTOCK", "RESTOCK", np.where(u < 0.5, "DAMAGED", "DESTROY"), np.where(u < 0.6, "RTV", "DESTROY")], "DESTROY")
    disp = np.where(rl["lot"][lm], "DESTROY", disp)        # lot-controlled goods (food, cosmetics) are never restocked
    gone = pd.Series([_owner_by_id(st, int(x))["status"] in ("OFFBOARDING", "INACTIVE") for x in r["owner_id"][rp]],
                     index=r["key"][rp]).reindex(rl["key"][lm]).to_numpy(bool)
    disp = np.where(gone & np.isin(disp, ["RESTOCK", "DAMAGED"]), "RTV", disp)   # offboarding clients take returns back
    rl["disposition"][lm] = disp
    rid = pd.Series(r["return_id"][rp], index=r["key"][rp]).reindex(rl["key"][lm]).to_numpy(np.int64)
    fl.update("return_line", ("return_id", "line_nbr"), pd.DataFrame({"return_id": rid, "line_nbr": rl["line_nbr"][lm], "disposition": disp}))
    tmap = pd.Series(t, index=r["key"][rp])
    wmap = pd.Series(r["wi"][rp], index=r["key"][rp])
    omap = pd.Series(r["owner_id"][rp], index=r["key"][rp])
    stock = np.isin(disp, ["RESTOCK", "DAMAGED"])
    if stock.any():
        sl = lm[stock]
        n = len(sl)
        wi = wmap.reindex(rl["key"][sl]).to_numpy(np.int64)
        tt = tmap.reindex(rl["key"][sl]).to_numpy(np.int64)
        owner = omap.reindex(rl["key"][sl]).to_numpy(np.int64)
        has_ret = np.array([("RETURNS" in st["wh_rules"][int(w)]["zones"]) for w in wi])
        loc = _pick_locations(st, rng, wi, np.where(has_ret, "RETURNS", "AMBIENT").astype(object),
                              np.where(has_ret, LT_RET, LT_PICK).astype(np.int8))
        ids = ctx.next_ids("wms.inventory", n)
        status = np.where(disp[stock] == "RESTOCK", I_AVAIL, I_DMG).astype(np.int8)
        tadd(st["inv"], n, inventory_id=ids, warehouse_id=wi + 1, location_id=loc, lpn_id=0, owner_id=owner,
             material_id=rl["material_id"][sl], lot_nbr=None, expiry_date=D_NONE, qty_on_hand=rl["qty"][sl], qty_allocated=0,
             inv_status=status, hold_reason=np.where(status == I_DMG, "RETURN_DAMAGED", None).astype(object), received_date=tt // DAY,
             create_ts=tt, mod_ts=tt, _st=1)
        _txn(ctx, st, fl, rng, wi, tt, owner, rl["material_id"][sl], "RETURN", rl["qty"][sl], to_loc=loc, reason=reason[stock],
             ref_type="RETURN", ref_id=rid[stock])
    units = pd.Series(rl["qty"][lm], index=rl["key"][lm]).groupby(level=0).sum().reindex(r["key"][rp]).fillna(0).to_numpy()
    _accrue(ctx, st, fl, rng, r["owner_id"][rp], r["wi"][rp].astype(np.int64), np.full(len(rp), "RET-PROC", dtype=object), t, units,
            "RETURN", r["return_id"][rp])


# =============================================================================================== inventory control
def _inventory_control(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush) -> None:
    rng = W.rng("invctl")
    for wi in _active_whs(st):
        for t in W.crosses(st, wi, 10, 0):
            if (t // DAY + 3) % 7 < 5:
                _cycle_counts(ctx, st, W, fl, wi, t, rng)
        for t in W.crosses(st, wi, 11, 0):
            _qa_holds(ctx, st, W, fl, wi, t, rng)
    _counts_progress(ctx, st, W, fl, rng)
    _excursion_holds(ctx, st, W, fl, rng)
    _recall_hold(ctx, st, W, fl, rng)
    _hold_reviews(ctx, st, W, fl, rng)


def _cycle_counts(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush, wi: int, t: int, rng: np.random.Generator) -> None:
    inv = st["inv"]
    lk = _loc_lookup(st)
    ltype = lk["ltype"][inv["location_id"]]
    cand = np.flatnonzero((inv["warehouse_id"] == wi + 1) & (inv["inv_status"] == I_AVAIL) & (inv["qty_allocated"] == 0)
                          & (inv["qty_on_hand"] > 0) & ((ltype == LT_PICK) | (ltype == LT_RES)))
    if not len(cand):
        return
    k = max(3, int(len(cand) * 0.006))
    pick = rng.choice(cand, size=min(k, len(cand)), replace=False)
    n = len(pick)
    lax = _wh_index(ctx).get("LAX1")
    ramp = lax == wi and (t // DAY - st["wh_open_day"][wi]) < 56
    p_var = 0.12 if ramp else 0.025
    has_var = rng.random(n) < p_var
    sys_q = inv["qty_on_hand"][pick].astype(np.int64)
    mag = np.maximum(1, (sys_q * rng.uniform(0.01, 0.08, n)).astype(np.int64))
    sign = np.where(rng.random(n) < 0.68, -1, 1)
    var = np.where(has_var, sign * np.minimum(mag, np.where(sign < 0, sys_q, mag)), 0)
    ts = t + np.arange(n) * rng.integers(60, 240, n)
    users = _pick_users(st, rng, np.full(n, wi), ts, "INV_CONTROL")
    leads = _pick_users(st, rng, np.full(n, wi), ts, "LEAD")
    ids = ctx.next_ids("wms.cycle_count", n)
    status = np.where(has_var, "COUNTED", "APPROVED").astype(object)
    t_rc = np.where(has_var, _cal_add(ctx, st, np.full(n, wi), ts, rng.integers(3600, 3 * 3600, n)), T_NONE)
    t_ap = np.where(has_var, _cal_add(ctx, st, np.full(n, wi), ts, rng.integers(4 * 3600, 20 * 3600, n)), T_NONE)
    tadd(st["counts"], n, count_id=ids, warehouse_id=wi + 1, location_id=inv["location_id"][pick], material_id=inv["material_id"][pick],
         lot_nbr=inv["lot_nbr"][pick], system_qty=sys_q, counted_qty=sys_q + var, variance_qty=var, count_ts=ts, counted_by=users,
         count_status=status, approved_by=np.where(has_var, 0, leads), create_ts=ts, mod_ts=ts, _st=1, inv_id=inv["inventory_id"][pick],
         t_recount=t_rc, t_approve=t_ap, wi=wi, owner_id=inv["owner_id"][pick])
    tids = ctx.next_ids("wms.task", n)
    tadd(st["tasks"], n, task_id=tids, warehouse_id=wi + 1, task_type=K_COUNT, task_status=0, priority=6, material_id=inv["material_id"][pick],
         qty=sys_q, from_location_id=inv["location_id"][pick], to_location_id=inv["location_id"][pick], assigned_user_id=users,
         created_ts=t - 1800, assigned_ts=ts - 300, started_ts=ts - 120, completed_ts=ts, mod_ts=t - 1800, _st=1, applied=False, ref=ids, wi=wi)
    _txn(ctx, st, fl, rng, wi, ts, inv["owner_id"][pick], inv["material_id"][pick], "COUNT", 0, from_loc=inv["location_id"][pick],
         to_loc=inv["location_id"][pick], lpn=inv["lpn_id"][pick], lot=inv["lot_nbr"][pick], ref_type="COUNT", ref_id=ids, user=users)


def _apply_counts(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush, rng: np.random.Generator, tp: np.ndarray) -> None:
    """COUNT tasks carry no inventory effect themselves (approval posts the ADJUST)."""


def _counts_progress(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush, rng: np.random.Generator) -> None:
    c = st["counts"]
    if not len(c["count_id"]):
        return
    le = W.le[c["wi"]]
    m = (c["count_status"] == "COUNTED") & (c["t_recount"] < le)
    if m.any():
        c["count_status"][m] = "RECOUNT"
        c["mod_ts"][m] = c["t_recount"][m]
        mark(c, m)
    m = (c["count_status"] == "RECOUNT") & (c["t_approve"] < le)
    if m.any():
        cp = np.flatnonzero(m)
        inv = st["inv"]
        ip = pos_of(inv["inventory_id"], c["inv_id"][cp])
        exists = inv["inventory_id"][ip] == c["inv_id"][cp]
        var = c["variance_qty"][cp].astype(np.int64)
        avail = (inv["qty_on_hand"][ip] - inv["qty_allocated"][ip]).astype(np.int64)
        adj = np.where(exists, np.maximum(var, -np.maximum(avail, 0)), 0)
        t = c["t_approve"][cp]
        ok = exists & (adj != 0)
        np.add.at(inv["qty_on_hand"], ip[ok], adj[ok].astype(np.int32))
        inv["mod_ts"][ip[ok]] = t[ok]
        mark_pos(inv, ip[ok])
        wi = c["wi"][cp].astype(np.int64)
        leads = _pick_users(st, rng, wi, t, "LEAD")
        c["count_status"][cp] = "APPROVED"
        c["approved_by"][cp] = leads
        c["mod_ts"][cp] = t
        mark(c, m)
        if ok.any():
            _txn(ctx, st, fl, rng, wi[ok], t[ok], c["owner_id"][cp][ok], c["material_id"][cp][ok], "ADJUST", adj[ok],
                 from_loc=c["location_id"][cp][ok], lot=c["lot_nbr"][cp][ok], reason="CC_VARIANCE", ref_type="COUNT",
                 ref_id=c["count_id"][cp][ok], user=leads[ok])
    done = (c["count_status"] == "APPROVED") & (c["_st"] == 0)
    if done.any():
        tkeep(c, ~done)


def _hold(ctx: Ctx, st: dict[str, Any], fl: Flush, rng: np.random.Generator, ip: np.ndarray, t: np.ndarray, reason: str,
          due: np.ndarray, destroy: np.ndarray) -> None:
    """Put inventory rows on HOLD (allocations on them are given back) and schedule the QA decision."""
    inv = st["inv"]
    if not len(ip):
        return
    A = st["allocs"]
    am = np.isin(A["inv_id"], inv["inventory_id"][ip]) & ~A["picked"]
    if am.any():
        # give the stock back; affected orders go back to allocation
        L = st["lines"]
        lkey = L["order_id"].astype(np.int64) * 1000 + L["line_nbr"]
        aq = pd.Series(A["qty"][am], index=A["order_id"][am].astype(np.int64) * 1000 + A["line_nbr"][am]).groupby(level=0).sum()
        hit = np.isin(lkey, aq.index.to_numpy())
        L["allocated_qty"][hit] -= aq.reindex(lkey[hit]).to_numpy(np.int32)
        L["open_alloc"][hit] -= aq.reindex(lkey[hit]).to_numpy(np.int32)
        L["line_status"][hit] = L_BACK
        mark(L, hit)
        iq = pd.Series(A["qty"][am], index=A["inv_id"][am]).groupby(level=0).sum()
        pp = pos_of(inv["inventory_id"], iq.index.to_numpy())
        inv["qty_allocated"][pp] = np.maximum(0, inv["qty_allocated"][pp] - iq.to_numpy())
        tk = st["tasks"]
        tm = np.isin(tk["task_id"], A["task_id"][am]) & ~tk["applied"]
        if tm.any():
            in_db = tm & (tk["_st"] != 1)
            if in_db.any():
                fl.delete("task", {"task_id": tk["task_id"][in_db]})
            tkeep(tk, ~tm)
        o = st["orders"]
        om = np.isin(o["order_id"], A["order_id"][am]) & (o["order_status"] < O_PACK)
        o["order_status"][om] = O_NEW
        o["t_alloc"][om] = np.max(t) + 600
        for k in ("t_release", "t_pick_start", "t_pick_done", "t_pack", "t_ship"):
            o[k][om] = T_NONE
        mark(o, om)
        tkeep(A, ~am)
    wi = inv["warehouse_id"][ip].astype(np.int64) - 1
    inv["inv_status"][ip] = I_HOLD
    inv["hold_reason"][ip] = reason
    inv["mod_ts"][ip] = t
    mark_pos(inv, ip)
    _txn(ctx, st, fl, rng, wi, t, inv["owner_id"][ip], inv["material_id"][ip], "HOLD", 0, from_loc=inv["location_id"][ip],
         lpn=inv["lpn_id"][ip], lot=inv["lot_nbr"][ip], reason=reason, ref_type="QA", ref_id=0)
    tadd(st["holds"], len(ip), inv_id=inv["inventory_id"][ip], t_due=due, destroy=destroy, reason=reason, t_hold=t, wi=wi)
    _publish_holds(st, ip, t, None, reason)


def _publish_holds(st: dict[str, Any], ip: np.ndarray, t: np.ndarray, released: np.ndarray | None, reason: Any) -> None:
    inv = st["inv"]
    wi = inv["warehouse_id"][ip].astype(np.int64) - 1
    mat = st["mat"]
    df = pd.DataFrame({"owner_code": [_owner_by_id(st, int(x))["code"] for x in inv["owner_id"][ip]], "warehouse_code": st["wh_code"][wi],
                       "sku": mat["sku"][inv["material_id"][ip] - 1], "lot_nbr": inv["lot_nbr"][ip], "qty": inv["qty_on_hand"][ip],
                       "hold_reason": reason, "hold_ts": _utc_series(st, wi, t),
                       "released_ts": _utc_series(st, wi, released) if released is not None else pd.Series(pd.NaT, index=range(len(ip)), dtype="datetime64[ns, UTC]")})
    st["pending_bus"].append(("inventory_holds", df))


def _qa_holds(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush, wi: int, t: int, rng: np.random.Generator) -> None:
    inv = st["inv"]
    cand = np.flatnonzero((inv["warehouse_id"] == wi + 1) & (inv["inv_status"] == I_AVAIL) & (inv["qty_on_hand"] > 0)
                          & (inv["qty_allocated"] == 0) & (inv["lpn_id"] > 0))
    pick = cand[rng.random(len(cand)) < 0.0003]
    if len(pick):
        reason = HOLD_QA_REASONS[int(rng.integers(0, len(HOLD_QA_REASONS)))]
        n = len(pick)
        due = _cal_add(ctx, st, np.full(n, wi), np.full(n, t), rng.integers(DAY, 5 * DAY, n))
        _hold(ctx, st, fl, rng, pick, np.full(n, t), str(reason), due, rng.random(n) < (0.2 if reason == "DAMAGED_FOUND" else 0.05))


def _excursion_holds(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush, rng: np.random.Generator) -> None:
    """Planted cold_excursion_summer: dock door 14 open > 20 min on hot afternoons; QA holds the cooler lots near it."""
    from .iot import cold_excursions

    story = next((s for s in ctx.planted.get("stories", []) if s["id"] == "cold_excursion_summer"), None)
    if not story:
        return
    wi = _wh_index(ctx).get(story["warehouse"])
    if wi is None or wi not in st["wh_ready"]:
        return
    for d in range(int(W.ls[wi] // DAY), int(W.le[wi] // DAY) + 1):
        ex = cold_excursions(ctx, d)
        if ex.empty:
            continue
        hit = ex[ex["excursion"] & (ex["detect_local"] >= W.ls[wi]) & (ex["detect_local"] < W.le[wi])]
        for _, e in hit.iterrows():
            lk = _loc_lookup(st)
            near = st.get("_near14")
            if near is None:
                loc = st["loc"]
                near = st["_near14"] = loc["id"][(loc["wi"] == wi) & (loc["zone"] == story["zone"]) &
                                                 np.isin(loc["aisle"], ["C01", "C02"]) & (loc["bay"] <= 6)]
            del lk
            inv = st["inv"]
            ip = np.flatnonzero((inv["warehouse_id"] == wi + 1) & np.isin(inv["location_id"], near) & (inv["inv_status"] == I_AVAIL)
                                & (inv["qty_on_hand"] > 0))
            rc = st["story"].get("recall")
            if rc:      # keep the two cold-chain stories separate: the recall lot was racked away from door 14
                ip = ip[~((inv["material_id"][ip] == rc["material_id"]) & (inv["lot_nbr"][ip] == rc["lot"]))]
            if not len(ip):
                continue
            t = int(e["detect_local"])
            n = len(ip)
            lo, hi = ctx.rules["cold_chain"]["hold_review_days"]
            due = _cal_add(ctx, st, np.full(n, wi), np.full(n, t), np.full(n, int(rng.integers(lo, hi + 1)) * 8 * 3600))
            share = float(ctx.rules["cold_chain"]["release_share"])
            lots = inv["lot_nbr"][ip]
            ulots = pd.unique(lots)
            destroy_lot = {lt: (rng.random() > share) for lt in ulots}
            destroy = np.array([destroy_lot[lt] for lt in lots])
            _hold(ctx, st, fl, rng, ip, np.full(n, t), "TEMP_EXCURSION", due, destroy)


def _recall_hold(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush, rng: np.random.Generator) -> None:
    rc = st["story"].get("recall")
    if not rc or rc["done"]:
        return
    wi = rc["wi"]
    t = rc["day"] * DAY + 10 * 3600
    if t >= W.le[wi]:
        return
    rc["done"] = True
    inv = st["inv"]
    lt = _loc_lookup(st)["ltype"][inv["location_id"]]
    ip = np.flatnonzero((inv["material_id"] == rc["material_id"]) & (inv["lot_nbr"] == rc["lot"]) & (inv["qty_on_hand"] > 0)
                        & (inv["inv_status"] != I_HOLD) & (lt != LT_STAGE))
    if not len(ip):
        return
    n = len(ip)
    due = np.full(n, t + int(rng.integers(5, 10)) * DAY)
    _hold(ctx, st, fl, rng, ip, np.full(n, max(t, int(W.ls[wi]))), "RECALL", due, np.ones(n, bool))


def _hold_reviews(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush, rng: np.random.Generator) -> None:
    h = st["holds"]
    if not len(h["inv_id"]):
        return
    due = h["t_due"] < W.le[h["wi"]]
    if not due.any():
        return
    inv = st["inv"]
    hp = np.flatnonzero(due)
    ip = pos_of(inv["inventory_id"], h["inv_id"][hp])
    ok = (inv["inventory_id"][ip] == h["inv_id"][hp]) & (inv["inv_status"][ip] == I_HOLD)
    hp, ip = hp[ok], ip[ok]
    t = h["t_due"][hp]
    rel = ~h["destroy"][hp]
    if rel.any():
        r_ip = ip[rel]
        inv["inv_status"][r_ip] = I_AVAIL
        inv["hold_reason"][r_ip] = None
        inv["mod_ts"][r_ip] = t[rel]
        wi = inv["warehouse_id"][r_ip].astype(np.int64) - 1
        _txn(ctx, st, fl, rng, wi, t[rel], inv["owner_id"][r_ip], inv["material_id"][r_ip], "RELEASE", 0,
             from_loc=inv["location_id"][r_ip], lpn=inv["lpn_id"][r_ip], lot=inv["lot_nbr"][r_ip], reason=h["reason"][hp][rel],
             ref_type="QA", ref_id=0)
        _publish_holds(st, r_ip, h["t_hold"][hp][rel], t[rel], h["reason"][hp][rel])
    de = h["destroy"][hp]
    if de.any():
        d_ip = ip[de]
        q = inv["qty_on_hand"][d_ip].astype(np.int64)
        wi = inv["warehouse_id"][d_ip].astype(np.int64) - 1
        _txn(ctx, st, fl, rng, wi, t[de], inv["owner_id"][d_ip], inv["material_id"][d_ip], "DESTROY", -q,
             from_loc=inv["location_id"][d_ip], lpn=inv["lpn_id"][d_ip], lot=inv["lot_nbr"][d_ip], reason=h["reason"][hp][de],
             ref_type="QA", ref_id=0)
        inv["qty_on_hand"][d_ip] = 0
        inv["mod_ts"][d_ip] = t[de]
    mark_pos(inv, ip)
    keep = np.ones(len(h["inv_id"]), bool)
    keep[np.flatnonzero(due)] = False
    tkeep(h, keep)


# =============================================================================================== nightly jobs
def _nightly(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush) -> None:
    rng = W.rng("nightly")
    for wi in _active_whs(st):
        for t in W.crosses(st, wi, 2, 0):
            _storage_accrual(ctx, st, fl, rng, wi, t)
            _expiry(ctx, st, fl, rng, wi, t)
            _zero_cleanup(ctx, st, fl, wi, t)
            _replenish(ctx, st, fl, rng, wi, t)
            if wi == _active_whs(st)[0]:
                _task_purge(ctx, st, fl, t)
                _billing_corrections(ctx, st, fl, rng, t)
    # month start: account management fees
    for info in st["owners"].values():
        if info["status"] not in ("ACTIVE", "OFFBOARDING"):
            continue
        for t in W.crosses(st, info["primary"], 4, 0):
            if geo.day_date(int(t // DAY)).day == 1:
                _accrue(ctx, st, fl, rng, np.array([info["owner_id"]]), np.array([info["primary"]]), np.array(["ACCT-MGT"], dtype=object),
                        np.array([t]), np.array([1.0]), None, np.array([0]))
    # remember the last task id created each day (180-day purge works on id ranges)
    tk_max = int(ctx.world.get("_ids", {}).get("wms.task", 1)) - 1
    day = int(W.we // DAY)
    if not st["task_days"] or st["task_days"][-1][0] != day:
        st["task_days"].append((day, tk_max))
    else:
        st["task_days"][-1] = (day, tk_max)
    st["task_days"] = st["task_days"][-400:]


def _storage_accrual(ctx: Ctx, st: dict[str, Any], fl: Flush, rng: np.random.Generator, wi: int, t: int) -> None:
    inv = st["inv"]
    m = (inv["warehouse_id"] == wi + 1) & (inv["qty_on_hand"] > 0)
    if not m.any():
        return
    lk = _loc_lookup(st)
    mat = st["mat"]
    mi = inv["material_id"][m] - 1
    ltype = lk["ltype"][inv["location_id"][m]]
    zone = lk["ztype"][inv["location_id"][m]]
    per_pal = np.maximum(mat["case_qty"][mi] * mat["cpp"][mi], 1)
    pallets = np.where(ltype == LT_PICK, 0.0, np.ceil(inv["qty_on_hand"][m] / per_pal))
    bins = (ltype == LT_PICK).astype(float)
    cold = np.isin(zone, ["COOLER", "FREEZER"]) * pallets
    owner = inv["owner_id"][m]
    prev = geo.day_date(int(t // DAY) - 1)
    dim = (date(prev.year + (prev.month == 12), prev.month % 12 + 1, 1) - date(prev.year, prev.month, 1)).days
    df = pd.DataFrame({"o": owner, "p": pallets, "b": bins, "c": cold}).groupby("o").sum()
    owners = df.index.to_numpy()
    for code, col in (("STOR-PLT", "p"), ("STOR-BIN", "b"), ("COLD-SUR", "c")):
        q = df[col].to_numpy() / dim
        ok = q > 0
        if ok.any():
            _accrue(ctx, st, fl, rng, owners[ok], np.full(int(ok.sum()), wi), np.full(int(ok.sum()), code, dtype=object),
                    np.full(int(ok.sum()), t), np.round(q[ok], 2), None, np.zeros(int(ok.sum()), int), activity_day=int(t // DAY) - 1)


def _expiry(ctx: Ctx, st: dict[str, Any], fl: Flush, rng: np.random.Generator, wi: int, t: int) -> None:
    inv = st["inv"]
    day = int(t // DAY)
    m = np.flatnonzero((inv["warehouse_id"] == wi + 1) & (inv["inv_status"] == I_AVAIL) & (inv["expiry_date"] > D_NONE)
                       & (inv["expiry_date"] < day) & (inv["qty_on_hand"] > 0) & (inv["qty_allocated"] == 0))
    if len(m):
        n = len(m)
        nxt_mon = (day + (7 - (day + 3) % 7)) * DAY + 2 * 3600 + 1800
        _hold(ctx, st, fl, rng, m, np.full(n, t), "EXPIRED", np.full(n, nxt_mon), np.ones(n, bool))
    dmg = np.flatnonzero((inv["warehouse_id"] == wi + 1) & (inv["inv_status"] == I_DMG) & (inv["qty_on_hand"] > 0)
                         & (inv["create_ts"] < t - 5 * DAY))
    if len(dmg) and (day + 3) % 7 == 0:
        q = inv["qty_on_hand"][dmg].astype(np.int64)
        _txn(ctx, st, fl, rng, wi, np.full(len(dmg), t + 900), inv["owner_id"][dmg], inv["material_id"][dmg], "DESTROY", -q,
             from_loc=inv["location_id"][dmg], reason="DAMAGED", ref_type="QA", ref_id=0)
        inv["qty_on_hand"][dmg] = 0
        inv["mod_ts"][dmg] = t + 900
        mark_pos(inv, dmg)


def _zero_cleanup(ctx: Ctx, st: dict[str, Any], fl: Flush, wi: int, t: int) -> None:
    inv = st["inv"]
    # only rows that were already empty at 02:00 (mod_ts = when they hit zero); later ones wait for the next night
    z = (inv["warehouse_id"] == wi + 1) & (inv["qty_on_hand"] <= 0) & (inv["qty_allocated"] <= 0) & (inv["mod_ts"] < t)
    if not z.any():
        return
    A = st["allocs"]
    z &= ~np.isin(inv["inventory_id"], A["inv_id"]) & ~np.isin(inv["inventory_id"], A["stage_inv"])
    h = st["holds"]
    z &= ~np.isin(inv["inventory_id"], h["inv_id"])
    tk = st["tasks"]
    z &= ~np.isin(inv["inventory_id"], tk["ref"][(tk["task_type"] != K_PICK) & ~tk["applied"]])
    if not z.any():
        return
    in_db = z & (inv["_st"] != 1)
    if in_db.any():
        fl.delete("inventory", {"inventory_id": inv["inventory_id"][in_db]})
    lp = inv["lpn_id"][z]
    lp = lp[(lp > 0) & ~np.isin(lp, st["lpns"]["lpn_id"])]
    if len(lp):
        fl.execute("license_plate", "UPDATE wms.license_plate SET lpn_status = 'CONSUMED', mod_ts = %s WHERE lpn_id = ANY(%s)",
                   (ts_col(np.array([t]))[0].astype("datetime64[us]").item(), lp.astype(np.int64).tolist()))
    tkeep(inv, ~z)


def _replenish(ctx: Ctx, st: dict[str, Any], fl: Flush, rng: np.random.Generator, wi: int, t: int) -> None:
    """Move the FEFO-first reserve pallet into a pick face for SKUs whose pick faces ran dry."""
    inv = st["inv"]
    lk = _loc_lookup(st)
    here = (inv["warehouse_id"] == wi + 1) & (inv["inv_status"] == I_AVAIL) & (inv["qty_on_hand"] > 0)
    lt = lk["ltype"][inv["location_id"]]
    face = here & (lt == LT_PICK)
    res = np.flatnonzero(here & (lt == LT_RES) & (inv["qty_allocated"] == 0))
    if not len(res):
        return
    has_face = np.isin(inv["material_id"][res], inv["material_id"][face])
    cand = res[~has_face]
    if not len(cand):
        return
    cand = cand[np.lexsort((np.where(inv["expiry_date"][cand] > D_NONE, inv["expiry_date"][cand], 10**7), inv["material_id"][cand]))]
    _, first = np.unique(inv["material_id"][cand], return_index=True)
    cand = cand[first][:400]
    n = len(cand)
    mat = st["mat"]
    to = _pick_locations(st, rng, np.full(n, wi), mat["zone"][inv["material_id"][cand] - 1], np.full(n, LT_PICK, np.int8))
    start = t + 3600 * 4 + np.arange(n) * rng.integers(60, 300, n)
    tids = ctx.next_ids("wms.task", n)
    fork = _pick_users(st, rng, np.full(n, wi), start, "FORKLIFT")
    tadd(st["tasks"], n, task_id=tids, warehouse_id=wi + 1, task_type=K_REPLEN, task_status=0, priority=3,
         material_id=inv["material_id"][cand], qty=inv["qty_on_hand"][cand], from_location_id=inv["location_id"][cand],
         to_location_id=to, assigned_user_id=fork, created_ts=np.full(n, t), assigned_ts=start - 300, started_ts=start,
         completed_ts=start + rng.integers(240, 900, n), mod_ts=np.full(n, t), _st=1, applied=False, ref=inv["inventory_id"][cand],
         wi=wi)


def _task_purge(ctx: Ctx, st: dict[str, Any], fl: Flush, t: int) -> None:
    """Nightly housekeeping: tasks created more than 180 days ago are DELETED (by id range: ids follow time)."""
    cutoff = int(t // DAY) - 180
    upto = 0
    for day, max_id in st["task_days"]:
        if day <= cutoff:
            upto = max_id
    if upto > st["purged_to"]:
        fl.execute("task", "DELETE FROM wms.task WHERE task_id > %s AND task_id <= %s AND task_status IN ('COMPLETE', 'CANCELLED')",
                   (int(st["purged_to"]), int(upto)))
        st["purged_to"] = upto


# =============================================================================================== billing
def _rate_lookup(st: dict[str, Any]) -> dict[tuple[int, str], tuple[float, str]]:
    r = st["rates"]
    return {(int(o), str(c)): (float(x), str(cur)) for o, c, x, cur in zip(r["owner_id"], r["charge_code"], r["rate"], r["currency"])}


def _accrue(ctx: Ctx, st: dict[str, Any], fl: Flush, rng: np.random.Generator, owner: np.ndarray, wi: np.ndarray,
            code: np.ndarray, t: np.ndarray, qty: np.ndarray, ref_type: str | None, ref_id: np.ndarray,
            activity_day: int | None = None) -> None:
    if not len(owner):
        return
    rates = _rate_lookup(st)
    rr = [rates.get((int(o), str(c))) for o, c in zip(owner, code)]
    ok = np.array([x is not None for x in rr]) & (np.asarray(qty, dtype=float) > 0)
    if not ok.any():
        return
    idx = np.flatnonzero(ok)
    rate = np.array([rr[i][0] for i in idx])
    cur = np.array([rr[i][1] for i in idx], dtype=object)
    q = np.round(np.asarray(qty, dtype=float)[idx], 2)
    code_ok = np.asarray(code, dtype=object)[idx]
    if "FRT-RCV" in set(code_ok):
        f = code_ok == "FRT-RCV"
        rate[f] = np.round(q[f] * (1 + rate[f] / 100.0), 4)
        q[f] = 1.0
    amount = np.round(q * rate, 2)
    ids = ctx.next_ids("wms.billing_record", len(idx))
    tt = np.asarray(t, dtype=np.int64)[idx]
    day = np.full(len(idx), activity_day) if activity_day is not None else tt // DAY
    df = pd.DataFrame({"billing_id": ids, "owner_id": np.asarray(owner)[idx], "warehouse_id": np.asarray(wi)[idx] + 1,
                       "charge_code": code_ok, "activity_date": day_col(day), "qty": q, "rate": np.round(rate, 4), "amount": amount,
                       "currency": cur, "ref_type": ref_type, "ref_id": nint(np.asarray(ref_id)[idx]), "billing_status": "ACCRUED",
                       "invoice_ref": None, "create_ts": ts_col(tt), "mod_ts": ts_col(tt)})
    bad = ctx.dq.mask("invalid_currency", len(df), rng)
    for i in np.flatnonzero(bad):
        before = df.at[i, "currency"]
        after = {"USD": "US$", "CAD": "CDN", "MXN": "MXP", "EUR": "eur", "GBP": "UKP"}.get(before, before.lower())
        df.at[i, "currency"] = after
        ctx.dq.log("invalid_currency", "wms", "billing_record", str(df.at[i, "billing_id"]), "currency", before, after)
    dup = ctx.dq.mask("duplicate_row", len(df), rng)
    if activity_day is not None:
        dst = ctx.dq.mask("dst_duplicate", len(df), rng) & _dst_eligible(st, np.asarray(wi)[idx], tt - 1800)
        dup = dup | dst
    else:
        dst = np.zeros(len(df), bool)
    if dup.any():
        d = df[dup].copy()
        new = ctx.next_ids("wms.billing_record", len(d))
        for old, nid, is_dst in zip(d["billing_id"], new, dst[dup]):
            ctx.dq.log("dst_duplicate" if is_dst else "duplicate_row", "wms", "billing_record", str(nid), None, None, None,
                       f"accrual duplicated from {old}" + (" (job re-ran in the repeated DST hour)" if is_dst else ""))
        d["billing_id"] = new
        df = pd.concat([df, d], ignore_index=True)
    fl.insert("billing_record", df)
    rec = st["billing_recent"]
    rec.extend(zip(df["billing_id"].tolist(), df["amount"].tolist(), df["rate"].tolist(), df["qty"].tolist(), tt.tolist() + tt[: len(df) - len(tt)].tolist()))
    if len(rec) > 4000:
        del rec[: len(rec) - 4000]


def _accrue_orders(ctx: Ctx, st: dict[str, Any], fl: Flush, rng: np.random.Generator, op: np.ndarray, spos: np.ndarray) -> None:
    o = st["orders"]
    s = st["ships"]
    owner = o["owner_id"][op]
    wi = o["wi"][op].astype(np.int64)
    t = o["t_ship"][op]
    otype = o["order_type"][op]
    b2c = otype == "B2C"
    oid = o["order_id"][op]
    units = o["total_units"][op].astype(float)
    L = st["lines"]
    lm = np.isin(L["order_id"], oid)
    shipped = pd.Series(L["shipped_qty"][lm], index=L["order_id"][lm]).groupby(level=0).sum().reindex(oid).fillna(0).to_numpy()
    units = shipped
    ev_o, ev_w, ev_c, ev_t, ev_q, ev_r, ev_rt = [], [], [], [], [], [], []

    def add(mask: np.ndarray, code: str, q: np.ndarray, ref_t: str | None, ref: np.ndarray) -> None:
        if mask.any():
            ev_o.append(owner[mask])
            ev_w.append(wi[mask])
            ev_c.append(np.full(int(mask.sum()), code, dtype=object))
            ev_t.append(t[mask])
            ev_q.append(q[mask])
            ev_r.append(ref[mask])
            ev_rt.append(ref_t)

    ones = np.ones(len(op))
    add(b2c, "ORD-B2C", ones, "ORDER", oid)
    add(~b2c, "ORD-B2B", ones, "ORDER", oid)
    est = s["freight_charge_est"][spos]
    fx_bill = _freight_in_billing_ccy(ctx, st, owner, s["currency"][spos], est, t)
    add(np.ones(len(op), bool), "FRT-RCV", fx_bill, "SHIPMENT", s["shipment_id"][spos])
    for (ref_t, q_arr, mask, code) in ((None, np.maximum(units - 1, 0), b2c, "PICK-ADD"), (None, ones, b2c, "PACK-MAT"),
                                       (None, units, ~b2c, "LBL-ACC"), (None, s["carton_count"][spos].astype(float), ~b2c, "PICK-CTN"),
                                       (None, o["kit"][op].astype(float), b2c, "KIT-ASM")):
        add(mask, code, q_arr, ref_t, np.zeros(len(op), np.int64))
    if not ev_o:
        return
    # per-order codes keep their reference; activity codes are aggregated per client, site, code and day
    per_ref = [i for i, r in enumerate(ev_rt) if r is not None]
    for i in per_ref:
        _accrue(ctx, st, fl, rng, ev_o[i], ev_w[i], ev_c[i], ev_t[i], ev_q[i], ev_rt[i], ev_r[i])
    agg = [i for i, r in enumerate(ev_rt) if r is None]
    if agg:
        df = pd.DataFrame({"o": np.concatenate([ev_o[i] for i in agg]), "w": np.concatenate([ev_w[i] for i in agg]),
                           "c": np.concatenate([ev_c[i] for i in agg]), "t": np.concatenate([ev_t[i] for i in agg]),
                           "q": np.concatenate([ev_q[i] for i in agg])})
        df["d"] = df["t"] // DAY
        g = df.groupby(["o", "w", "c", "d"], as_index=False).agg(q=("q", "sum"), t=("t", "max"))
        _accrue(ctx, st, fl, rng, g["o"].to_numpy(), g["w"].to_numpy(), g["c"].to_numpy(object), g["t"].to_numpy(),
                g["q"].to_numpy(), None, np.zeros(len(g), int))


def _freight_in_billing_ccy(ctx: Ctx, st: dict[str, Any], owner: np.ndarray, ship_ccy: np.ndarray, amt: np.ndarray, t: np.ndarray) -> np.ndarray:
    out = amt.astype(float).copy()
    bill = np.array([_owner_by_id(st, int(x))["currency"] for x in owner], dtype=object)
    diff = bill != ship_ccy
    for i in np.flatnonzero(diff):
        d = geo.day_date(int(t[i] // DAY))
        usd = amt[i] / (ctx.signals.fx(d, ship_ccy[i]) if ship_ccy[i] != "USD" else 1.0)
        out[i] = usd * (ctx.signals.fx(d, bill[i]) if bill[i] != "USD" else 1.0)
    return np.round(out, 2)


def _billing_corrections(ctx: Ctx, st: dict[str, Any], fl: Flush, rng: np.random.Generator, t: int) -> None:
    """Billing analysts fix accruals before invoicing: delete duplicates, re-rate a few lines."""
    rec = st["billing_recent"]
    if len(rec) < 50:
        return
    k = int(rng.poisson(max(1.0, len(rec) * 0.0008)))
    if not k:
        return
    pick = rng.choice(len(rec), size=min(k, len(rec)), replace=False)
    dels, upd = [], []
    for i in pick:
        bid, _amount, rate, qty, _ = rec[i]
        if rng.random() < 0.5:
            dels.append(bid)
        else:
            new_rate = round(rate * float(rng.choice([0.9, 0.95, 1.05, 1.1])), 4)
            upd.append((bid, new_rate, round(new_rate * qty, 2)))
    for i in sorted(pick, reverse=True):
        rec.pop(int(i))
    ts = ts_col(np.array([t + 9 * 3600]))[0].astype("datetime64[us]").item()
    if dels:
        fl.execute("billing_record", "DELETE FROM wms.billing_record WHERE billing_id = ANY(%s) AND billing_status = 'ACCRUED'",
                   ([int(x) for x in dels],))
    if upd:
        fl.execute("billing_record",
                   "UPDATE wms.billing_record b SET rate = v.r, amount = v.a, mod_ts = %s FROM unnest(%s::bigint[], %s::numeric[], %s::numeric[]) "
                   "AS v(id, r, a) WHERE b.billing_id = v.id AND b.billing_status = 'ACCRUED'",
                   (ts, [int(u[0]) for u in upd], [u[1] for u in upd], [u[2] for u in upd]))


# =============================================================================================== offboarding / ship-out
def _ship_out(ctx: Ctx, st: dict[str, Any], W: _WinCtx, fl: Flush, info: dict[str, Any], t: int) -> None:
    """Remaining stock ships back to the client as TRANSFER orders (one per site); holds and damaged stock are destroyed."""
    inv = st["inv"]
    rng = W.rng(f"shipout-{info['code']}")
    st.setdefault("no_reorder", set()).add(info["owner_id"])
    a = st["asns"]
    open_asn = (a["owner_id"] == info["owner_id"]) & (a["receipt_id"] == 0)
    if open_asn.any():
        _delete_asns(st, fl, open_asn)
    for wi in info["whs"]:
        if wi not in st["wh_ready"]:
            continue
        m = (inv["owner_id"] == info["owner_id"]) & (inv["warehouse_id"] == wi + 1) & (inv["qty_on_hand"] > 0)
        bad = np.flatnonzero(m & (inv["inv_status"] != I_AVAIL))
        if len(bad):
            q = inv["qty_on_hand"][bad].astype(np.int64)
            _txn(ctx, st, fl, rng, wi, np.full(len(bad), t), inv["owner_id"][bad], inv["material_id"][bad], "DESTROY", -q,
                 from_loc=inv["location_id"][bad], lot=inv["lot_nbr"][bad], reason="OFFBOARD", ref_type="QA", ref_id=0)
            inv["qty_on_hand"][bad] = 0
            inv["mod_ts"][bad] = t
            mark_pos(inv, bad)
        good = np.flatnonzero(m & (inv["inv_status"] == I_AVAIL))
        if not len(good):
            continue
        q = pd.Series(inv["qty_on_hand"][good] - inv["qty_allocated"][good], index=inv["material_id"][good]).groupby(level=0).sum()
        q = q[q > 0]
        if q.empty:
            continue
        mat_idx = q.index.to_numpy(np.int64) - 1
        cities = geo.cities()
        hq = cities.index[(cities["country"] == info["country"])][0]
        tr = {"city": int(hq), "company": info["name"], "name": "Returns to client - Inbound", "a1": "Client DC",
              "postal": geo.postal_codes(rng, np.array([hq]))[0], "wi": wi, "mat_idx": mat_idx, "qty": q.to_numpy()}
        ci = 0
        utc = np.array([int(geo.local_to_utc(st["wh_tz"][wi], np.array([t]))[0])])
        _build_orders(ctx, st, W, fl, rng, [info], np.array([ci]), np.array([True]), utc, transfer=tr)


# =============================================================================================== open-table flush
def _flush_open(ctx: Ctx, st: dict[str, Any], fl: Flush, defer: bool = False) -> None:
    """Write the open tables. ``defer`` (build mode, not the last window): open orders, lines, tasks, unshipped
    shipments and the inventory index are written later in their final state - nobody reads mid-build states."""
    o = st["orders"]
    base = ["order_id", "order_nbr", "owner_id", "warehouse_id", "client_order_ref", "order_type", "channel", "order_status", "priority",
            "ordered_ts", "received_ts", "requested_ship_date", "carrier_service_id", "ship_to_name", "ship_to_company",
            "ship_to_address1", "ship_to_address2", "ship_to_city", "ship_to_state", "ship_to_postal", "ship_to_country",
            "ship_to_phone", "ship_to_email", "is_residential", "total_lines", "total_units", "cancel_reason", "create_ts",
            "mod_ts", "create_user", "mod_user"]
    if st["schema_v2"]:
        base = base + ["delivery_instructions"]
    upd = ["order_status", "priority", "requested_ship_date", "carrier_service_id", "ship_to_address1", "ship_to_address2",
           "ship_to_postal", "total_lines", "total_units", "cancel_reason", "mod_ts", "mod_user"]
    o_done = (o["order_status"] == O_SHIP) | (o["order_status"] == O_CANC)
    done_orders = o["order_id"][o_done]
    _sync(fl, "order_header", o, base, ("order_id",), upd, {"ordered_ts", "received_ts", "create_ts", "mod_ts"},
          {"requested_ship_date"}, {"order_status": ORD_ST}, None, o_done, defer=~o_done if defer else None)
    L = st["lines"]
    ins_l = L["_st"] == 1
    if defer:
        ins_l &= np.isin(L["order_id"], done_orders)
    if ins_l.any():
        df = _frame(L, ins_l, ["order_id", "line_nbr", "material_id", "ordered_qty", "allocated_qty", "picked_qty", "shipped_qty",
                               "uom", "unit_price", "line_status"], codes={"line_status": LINE_ST})
        orphan = ctx.dq.mask("orphan_fk", len(df), ctx.rng(NAME, ctx.window if getattr(ctx, "window", None) else Window(ctx.history_start, ctx.history_start), "orphan"))
        for i in np.flatnonzero(orphan):
            before = int(df.at[i, "material_id"])
            after = int(st["dq_orphans"][-1]) if st["dq_orphans"] else 9_000_000 + before
            df.at[i, "material_id"] = after
            ctx.dq.log("orphan_fk", "wms", "order_line", f"{df.at[i, 'order_id']}-{df.at[i, 'line_nbr']}", "material_id", before, after,
                       "line references an item master row that does not exist")
        fl.insert("order_line", df)
    upd_l = L["_st"] == 2
    if upd_l.any():
        fl.update("order_line", ("order_id", "line_nbr"), _frame(L, upd_l, ["order_id", "line_nbr", "allocated_qty", "picked_qty",
                                                                          "shipped_qty", "line_status"], codes={"line_status": LINE_ST}))
    L["_st"][(L["_st"] == 2) | ins_l] = 0
    tkeep(L, ~np.isin(L["order_id"], done_orders))
    tk = st["tasks"]
    tcols = ["task_id", "warehouse_id", "task_type", "task_status", "priority", "order_id", "order_line_nbr", "receipt_id", "material_id",
             "qty", "from_location_id", "to_location_id", "assigned_user_id", "created_ts", "assigned_ts", "started_ts", "completed_ts", "mod_ts"]
    nulls = {"order_id": 0, "order_line_nbr": 0, "receipt_id": 0, "material_id": 0, "from_location_id": 0, "to_location_id": 0,
             "assigned_user_id": 0}
    _sync(fl, "task", tk, tcols, ("task_id",), ["task_status", "assigned_user_id", "assigned_ts", "started_ts", "completed_ts", "mod_ts"],
          {"created_ts", "assigned_ts", "started_ts", "completed_ts", "mod_ts"}, set(), {"task_type": TASK_TYPE, "task_status": TASK_ST},
          nulls, (tk["task_status"] == 3) & tk["applied"], status_times=True,
          defer=~((tk["task_status"] == 3) & tk["applied"]) if defer else None)
    inv = st["inv"]
    icols = ["inventory_id", "warehouse_id", "location_id", "lpn_id", "owner_id", "material_id", "lot_nbr", "expiry_date", "qty_on_hand",
             "qty_allocated", "inv_status", "hold_reason", "received_date", "create_ts", "mod_ts"]
    drop_new = (inv["_st"] == 1) & (inv["qty_on_hand"] <= 0) & (inv["qty_allocated"] <= 0)
    if drop_new.any():
        A = st["allocs"]
        drop_new &= ~np.isin(inv["inventory_id"], A["stage_inv"]) & ~np.isin(inv["inventory_id"], A["inv_id"])
        tkeep(inv, ~drop_new)
    ins = (inv["_st"] == 1) & (not defer)
    if ins.any():
        df = _frame(inv, ins, icols, {"create_ts", "mod_ts"}, {"expiry_date", "received_date"}, {"inv_status": INV_ST}, {"lpn_id": 0})
        sent = ctx.dq.mask("sentinel_date", len(df), np.random.default_rng(int(inv["inventory_id"][ins][0]) % 2**31))
        for i in np.flatnonzero(sent & df["expiry_date"].isna().to_numpy()):
            df.at[i, "expiry_date"] = "9999-12-31"
            ctx.dq.log("sentinel_date", "wms", "inventory", str(df.at[i, "inventory_id"]), "expiry_date", None, "9999-12-31",
                       "non-lot item keyed with a never-expires sentinel")
        fl.insert("inventory", df)
    upd = inv["_st"] == 2
    if upd.any():
        fl.update("inventory", ("inventory_id",), _frame(inv, upd, ["inventory_id", "location_id", "qty_on_hand", "qty_allocated",
                                                                  "inv_status", "hold_reason", "mod_ts"], {"mod_ts"}, set(), {"inv_status": INV_ST}))
    inv["_st"][(inv["_st"] == 2) | ins] = 0
    s = st["ships"]
    _sync(fl, "shipment", s, ["shipment_id", "shipment_nbr", "order_id", "warehouse_id", "carrier_service_id", "scac", "tracking_nbr",
                              "bol_nbr", "ship_ts", "shipment_status", "total_weight", "weight_uom", "carton_count",
                              "freight_charge_est", "currency", "create_ts", "mod_ts"], ("shipment_id",),
          ["tracking_nbr", "ship_ts", "shipment_status", "total_weight", "mod_ts"], {"ship_ts", "create_ts", "mod_ts"}, set(),
          {"shipment_status": SHIP_ST}, None, s["shipment_status"] >= 2, defer=(s["shipment_status"] < 2) if defer else None)
    c = st["cartons"]
    _sync(fl, "shipment_carton", c, ["carton_id", "shipment_id", "carton_nbr", "tracking_nbr", "weight", "length", "width", "height",
                                     "dim_uom", "create_ts", "mod_ts"], ("carton_id",), ["weight", "mod_ts"], {"create_ts", "mod_ts"},
          set(), None, None, ~np.isin(c["shipment_id"], s["shipment_id"]),
          defer=np.isin(c["shipment_id"], s["shipment_id"]) if defer else None)
    a = st["asns"]
    _sync(fl, "asn_header", a, ["asn_id", "asn_nbr", "owner_id", "warehouse_id", "supplier_name", "source", "expected_date", "asn_status",
                                "create_ts", "mod_ts"], ("asn_id",), ["asn_status", "mod_ts"], {"create_ts", "mod_ts"}, {"expected_date"},
          None, None, None)
    rc = st["rcpts"]
    _sync(fl, "receipt_header", rc, ["receipt_id", "receipt_nbr", "asn_id", "owner_id", "warehouse_id", "dock_door", "trailer_nbr",
                                     "arrived_ts", "started_ts", "completed_ts", "receipt_status", "create_ts", "mod_ts"], ("receipt_id",),
          ["started_ts", "completed_ts", "receipt_status", "mod_ts"], {"arrived_ts", "started_ts", "completed_ts", "create_ts", "mod_ts"},
          set(), None, {"asn_id": 0}, rc["receipt_status"] == "COMPLETE", status_times=True, status_col="receipt_status",
          hide={"started_ts": "RECEIVING", "completed_ts": "COMPLETE"})
    lp = st["lpns"]
    _sync(fl, "license_plate", lp, ["lpn_id", "lpn_nbr", "warehouse_id", "location_id", "lpn_type", "lpn_status", "create_ts", "mod_ts"],
          ("lpn_id",), ["location_id", "lpn_status", "mod_ts"], {"create_ts", "mod_ts"}, set(), None, None, lp["lpn_status"] == "STORED")
    cc = st["counts"]
    _sync(fl, "cycle_count", cc, ["count_id", "warehouse_id", "location_id", "material_id", "lot_nbr", "system_qty", "counted_qty",
                                  "variance_qty", "count_ts", "counted_by", "count_status", "approved_by", "create_ts", "mod_ts"],
          ("count_id",), ["count_status", "approved_by", "mod_ts"], {"count_ts", "create_ts", "mod_ts"}, set(), None,
          {"counted_by": 0, "approved_by": 0}, None)
    r = st["rets"]
    _sync(fl, "return_header", r, ["return_id", "rma_nbr", "owner_id", "warehouse_id", "original_order_id", "carrier_tracking_nbr",
                                   "received_ts", "return_status", "create_ts", "mod_ts"], ("return_id",),
          ["received_ts", "return_status", "mod_ts"], {"received_ts", "create_ts", "mod_ts"}, set(), None, None, None)


def _sync(fl: Flush, table: str, t: dict[str, np.ndarray], ins_cols: list[str], keys: tuple[str, ...], upd_cols: list[str],
          tcols: set[str], dcols: set[str], codes: dict[str, np.ndarray] | None, nulls: dict[str, int] | None,
          evict: np.ndarray | None, status_times: bool = False, status_col: str | None = None,
          hide: dict[str, str] | None = None, defer: np.ndarray | None = None) -> None:
    """Insert new rows, update dirty rows, then evict finished rows from memory. Rows in ``defer`` (build mode:
    still open) stay new in memory and are written once, in their final state."""
    st_ = t["_st"]
    view = t
    if status_times and table == "task":
        # timestamps of steps that have not happened yet are NULL in the database
        view = dict(t)
        s = t["task_status"]
        view["assigned_ts"] = np.where(s >= 1, t["assigned_ts"], T_NONE)
        view["started_ts"] = np.where(s >= 2, t["started_ts"], T_NONE)
        view["completed_ts"] = np.where(s >= 3, t["completed_ts"], T_NONE)
        view["assigned_user_id"] = np.where(s >= 1, t["assigned_user_id"], 0)
    if hide:
        view = dict(view)
        order = {"ARRIVED": 0, "RECEIVING": 1, "COMPLETE": 2}
        lvl = np.array([order.get(x, 0) for x in t[status_col]])
        for col, need in hide.items():
            view[col] = np.where(lvl >= order[need], t[col], T_NONE)
    new = st_ == 1
    if defer is not None:
        new &= ~defer
    if new.any():
        fl.insert(table, _frame(view, new, ins_cols, tcols, dcols, codes, nulls))
    dirty = st_ == 2
    if dirty.any():
        fl.update(table, keys, _frame(view, dirty, list(keys) + upd_cols, tcols, dcols, codes, nulls))
    st_[(st_ == 2) | new] = 0
    if evict is not None and evict.any():
        tkeep(t, ~evict)


# =============================================================================================== scenarios
def _scenario(ctx: Ctx, st: dict[str, Any], sid: str, p: dict[str, Any], win: Window) -> str | None:
    whi = _wh_index(ctx)
    now = int(win.start.timestamp())
    fl = Flush()
    msg: str | None = None
    if sid == "peak_surge":
        wh = None if p.get("warehouse", "ALL") == "ALL" else whi[p["warehouse"]]
        d0 = geo.epoch_day(win.start.date())
        st["surges"].append((wh, d0, d0 + int(p["days"]), float(p["multiplier"])))
        msg = f"order intake x{p['multiplier']} for {p['days']} days at {p.get('warehouse', 'ALL')}"
    elif sid == "storm":
        wi = whi[p["warehouse"]]
        days = int(p["days"])
        sev = p.get("severity", "severe")
        loc0 = int(geo.utc_to_local(st["wh_tz"][wi], np.array([now]))[0])
        st["blackouts"].append((wi, loc0, loc0 + days * DAY if sev == "severe" else loc0 + DAY))
        st["slowdowns"].append((wi, loc0 // DAY, loc0 // DAY + days, 2.2 if sev == "severe" else 1.5))
        a = st["asns"]
        late = (a["wi"] == wi) & (a["receipt_id"] == 0) & (a["t_arrive"] < loc0 + days * DAY)
        a["t_arrive"][late] = loc0 + days * DAY + np.random.default_rng(now % 2**31).integers(3600, 10 * 3600, int(late.sum()))
        o = st["orders"]
        held = (o["wi"] == wi) & (o["order_status"] == O_PACK) & (o["t_ship"] < loc0 + days * DAY)
        if held.any():
            o["t_ship"][held] = _next_pickup(ctx, st, np.full(int(held.sum()), wi), o["t_ship"][held], o["is_ltl"][held],
                                             np.full(int(held.sum()), "", dtype=object))
        msg = f"{p['warehouse']}: no carrier pickups for {days if sev == 'severe' else 1} day(s), slower picking, {int(late.sum())} inbound trailers late"
    elif sid == "rate_increase":
        info = st["owners"].get(p["client"])
        if info is None:
            return None
        keyed = p.get("keyed_in_wms", "on_time")
        if keyed == "never":
            msg = f"{p['client']}: +{p['pct']}% NOT keyed into WMS billing_rate"
        else:
            local_now = int(geo.utc_to_local(st["wh_tz"][info["primary"]], np.array([now]))[0])
            day = local_now // DAY + (int(p.get("delay_days", 21)) if keyed == "late" else 0)
            ev = {"code": info["code"], "day": day, "eff": local_now // DAY, "pct": float(p["pct"]), "kind": "increase"}
            if keyed == "on_time":
                ev["t"] = local_now + 600
            st["rate_events"].append(ev)
            msg = f"{p['client']}: +{p['pct']}% keyed into WMS billing_rate on {geo.day_date(day)}"
    elif sid == "onboard_client":
        client = p.get("client")
        if client:
            c = {**client, "_idx": next(i for i, x in enumerate(ctx.rules["clients"]) if x["code"] == client["code"])}
            present = {w["code"] for w in _preset_whs(ctx)}
            c["warehouses"] = [w for w in c["warehouses"] if w in present] or [_preset_whs(ctx)[0]["code"]]
            info = _client_info(ctx, st, c)
            wi = info["primary"]
            t = int(geo.utc_to_local(st["wh_tz"][wi], np.array([now]))[0])
            info["wms_created"] = t // DAY
            _create_owner(ctx, st, fl, info, t, at_boot=False)
            _init_ema(ctx, st, info, t // DAY)
            n_mat = int((st["mat"]["owner_id"] == info["owner_id"]).sum())
            msg = f"owner {info['code']} created (ONBOARDING, go-live {geo.day_date(info['go_live'])}) with {n_mat} SKUs"
    elif sid == "offboard_client":
        info = st["owners"].get(p["client"])
        if info is None:
            return None
        end = date.fromisoformat(p["end_date"])
        info["churn"] = geo.epoch_day(end)
        st.setdefault("churn_override", {})[info["code"]] = end
        wi = info["primary"]
        t = int(geo.utc_to_local(st["wh_tz"][wi], np.array([now]))[0])
        if info["status"] == "ACTIVE":
            _set_owner_status(fl, info, "OFFBOARDING", t)
        if int(p.get("notice_days", 30)) == 0:
            info["shipped_out"] = True
            W = _WinCtx(ctx, st, win)
            _ship_out(ctx, st, W, fl, info, t)
        msg = f"{info['code']} OFFBOARDING; stock ships out on {end}, owner INACTIVE ~12 days later"
    elif sid == "schema_drift_release" and p.get("systems", "all") in ("all", "wms"):
        if not st["schema_v2"]:
            conn = ctx.conn("wms")
            conn.execute("ALTER TABLE wms.order_header ADD COLUMN IF NOT EXISTS delivery_instructions varchar(200)")
            rel = ctx.next_ids("wms.sys_release", 1, start=2)[0]
            t = int(geo.utc_to_local(st["wh_tz"][0], np.array([now]))[0])
            fl.insert("sys_release", pd.DataFrame([{"release_id": int(rel), "version": "7.5.0", "released_ts": ts_col(np.array([t]))[0],
                                                    "notes": "Storefront checkout notes: adds order_header.delivery_instructions."}]))
            st["schema_v2"] = True
            msg = "release 7.5.0: wms.order_header.delivery_instructions added"
    elif sid == "late_data_dump":
        return None
    fl.run(ctx)
    for key, df in st["pending_bus"]:
        ctx.bus.publish(key, df)
    st["pending_bus"] = []
    return msg


MODULE = WmsModule()
