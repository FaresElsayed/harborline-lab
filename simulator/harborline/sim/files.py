"""Business files dropped in ``landing/files`` (written atomically: temp file + rename).

* ``carriers/rate_cards/<SCAC>_rates_<effective>.xlsx``: contracted zone x weight matrices per service and country,
  fuel-surcharge table and accessorials. A new version (general rate increase) every January. AX freight bills are
  priced from the same tariff (``freight_charges``), so the freight audit can recompute every line.
* ``finance/budget/budget_FY<yyyy>.xlsx`` (+ ``_reforecast``): wide months, merged headers, subtotal rows.
* ``facilities/warehouse_master_<date>.xlsx``: site master, a new version when a site opens.
* ``clients/<CODE>/item_master_<YYYYMMDD>[_delta].csv``: per-client format profile (delimiter, encoding, dates,
  headers), described in ``clients/<CODE>/file_spec.txt``.
* ``carrier/history_export/tracking_events_<yyyymm>.csv.gz``: carrier events older than the API's retention.
* ``reference/diesel_weekly.csv``: the weekly diesel price carrier bills are rated from, one more row each Monday.
* ``reference/signal_sources.csv``: what this build's FX, weather, holidays and diesel came from.
"""

from __future__ import annotations

import csv
import gzip
import io
import itertools
import math
import os
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import xlsxwriter

from ..core import Window, derive_seed
from .crm import (
    add_months,
    as_utc,
    dates_of,
    diesel,
    est_monthly_qty,
    module_state,
    preset_warehouses,
    rate_versions,
    scenario_client,
    shared_state,
    sim_clients,
    to_date,
    warehouse_opened,
)

# ======================================================================================================== tariff

TARIFF: dict[str, dict[str, Any]] = {
    "UPSN": {"kind": "parcel", "base": 9.85, "lb": 0.62, "zone": 0.115, "resi": 5.65, "ahs": 27.50, "das": 4.60,
             "svc": {"GND": 1.0, "3DS": 1.65, "2DA": 2.25, "NDA": 3.6}},
    "FDEG": {"kind": "parcel", "base": 9.70, "lb": 0.60, "zone": 0.112, "resi": 5.55, "ahs": 26.75, "das": 4.50,
             "svc": {"FXG": 1.0, "FHD": 1.02}},
    "FDE": {"kind": "parcel", "base": 10.40, "lb": 0.66, "zone": 0.120, "resi": 5.55, "ahs": 26.75, "das": 4.50,
            "svc": {"F2D": 2.2, "FPO": 3.8}},
    "USPS": {"kind": "parcel", "base": 7.90, "lb": 0.55, "zone": 0.100, "resi": 0.0, "ahs": 0.0, "das": 0.0,
             "svc": {"UGA": 1.0, "UPM": 1.35}},
    "DHLC": {"kind": "parcel", "base": 7.40, "lb": 0.50, "zone": 0.095, "resi": 0.0, "ahs": 0.0, "das": 0.0, "svc": {"DPE": 1.0}},
    "VRPX": {"kind": "parcel", "base": 8.20, "lb": 0.48, "zone": 0.090, "resi": 3.95, "ahs": 18.0, "das": 3.25, "svc": {"VRG": 1.0}},
    "CNPC": {"kind": "parcel", "base": 9.10, "lb": 0.58, "zone": 0.110, "resi": 0.0, "ahs": 15.0, "das": 0.0,
             "svc": {"CEX": 1.0, "CXP": 1.45}},
    "PURO": {"kind": "parcel", "base": 9.40, "lb": 0.60, "zone": 0.110, "resi": 2.95, "ahs": 19.0, "das": 0.0,
             "svc": {"PGR": 1.0, "PEX": 2.3}},
    "ESTF": {"kind": "parcel", "base": 7.10, "lb": 0.52, "zone": 0.100, "resi": 0.0, "ahs": 0.0, "das": 0.0,
             "svc": {"ETR": 1.0, "EDS": 1.8}},
    "ODFL": {"kind": "ltl", "cwt": 38.0, "zone": 0.09, "min": 145.0, "liftgate": 95.0, "resi": 110.0, "svc": {"LTL": 1.0}},
    "MRLF": {"kind": "ltl", "cwt": 34.5, "zone": 0.10, "min": 130.0, "liftgate": 90.0, "resi": 105.0, "svc": {"LTL": 1.0}},
    "EXLA": {"kind": "ltl", "cwt": 35.8, "zone": 0.095, "min": 138.0, "liftgate": 92.0, "resi": 108.0, "svc": {"LTL": 1.0}},
    "TRMX": {"kind": "ltl", "cwt": 30.0, "zone": 0.08, "min": 120.0, "liftgate": 70.0, "resi": 80.0, "svc": {"LTL": 1.0}},
}
COUNTRY_FACTOR = {"US": 1.0, "CA": 1.36, "MX": 18.6}
COUNTRY_CCY = {"US": "USD", "CA": "CAD", "MX": "MXN"}
UNIT = {"US": "LB", "CA": "KG", "MX": "KG"}
MAX_W = {"LB": 150, "KG": 68}
LTL_BREAKS = [0, 500, 1000, 2000, 5000, 10000]
LTL_BREAK_FACTOR = [1.0, 0.86, 0.74, 0.62, 0.52, 0.45]
AIR = {"3DS", "2DA", "NDA", "F2D", "FPO", "UPM", "CXP", "PEX", "EDS"}
ZONES = list(range(2, 9))
LB_PER_KG = 2.20462


def tariff_spec(scac: str) -> dict[str, Any]:
    return TARIFF.get(scac) or {"kind": "parcel", "base": 9.0, "lb": 0.55, "zone": 0.1, "resi": 4.0, "ahs": 20.0, "das": 3.5,
                                "svc": {}}


def gri_pct(scac: str, year: int) -> float:
    kind = tariff_spec(scac)["kind"]
    lo, hi = (4.9, 6.9) if kind == "parcel" else (3.9, 5.9)
    r = np.random.default_rng(derive_seed("gri", scac, year))
    return round(float(r.uniform(lo, hi)), 1)


def gri_factor(scac: str, year: int) -> float:
    f = 1.0
    for y in range(2021, year + 1):
        f *= 1 + gri_pct(scac, y) / 100
    return f


def rate_effective(year: int) -> date:
    """General rate increases take effect the first Monday of January."""
    d = date(year, 1, 1)
    return d + timedelta(days=(7 - d.weekday()) % 7)


def tariff_year(d: date) -> int:
    return d.year if d >= rate_effective(d.year) else d.year - 1


def svc_mult(scac: str, svc: str) -> float:
    return float(tariff_spec(scac)["svc"].get(svc, 1.0))


def parcel_rate(scac: str, svc: str, country: str, year: int, zone: np.ndarray, weight: np.ndarray) -> np.ndarray:
    """Net contracted charge for whole units of weight (LB in the US, KG elsewhere)."""
    t = tariff_spec(scac)
    unit = UNIT.get(country, "LB")
    w = np.clip(np.ceil(np.asarray(weight, dtype=float)), 1, MAX_W[unit])
    per = t["lb"] * (LB_PER_KG if unit == "KG" else 1.0)
    z = np.clip(np.asarray(zone, dtype=float), 2, 8)
    raw = (t["base"] + per * (w - 1)) * (1 + t["zone"] * (z - 2)) * svc_mult(scac, svc)
    return np.round(raw * gri_factor(scac, year) * COUNTRY_FACTOR.get(country, 1.0), 2)


