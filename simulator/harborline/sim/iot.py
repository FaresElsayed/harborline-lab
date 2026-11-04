"""Sensor gateways (database iot, schema telemetry): temperature / humidity readings and dock-door events.

Readings are generated every 5 minutes per sensor for the last ``iot_days`` of the build and for every batch /
stream window after it. A row is INSERTED only when the gateway lands it (``ingested_at`` inside the window):
gateway outages and the late-data-dump scenario buffer readings in the world and flush them late.
Planted: CHI1 cooler excursions when dock door 14 stays open > 20 min on hot afternoons (the WMS holds the lots,
see ``cold_excursions``) and the stuck sensor CHI1-CLR-03-T.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache
from typing import Any

import numpy as np
import pandas as pd

from ..core import Ctx, Window, derive_seed
from . import geo

NAME = "iot"
DAY = 86400
CADENCE = 300
ABBR = {"AMBIENT": "AMB", "COOLER": "CLR", "FREEZER": "FRZ", "BULK": "BLK", "CAGE": "CGE", "HAZMAT": "HAZ", "RETURNS": "RTN"}
TEMP_SENSORS = {"COOLER": 8, "FREEZER": 6, "AMBIENT": 4, "BULK": 2, "CAGE": 1, "HAZMAT": 2, "RETURNS": 1}
HUM_SENSORS = {"COOLER": 2, "FREEZER": 1, "AMBIENT": 2}
MODELS = {"TEMP": "ThermaLink TL-200", "HUMIDITY": "HumiSense H3", "DOOR": "DoorGuard DG-1"}
BUF_COLS = ("sensor_id", "device_ts_ms", "value", "battery_pct", "ingested", "dq")


# --------------------------------------------------------------------------------------------- excursion schedule
def _wh(ctx: Ctx, code: str) -> dict[str, Any] | None:
    return next((w for w in ctx.rules["warehouses"] if w["code"] == code), None)


def _story(ctx: Ctx, sid: str) -> dict[str, Any] | None:
    return next((s for s in ctx.planted.get("stories", []) if s["id"] == sid), None)


_EXC_CACHE: dict[tuple, pd.DataFrame] = {}


def cold_excursions(ctx: Ctx, day: int) -> pd.DataFrame:
    """Door-14 incidents at the cold-chain site on local ``day`` (epoch day).

    Columns: start_local, close_local (door open/close, local seconds), peak_c, above_min (minutes above the zone max),
    excursion (above_min >= excursion_minutes), detect_local (alarm -> QA hold time). Shared by the WMS (holds) and
    the sensor feed (readings, door events) so both systems tell the same story.
    """
    story = _story(ctx, "cold_excursion_summer")
    empty = pd.DataFrame(columns=["start_local", "close_local", "peak_c", "above_min", "excursion", "detect_local"])
    if not story:
        return empty
    key = (ctx.seed, day)
    if key in _EXC_CACHE:
        return _EXC_CACHE[key]
    wh = _wh(ctx, story["warehouse"])
    d = geo.day_date(day)
    out = empty
    if wh is not None and d.month in (6, 7, 8, 9):
        wx = geo.weather(ctx, wh["code"], wh["address"]["lat"], wh["address"]["lon"], d)
        rng = np.random.default_rng(derive_seed(ctx.seed, "door14", day))
        hot = wx["tmax_c"] >= 29.0
        u = rng.random()
        if hot and u < 0.24:
            near_miss = rng.random() < 0.15
            open_min = float(rng.uniform(12, 19) if near_miss else rng.uniform(22, 58))
            start = day * DAY + int(rng.integers(13 * 3600, 16 * 3600 + 1800))
            close = start + int(open_min * 60)
            zmax = ctx.rules["cold_chain"][story["zone"]]["max_c"]
            curve, peak = _excursion_curve(open_min, wx["tmax_c"])
            above = np.flatnonzero(curve > zmax)
            above_min = float(len(above))
            exc = above_min >= ctx.rules["cold_chain"][story["zone"]]["excursion_minutes"]
            first = start + int(above[0]) * 60 if len(above) else start
            detect = first + 30 * 60 + int(rng.integers(5, 21)) * 60
            out = pd.DataFrame([{"start_local": start, "close_local": close, "peak_c": round(peak, 1), "above_min": above_min,
                                 "excursion": bool(exc), "detect_local": detect}])
    _EXC_CACHE[key] = out
    if len(_EXC_CACHE) > 5000:
        _EXC_CACHE.clear()
    return out


@lru_cache(maxsize=256)
def _excursion_curve(open_min: float, tmax: float) -> tuple[np.ndarray, float]:
    """Cooler air temperature near the door, per minute from door open (C)."""
    base = 3.8
    target = min(15.0, 9.5 + 0.25 * (tmax - 29) + open_min * 0.04)
    t = np.arange(0, int(open_min) + 120)
    rise = base + (target - base) * (1 - np.exp(-t / 11.0))
    at_close = base + (target - base) * (1 - np.exp(-open_min / 11.0))
    fall = base + (at_close - base) * np.exp(-(t - open_min) / 16.0)
    curve = np.where(t <= open_min, rise, fall)
    return curve, float(curve.max())


def _stuck_window(ctx: Ctx) -> tuple[int, int, str] | None:
    s = _story(ctx, "stuck_sensor")
    if not s:
        return None
    iot_days = int(ctx.rules["size_presets"][ctx.preset]["iot_days"])
    end = int(ctx.history_end.timestamp())
    start = end - round(iot_days * 0.45) * DAY + 7 * 3600
    return start, start + int(s.get("days", 9)) * DAY, s["sensor"]


# --------------------------------------------------------------------------------------------- module
class IotModule:
    name = NAME

    def bootstrap(self, ctx: Ctx) -> None:
        st = {"sensors": None, "buffer": {k: np.zeros(0, dtype=object if k == "sensor_id" else (np.int64 if k != "value" else float))
                                          for k in BUF_COLS}, "dumps": [], "done_wh": set()}
        st["buffer"]["dq"] = np.zeros(0, dtype=object)
        ctx.world[NAME] = st
        self._ensure_sensors(ctx, st, int(ctx.history_start.timestamp()))

    def _ensure_sensors(self, ctx: Ctx, st: dict[str, Any], now_utc: int) -> None:
        wst = ctx.world.get("wms", {})
        ready = wst.get("wh_ready", set())
        rows = []
        for wi, wh in enumerate(ctx.rules["warehouses"]):
            if wi not in ready or wh["code"] in st["done_wh"]:
                continue
            st["done_wh"].add(wh["code"])
            rng = np.random.default_rng(derive_seed(ctx.seed, "sensors", wh["code"]))
            opened = geo.epoch_day(date.fromisoformat(str(wh["opened"]))) if wh.get("opened") else now_utc // DAY - 40
            for z in wh["zones"]:
                for typ, n in (("TEMP", TEMP_SENSORS.get(z, 1)), ("HUMIDITY", HUM_SENSORS.get(z, 0))):
                    for k in range(1, n + 1):
                        inst = max(opened, now_utc // DAY - int(rng.integers(200, 1500)))
                        rows.append({"sensor_id": f"{wh['code']}-{ABBR.get(z, z[:3])}-{k:02d}-{typ[0]}", "warehouse_code": wh["code"],
                                     "zone_code": f"{wh['code']}-{z}", "sensor_type": typ, "unit": "C" if typ == "TEMP" else "%RH",
                                     "model": MODELS[typ], "installed_date": geo.day_date(inst),
                                     "calibration_due": geo.day_date(now_utc // DAY + int(rng.integers(20, 360))), "is_active": True,
                                     "_zone": z, "_wi": wi, "_off": float(rng.normal(0, 0.35)), "_drift": float(rng.normal(0, 1.2)),
                                     "_phase": int(rng.integers(0, CADENCE)), "_batt": float(rng.uniform(40, 100))})
            for door in range(1, wh["dock_doors"] + 1):
                rows.append({"sensor_id": f"{wh['code']}-DOOR-{door:02d}", "warehouse_code": wh["code"], "zone_code": f"{wh['code']}-DOCK",
                             "sensor_type": "DOOR", "unit": None, "model": MODELS["DOOR"], "installed_date": geo.day_date(now_utc // DAY - 300),
                             "calibration_due": None, "is_active": True, "_zone": "DOCK", "_wi": wi, "_off": 0.0, "_drift": 0.0,
                             "_phase": 0, "_batt": 100.0})
        if not rows:
            return
        df = pd.DataFrame(rows)
        geo.copy_fast(ctx, "iot", "telemetry.sensor", df[[c for c in df.columns if not c.startswith("_")]])
        keep = df[df["sensor_type"] != "DOOR"]
        st["sensors"] = keep if st["sensors"] is None else pd.concat([st["sensors"], keep], ignore_index=True)

    def step(self, ctx: Ctx, win: Window) -> None:
        st = ctx.world.setdefault(NAME, None)
        if st is None:
            self.bootstrap(ctx)
            st = ctx.world[NAME]
        ws, we = int(win.start.timestamp()), int(win.end.timestamp())
        self._ensure_sensors(ctx, st, ws)
        iot_days = int(ctx.rules["size_presets"][ctx.preset]["iot_days"])
        start = int(ctx.history_end.timestamp()) - iot_days * DAY if ctx.mode == "build" else -1
        rng = ctx.rng(NAME, win, "readings")
        if we > start and st["sensors"] is not None:
            _readings(ctx, st, max(ws, start), we, rng)
            _door_events(ctx, st, max(ws, start), we, rng)
        _flush_buffer(ctx, st, we)

    def apply_scenario(self, ctx: Ctx, scenario_id: str, params: dict[str, Any], win: Window) -> str | None:
        st = ctx.world.get(NAME)
        if st is None or scenario_id != "late_data_dump" or params.get("source", "all") not in ("all", "iot"):
            return None
        hours = int(params.get("hours_late", 36))
        now = int(win.start.timestamp())
        rng = np.random.default_rng(derive_seed(ctx.seed, "dump", now))
        codes = sorted({s for s in st["sensors"]["warehouse_code"]}) if st["sensors"] is not None else []
        if not codes:
            return None
        pick = [codes[int(i)] for i in rng.choice(len(codes), size=min(2, len(codes)), replace=False)]
        for c in pick:
            st["dumps"].append((c, now, now + hours * 3600))
        return f"gateways at {', '.join(pick)} buffer readings for {hours} h and flush them in one dump"


def _readings(ctx: Ctx, st: dict[str, Any], ws: int, we: int, rng: np.random.Generator) -> None:
    s = st["sensors"]
    n = len(s)
    grid = np.arange((ws // CADENCE) * CADENCE, we + CADENCE, CADENCE, dtype=np.int64)
    t = grid[None, :] + s["_phase"].to_numpy(np.int64)[:, None]
    ok = (t >= ws) & (t < we)
    si, gi = np.nonzero(ok)
    if not len(si):
        return
    tt = t[si, gi]
    m = len(tt)
    zone = s["_zone"].to_numpy(object)[si]
    typ = s["sensor_type"].to_numpy(object)[si]
    wi = s["_wi"].to_numpy(np.int64)[si]
    sid = s["sensor_id"].to_numpy(object)[si]
    tz = np.array([ctx.rules["warehouses"][int(w)]["time_zone"] for w in wi], dtype=object)
    loc = geo.utc_to_local_many(tz, tt)
    hour = (loc % DAY) / 3600.0
    day = loc // DAY
    outdoor = np.zeros(m)
    for w in np.unique(wi):
        wh = ctx.rules["warehouses"][int(w)]
        for d in np.unique(day[wi == w]):
            wx = geo.weather(ctx, wh["code"], wh["address"]["lat"], wh["address"]["lon"], geo.day_date(int(d)))
            outdoor[(wi == w) & (day == d)] = wx["tmax_c"]
    diurnal = np.sin(2 * np.pi * (hour - 10) / 24)
    off = s["_off"].to_numpy(float)[si]
    noise = rng.normal(0, 0.12, m)
    amb = np.clip(15.5 + 0.42 * (outdoor - 15) + 1.6 * diurnal, 11, 31.5)
    val = np.select([zone == "COOLER", zone == "FREEZER"], [3.7 + 0.25 * diurnal, -21.0 + 0.4 * diurnal], amb) + off + noise
    defrost = (zone == "FREEZER") & (np.isin(np.floor(hour).astype(int), [2, 8, 14, 20])) & ((hour % 1) < 0.42)
    val = np.where(defrost, val + 4.2 * np.sin(np.pi * (hour % 1) / 0.42), val)
    hum = np.clip(46 + 9 * np.sin(2 * np.pi * (day % 365) / 365) + 6 * diurnal + rng.normal(0, 1.5, m)
                  + np.where(zone == "COOLER", 18, np.where(zone == "FREEZER", 8, 0)), 18, 96)
    val = np.where(typ == "HUMIDITY", hum, val)
    # planted: CHI1 door-14 excursions
    story = _story(ctx, "cold_excursion_summer")
    if story:
        z_m = (s["warehouse_code"].to_numpy(object)[si] == story["warehouse"]) & (zone == story["zone"]) & (typ == "TEMP")
        if z_m.any():
            num = np.array([int(x.split("-")[2]) for x in sid[z_m]])
            weight = np.select([num <= 3, num <= 5], [1.0, 0.35], 0.0)
            for d in np.unique(day[z_m]):
                ex = cold_excursions(ctx, int(d))
                for _, e in ex.iterrows():
                    curve, _ = _excursion_curve(float((e["close_local"] - e["start_local"]) / 60), 30.0)
                    k = ((loc[z_m] - int(e["start_local"])) // 60).astype(np.int64)
                    inside = (k >= 0) & (k < len(curve))
                    add = np.zeros(int(z_m.sum()))
                    add[inside] = (curve[k[inside]] - 3.8) * weight[inside]
                    sub = val[z_m]
                    val[z_m] = sub + add
    # planted: stuck sensor reports exactly 4.0 C while the room drifts up
    stuck = _stuck_window(ctx)
    if stuck:
        t0, t1, sens = stuck
        in_p = (tt >= t0) & (tt < t1)
        if in_p.any():
            prog = np.clip((tt - t0) / (t1 - t0), 0, 1)
            drift = 3.6 * np.sin(np.pi * prog) ** 1.5
            parts = sens.split("-")
            neigh = {f"{parts[0]}-{parts[1]}-{int(parts[2]) + k:02d}-T" for k in (-1, 1)}
            nm = in_p & np.isin(sid, list(neigh))
            val = np.where(nm, val + drift * 0.95, val)
            val = np.where(in_p & (sid == sens), 4.0, val)
    # sensor defects (logged)
    dq_spike = ctx.dq.mask("sensor_spike", m, rng)
    spike_vals = np.array([85.0, -40.0, 999.9, -273.1])
    val = np.where(dq_spike, spike_vals[rng.integers(0, 4, m)], val)
    stuck_pick = ctx.dq.mask("sensor_stuck", n, rng)
    dq_note = np.full(m, None, dtype=object)
    for i in np.flatnonzero(stuck_pick):
        mm = si == i
        if mm.any():
            frozen = round(float(val[mm][0]), 1)
            val[mm] = frozen
            ctx.dq.log("sensor_stuck", "iot", "sensor_reading", s["sensor_id"].iloc[i], "value", None, frozen,
                       f"sensor repeats {frozen} for the whole window starting {pd.Timestamp(int(tt[mm][0]), unit='s', tz='UTC')}")
    dq_note[dq_spike] = "spike"
    val = np.where(typ == "HUMIDITY", np.round(val, 1), np.round(val, 2))
    # device clock drift (weekly NTP sync on Sundays) and gateway landing time
    since_sync = ((tt - 3 * DAY - 3 * 3600) % (7 * DAY)) / DAY
    dev = tt * 1000 + (s["_drift"].to_numpy(float)[si] * since_sync * 1000).astype(np.int64) + rng.integers(-400, 400, m)
    ingest = tt + rng.integers(2, 25, m)
    for w in np.unique(wi):
        for d in np.unique(day[wi == w]):
            for (o0, o1) in _outages(ctx, ctx.rules["warehouses"][int(w)]["code"], int(d)):
                hit = (wi == w) & (loc >= o0) & (loc < o1)
                if hit.any():
                    end_utc = int(geo.local_to_utc(ctx.rules["warehouses"][int(w)]["time_zone"], np.array([o1]))[0])
                    ingest[hit] = end_utc + rng.integers(5, 300, int(hit.sum()))
    for (code, d0, d1) in st["dumps"]:
        hit = (s["warehouse_code"].to_numpy(object)[si] == code) & (tt >= d0) & (tt < d1)
        ingest[hit] = np.maximum(ingest[hit], d1 + rng.integers(0, 900, int(hit.sum())))
    late = ctx.dq.mask("late_arriving", m, rng)
    ingest[late] = ingest[late] + rng.integers(2 * DAY, 7 * DAY, int(late.sum()))
    dq_note[late] = "late"
    batt = (100 - ((tt / DAY) * 0.05 + s["_batt"].to_numpy(float)[si]) % 85).astype(np.int64)   # sawtooth: replaced at 15 %
    buf = {"sensor_id": sid, "device_ts_ms": dev, "value": val, "battery_pct": batt, "ingested": ingest, "dq": dq_note}
    dup = ctx.dq.mask("duplicate_row", m, rng)
    if dup.any():
        d = {k: v[dup] for k, v in buf.items()}
        d["ingested"] = d["ingested"] + rng.integers(30, 3600, int(dup.sum()))
        d["dq"] = np.full(int(dup.sum()), "dup", dtype=object)
        buf = {k: np.concatenate([buf[k], d[k]]) for k in buf}
    b = st["buffer"]
    st["buffer"] = {k: np.concatenate([b[k], np.asarray(buf[k]).astype(b[k].dtype if len(b[k]) else np.asarray(buf[k]).dtype)])
                    for k in BUF_COLS}


def _outages(ctx: Ctx, code: str, day: int) -> list[tuple[int, int]]:
    """Gateway network drops (local seconds) affecting ``day``: readings during them land when the link returns."""
    out = []
    for d in (day - 1, day):
        rng = np.random.default_rng(derive_seed(ctx.seed, "gateway", code, d))
        if rng.random() < 0.06:
            start = d * DAY + int(rng.integers(0, DAY))
            dur = int(min(10 * 3600, rng.lognormal(np.log(75 * 60), 0.9)))
            out.append((start, start + dur))
    return out


def _flush_buffer(ctx: Ctx, st: dict[str, Any], we: int) -> None:
    b = st["buffer"]
    if not len(b["sensor_id"]):
        return
    due = b["ingested"] < we
    if not due.any():
        return
    n = int(due.sum())
    ids = ctx.next_ids("iot.sensor_reading", n)
    df = pd.DataFrame({"reading_id": ids, "sensor_id": b["sensor_id"][due], "device_ts_ms": b["device_ts_ms"][due].astype(np.int64),
                       "value": b["value"][due].astype(float), "battery_pct": b["battery_pct"][due].astype(np.int64),
                       "ingested_at": pd.to_datetime(b["ingested"][due].astype(np.int64), unit="s", utc=True)})
    geo.copy_fast(ctx, "iot", "telemetry.sensor_reading", df)
    note = b["dq"][due]
    for rid, sid, nt, dev in zip(ids, df["sensor_id"], note, df["device_ts_ms"]):
        if nt == "spike":
            ctx.dq.log("sensor_spike", "iot", "sensor_reading", str(rid), "value", None, None, f"{sid} impossible reading")
        elif nt == "late":
            ctx.dq.log("late_arriving", "iot", "sensor_reading", str(rid), "ingested_at", None, None, f"{sid} reading landed 2-7 days late")
        elif nt == "dup":
            ctx.dq.log("duplicate_row", "iot", "sensor_reading", str(rid), None, None, None, f"{sid} gateway resent device_ts_ms {dev}")
    st["buffer"] = {k: v[~due] for k, v in b.items()}


def _door_events(ctx: Ctx, st: dict[str, Any], ws: int, we: int, rng: np.random.Generator) -> None:
    """OPEN / CLOSE events: inbound trailers and LTL loads logged by the WMS, parcel pickups, and the planted door 14.

    WMS door activity arrives once (its CLOSE may fall in a later window), so all events go through a buffer and are
    inserted when the controller lands them; the synthetic per-day events are regenerated only for this window."""
    wst = ctx.world.get("wms", {})
    rows = [(int(wi), int(door), tr, int(t0), int(t1)) for (wi, door, tr, t0, t1) in wst.get("door_log", [])]
    synth = []
    for wi in sorted(wst.get("wh_ready", set())):
        wh = ctx.rules["warehouses"][wi]
        tz = wh["time_zone"]
        l0 = int(geo.utc_to_local(tz, np.array([ws]))[0])
        l1 = int(geo.utc_to_local(tz, np.array([we]))[0])
        for d in range(l0 // DAY, l1 // DAY + 1):
            if (d + 3) % 7 < 5:
                r2 = np.random.default_rng(derive_seed(ctx.seed, "parcel-doors", wh["code"], d))
                k = int(r2.integers(2, 6))
                doors = r2.choice(np.arange(1, wh["dock_doors"] + 1), size=k, replace=False)
                for j, door in enumerate(doors):
                    close = d * DAY + int(r2.integers(16 * 3600, 19 * 3600))
                    synth.append((wi, int(door), f"{['UPSN', 'FDEG', 'USPS', 'DHLC'][j % 4]} {int(r2.integers(10000, 99999))}",
                                  close - int(r2.integers(25, 70)) * 60, close))
        story = _story(ctx, "cold_excursion_summer")
        if story and wh["code"] == story["warehouse"]:
            for d in range(l0 // DAY, l1 // DAY + 1):
                for _, e in cold_excursions(ctx, d).iterrows():
                    synth.append((wi, 14, f"RFR {int(e['start_local']) % 90000 + 10000}", int(e["start_local"]), int(e["close_local"])))
    frames = []
    for src, data in (("wms", rows), ("synth", synth)):
        if not data:
            continue
        df = pd.DataFrame(data, columns=["wi", "door", "trailer", "open", "close"])
        ev = pd.concat([df.assign(event_type="OPEN", t=df["open"]), df.assign(event_type="CLOSE", t=df["close"])], ignore_index=True)
        tz = np.array([ctx.rules["warehouses"][int(w)]["time_zone"] for w in ev["wi"]], dtype=object)
        ev["utc"] = geo.local_to_utc_many(tz, ev["t"].to_numpy(np.int64))
        if src == "synth":
            ev = ev[(ev["utc"] >= ws) & (ev["utc"] < we)]
        else:
            ev = ev[ev["utc"] >= ws - DAY]
        frames.append(ev)
    if frames:
        ev = pd.concat(frames, ignore_index=True)
        ev["ingested"] = ev["utc"].to_numpy(np.int64) + rng.integers(1, 20, len(ev))
        ev["code"] = [ctx.rules["warehouses"][int(w)]["code"] for w in ev["wi"]]
        st["doors"] = pd.concat([st.get("doors"), ev[["code", "door", "event_type", "trailer", "utc", "ingested"]]], ignore_index=True) \
            if st.get("doors") is not None and len(st["doors"]) else ev[["code", "door", "event_type", "trailer", "utc", "ingested"]]
    buf = st.get("doors")
    if buf is None or not len(buf):
        return
    due = buf["ingested"].to_numpy(np.int64) < we
    if not due.any():
        return
    out = buf[due]
    ids = ctx.next_ids("iot.dock_door_event", len(out))
    geo.copy_fast(ctx, "iot", "telemetry.dock_door_event", pd.DataFrame({
        "event_id": ids, "warehouse_code": out["code"].to_numpy(object), "door_nbr": out["door"].to_numpy(np.int64),
        "event_type": out["event_type"].to_numpy(object), "trailer_nbr": out["trailer"].to_numpy(object),
        "device_ts_ms": out["utc"].to_numpy(np.int64) * 1000 + rng.integers(-800, 800, len(out)),
        "ingested_at": pd.to_datetime(out["ingested"].to_numpy(np.int64), unit="s", utc=True)}))
    st["doors"] = buf[~due].reset_index(drop=True)


def stuck_sensor_window(ctx: Ctx) -> tuple[date, date] | None:
    """Answer-key helper: the stuck period of the planted sensor (UTC dates)."""
    w = _stuck_window(ctx)
    if not w:
        return None
    return geo.day_date(w[0] // DAY), geo.day_date(w[1] // DAY) - timedelta(days=0)


MODULE = IotModule()
