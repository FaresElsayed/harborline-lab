"""Carrier aggregator: service selection, tracking numbers, freight estimates and tracking events.

The WMS module calls the pure helpers (service choice, tracking numbers, freight). The ``MODULE`` step turns
every package the WMS shipped in the window into a tracking-event timeline in ``sim_internal.carrier.tracking_event``
(``received_at`` = when the aggregator API exposes it; the API never shows rows with received_at > sim clock) and
moves ``wms.shipment`` to DELIVERED / EXCEPTION when the final event becomes visible.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from ..core import Ctx, Window
from . import geo

NAME = "carrier"
EV_SCHEMA = "carrier.tracking_event"
T_NONE = np.int64(2**62)

# Service codes used in UPS 1Z numbers.
_UPS_SVC = {"GND": "03", "3DS": "12", "2DA": "02", "NDA": "01"}
# Per-shipper 6-character account codes printed in UPS numbers (one per origin site).
_UPS_SHIPPER = {"ATL1": "4V81E2", "DFW1": "A2R417", "RNO1": "7W9X03", "CHI1": "2F6Y88", "NJ1": "E10457",
                "LAX1": "9AY215", "TOR1": "X3V605", "MTY1": "R8W330"}
# Sorting hubs used for IN_TRANSIT scans (city, state, country).
_HUBS = {
    "UPSN": [("Louisville", "KY", "US"), ("Dallas", "TX", "US"), ("Ontario", "CA", "US"), ("Atlanta", "GA", "US"),
             ("Chicago", "IL", "US"), ("Newark", "NJ", "US"), ("Mississauga", "ON", "CA")],
    "FDEG": [("Memphis", "TN", "US"), ("Dallas", "TX", "US"), ("Sacramento", "CA", "US"), ("Indianapolis", "IN", "US"),
             ("Edison", "NJ", "US"), ("Brampton", "ON", "CA")],
    "FDE": [("Memphis", "TN", "US"), ("Indianapolis", "IN", "US"), ("Ciudad de México", "CDMX", "MX")],
    "USPS": [("Atlanta", "GA", "US"), ("Dallas", "TX", "US"), ("Los Angeles", "CA", "US"), ("Chicago", "IL", "US"),
             ("Philadelphia", "PA", "US"), ("Denver", "CO", "US")],
    "DHLC": [("Atlanta", "GA", "US"), ("Dallas", "TX", "US"), ("Los Angeles", "CA", "US"), ("Chicago", "IL", "US")],
    "VRPX": [("Atlanta", "GA", "US"), ("Dallas", "TX", "US"), ("Reno", "NV", "US"), ("Riverside", "CA", "US")],
    "CNPC": [("Mississauga", "ON", "CA"), ("Montréal", "QC", "CA"), ("Calgary", "AB", "CA"), ("Richmond", "BC", "CA")],
    "PURO": [("Mississauga", "ON", "CA"), ("Laval", "QC", "CA"), ("Edmonton", "AB", "CA"), ("Burnaby", "BC", "CA")],
    "ESTF": [("Ciudad de México", "CDMX", "MX"), ("Guadalajara", "JAL", "MX"), ("Monterrey", "NL", "MX"), ("Querétaro", "QRO", "MX")],
    "ODFL": [("Atlanta", "GA", "US"), ("Dallas", "TX", "US"), ("Chicago", "IL", "US"), ("Columbus", "OH", "US"), ("Fontana", "CA", "US")],
    "MRLF": [("Atlanta", "GA", "US"), ("Houston", "TX", "US"), ("Chicago", "IL", "US"), ("Phoenix", "AZ", "US")],
    "EXLA": [("Richmond", "VA", "US"), ("Charlotte", "NC", "US"), ("Dallas", "TX", "US"), ("Chicago", "IL", "US")],
    "TRMX": [("Monterrey", "NL", "MX"), ("Ciudad de México", "CDMX", "MX"), ("Guadalajara", "JAL", "MX")],
}
_EXC_CODES = np.array(["WEATHER", "ADDRESS", "DAMAGED", "REFUSED", "CARRIER_DELAY"], dtype=object)


# --------------------------------------------------------------------------------------------- services
def services(rules: dict[str, Any]) -> pd.DataFrame:
    """One row per carrier service with stable ids (carrier_id / service_id follow the order in the rules)."""
    rows = []
    sid = 0
    for cid, c in enumerate(rules["carriers"], start=1):
        for s in c["services"]:
            sid += 1
            rows.append({"service_id": sid, "carrier_id": cid, "scac": c["scac"], "carrier_name": c["name"], "mode": c["mode"],
                         "service_code": s["code"], "service_name": s["name"], "tmin": s["transit"][0], "tmax": s["transit"][1],
                         "countries": tuple(c["countries"]), "regions": tuple(c.get("regions", ())),
                         "fmt": c["tracking_format"]})
    return pd.DataFrame(rows)


# Candidate (scac, service, weight) per lane. Weights are market shares for B2C parcel traffic.
_LANES: dict[tuple[str, str, str], list[tuple[str, str, float]]] = {
    ("US", "US", "PARCEL"): [("UPSN", "GND", 0.30), ("FDEG", "FHD", 0.22), ("USPS", "UGA", 0.16), ("USPS", "UPM", 0.04),
                             ("DHLC", "DPE", 0.08), ("VRPX", "VRG", 0.08), ("UPSN", "2DA", 0.04), ("FDE", "F2D", 0.03),
                             ("UPSN", "3DS", 0.02), ("UPSN", "NDA", 0.015), ("FDE", "FPO", 0.015)],
    ("US", "CA", "PARCEL"): [("UPSN", "GND", 0.6), ("FDEG", "FXG", 0.4)],
    ("CA", "CA", "PARCEL"): [("CNPC", "CEX", 0.42), ("PURO", "PGR", 0.30), ("UPSN", "GND", 0.12), ("FDEG", "FXG", 0.08),
                             ("CNPC", "CXP", 0.05), ("PURO", "PEX", 0.03)],
    ("CA", "US", "PARCEL"): [("UPSN", "GND", 0.55), ("FDEG", "FXG", 0.45)],
    ("MX", "MX", "PARCEL"): [("ESTF", "ETR", 0.72), ("ESTF", "EDS", 0.10), ("FDE", "F2D", 0.18)],
    ("MX", "US", "PARCEL"): [("FDE", "F2D", 0.8), ("FDE", "FPO", 0.2)],
    ("US", "MX", "PARCEL"): [("FDE", "F2D", 1.0)],
    ("CA", "MX", "PARCEL"): [("FDE", "F2D", 1.0)],
    ("MX", "CA", "PARCEL"): [("FDE", "F2D", 1.0)],
    ("US", "US", "LTL"): [("ODFL", "LTL", 0.4), ("MRLF", "LTL", 0.3), ("EXLA", "LTL", 0.3)],
    ("US", "CA", "LTL"): [("ODFL", "LTL", 1.0)],
    ("CA", "CA", "LTL"): [("ODFL", "LTL", 1.0)],
    ("CA", "US", "LTL"): [("ODFL", "LTL", 1.0)],
    ("MX", "MX", "LTL"): [("TRMX", "LTL", 1.0)],
    ("MX", "US", "LTL"): [("TRMX", "LTL", 1.0)],
    ("US", "MX", "LTL"): [("TRMX", "LTL", 1.0)],
    ("CA", "MX", "LTL"): [("TRMX", "LTL", 1.0)],
    ("MX", "CA", "LTL"): [("ODFL", "LTL", 1.0)],
}


def choose_services(rng: np.random.Generator, svc: pd.DataFrame, origin_country: np.ndarray, origin_state: np.ndarray,
                    dest_country: np.ndarray, dest_state: np.ndarray, is_ltl: np.ndarray, est_lb: np.ndarray,
                    b2b: np.ndarray) -> np.ndarray:
    """service_id per shipment: lane, weight (USPS / DHL eCommerce only for light parcels), regional carrier only
    when origin and destination are in its footprint, B2B parcel mostly ground."""
    key_of = {(r.scac, r.service_code): r.service_id for r in svc.itertuples()}
    regions = {r.scac: set(r.regions) for r in svc.itertuples() if r.regions}
    out = np.zeros(len(origin_country), dtype=np.int16)
    mode = np.where(is_ltl, "LTL", "PARCEL")
    lane = pd.Series(list(zip(origin_country, dest_country, mode)))
    u = rng.random(len(out))
    for k, idx in lane.groupby(lane).groups.items():
        idx = np.asarray(idx)
        cands = _LANES.get(k) or _LANES[("US", "US", k[2])]
        for i in idx:   # per row: weights depend on weight / state / b2b (cheap; tens per window per lane)
            ws = []
            for scac, code, w in cands:
                if scac in ("USPS", "DHLC") and est_lb[i] > (3.0 if scac == "USPS" and code == "UGA" else 10.0):
                    w = 0.0
                if scac in regions and not (origin_state[i] in regions[scac] and dest_state[i] in regions[scac]):
                    w = 0.0
                if b2b[i] and code not in ("GND", "FXG", "FHD", "LTL", "CEX", "PGR", "ETR", "F2D"):
                    w *= 0.1
                if b2b[i] and code == "FHD":
                    w = 0.0
                ws.append(w)
            ws_arr = np.asarray(ws)
            if ws_arr.sum() <= 0:
                ws_arr = np.ones(len(cands))
            cum = np.cumsum(ws_arr) / ws_arr.sum()
            j = int(np.searchsorted(cum, u[i], side="right"))
            scac, code, _ = cands[min(j, len(cands) - 1)]
            out[i] = key_of[(scac, code)]
    return out


# --------------------------------------------------------------------------------------------- tracking numbers
def _digits(x: np.ndarray, width: int) -> np.ndarray:
    return np.char.zfill(x.astype(np.int64).astype(str), width)


def _scramble(counter: np.ndarray, modulus: int, salt: int) -> np.ndarray:
    """Bijective, random-looking map of a counter into [0, modulus) so numbers never repeat."""
    a = 6364136223846793 % modulus
    while np.gcd(a, modulus) != 1:
        a += 1
    return (counter.astype(object) * a + salt) % modulus


def _ups_check(body: str) -> str:
    total = 0
    for i, ch in enumerate(body):
        v = int(ch) if ch.isdigit() else (ord(ch) - 63) % 10
        total += v * (2 if i % 2 else 1)
    return str((10 - total % 10) % 10)


def _mod10_usps(body: str) -> str:
    total = 0
    for i, ch in enumerate(reversed(body)):
        total += int(ch) * (3 if i % 2 == 0 else 1)
    return str((10 - total % 10) % 10)


def tracking_numbers(fmt: np.ndarray, service_code: np.ndarray, wh_code: np.ndarray, counter: np.ndarray) -> np.ndarray:
    """Carrier-format tracking numbers (spec 4.2). ``counter`` must be unique per call (global sequence)."""
    n = len(fmt)
    out = np.empty(n, dtype=object)
    c = np.asarray(counter, dtype=np.int64)
    for f in np.unique(fmt):
        m = fmt == f
        k = c[m]
        if f == "ups":
            body = _digits(np.asarray(_scramble(k, 10**7, 1_234_567), dtype=np.int64), 7)
            ship = np.array([_UPS_SHIPPER.get(w, "4V81E2") for w in wh_code[m]])
            svc = np.array([_UPS_SVC.get(s, "03") for s in service_code[m]])
            out[m] = [f"1Z{a}{b}{d}{_ups_check(a + b + d)}" for a, b, d in zip(ship, svc, body)]
        elif f == "fedex":
            out[m] = list(_digits(np.asarray(_scramble(k, 10**11, 77_123_456_789), dtype=np.int64) + 10**11 * 7, 12))
        elif f == "usps":
            pre = np.where(k % 3 == 0, "9205", "9400")
            mid = _digits(np.asarray(_scramble(k, 10**17, 1_100_220_330_440_550), dtype=np.int64), 17)
            out[m] = [f"{p}{b}{_mod10_usps(p + b)}" for p, b in zip(pre, mid)]
        elif f == "dhl":
            out[m] = ["GM" + s for s in _digits(np.asarray(_scramble(k, 10**16, 5_550_001_234_567), dtype=np.int64), 16)]
        elif f == "vrpx":
            out[m] = ["VR" + s for s in _digits(np.asarray(_scramble(k, 10**12, 410_200_300_400), dtype=np.int64), 12)]
        elif f == "canadapost":
            out[m] = list(_digits(np.asarray(_scramble(k, 10**15, 731_000_444_222_111), dtype=np.int64) + 10**15 * 4, 16))
        elif f == "purolator":
            out[m] = list(_digits(np.asarray(_scramble(k, 10**11, 33_100_200_300), dtype=np.int64) + 3 * 10**11, 12))
        elif f == "estafeta":
            alnum = np.array(list("0123456789ABCDEFGHJKLMNPRSTUVWXYZ"))
            v = np.asarray(_scramble(k, 33**12, 98_765_432_101), dtype=np.int64)
            chars = []
            for x in v:
                s = []
                for _ in range(12):
                    x, r = divmod(int(x), 33)
                    s.append(alnum[r])
                chars.append("".join(s))
            out[m] = [f"{8055 + (int(x) % 900):04d}{y}{int(x) % 10**6:06d}" for x, y in zip(k, chars)]
        else:   # LTL PRO numbers, 9-11 digits
            width = np.where(k % 3 == 0, 9, np.where(k % 3 == 1, 10, 11))
            v = np.asarray(_scramble(k, 10**9, 123_456_789), dtype=np.int64)
            out[m] = [f"{int(x):0{w}d}" if w == 9 else f"{int(x) + 10**9 * (1 + int(x) % 8):0{w}d}" for x, w in zip(v, width)]
    return out


# --------------------------------------------------------------------------------------------- freight
def billed_weight_lb(actual_lb: np.ndarray, dims_in: np.ndarray, is_ltl: np.ndarray) -> np.ndarray:
    """Parcel billed weight = max(actual, L x W x H / 139) rounded up to the next lb; LTL = actual."""
    dim = dims_in.prod(axis=1) / 139.0 if dims_in.ndim == 2 else dims_in / 139.0
    parcel = np.ceil(np.maximum(actual_lb, dim))
    return np.where(is_ltl, np.round(actual_lb, 1), np.maximum(parcel, 1.0))


def freight_estimate_usd(svc_code: np.ndarray, is_ltl: np.ndarray, billed_lb: np.ndarray, zone: np.ndarray,
                         residential: np.ndarray, diesel: float) -> np.ndarray:
    """Rate-shop estimate the WMS stores on the shipment (USD)."""
    z = zone.astype(float)
    ground = 7.9 + 0.62 * billed_lb * (1 + 0.11 * (z - 2)) + 0.45 * (z - 2)
    mult = np.ones(len(svc_code))
    for codes, f in ((("2DA", "F2D", "UPM", "CXP", "EDS"), 2.2), (("NDA", "FPO", "PEX"), 3.6), (("3DS",), 1.6),
                     (("UGA",), 0.82), (("DPE",), 0.78), (("VRG",), 0.74)):
        mult = np.where(np.isin(svc_code, codes), f, mult)
    parcel = ground * mult + np.where(residential, 5.55, 0.0)
    fuel = 1 + max(0.0, (diesel - 3.0) * 0.065 + 0.08)
    cwt = np.maximum(billed_lb, 150) / 100.0
    ltl = np.maximum(165.0, cwt * (24.0 + 3.2 * (z - 2)))
    return np.round(np.where(is_ltl, ltl, parcel) * fuel, 2)


def transit_plan(rng: np.random.Generator, tmin: np.ndarray, tmax: np.ndarray, zone: np.ndarray) -> np.ndarray:
    """Business days in transit before delays: ground services scale with zone, express services are fixed."""
    ground = tmax > tmin
    est = np.clip(zone.astype(int) - 2 + (rng.random(len(zone)) < 0.35), tmin, tmax)
    return np.where(ground, est, tmax).astype(np.int16)


# --------------------------------------------------------------------------------------------- module
class CarrierModule:
    name = NAME

    def bootstrap(self, ctx: Ctx) -> None:
        ctx.world.setdefault(NAME, _empty_state())

    def step(self, ctx: Ctx, win: Window) -> None:
        st = ctx.world.setdefault(NAME, _empty_state())
        rng = ctx.rng(NAME, win, "events")
        ship = ctx.bus.get("wms_ship_ctx")
        voided = ctx.bus.get("wms_voided_labels")
        if len(ship):
            _events_for_shipments(ctx, st, win, ship, rng)
        if len(voided):
            _label_only_events(ctx, st, voided)
        _apply_visible_outcomes(ctx, st, win)

    def apply_scenario(self, ctx: Ctx, scenario_id: str, params: dict[str, Any], win: Window) -> str | None:
        st = ctx.world.setdefault(NAME, _empty_state())
        now = int(win.start.timestamp())
        if scenario_id == "carrier_outage":
            scac, hours = params["scac"], int(params["hours"])
            end = now + hours * 3600
            st["outages"].append((scac, now, end))
            n = _delay_feed(ctx, st, scac, now, end)
            return f"{scac} tracking feed down until {_iso(end)}; {n} queued events will arrive in one burst"
        if scenario_id == "storm":
            wh = next(w for w in ctx.rules["warehouses"] if w["code"] == params["warehouse"])
            days = int(params["days"])
            sev = 2.0 if params.get("severity") == "severe" else 1.0
            st["storms"].append((wh["address"]["lat"], wh["address"]["lon"], now, now + days * 86400, sev))
            n = _storm_delay_in_flight(ctx, st, wh, now, days, sev)
            return f"weather exceptions and +1-3 day delays within ~400 mi of {wh['code']}; {n} in-flight packages delayed"
        if scenario_id == "late_data_dump" and params.get("source", "all") in ("all", "carrier"):
            n = _late_scan_dump(ctx, st, win, int(params.get("hours_late", 36)))
            return f"aggregator flushed {n} buffered hub scans up to {params.get('hours_late', 36)} h old"
        return None


def _empty_state() -> dict[str, Any]:
    return {"pending": {k: np.zeros(0, dtype=t) for k, t in _PENDING_DTYPES.items()}, "outages": [], "storms": []}


_PENDING_DTYPES = {"shipment_id": np.int64, "visible": np.int64, "stage": np.int8, "delivered": np.int64,
                   "promised_day": np.int64, "is_late": bool, "exc": object, "tracking_nbr": object,
                   "order_nbr": object, "owner_code": object, "scac": object, "tz": object, "exc_visible": np.int64}


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%d %H:%M UTC")


def _city_arrays() -> tuple[np.ndarray, ...]:
    c = geo.cities()
    return (c["lat"].to_numpy(float), c["lon"].to_numpy(float), c["tz"].to_numpy(object), c["city"].to_numpy(object),
            c["state"].to_numpy(object), c["country"].to_numpy(object))


def _hub_index() -> dict[str, np.ndarray]:
    out = {}
    for scac, hubs in _HUBS.items():
        out[scac] = np.array([geo.city_index(*h) for h in hubs], dtype=np.int64)
    return out


_HUB_IDX: dict[str, np.ndarray] = {}


def _business_days_after(day: np.ndarray, n: np.ndarray, sat_ok: np.ndarray) -> np.ndarray:
    """Epoch day after adding n delivery days, skipping Sundays (and Saturdays unless sat_ok)."""
    d = day.astype(np.int64).copy()
    left = n.astype(np.int64).copy()
    while (left > 0).any():
        m = left > 0
        d[m] += 1
        dow = (d + 3) % 7          # 1970-01-01 was a Thursday -> Monday = 0
        good = (dow < 5) | ((dow == 5) & sat_ok)
        left[m & good] -= 1
    return d


def _events_for_shipments(ctx: Ctx, st: dict[str, Any], win: Window, s: pd.DataFrame, rng: np.random.Generator) -> None:
    global _HUB_IDX
    if not _HUB_IDX:
        _HUB_IDX = _hub_index()
    lat, lon, tzs, cname, cstate, cctry = _city_arrays()
    n = len(s)
    scac = s["scac"].to_numpy(object)
    svc = s["service_code"].to_numpy(object)
    ltl = s["is_ltl"].to_numpy(bool)
    origin = s["origin_city_idx"].to_numpy(np.int64)
    dest = s["dest_city_idx"].to_numpy(np.int64)
    ship_utc = s["ship_utc"].to_numpy(np.int64)
    label_utc = s["label_utc"].to_numpy(np.int64)
    transit = s["transit_days"].to_numpy(np.int64)
    wh_lat = lat[origin]
    # ---- delays and exceptions
    dest_lat, dest_lon = lat[dest], lon[dest]
    ship_day_local = (geo.utc_to_local("America/Chicago", ship_utc) // 86400)
    delay = np.zeros(n, dtype=np.int64)
    exc = np.full(n, None, dtype=object)
    month = pd.to_datetime(ship_day_local * 86400, unit="s").month.to_numpy()
    dom = pd.to_datetime(ship_day_local * 86400, unit="s").day.to_numpy()
    peak = (month == 12) & (dom <= 23)
    delay += (peak & (rng.random(n) < 0.18)).astype(int)
    winter = np.isin(month, (12, 1, 2, 3))
    cold_dest = winter & (dest_lat > 38.5) & (rng.random(n) < 0.06)
    delay += cold_dest.astype(int)
    wx_origin = np.zeros(n, dtype=bool)
    for code in np.unique(s["warehouse_code"].to_numpy(object)):
        m = s["warehouse_code"].to_numpy(object) == code
        days = np.unique(ship_day_local[m])
        for d in days:
            wx = ctx.signals.weather(code, date(1970, 1, 1) + timedelta(days=int(d)))
            if wx.get("snow_cm", 0) >= 4:
                mm = m & (ship_day_local == d)
                wx_origin[mm] = rng.random(int(mm.sum())) < min(0.9, 0.25 + wx["snow_cm"] / 30)
    delay += wx_origin.astype(int)
    for (slat, slon, t0, t1, sev) in st["storms"]:
        near = (geo.haversine_mi(dest_lat, dest_lon, slat, slon) < 400) | (geo.haversine_mi(wh_lat, lon[origin], slat, slon) < 400)
        hit = near & (ship_utc >= t0 - 86400) & (ship_utc < t1)
        delay += np.where(hit, rng.integers(1, 2 + int(sev), n), 0)
        exc[hit & (rng.random(n) < 0.3 * sev)] = "WEATHER"
    u = rng.random(n)
    exc_none = np.array([e is None for e in exc])
    pick = exc_none & (u < 0.026)
    codes = rng.choice(_EXC_CODES, size=n, p=[0.18, 0.32, 0.12, 0.08, 0.30])
    codes = np.where(cold_dest | wx_origin, "WEATHER", codes)
    exc[pick] = codes[pick]
    rts = np.isin(exc, ["REFUSED"]) | ((exc == "DAMAGED") & (rng.random(n) < 0.5))
    exc_delay = np.select([exc == "ADDRESS", exc == "WEATHER", exc == "CARRIER_DELAY", exc == "DAMAGED"],
                          [rng.integers(1, 3, n), rng.integers(1, 4, n), np.ones(n, int), rng.integers(1, 3, n)], 0)
    sat_ok = np.isin(svc, ["UGA", "UPM", "FHD"])
    ship_local_dest = geo.utc_to_local_many(tzs[dest], ship_utc)
    ship_day_dest = ship_local_dest // 86400
    promised = _business_days_after(ship_day_dest, np.maximum(transit, 1), sat_ok)
    deliver_day = _business_days_after(ship_day_dest, np.maximum(transit, 1) + delay + exc_delay, sat_ok)
    is_late = deliver_day > promised
    # ---- event timeline (local seconds at the event city, then UTC)
    rows: list[pd.DataFrame] = []
    trk = s["tracking_nbr"].to_numpy(object)

    def add(mask: np.ndarray, code: str, city_idx: np.ndarray, local_s: np.ndarray, desc: str,
            exc_code: np.ndarray | None = None) -> None:
        if not mask.any():
            return
        ci = city_idx[mask]
        ls = local_s[mask]
        tz = tzs[ci]
        utc = geo.local_to_utc_many(tz, ls)
        off = utc_off = ls - utc
        rows.append(pd.DataFrame({
            "tracking_nbr": trk[mask], "scac": scac[mask], "event_code": code, "event_utc": utc,
            "event_local_ts": geo.iso_with_offset(ls, utc_off), "city": cname[ci], "state_prov": cstate[ci],
            "country_code": cctry[ci], "description": desc,
            "exception_code": exc_code[mask] if exc_code is not None else None, "_off": off,
        }))

    all_rows = np.ones(n, dtype=bool)
    o_local_label = geo.utc_to_local_many(tzs[origin], label_utc)
    add(all_rows, "LABEL_CREATED", origin, o_local_label, "Shipping label created, carrier awaiting item")
    o_local_ship = geo.utc_to_local_many(tzs[origin], ship_utc) + rng.integers(300, 2400, n)
    add(all_rows, "PICKED_UP", origin, o_local_ship, "Picked up")
    dep = o_local_ship + rng.integers(2 * 3600, 5 * 3600, n)
    add(~ltl & (rng.random(n) < 0.5), "IN_TRANSIT", origin, dep, "Departed origin facility")
    # hub scan for longer lanes
    hub = np.full(n, -1, dtype=np.int64)
    for sc in np.unique(scac):
        m = scac == sc
        hubs = _HUB_IDX.get(sc)
        if hubs is None or not len(hubs):
            continue
        d = (geo.haversine_mi(lat[origin[m]][:, None], lon[origin[m]][:, None], lat[hubs][None, :], lon[hubs][None, :])
             + geo.haversine_mi(lat[hubs][None, :], lon[hubs][None, :], lat[dest[m]][:, None], lon[dest[m]][:, None]))
        hub[m] = hubs[np.argmin(d, axis=1)]
    has_hub = (hub >= 0) & (hub != origin) & (hub != dest) & (s["zone"].to_numpy(int) >= 4)
    hub_safe = np.where(hub >= 0, hub, origin)
    span = np.maximum(deliver_day - ship_day_dest, 1)
    hub_local = geo.utc_to_local_many(tzs[hub_safe], ship_utc) + (span * 86400 * 0.45).astype(np.int64) + rng.integers(0, 6 * 3600, n)
    add(has_hub, "IN_TRANSIT", hub_safe, hub_local, "Arrived at sort facility")
    arr_local = (deliver_day - 1) * 86400 + rng.integers(19 * 3600, 23 * 3600, n)
    arr_local = np.maximum(arr_local, dep + 3600)
    add(all_rows, "IN_TRANSIT", dest, arr_local, "Arrived at destination facility")
    exc_mask = np.array([e is not None for e in exc])
    exc_local = (deliver_day - exc_delay) * 86400 + rng.integers(8 * 3600, 17 * 3600, n)
    exc_local = np.maximum(exc_local, arr_local - 3600 * 10)
    exc_desc = {"WEATHER": "Delay due to weather conditions", "ADDRESS": "Address information required",
                "DAMAGED": "Package damaged in transit", "REFUSED": "Delivery refused by recipient",
                "CARRIER_DELAY": "Delayed in carrier network"}
    for code, desc in exc_desc.items():
        m = exc_mask & (exc == code)
        add(m, "EXCEPTION", dest, exc_local, desc, exc)
    deliver_ok = ~rts
    ofd_local = deliver_day * 86400 + rng.integers(int(6.5 * 3600), int(9.5 * 3600), n)
    add(deliver_ok & ~ltl, "OUT_FOR_DELIVERY", dest, ofd_local, "Out for delivery")
    dl_local = np.where(ltl, deliver_day * 86400 + rng.integers(8 * 3600, 16 * 3600, n),
                        np.maximum(ofd_local + 1800, deliver_day * 86400 + rng.integers(int(9.5 * 3600), int(19.5 * 3600), n)))
    add(deliver_ok, "DELIVERED", dest, dl_local, "Delivered")
    rts_local = exc_local + rng.integers(86400, 2 * 86400, n)
    add(rts, "RETURN_TO_SENDER", dest, rts_local, "Returning package to shipper")
    ev = pd.concat(rows, ignore_index=True)
    # ---- received_at lag: minutes for most, hours for LTL EDI 214 batches, days for a few late scans
    m = len(ev)
    ev_ltl = np.isin(ev["scac"].to_numpy(object), ["ODFL", "MRLF", "EXLA", "TRMX"])
    lag = np.where(ev_ltl, rng.integers(1800, 6 * 3600, m), np.exp(rng.normal(5.6, 0.9, m)).astype(np.int64))
    late = rng.random(m) < 0.004
    lag = np.where(late, rng.integers(86400, 3 * 86400, m), lag)
    recv = ev["event_utc"].to_numpy(np.int64) + lag
    for (sc, t0, t1) in st["outages"]:
        hit = (ev["scac"].to_numpy(object) == sc) & (recv >= t0) & (recv < t1)
        recv = np.where(hit, t1 + rng.integers(0, 1800, m), recv)
    ev["received_utc"] = recv
    ev["is_correction"] = False
    # natural feed resends (logged: they are duplicates a pipeline must remove)
    dup = rng.random(m) < 0.004
    dq_dup = ctx.dq.mask("duplicate_row", m, rng)
    dup |= dq_dup
    dq_late = ctx.dq.mask("late_arriving", m, rng)
    if dq_late.any():
        ev.loc[dq_late, "received_utc"] = ev.loc[dq_late, "received_utc"].to_numpy() + rng.integers(4 * 86400, 12 * 86400, int(dq_late.sum()))
    corr = (ev["event_code"].to_numpy(object) == "DELIVERED") & (rng.random(m) < 0.003)
    extra = []
    if dup.any():
        d = ev[dup].copy()
        d["received_utc"] = d["received_utc"].to_numpy() + rng.integers(60, 3 * 3600, len(d))
        extra.append(d)
    if corr.any():
        c = ev[corr].copy()
        shift = rng.integers(-3 * 3600, 3 * 3600, len(c))
        c["event_utc"] = c["event_utc"].to_numpy() + shift
        c["event_local_ts"] = geo.iso_with_offset(c["event_utc"].to_numpy() + c["_off"].to_numpy(), c["_off"].to_numpy())
        c["received_utc"] = np.maximum(c["received_utc"].to_numpy() + rng.integers(3600, 2 * 86400, len(c)), c["event_utc"].to_numpy())
        c["description"] = "Delivered (time corrected)"
        c["is_correction"] = True
        extra.append(c)
    base_n = m
    if extra:
        ev = pd.concat([ev] + extra, ignore_index=True)
    ids = ctx.next_ids("carrier.tracking_event", len(ev))
    ev["event_id"] = ids
    if dup.any():
        dup_ids = ids[base_n:base_n + int(dup.sum())]
        for eid, tn, cat in zip(dup_ids, ev["tracking_nbr"].to_numpy()[base_n:base_n + int(dup.sum())], np.where(dq_dup[dup], "dq", "feed")):
            ctx.dq.log("duplicate_row", "carrier", "tracking_event", str(eid), None, None, tn,
                       "carrier feed resent an event" if cat == "feed" else "duplicate event injected")
    for eid, tn in zip(ids[:base_n][dq_late], ev["tracking_nbr"].to_numpy()[:base_n][dq_late]):
        ctx.dq.log("late_arriving", "carrier", "tracking_event", str(eid), "received_at", None, tn, "event delivered 4-12 days late")
    out = pd.DataFrame({
        "event_id": ev["event_id"], "tracking_nbr": ev["tracking_nbr"], "scac": ev["scac"], "event_code": ev["event_code"],
        "event_ts": pd.to_datetime(ev["event_utc"].to_numpy(np.int64), unit="s", utc=True),
        "event_local_ts": ev["event_local_ts"], "city": ev["city"], "state_prov": ev["state_prov"],
        "country_code": ev["country_code"], "description": ev["description"], "exception_code": ev["exception_code"],
        "received_at": pd.to_datetime(ev["received_utc"].to_numpy(np.int64), unit="s", utc=True),
        "is_correction": ev["is_correction"],
    })
    geo.copy_fast(ctx, "sim_internal", EV_SCHEMA, out)
    # ---- pending WMS status updates (visible when the final event reaches the API)
    base = ev.iloc[:base_n]
    final_code = np.where(rts, "RETURN_TO_SENDER", "DELIVERED")
    key = pd.Series(trk).astype(str) + "|" + pd.Series(final_code)
    fin = base[base["event_code"].isin(["DELIVERED", "RETURN_TO_SENDER"])]
    vis = dict(zip(fin["tracking_nbr"].astype(str) + "|" + fin["event_code"], fin["received_utc"].to_numpy(np.int64)))
    when = dict(zip(fin["tracking_nbr"].astype(str) + "|" + fin["event_code"], fin["event_utc"].to_numpy(np.int64)))
    visible = np.array([vis.get(k, T_NONE) for k in key], dtype=np.int64)
    delivered = np.array([when.get(k, T_NONE) for k in key], dtype=np.int64)
    exc_rows = base[base["event_code"] == "EXCEPTION"]
    exc_vis = dict(zip(exc_rows["tracking_nbr"].astype(str), exc_rows["received_utc"].to_numpy(np.int64)))
    exc_visible = np.array([exc_vis.get(str(t), T_NONE) for t in trk], dtype=np.int64)
    add_p = {
        "shipment_id": s["shipment_id"].to_numpy(np.int64), "visible": visible, "stage": np.zeros(n, dtype=np.int8),
        "delivered": np.where(rts, T_NONE, delivered), "promised_day": promised.astype(np.int64), "is_late": is_late | rts,
        "exc": exc, "tracking_nbr": trk, "order_nbr": s["order_nbr"].to_numpy(object), "owner_code": s["owner_code"].to_numpy(object),
        "scac": scac, "tz": s["wh_tz"].to_numpy(object), "exc_visible": exc_visible,
    }
    p = st["pending"]
    for k, dt in _PENDING_DTYPES.items():
        p[k] = np.concatenate([p[k], np.asarray(add_p[k], dtype=dt)])


def _label_only_events(ctx: Ctx, st: dict[str, Any], v: pd.DataFrame) -> None:
    """Voided labels: the aggregator saw LABEL_CREATED and then nothing (a stale 'pre-transit' number)."""
    _, _, tzs, cname, cstate, cctry = _city_arrays()
    ci = v["origin_city_idx"].to_numpy(np.int64)
    utc = v["label_utc"].to_numpy(np.int64)
    loc = geo.utc_to_local_many(tzs[ci], utc)
    ids = ctx.next_ids("carrier.tracking_event", len(v))
    out = pd.DataFrame({
        "event_id": ids, "tracking_nbr": v["tracking_nbr"].to_numpy(object), "scac": v["scac"].to_numpy(object),
        "event_code": "LABEL_CREATED", "event_ts": pd.to_datetime(utc, unit="s", utc=True),
        "event_local_ts": geo.iso_with_offset(loc, loc - utc), "city": cname[ci], "state_prov": cstate[ci],
        "country_code": cctry[ci], "description": "Shipping label created, carrier awaiting item", "exception_code": None,
        "received_at": pd.to_datetime(utc + 240, unit="s", utc=True), "is_correction": False,
    })
    geo.copy_fast(ctx, "sim_internal", EV_SCHEMA, out)


def _apply_visible_outcomes(ctx: Ctx, st: dict[str, Any], win: Window) -> None:
    p = st["pending"]
    if not len(p["shipment_id"]):
        return
    we = int(win.end.timestamp())
    # stage 0 -> EXCEPTION visible first (exception then delivered later)
    exc_due = (p["stage"] == 0) & (p["exc_visible"] < we) & (p["exc_visible"] < p["visible"])
    final_due = p["visible"] < we
    upd_ids, upd_status, upd_ts = [], [], []
    m = exc_due & ~final_due
    if m.any():
        upd_ids.append(p["shipment_id"][m])
        upd_status.append(np.full(int(m.sum()), "EXCEPTION", dtype=object))
        upd_ts.append(_local_naive(p["tz"][m], p["exc_visible"][m]))
        p["stage"][m] = 1
    if final_due.any():
        f = final_due
        status = np.where(p["delivered"][f] < T_NONE, "DELIVERED", "EXCEPTION")
        upd_ids.append(p["shipment_id"][f])
        upd_status.append(status.astype(object))
        upd_ts.append(_local_naive(p["tz"][f], p["visible"][f]))
        delivered = p["delivered"][f]
        out = pd.DataFrame({
            "tracking_nbr": p["tracking_nbr"][f], "order_nbr": p["order_nbr"][f], "owner_code": p["owner_code"][f],
            "scac": p["scac"][f],
            "delivered_ts": pd.to_datetime(np.where(delivered < T_NONE, delivered, 0), unit="s", utc=True).where(delivered < T_NONE),
            "promised_date": pd.to_datetime(p["promised_day"][f] * 86400, unit="s").date,
            "is_late": p["is_late"][f], "exception_code": p["exc"][f],
        })
        ctx.bus.publish("delivery_outcomes", out)
        for k in p:
            p[k] = p[k][~f]
    if upd_ids:
        ids = np.concatenate(upd_ids)
        stt = np.concatenate(upd_status)
        ts = np.concatenate(upd_ts)
        with ctx.conn("wms").cursor() as cur:
            cur.execute(
                "UPDATE wms.shipment s SET shipment_status = v.st, mod_ts = v.ts "
                "FROM unnest(%s::bigint[], %s::text[], %s::timestamp[]) AS v(id, st, ts) WHERE s.shipment_id = v.id",
                (ids.tolist(), stt.tolist(), ts.astype("datetime64[us]").tolist()))


def _local_naive(tz: np.ndarray, utc: np.ndarray) -> np.ndarray:
    return geo.utc_to_local_many(tz, utc).astype("datetime64[s]")


def _delay_feed(ctx: Ctx, st: dict[str, Any], scac: str, t0: int, t1: int) -> int:
    """Events of ``scac`` that would become visible during the outage arrive in a burst after it."""
    with ctx.conn("sim_internal").cursor() as cur:
        cur.execute(
            "UPDATE carrier.tracking_event SET received_at = to_timestamp(%s) + (random() * interval '30 minutes') "
            "WHERE scac = %s AND received_at >= to_timestamp(%s) AND received_at < to_timestamp(%s)",
            (t1, scac, t0, t1))
        n = cur.rowcount
    p = st["pending"]
    hit = (p["scac"] == scac) & (p["visible"] >= t0) & (p["visible"] < t1)
    p["visible"][hit] = t1 + 900
    hit2 = (p["scac"] == scac) & (p["exc_visible"] >= t0) & (p["exc_visible"] < t1)
    p["exc_visible"][hit2] = t1 + 900
    return n


def _storm_delay_in_flight(ctx: Ctx, st: dict[str, Any], wh: dict[str, Any], now: int, days: int, sev: float) -> int:
    """Push not-yet-visible delivery scans of in-flight packages near the storm by 1-3 days and add a weather exception."""
    p = st["pending"]
    if not len(p["shipment_id"]):
        return 0
    trk = p["tracking_nbr"][(p["visible"] > now) & (p["delivered"] < T_NONE)]
    if not len(trk):
        return 0
    lat, lon, *_ = _city_arrays()
    df = ctx.read_df("sim_internal",
                     "SELECT event_id, tracking_nbr, scac, event_code, extract(epoch from event_ts)::bigint AS ev, event_local_ts, city, "
                     "state_prov, country_code, description, extract(epoch from received_at)::bigint AS rc "
                     "FROM carrier.tracking_event WHERE tracking_nbr = ANY(%s) AND event_code IN ('OUT_FOR_DELIVERY', 'DELIVERED') "
                     "AND received_at > to_timestamp(%s)", (trk.tolist(), now))
    if df.empty:
        return 0
    c = geo.cities()
    key = c["city"] + "|" + c["state"]
    idx = pd.Series(np.arange(len(c)), index=key)
    ci = idx.reindex(df["city"] + "|" + df["state_prov"]).to_numpy()
    ok = ~np.isnan(ci)
    near = np.zeros(len(df), dtype=bool)
    near[ok] = geo.haversine_mi(lat[ci[ok].astype(int)], lon[ci[ok].astype(int)], wh["address"]["lat"], wh["address"]["lon"]) < 400
    df = df[near]
    if df.empty:
        return 0
    rng = np.random.default_rng(int(now) % 2**31)
    shift_by_trk = {t: int(rng.integers(1, 2 + int(sev))) * 86400 for t in df["tracking_nbr"].unique()}
    sh = df["tracking_nbr"].map(shift_by_trk).to_numpy(np.int64)
    off = np.array([_offset_from_iso(s) for s in df["event_local_ts"]], dtype=np.int64)
    new_ev = df["ev"].to_numpy(np.int64) + sh
    with ctx.conn("sim_internal").cursor() as cur:
        cur.execute(
            "UPDATE carrier.tracking_event t SET event_ts = to_timestamp(v.ev), received_at = to_timestamp(v.rc), event_local_ts = v.lt "
            "FROM unnest(%s::bigint[], %s::bigint[], %s::bigint[], %s::text[]) AS v(id, ev, rc, lt) WHERE t.event_id = v.id",
            (df["event_id"].tolist(), new_ev.tolist(), (df["rc"].to_numpy(np.int64) + sh).tolist(),
             list(geo.iso_with_offset(new_ev + off, off))))
    first = df.sort_values("ev").drop_duplicates("tracking_nbr")
    ids = ctx.next_ids("carrier.tracking_event", len(first))
    exc_ev = first["ev"].to_numpy(np.int64) - 3600
    foff = np.array([_offset_from_iso(s) for s in first["event_local_ts"]], dtype=np.int64)
    geo.copy_fast(ctx, "sim_internal", EV_SCHEMA, pd.DataFrame({
        "event_id": ids, "tracking_nbr": first["tracking_nbr"], "scac": first["scac"], "event_code": "EXCEPTION",
        "event_ts": pd.to_datetime(exc_ev, unit="s", utc=True), "event_local_ts": geo.iso_with_offset(exc_ev + foff, foff),
        "city": first["city"], "state_prov": first["state_prov"], "country_code": first["country_code"],
        "description": "Delay due to weather conditions", "exception_code": "WEATHER",
        "received_at": pd.to_datetime(np.maximum(exc_ev + 600, now + 300), unit="s", utc=True), "is_correction": False}))
    shifted = set(shift_by_trk)
    hit = np.array([t in shifted for t in p["tracking_nbr"]])
    add = np.array([shift_by_trk.get(t, 0) for t in p["tracking_nbr"]], dtype=np.int64)
    p["visible"][hit] += add[hit]
    p["delivered"][hit] += add[hit]
    p["is_late"][hit] = True
    p["exc"][hit & (p["exc"] == None)] = "WEATHER"
    p["exc_visible"][hit] = np.maximum(now + 300, p["exc_visible"][hit].clip(max=now + 300))
    return len(shifted)


def _offset_from_iso(s: str) -> int:
    sign = -1 if s[-6] == "-" else 1
    return sign * (int(s[-5:-3]) * 3600 + int(s[-2:]) * 60)


def _late_scan_dump(ctx: Ctx, st: dict[str, Any], win: Window, hours: int) -> int:
    """Hub scanners buffered intermediate scans for ``hours``; they reach the aggregator now (late-arriving facts)."""
    now = int(win.start.timestamp())
    df = ctx.read_df("sim_internal",
                     "SELECT tracking_nbr, scac, extract(epoch from min(event_ts))::bigint AS first_ev, "
                     "extract(epoch from max(event_ts))::bigint AS last_ev FROM carrier.tracking_event "
                     "WHERE received_at >= to_timestamp(%s) AND received_at < to_timestamp(%s) AND event_code = 'IN_TRANSIT' "
                     "GROUP BY tracking_nbr, scac LIMIT 20000", (now - hours * 3600, now))
    if df.empty:
        return 0
    rng = np.random.default_rng((now // 60) % 2**31)
    df = df.sample(frac=0.35, random_state=int(rng.integers(0, 2**31)))
    hubs = _HUB_IDX or _hub_index()
    _, _, tzs, cname, cstate, cctry = _city_arrays()
    hub_ci = np.array([hubs.get(s, hubs["UPSN"])[int(rng.integers(0, len(hubs.get(s, hubs['UPSN']))))] for s in df["scac"]])
    ev = df["first_ev"].to_numpy(np.int64) + ((df["last_ev"] - df["first_ev"]).to_numpy(np.int64) * rng.random(len(df))).astype(np.int64)
    ev = np.maximum(ev, now - hours * 3600)
    loc = geo.utc_to_local_many(tzs[hub_ci], ev)
    ids = ctx.next_ids("carrier.tracking_event", len(df))
    recv = now + rng.integers(30, 3000, len(df))
    geo.copy_fast(ctx, "sim_internal", EV_SCHEMA, pd.DataFrame({
        "event_id": ids, "tracking_nbr": df["tracking_nbr"].to_numpy(), "scac": df["scac"].to_numpy(), "event_code": "IN_TRANSIT",
        "event_ts": pd.to_datetime(ev, unit="s", utc=True), "event_local_ts": geo.iso_with_offset(loc, loc - ev),
        "city": cname[hub_ci], "state_prov": cstate[hub_ci], "country_code": cctry[hub_ci], "description": "Processed at sort facility",
        "exception_code": None, "received_at": pd.to_datetime(recv, unit="s", utc=True), "is_correction": False}))
    for eid, tn in zip(ids, df["tracking_nbr"]):
        ctx.dq.log("late_arriving", "carrier", "tracking_event", str(eid), "received_at", None, tn,
                   f"scenario late_data_dump: hub scan delivered up to {hours} h late")
    return len(df)


MODULE = CarrierModule()