def ltl_cwt(scac: str, country: str, year: int, zone: np.ndarray, weight_lb: np.ndarray) -> np.ndarray:
    t = tariff_spec(scac)
    idx = np.searchsorted(LTL_BREAKS, np.asarray(weight_lb, dtype=float), side="right") - 1
    factor = np.asarray(LTL_BREAK_FACTOR)[np.clip(idx, 0, len(LTL_BREAKS) - 1)]
    z = np.clip(np.asarray(zone, dtype=float), 2, 8)
    raw = t["cwt"] * (1 + t["zone"] * (z - 2)) * factor
    return np.round(raw * gri_factor(scac, year) * COUNTRY_FACTOR.get(country, 1.0), 2)


def ltl_charge(scac: str, country: str, year: int, zone: np.ndarray, weight_lb: np.ndarray) -> np.ndarray:
    t = tariff_spec(scac)
    w = np.maximum(np.asarray(weight_lb, dtype=float), 1)
    minimum = round(t["min"] * gri_factor(scac, year) * COUNTRY_FACTOR.get(country, 1.0), 2)
    return np.maximum(minimum, np.round(ltl_cwt(scac, country, year, zone, w) * w / 100, 2))


def accessorials(scac: str, country: str, year: int) -> dict[str, float]:
    t = tariff_spec(scac)
    f = gri_factor(scac, year) * COUNTRY_FACTOR.get(country, 1.0)
    keys = ("liftgate", "resi") if t["kind"] == "ltl" else ("resi", "ahs", "das")
    return {k: round(float(t.get(k, 0.0)) * f, 2) for k in keys}


def fuel_pct(scac: str, svc: str, diesel_usd: float) -> float:
    steps = max(0, math.floor(round((diesel_usd - 3.00) / 0.10, 6)))
    if tariff_spec(scac)["kind"] == "ltl":
        return round(24.0 + 0.8 * steps, 2)
    return round(12.0 + 0.25 * steps + (3.5 if svc in AIR else 0.0), 2)


def das_zip(postal: Any) -> bool:
    """Delivery-area-surcharge ZIPs: a deterministic ~6% of postal codes."""
    return derive_seed("das", str(postal)[:5]) % 100 < 6


# ======================================================================================================== writing


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def write_xlsx(path: Path, build: Any) -> None:
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    build(wb)
    wb.close()
    atomic_write_bytes(path, buf.getvalue())


def fdir(ctx: Any) -> Path:
    return Path(ctx.landing_dir) / "files"


# ======================================================================================================== rate cards


def scacs_in_scope(ctx: Any) -> list[str]:
    countries = {w["address"]["country"] for w in preset_warehouses(ctx).values()}
    return [c["scac"] for c in ctx.rules["carriers"] if countries & set(c.get("countries", []))]


def write_rate_card(ctx: Any, scac: str, year: int) -> Path:
    carrier = next(c for c in ctx.rules["carriers"] if c["scac"] == scac)
    eff = rate_effective(year)
    path = fdir(ctx) / "carriers" / "rate_cards" / f"{scac}_rates_{eff:%Y%m%d}.xlsx"
    countries = [c for c in carrier.get("countries", ["US"]) if c in COUNTRY_FACTOR]
    t = tariff_spec(scac)
    diesel_bands = np.round(np.arange(2.50, 5.01, 0.10), 2)

    def build(wb: Any) -> None:
        bold = wb.add_format({"bold": True})
        title = wb.add_format({"bold": True, "font_size": 14})
        money = wb.add_format({"num_format": "#,##0.00"})
        pct = wb.add_format({"num_format": "0.00"})
        head = wb.add_format({"bold": True, "bg_color": "#DDE6F0", "border": 1, "align": "center"})
        cover = wb.add_worksheet("Cover")
        cover.set_column(0, 0, 28)
        cover.set_column(1, 1, 60)
        cover.write(0, 0, f"{carrier['name']} - Contract Rate Card", title)
        info = [("SCAC", scac), ("Shipper", ctx.rules["company"]["legal_name"]),
                ("Account", f"HL{derive_seed('acct', scac) % 900000 + 100000}"), ("Effective date", eff.isoformat()),
                ("General rate increase", f"{gri_pct(scac, year):.1f}%"), ("Mode", carrier["mode"]),
                ("Currency", ", ".join(f"{c}: {COUNTRY_CCY[c]}" for c in countries)),
                ("Weight basis", ", ".join(f"{c}: {UNIT[c]}" for c in countries)),
                ("Notes", ("Net contracted rates (discounts applied). Fuel surcharge per the Fuel Surcharge tab, "
                          "based on the weekly on-highway diesel reference price published each Monday "
                          "(reference/diesel_weekly.csv)."))]
        for i, (k, v) in enumerate(info, start=2):
            cover.write(i, 0, k, bold)
            cover.write(i, 1, v)
        for country in countries:
            for svc in t["svc"]:
                ws = wb.add_worksheet(f"{svc}_{country}"[:31])
                if t["kind"] == "parcel":
                    unit = UNIT[country]
                    ws.write(0, 0, f"Weight ({unit})", head)
                    for j, z in enumerate(ZONES, start=1):
                        ws.write(0, j, f"Zone {z}", head)
                    weights = np.arange(1, MAX_W[unit] + 1)
                    for j, z in enumerate(ZONES, start=1):
                        col = parcel_rate(scac, svc, country, year, np.full(len(weights), z), weights)
                        ws.write_column(1, j, col.tolist(), money)
                    ws.write_column(1, 0, weights.tolist())
                else:
                    ws.write(0, 0, "Weight break (LB)", head)
                    for j, z in enumerate(ZONES, start=1):
                        ws.write(0, j, f"Zone {z}", head)
                    labels = [f"{a}-{b - 1}" for a, b in itertools.pairwise(LTL_BREAKS)] + [f"{LTL_BREAKS[-1]}+"]
                    for i, (lbl, w) in enumerate(zip(labels, LTL_BREAKS, strict=True), start=1):
                        ws.write(i, 0, lbl)
                        for j, z in enumerate(ZONES, start=1):
                            ws.write(i, j, float(ltl_cwt(scac, country, year, np.array([z]), np.array([max(w, 1)]))[0]), money)
                    n = len(labels) + 2
                    ws.write(n, 0, "Rates per 100 lb (CWT). Minimum charge:", bold)
                    ws.write(n, 1, float(ltl_charge(scac, country, year, np.array([2]), np.array([1]))[0]), money)
                ws.freeze_panes(1, 1)
                ws.set_column(0, 0, 18)
        fuel = wb.add_worksheet("Fuel Surcharge")
        cols = ["Diesel from (USD/gal)", "Diesel to (USD/gal)"] + (["LTL %"] if t["kind"] == "ltl" else ["Ground %", "Air %"])
        for j, c in enumerate(cols):
            fuel.write(0, j, c, head)
        for i, lo in enumerate(diesel_bands, start=1):
            fuel.write(i, 0, float(lo), money)
            fuel.write(i, 1, round(float(lo) + 0.099, 3), money)
            if t["kind"] == "ltl":
                fuel.write(i, 2, fuel_pct(scac, "LTL", float(lo) + 0.001), pct)
            else:
                fuel.write(i, 2, fuel_pct(scac, "GND", float(lo) + 0.001), pct)
                fuel.write(i, 3, fuel_pct(scac, "NDA", float(lo) + 0.001), pct)
        fuel.set_column(0, 3, 22)
        acc = wb.add_worksheet("Accessorials")
        for j, c in enumerate(["Code", "Description"] + [f"Amount ({COUNTRY_CCY[c]})" for c in countries]):
            acc.write(0, j, c, head)
        names = {"resi": "Residential delivery surcharge", "ahs": "Additional handling (over 50 lb / 23 kg)",
                 "das": "Delivery area surcharge", "liftgate": "Liftgate delivery"}
        for i, key in enumerate(accessorials(scac, countries[0], year), start=1):
            acc.write(i, 0, key.upper())
            acc.write(i, 1, names[key])
            for j, c in enumerate(countries, start=2):
                acc.write(i, j, accessorials(scac, c, year)[key], money)
        acc.set_column(0, 0, 10)
        acc.set_column(1, 1, 42)
        acc.set_column(2, 4, 16)

    write_xlsx(path, build)
    ctx.changes.add("files", "carrier_rate_card", ins=1)
    return path


# ======================================================================================================== reference

DIESEL_HEADER = ("week_start", "diesel_usd_per_gal")
DIESEL_WEEKS_BEFORE_HISTORY = 2         # so the first bills in the history can always look their week up
SOURCES_HEADER = ("signal", "key", "source", "real_from", "real_to", "note")


def diesel_week_published(now: datetime) -> date:
    """The latest week whose price is out at ``now``: each week's price is published on its Monday at 12:00 UTC."""
    day = (as_utc(now) - timedelta(hours=12)).date()
    return day - timedelta(days=day.weekday())


def write_diesel_series(ctx: Any, through: date) -> Path:
    """The whole series, from before the first bill to ``through``. It comes straight from the signal billing reads, so
    a build made before this file existed gets the same prices as billing used when it first runs a batch."""
    first = as_utc(ctx.history_start).date()
    week = first - timedelta(days=first.weekday(), weeks=DIESEL_WEEKS_BEFORE_HISTORY)
    rows = [",".join(DIESEL_HEADER)]
    while week <= through:
        rows.append(f"{week.isoformat()},{diesel(ctx, week):.3f}")
        week += timedelta(days=7)
    path = fdir(ctx) / "reference" / "diesel_weekly.csv"
    atomic_write_bytes(path, ("\n".join(rows) + "\n").encode("ascii"))
    return path


def write_signal_sources(ctx: Any) -> Path:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, SOURCES_HEADER, lineterminator="\n")
    writer.writeheader()
    writer.writerows(ctx.signals.source_rows())
    path = fdir(ctx) / "reference" / "signal_sources.csv"
    atomic_write_bytes(path, buf.getvalue().encode("utf-8"))
    return path


# ======================================================================================================== budget

SITE_GL = [("4000", "Storage Revenue"), ("4010", "Handling Revenue"), ("4020", "Value-Added Services Revenue"),
           ("4030", "Freight Recovery Revenue"), ("5000", "Direct Labor"), ("5100", "Outbound Freight Expense"),
           ("5200", "Packaging Materials"), ("6000", "Facility Rent"), ("6100", "Utilities"), ("6200", "Equipment Leases")]
CORP_GL = [("6300", "IT & Software"), ("6400", "Insurance"), ("7000", "SG&A Salaries")]
REVENUE_GL = {"4000", "4010", "4020", "4030"}
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
SEASON = np.array([0.92, 0.88, 0.95, 0.96, 0.98, 0.97, 0.98, 1.0, 1.0, 1.05, 1.22, 1.30])


def budget_estimate(ctx: Any, fy: int) -> dict[tuple[str, str], np.ndarray]:
    """Plan built from contracts (no actuals yet): expected client revenue per site and a cost model."""
    scale = float(ctx.rules["size_presets"].get(ctx.preset, {}).get("volume_scale", 1.0))
    gl_of = {c["code"]: c["gl"] for c in ctx.rules["charge_codes"]}
    out: dict[tuple[str, str], np.ndarray] = {}
    sites = preset_warehouses(ctx)
    for c in sim_clients(ctx):
        d = dates_of(ctx, c)
        versions = rate_versions(ctx, c)
        if not versions:
            continue
        qty = est_monthly_qty(ctx.rules, c, list(versions[0].rates))
        fx = 1.0 if c["currency"] == "USD" else 1 / float(ctx.signals.fx(date(fy - 1, 11, 15), c["currency"]))
        for m in range(12):
            first = date(fy, m + 1, 1)
            if d.go_live > first + timedelta(days=27):
                continue
            ramp = min(1.0, max(0.25, (first - d.go_live).days / 120)) if d.go_live <= first else 0.25
            for code, q in qty.items():
                rate = versions[-1].rates.get(code, 0.0)
                for wh in c["warehouses"]:
                    key = (wh, gl_of.get(code, "4020"))
                    out.setdefault(key, np.zeros(12))[m] += q * rate * fx * scale * ramp * SEASON[m] / len(c["warehouses"])
    for wh in sites:
        rev = sum(out.get((wh, g), np.zeros(12)) for g in REVENUE_GL)
        out[(wh, "4030")] = out.get((wh, "4030"), np.zeros(12)) + rev * 0.08
        rev = sum(out.get((wh, g), np.zeros(12)) for g in REVENUE_GL)
        out[(wh, "5000")] = rev * 0.41
        out[(wh, "5100")] = rev * 0.11
        out[(wh, "5200")] = rev * 0.03
        for gl, monthly in fixed_site_costs(ctx, wh, fy).items():
            out[(wh, gl)] = monthly
    for gl, monthly in corp_costs(fy).items():
        out[("CORP", gl)] = monthly
    return out


RENT_PSF = {"ATL1": 6.25, "DFW1": 5.75, "RNO1": 7.40, "CHI1": 9.80, "NJ1": 13.50, "LAX1": 15.20, "TOR1": 14.0, "MTY1": 95.0}


def rent_monthly(ctx: Any, wh: dict[str, Any], d: date) -> float:
    """Monthly rent in the site's local currency, escalating 3% every January."""
    base = wh["sqft"] * RENT_PSF.get(wh["code"], 8.0) / 12
    return round(base * 1.03 ** max(0, d.year - 2022), 2)


def utilities_monthly(wh: dict[str, Any], d: date) -> float:
    cold = wh.get("profile") == "cold_chain"
    rate = 0.16 if cold else 0.045
    m = d.month
    season = 1.0 + (0.25 if m in (6, 7, 8, 9) and (cold or wh["code"] in ("DFW1", "LAX1", "MTY1")) else 0.0) \
        + (0.20 if m in (12, 1, 2) and wh["code"] in ("CHI1", "NJ1", "TOR1") else 0.0)
    local = {"TOR1": 1.36, "MTY1": 18.6}.get(wh["code"], 1.0)
    return round(wh["sqft"] * rate * season * local, 2)


def equipment_monthly(wh: dict[str, Any]) -> float:
    local = {"TOR1": 1.36, "MTY1": 18.6}.get(wh["code"], 1.0)
    return round(wh["pallet_positions"] * 0.92 * local, 2)


def fixed_site_costs(ctx: Any, wh_code: str, fy: int) -> dict[str, np.ndarray]:
    wh = preset_warehouses(ctx)[wh_code]
    to_usd = {"TOR1": 1 / 1.36, "MTY1": 1 / 18.6}.get(wh_code, 1.0)
    opened = warehouse_opened(ctx, wh)
    rent, util, equip = np.zeros(12), np.zeros(12), np.zeros(12)
    for m in range(12):
        d = date(fy, m + 1, 1)
        if d < opened.replace(day=1):
            continue
        rent[m] = rent_monthly(ctx, wh, d) * to_usd
        util[m] = utilities_monthly(wh, d) * to_usd
        equip[m] = equipment_monthly(wh) * to_usd
    return {"6000": rent, "6100": util, "6200": equip}


SGA_MONTHLY = {"EXEC": 85000, "FIN": 120000, "SLS": 140000, "CS": 90000, "IT": 130000, "QA": 40000, "HR": 35000}


def corp_costs(fy: int) -> dict[str, np.ndarray]:
    g = 1.035 ** max(0, fy - 2023)
    return {"6300": np.full(12, 64000.0 * g), "6400": np.full(12, 38000.0 * g), "7000": np.full(12, sum(SGA_MONTHLY.values()) * g)}


def ledger_actuals(ctx: Any, start: date, end: date) -> dict[tuple[str, str], np.ndarray]:
    """Monthly actuals in USD (reporting currency) by (site, main account) from the AX general journal."""
    rows = ctx.conn("ax2012").execute(
        "SELECT split_part(a.ledgeraccount, '-', 1), coalesce(nullif(split_part(a.ledgeraccount, '-', 2), ''), 'CORP'), "
        "extract(month FROM e.accountingdate)::int, sum(a.reportingcurrencyamount) "
        "FROM dbo.generaljournalaccountentry a JOIN dbo.generaljournalentry e ON e.recid = a.generaljournalentry "
        "WHERE e.accountingdate >= %s AND e.accountingdate < %s AND split_part(a.ledgeraccount, '-', 1) >= '4000' "
        "GROUP BY 1, 2, 3", (start, end)).fetchall()
    out: dict[tuple[str, str], np.ndarray] = {}
    for gl, site, month, amount in rows:
        sign = -1.0 if gl.startswith("4") else 1.0
        out.setdefault((site, gl), np.zeros(12))[month - 1] += sign * float(amount or 0)
    return out


def budget_from_actuals(ctx: Any, fy: int, asof: date) -> dict[tuple[str, str], np.ndarray]:
    plan = budget_estimate(ctx, fy)
    act = ledger_actuals(ctx, date(fy - 1, 1, 1), asof.replace(day=1))
    months = asof.month - 1
    if months < 6:
        return plan
    r = np.random.default_rng(derive_seed(ctx.seed, "budget", fy))
    out: dict[tuple[str, str], np.ndarray] = {}
    for key in set(plan) | set(act):
        a = act.get(key, np.zeros(12))
        if key[1] in ("6000", "6100", "6200") or key[0] == "CORP" or not a[:months].any():
            out[key] = plan.get(key, np.zeros(12))
            continue
        p = plan.get(key, np.zeros(12))
        base = np.where(a != 0, a, p)
        last = max(i for i in range(months) if a[i] != 0)
        for m in range(months, 12):
            base[m] = a[last] * SEASON[m] / SEASON[last]
        growth = float(r.uniform(1.04, 1.09)) if key[1] in REVENUE_GL else float(r.uniform(1.02, 1.06))
        out[key] = base * growth
    return out


def write_budget(ctx: Any, fy: int, values: dict[tuple[str, str], np.ndarray], prepared: date, version: str,
                 actual_months: int = 0) -> Path:
    suffix = "" if version == "Original Budget" else "_reforecast"
    path = fdir(ctx) / "finance" / "budget" / f"budget_FY{fy}{suffix}.xlsx"
    sites = list(preset_warehouses(ctx).values())

    def build(wb: Any) -> None:
        ws = wb.add_worksheet(f"FY{fy} {'Budget' if not suffix else 'Reforecast'}")
        title = wb.add_format({"bold": True, "font_size": 14})
        sub = wb.add_format({"italic": True, "font_color": "#555555"})
        head = wb.add_format({"bold": True, "bg_color": "#0E2A47", "font_color": "#FFFFFF", "align": "center", "border": 1})
        head2 = wb.add_format({"bold": True, "bg_color": "#DDE6F0", "align": "center", "border": 1})
        section = wb.add_format({"bold": True, "bg_color": "#F4EFE6"})
        num = wb.add_format({"num_format": "#,##0;(#,##0)"})
        total = wb.add_format({"num_format": "#,##0;(#,##0)", "bold": True, "top": 1})
        total_lbl = wb.add_format({"bold": True, "top": 1})
        ws.merge_range(0, 0, 0, 16, f"{ctx.rules['company']['legal_name']} - FY{fy} Operating {'Budget' if not suffix else 'Reforecast'}", title)
        ws.merge_range(1, 0, 1, 16, f"All amounts in USD. Prepared by FP&A on {prepared.isoformat()}. Version: {version}."
                       + (f" Jan-{MONTHS[actual_months - 1]} = actuals." if actual_months else ""), sub)
        ws.merge_range(3, 0, 3, 2, "Line item", head)
        for q in range(4):
            ws.merge_range(3, 3 + q * 3, 3, 5 + q * 3, f"Q{q + 1}", head)
        ws.write(3, 15, "FY", head)
        ws.write(3, 16, "", head)
        for j, h in enumerate(["Site", "Account", "Description"] + [f"{m}-{str(fy)[2:]}" + (" A" if i < actual_months else "")
                                                                     for i, m in enumerate(MONTHS)] + [f"FY{fy} Total", "Comment"]):
            ws.write(4, j, h, head2)
        row = 5
        totals: dict[str, list[int]] = {"rev": [], "opex": []}

        def write_line(site: str, gl: str, name: str, vals: np.ndarray) -> int:
            nonlocal row
            ws.write(row, 0, site)
            ws.write(row, 1, gl)
            ws.write(row, 2, name)
            for m in range(12):
                ws.write_number(row, 3 + m, round(float(vals[m])), num)
            ws.write_formula(row, 15, f"=SUM(D{row + 1}:O{row + 1})", num, round(float(vals.sum())))
            row += 1
            return row - 1

        def write_total(label: str, rows: list[int], sign: list[int] | None = None) -> int:
            nonlocal row
            ws.write(row, 2, label, total_lbl)
            for m in range(16 - 3):
                col = 3 + m
                letter = xlsxwriter.utility.xl_col_to_name(col)
                parts = [("-" if sign and s < 0 else "+") + f"{letter}{r + 1}" for r, s in zip(rows, sign or [1] * len(rows), strict=True)]
                formula = "=" + "".join(parts).lstrip("+")
                value = sum((s if sign else 1) * cell_values.get((r, col), 0.0) for r, s in zip(rows, sign or [1] * len(rows), strict=True))
                ws.write_formula(row, col, formula, total, round(value))
                cell_values[(row, col)] = value
            row += 1
            return row - 1

        cell_values: dict[tuple[int, int], float] = {}
        company_rev, company_cost = [], []
        for wh in sites:
            ws.merge_range(row, 0, row, 16, f"{wh['code']} - {wh['name']}", section)
            row += 1
            rev_rows, cost_rows = [], []
            for gl, name in SITE_GL:
                vals = values.get((wh["code"], gl), np.zeros(12))
                r_ = write_line(wh["code"], gl, name, vals)
                for m in range(12):
                    cell_values[(r_, 3 + m)] = round(float(vals[m]))
                cell_values[(r_, 15)] = round(float(vals.sum()))
                (rev_rows if gl in REVENUE_GL else cost_rows).append(r_)
            tr = write_total("Total Revenue", rev_rows)
            tc = write_total("Total Operating Expense", cost_rows)
            write_total("Site Contribution", [tr, tc], [1, -1])
            company_rev.append(tr)
            company_cost.append(tc)
            totals["rev"].append(tr)
            row += 1
        ws.merge_range(row, 0, row, 16, "CORP - Corporate / shared services", section)
        row += 1
        corp_rows = []
        for gl, name in CORP_GL:
            vals = values.get(("CORP", gl), np.zeros(12))
            r_ = write_line("CORP", gl, name, vals)
            for m in range(12):
                cell_values[(r_, 3 + m)] = round(float(vals[m]))
            cell_values[(r_, 15)] = round(float(vals.sum()))
            corp_rows.append(r_)
        tcorp = write_total("Total Corporate Expense", corp_rows)
        row += 1
        ws.merge_range(row, 0, row, 16, "TOTAL COMPANY", section)
        row += 1
        trev = write_total("Total Revenue", company_rev)
        tcost = write_total("Total Expense", company_cost + [tcorp])
        write_total("Operating Income (EBITDA)", [trev, tcost], [1, -1])
        ws.set_column(0, 0, 7)
        ws.set_column(1, 1, 9)
        ws.set_column(2, 2, 32)
        ws.set_column(3, 15, 11)
        ws.set_column(16, 16, 24)
        ws.freeze_panes(5, 3)
        a = wb.add_worksheet("Assumptions")
        a.write(0, 0, "Budget assumptions", title)
        a.write_row(2, 0, ["Currency", "Budget rate (USD per unit)"], head2)
        for i, ccy in enumerate(["CAD", "MXN", "EUR", "GBP"], start=3):
            a.write(i, 0, ccy)
            a.write_number(i, 1, round(1 / float(ctx.signals.fx(prepared, ccy)), 4))
        a.write(8, 0, "Revenue growth on prior-year run rate: 4-9%. Rent escalates 3% each January.")
        a.write(9, 0, "Labor at 41% of revenue for new sites; actual run rate for existing sites.")
        a.set_column(0, 0, 24)
        a.set_column(1, 1, 26)

    write_xlsx(path, build)
    ctx.changes.add("files", "budget", ins=1)
    return path


# ======================================================================================================== warehouse master


def write_warehouse_master(ctx: Any, asof: date) -> Path:
    path = fdir(ctx) / "facilities" / f"warehouse_master_{asof:%Y%m%d}.xlsx"
    whs = list(preset_warehouses(ctx).values())
    gm = {"ATL1": "Shanice Carter"}

    def build(wb: Any) -> None:
        head = wb.add_format({"bold": True, "bg_color": "#0E2A47", "font_color": "#FFFFFF", "border": 1})
        dfmt = wb.add_format({"num_format": "yyyy-mm-dd"})
        ws = wb.add_worksheet("Sites")
        cols = ["Site Code", "Site Name", "Status", "Street", "City", "State/Prov", "Postal Code", "Country", "Latitude",
                "Longitude", "Time Zone", "Legal Entity", "Weight UOM", "Dim UOM", "Square Feet", "Dock Doors",
                "Pallet Positions", "Pick Faces", "Temperature Zones", "Parcel Cutoff", "LTL Cutoff", "Shifts", "Opened",
                "General Manager"]
        ws.write_row(0, 0, cols, head)
        for i, w in enumerate(whs, start=1):
            opened = warehouse_opened(ctx, w)
            status = "ACTIVE" if opened <= asof else "PLANNED"
            a = w["address"]
            vals = [w["code"], w["name"], status, a["street"], a["city"], a["state"], a["postal"], a["country"], a["lat"], a["lon"],
                    w["time_zone"], w["legal_entity"].upper(), w["uom"]["weight"], w["uom"]["dim"], w["sqft"], w["dock_doors"],
                    w["pallet_positions"], w["pick_faces"], ", ".join(w["zones"]), w["cutoffs"]["parcel"], w["cutoffs"]["ltl"],
                    "; ".join(f"{k} {v}" for k, v in w["shifts"].items()), None, gm.get(w["code"], "")]
            ws.write_row(i, 0, vals)
            ws.write_datetime(i, 22, datetime.combine(opened, datetime.min.time()), dfmt)
        ws.freeze_panes(1, 1)
        ws.set_column(0, 0, 9)
        ws.set_column(1, 1, 30)
        ws.set_column(3, 3, 32)
        ws.set_column(4, 23, 14)
        z = wb.add_worksheet("Zones")
        z.write_row(0, 0, ["Site Code", "Zone", "Temp Min C", "Temp Max C"], head)
        i = 1
        for w in whs:
            for zone in w["zones"]:
                cc = ctx.rules["cold_chain"].get(zone, {})
                z.write_row(i, 0, [w["code"], zone, cc.get("min_c"), cc.get("max_c")])
                i += 1
        z.set_column(0, 3, 14)

    write_xlsx(path, build)
    ctx.changes.add("files", "warehouse_master", ins=1)
    return path


# ======================================================================================================== item masters

CANON = ["sku", "description", "upc", "base_uom", "unit_weight", "weight_uom", "length", "width", "height", "dim_uom",
         "case_qty", "cases_per_pallet", "lot_controlled", "serial_controlled", "shelf_life_days", "temp_class",
         "hazmat_class", "unit_value", "country_of_origin", "hs_code", "status", "last_updated"]
HEADERS = {
    "snake": {c: c for c in CANON},
    "title": {"sku": "Item Number", "description": "Item Description", "upc": "UPC", "base_uom": "Base UOM",
              "unit_weight": "Unit Weight", "weight_uom": "Weight UOM", "length": "Length", "width": "Width", "height": "Height",
              "dim_uom": "Dim UOM", "case_qty": "Case Pack", "cases_per_pallet": "Cases Per Pallet",
              "lot_controlled": "Lot Controlled", "serial_controlled": "Serial Controlled", "shelf_life_days": "Shelf Life (Days)",
              "temp_class": "Temperature Class", "hazmat_class": "Hazmat Class", "unit_value": "Unit Cost",
              "country_of_origin": "Country of Origin", "hs_code": "HTS Code", "status": "Status", "last_updated": "Last Updated"},
    "upper": {"sku": "ITEM_NO", "description": "ITEM_DESC", "upc": "UPC_CODE", "base_uom": "UOM", "unit_weight": "WEIGHT",
              "weight_uom": "WT_UOM", "length": "LEN", "width": "WID", "height": "HGT", "dim_uom": "DIM_UOM", "case_qty": "CASE_QTY",
              "cases_per_pallet": "CS_PER_PLT", "lot_controlled": "LOT_FLAG", "serial_controlled": "SERIAL_FLAG",
              "shelf_life_days": "SHELF_LIFE", "temp_class": "TEMP_CLASS", "hazmat_class": "HAZ_CLASS", "unit_value": "UNIT_COST",
              "country_of_origin": "COO", "hs_code": "HTS", "status": "ITEM_STATUS", "last_updated": "LAST_UPD"},
    "camel": {c: "".join(p.capitalize() for p in c.split("_")) for c in CANON},
}
DRIFT_HEADER = {"snake": "case_gtin", "title": "Case GTIN", "upper": "CASE_GTIN", "camel": "CaseGtin"}
EU_CODES = {"BRUNE", "LUMIERE", "NORDHS"}


@lru_cache(maxsize=512)
def client_profile(seed: int, code: str, currency: str) -> dict[str, Any]:
    r = np.random.default_rng(derive_seed(seed, "csvprofile", code))
    eu = code in EU_CODES or currency == "EUR"
    delim = ";" if eu else str(r.choice([",", ",", ";", "|", "\t"]))
    enc = "cp1252" if eu or currency == "MXN" else str(r.choice(["utf-8", "utf-8", "utf-8-sig", "cp1252"]))
    date_fmt = str(r.choice(["%d.%m.%Y", "%d/%m/%Y"])) if eu else str(r.choice(["%Y-%m-%d", "%m/%d/%Y", "%Y%m%d", "%m/%d/%Y %H:%M"]))
    return {
        "delimiter": delim, "encoding": enc, "date_format": date_fmt, "header": str(r.choice(list(HEADERS))),
        "bool": [list(x) for x in [("Y", "N"), ("TRUE", "FALSE"), ("1", "0"), ("Yes", "No")]][int(r.integers(0, 4))],
        "decimal": "," if delim != "," and (eu or r.random() < 0.15) else ".",
        "cadence": str(r.choice(["weekly_delta", "weekly_full", "monthly_full"], p=[0.5, 0.2, 0.3])),
        "weekday": int(r.integers(0, 5)), "newline": "\r\n" if enc == "cp1252" else "\n",
    }


def spec_text(code: str, p: dict[str, Any]) -> str:
    delim = {",": "comma (,)", ";": "semicolon (;)", "|": "pipe (|)", "\t": "tab"}[p["delimiter"]]
    enc = {"utf-8": "UTF-8", "utf-8-sig": "UTF-8 with BOM", "cp1252": "Windows-1252 (ANSI)"}[p["encoding"]]
    cad = {"weekly_delta": "weekly delta (new/changed items only, file name ends _delta)", "weekly_full": "weekly full snapshot",
           "monthly_full": "monthly full snapshot"}[p["cadence"]]
    return (f"Client: {code}\nItem master interface specification (as agreed at onboarding)\n\n"
            f"File name: item_master_YYYYMMDD.csv (delta files: item_master_YYYYMMDD_delta.csv)\n"
            f"Delimiter: {delim}\nEncoding: {enc}\nDecimal separator: '{p['decimal']}'\n"
            f"Date format: {p['date_format'].replace('%', '')}\nBooleans: {p['bool'][0]}/{p['bool'][1]}\n"
            f"Header style: {p['header']}\nCadence: {cad}\nLine endings: {'CRLF' if p['newline'] == chr(13) + chr(10) else 'LF'}\n")


def material_frame(ctx: Any, where: str, params: tuple) -> pd.DataFrame:
    rows = ctx.conn("wms").execute(
        "SELECT o.owner_code, m.sku, m.description, m.upc, m.base_uom, m.unit_weight, m.weight_uom, m.unit_length, "
        "m.unit_width, m.unit_height, m.dim_uom, m.case_qty, m.cases_per_pallet, m.is_lot_controlled, m.is_serial_controlled, "
        "m.shelf_life_days, m.temp_class, m.hazmat_class, m.unit_value, m.country_of_origin, m.hs_code, m.material_status, "
        "m.mod_ts, m.create_ts FROM wms.material m JOIN wms.owner o ON o.owner_id = m.owner_id WHERE " + where, params).fetchall()
    cols = ["owner_code"] + CANON[:-1] + ["mod_ts", "create_ts"]
    return pd.DataFrame(rows, columns=cols)


def render_item_csv(ctx: Any, code: str, df: pd.DataFrame, prof: dict[str, Any], drift: bool, r: np.random.Generator,
                    path: Path, ts: datetime) -> bytes:
    out = pd.DataFrame(index=df.index)
    t, f = prof["bool"]
    for c in CANON:
        if c == "last_updated":
            out[c] = pd.to_datetime(df["mod_ts"]).dt.strftime(prof["date_format"])
        elif c in ("lot_controlled", "serial_controlled"):
            out[c] = np.where(df[c].fillna(False).astype(bool), t, f)
        elif c in ("unit_weight", "length", "width", "height", "unit_value"):
            vals = pd.to_numeric(df[c], errors="coerce").astype(float).round(3)
            txt = vals.map(lambda v: "" if pd.isna(v) else f"{v:g}")
            out[c] = txt.str.replace(".", ",", regex=False) if prof["decimal"] == "," else txt
        elif c in ("case_qty", "cases_per_pallet", "shelf_life_days"):
            out[c] = pd.to_numeric(df[c], errors="coerce").map(lambda v: "" if pd.isna(v) else str(int(v)))
        else:
            out[c] = df[c].astype(object).where(df[c].notna(), "")
    if drift:
        upc = df["upc"].fillna("").astype(str)
        out.insert(3, "case_gtin", np.where(upc.str.len() >= 12, "1" + upc.str.zfill(13).str[-13:], ""))
    n = len(out)
    for cat, col in (("mojibake", "description"), ("key_whitespace_case", "sku"), ("null_required", "base_uom")):
        hits = np.flatnonzero(ctx.dq.mask(cat, n, r)) if n else []
        for i in hits:
            before = str(out.iat[i, out.columns.get_loc(col)])
            if cat == "mojibake":
                after = before.replace("e", "Ã©", 1) if "e" in before else before + " â€“"
            elif cat == "key_whitespace_case":
                after = before.lower() + " " if before else before
            else:
                after = ""
            out.iat[i, out.columns.get_loc(col)] = after
            ctx.dq.log(cat, "files", str(path.relative_to(Path(ctx.landing_dir))), before, col, before, after, None, ts)
    if n:
        dups = np.flatnonzero(ctx.dq.mask("duplicate_row", n, r))
        if len(dups):
            out = pd.concat([out, out.iloc[dups]], ignore_index=True)
            for i in dups:
                ctx.dq.log("duplicate_row", "files", str(path.relative_to(Path(ctx.landing_dir))), str(df["sku"].iloc[i]), None,
                           None, None, "row repeated in item master file", ts)
    header_map = dict(HEADERS[prof["header"]])
    header_map["case_gtin"] = DRIFT_HEADER[prof["header"]]
    out.columns = [header_map.get(c, c) for c in out.columns]
    if ctx.dq.hit("schema_drift", r):
        out["Notes"] = ""
        ctx.dq.log("schema_drift", "files", str(path.relative_to(Path(ctx.landing_dir))), None, "Notes", None, "Notes",
                   "unannounced extra column", ts)
    buf = io.StringIO()
    out.to_csv(buf, sep=prof["delimiter"], index=False, lineterminator=prof["newline"], quoting=csv.QUOTE_MINIMAL)
    return buf.getvalue().encode(prof["encoding"], errors="replace")


# ======================================================================================================== module


class Files:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.st = module_state(ctx, "files")
        for key in ("rate_cards", "budgets", "items", "history", "masters"):
            self.st.setdefault(key, {})
        self.shared = shared_state(ctx)

    # -------------------------------------------------------------- rate cards
    def ensure_rate_cards(self, upto: date) -> None:
        for scac in scacs_in_scope(self.ctx):
            for year in range(tariff_year(upto) - 1, tariff_year(upto) + 1):
                key = f"{scac}:{year}"
                if key not in self.st["rate_cards"]:
                    write_rate_card(self.ctx, scac, year)
                    self.st["rate_cards"][key] = True

    # -------------------------------------------------------------- budgets
    def budget(self, fy: int, prepared: date) -> None:
        if f"{fy}" in self.st["budgets"]:
            return
        values = budget_from_actuals(self.ctx, fy, prepared) if prepared > as_utc(self.ctx.history_start).date() else budget_estimate(self.ctx, fy)
        write_budget(self.ctx, fy, values, prepared, "Original Budget")
        self.st["budgets"][f"{fy}"] = prepared.isoformat()

    def reforecast(self, fy: int, prepared: date) -> None:
        key = f"{fy}rf"
        if key in self.st["budgets"]:
            return
        prepared0 = self.st["budgets"].get(f"{fy}")
        start = as_utc(self.ctx.history_start).date()
        plan = budget_from_actuals(self.ctx, fy, date.fromisoformat(prepared0)) if prepared0 and date.fromisoformat(prepared0) > start \
            else budget_estimate(self.ctx, fy)
        act = ledger_actuals(self.ctx, date(fy, 1, 1), date(fy, 7, 1))
        out: dict[tuple[str, str], np.ndarray] = {}
        for k in set(plan) | set(act):
            p = plan.get(k, np.zeros(12)).copy()
            a = act.get(k, np.zeros(12))
            ratio = (a[:6].sum() / p[:6].sum()) if p[:6].sum() > 0 and a[:6].sum() > 0 else 1.0
            p[:6] = a[:6]
            p[6:] = p[6:] * min(max(ratio, 0.5), 1.8)
            out[k] = p
        write_budget(self.ctx, fy, out, prepared, "Mid-year Reforecast (6+6)", actual_months=6)
        self.st["budgets"][key] = prepared.isoformat()

    # -------------------------------------------------------------- item masters
    def profile(self, code: str) -> dict[str, Any]:
        c = next((x for x in self.ctx.rules["clients"] if x["code"] == code), {"currency": "USD"})
        return client_profile(self.ctx.seed, code, c.get("currency", "USD"))

    def write_items(self, code: str, df: pd.DataFrame, day: date, delta: bool, ts: datetime) -> None:
        prof = self.profile(code)
        cdir = fdir(self.ctx) / "clients" / code
        spec = cdir / "file_spec.txt"
        if not spec.exists():
            atomic_write_bytes(spec, spec_text(code, prof).encode("utf-8"))
        name = f"item_master_{day:%Y%m%d}{'_delta' if delta else ''}.csv"
        path = cdir / name
        r = np.random.default_rng(derive_seed(self.ctx.seed, "items", code, day.isoformat(), delta))
        drift = bool(self.shared["drift"].get("item_gtin")) and day >= to_date(self.shared["drift"]["item_gtin"])
        data = render_item_csv(self.ctx, code, df.sort_values("sku", kind="stable"), prof, drift, r, path, ts)
        atomic_write_bytes(path, data)
        self.ctx.changes.add("files", "item_master", ins=1)
        info = self.st["items"].setdefault(code, {})
        info["last"] = day.isoformat()
        info.setdefault("files", []).append(name)
        info["files"] = info["files"][-60:]
        mx = pd.to_datetime(df["mod_ts"]).max() if len(df) else None
        if mx is not None and not pd.isna(mx):
            info["max_mod"] = max(info.get("max_mod", ""), mx.isoformat())

    def item_masters(self, ts: datetime) -> None:
        day = ts.date()
        known = self.st["items"]
        rows = self.ctx.conn("wms").execute(
            "SELECT o.owner_code, o.owner_id FROM wms.owner o WHERE EXISTS (SELECT 1 FROM wms.material m WHERE m.owner_id = o.owner_id)"
        ).fetchall() if day.weekday() < 5 else []
        owners = {str(c).strip(): oid for c, oid in rows}
        new = [c for c in sorted(owners) if c not in known]
        if new:
            df = material_frame(self.ctx, "o.owner_code = ANY(%s)", (new,))
            for code, g in df.groupby("owner_code"):
                self.write_items(str(code).strip(), g, day, False, ts)
        due_full, due_delta = [], []
        for code, info in sorted(known.items()):
            if code not in owners or code in new or info.get("stopped"):
                continue
            prof = self.profile(code)
            if prof["cadence"] == "monthly_full" and day.day <= 7 and day.weekday() == prof["weekday"] or prof["cadence"] == "weekly_full" and day.weekday() == prof["weekday"]:
                due_full.append(code)
            elif prof["cadence"] == "weekly_delta" and day.weekday() == prof["weekday"]:
                due_delta.append(code)
        if due_full:
            df = material_frame(self.ctx, "o.owner_code = ANY(%s)", (due_full,))
            for code, g in df.groupby("owner_code"):
                self.write_items(str(code).strip(), g, day, False, ts)
        if due_delta:
            since = [(owners[c], known[c].get("max_mod", "1900-01-01")) for c in due_delta]
            values = ", ".join(["(%s::int, %s::timestamp)"] * len(since))
            df = material_frame(self.ctx, f"EXISTS (SELECT 1 FROM (VALUES {values}) v(o, s) WHERE v.o = m.owner_id AND m.mod_ts > v.s)",
                                tuple(x for pair in since for x in pair))
            for code, g in df.groupby("owner_code"):
                self.write_items(str(code).strip(), g, day, True, ts)

    # -------------------------------------------------------------- carrier history export
    def export_history(self, now: datetime) -> None:
        retention = int(self.ctx.rules["size_presets"].get(self.ctx.preset, {}).get("carrier_api_retention_days", 90))
        cutoff = as_utc(now) - timedelta(days=retention)
        first = self.ctx.conn("sim_internal").execute("SELECT min(event_ts) FROM carrier.tracking_event").fetchone()[0]
        if first is None:
            return
        m = as_utc(first).date().replace(day=1)
        while m <= cutoff.date():
            key = f"{m:%Y%m}"
            m_end = add_months(m, 1)
            complete = as_utc(datetime.combine(m_end, datetime.min.time())) <= cutoff
            done = self.st["history"].get(key)
            if done != "complete" and (complete or done is None or done < cutoff.isoformat()):
                upper = min(as_utc(datetime.combine(m_end, datetime.min.time())), cutoff)
                self.write_history_month(m, upper)
                self.st["history"][key] = "complete" if complete else cutoff.isoformat()
            m = m_end

    def write_history_month(self, month: date, upper: datetime) -> None:
        path = fdir(self.ctx) / "carrier" / "history_export" / f"tracking_events_{month:%Y%m}.csv.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        q = ("COPY (SELECT event_id, tracking_nbr, scac, event_code, to_char(event_ts AT TIME ZONE 'UTC', "
             "'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS event_ts_utc, event_local_ts, city, state_prov, country_code, description, "
             "exception_code, to_char(received_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS received_at_utc, "
             "is_correction FROM carrier.tracking_event WHERE event_ts >= %s AND event_ts < %s ORDER BY event_ts, event_id) "
             "TO STDOUT WITH (FORMAT csv, HEADER true)")
        start = as_utc(datetime.combine(month, datetime.min.time()))
        with gzip.open(tmp, "wb", compresslevel=5) as gz, self.ctx.conn("sim_internal").cursor() as cur, \
                cur.copy(q, (start, upper)) as cp:
            for chunk in cp:
                gz.write(chunk)
        os.replace(tmp, path)
        self.ctx.changes.add("files", "carrier_history_export", ins=1)

    # -------------------------------------------------------------- reference files
    def publish_reference(self, now: datetime) -> None:
        """Keep the reference files current. Keyed on saved state, so an older build gets them at its next window."""
        week = diesel_week_published(now).isoformat()
        if self.st.get("diesel_week") != week:
            write_diesel_series(self.ctx, date.fromisoformat(week))
            kind = {"upd": 1} if "diesel_week" in self.st else {"ins": 1}
            self.ctx.changes.add("files", "diesel_weekly", **kind)
            self.st["diesel_week"] = week
        if "signal_sources" not in self.st:
            write_signal_sources(self.ctx)
            self.ctx.changes.add("files", "signal_sources", ins=1)
            self.st["signal_sources"] = True

    # -------------------------------------------------------------- lifecycle
    def bootstrap(self) -> None:
        start = as_utc(self.ctx.history_start)
        d0 = start.date()
        self.publish_reference(start)
        self.ensure_rate_cards(d0)
        prepared = date(d0.year - 1, 12, 5)
        self.budget(d0.year, prepared)
        if d0.month >= 8:
            self.reforecast_from_plan(d0.year)
        changes = [warehouse_opened(self.ctx, w) for w in preset_warehouses(self.ctx).values()]
        asof = max([d for d in changes if d <= d0], default=d0)
        write_warehouse_master(self.ctx, asof)
        self.st["masters"][asof.isoformat()] = True
        self.item_masters_bootstrap(d0 - timedelta(days=1), start)

    def reforecast_from_plan(self, fy: int) -> None:
        self.st["budgets"][f"{fy}rf"] = "skipped-before-history"

    def item_masters_bootstrap(self, day: date, ts: datetime) -> None:
        df = material_frame(self.ctx, "true", ())
        for code, g in df.groupby("owner_code"):
            self.write_items(str(code).strip(), g, day, False, ts)

    def step(self, win: Window) -> None:
        self.publish_reference(win.end)
        for ts in win.crosses_local_time("America/New_York", 6, 30):
            day = ts.date()
            if day.month == 12 and day.day == 10:
                self.ensure_rate_cards(rate_effective(day.year + 1))
            if day.month == 12 and day.day == 5:
                self.budget(day.year + 1, day)
            if day.month == 7 and day.day == 15:
                self.reforecast(day.year, day)
            for w in preset_warehouses(self.ctx).values():
                opened = warehouse_opened(self.ctx, w)
                if opened == day and opened.isoformat() not in self.st["masters"]:
                    write_warehouse_master(self.ctx, day)
                    self.st["masters"][opened.isoformat()] = True
            self.item_masters(ts)
        end = as_utc(self.ctx.history_end)
        if self.ctx.mode == "build":
            if win.start < end <= win.end:
                self.export_history(end)
        else:
            for ts in win.crosses_local_time("UTC", 4, 0):
                if ts.day == 1 or ts.day == 2:
                    self.export_history(ts)

    def resend(self, ts: datetime, skip: int = 0) -> str | None:
        """duplicate_file_resend: a client re-drops its latest item master with today's date (identical content)."""
        ranked = sorted(self.st["items"].items(), key=lambda kv: kv[1].get("last", ""), reverse=True)
        for code, info in ranked[skip:]:
            files = info.get("files") or []
            if not files:
                continue
            src = fdir(self.ctx) / "clients" / code / files[-1]
            if not src.exists():
                continue
            dst = src.with_name(f"item_master_{ts.date():%Y%m%d}{'_delta' if files[-1].endswith('_delta.csv') else ''}.csv")
            if dst == src:
                dst = src.with_name(src.stem + "_resend.csv")
            atomic_write_bytes(dst, src.read_bytes())
            self.ctx.changes.add("files", "item_master", ins=1)
            self.ctx.dq.log("duplicate_row", "files", str(dst.relative_to(Path(self.ctx.landing_dir))), files[-1], None, None, None,
                            "scenario duplicate_file_resend: identical item master re-sent", ts)
            return f"files: {code} re-sent {files[-1]} as {dst.name}"
        return None


class FilesModule:
    name = "files"

    def bootstrap(self, ctx: Any) -> None:
        Files(ctx).bootstrap()

    def step(self, ctx: Any, win: Window) -> None:
        Files(ctx).step(win)

    def apply_scenario(self, ctx: Any, scenario_id: str, params: dict[str, Any], win: Window) -> str | None:
        f = Files(ctx)
        if scenario_id == "schema_drift_release" and params.get("systems", "all") in ("all", "files"):
            f.shared["drift"]["item_gtin"] = win.start.isoformat()
            ctx.dq.log("schema_drift", "files", "clients/*/item_master_*.csv", None, "case_gtin", None, "case_gtin",
                       "scenario schema_drift_release: item master files gain a Case GTIN column", win.start)
            return "item master CSVs gain a Case GTIN column from now on"
        if scenario_id == "duplicate_file_resend" and params.get("kind", "all") in ("all", "files"):
            notes = [n for n in (f.resend(win.start, i) for i in range(int(params.get("count") or 1))) if n]
            return "; ".join(notes) or None
        if scenario_id == "offboard_client":
            code = scenario_client(params)
            if code in f.st["items"]:
                f.st["items"][code]["stopped"] = (params.get("end_date") or win.start.date().isoformat())
                return f"item master feed for {code} stops"
        return None


MODULE = FilesModule()
