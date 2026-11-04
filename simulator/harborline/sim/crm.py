"""Salesforce CRM simulator.

Every Salesforce record lives in ``sim_internal.sfdc.record`` (served by the mock REST API). The CRM reacts to the
client lifecycle in ``commercial.py`` (prospect -> opportunity -> Closed Won -> contract -> rate cards -> churn), to
WMS / carrier outcomes on the bus (cases) and keeps a realistic background of tasks, pipeline moves, owner changes,
spam cases, junk tasks and account merges.

The top of this file also holds the small helpers shared by the finance-crm modules (``ax``, ``edi``, ``files``):
COPY / bulk-update helpers, the preset's clients and warehouses, FX / diesel adapters and business-day maths.
"""

from __future__ import annotations

import heapq
import io
import itertools
import json
import unicodedata
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from functools import cache, lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import holidays as holidays_lib
import numpy as np
import pandas as pd
import yaml
from faker import Faker

from ..commercial import RateVersion, applicable_charge_codes, ax_account_num, client_dates, contract_rates, month_start
from ..core import Window, derive_seed
from ..ids import sf_id

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schema"

# ======================================================================================================== shared


def as_utc(ts: Any) -> datetime:
    if isinstance(ts, pd.Timestamp):
        ts = ts.to_pydatetime(warn=False)
    if isinstance(ts, date) and not isinstance(ts, datetime):
        ts = datetime(ts.year, ts.month, ts.day)
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)


def epoch_s(values: Any) -> pd.Series:
    """Unix seconds of a datetime Series, whatever its resolution (pandas 3 keeps us/ms/s units)."""
    ts = pd.to_datetime(values, utc=True)
    return (ts - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(seconds=1)


def naive_utc(ts: Any) -> datetime:
    return as_utc(ts).replace(tzinfo=None)


def sf_dt(ts: Any) -> str:
    return as_utc(ts).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def to_date(v: Any) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, pd.Timestamp):
        return v.date()
    return date.fromisoformat(str(v)[:10])


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 + n, 12)
    m += 1
    last = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)).day
    return date(y, m, min(d.day, last))


def month_end(d: date) -> date:
    return add_months(d.replace(day=1), 1) - timedelta(days=1)


def at_local(d: date, hh: float, tz: str) -> datetime:
    """UTC instant of local clock time ``hh`` (fractional hours) on ``d`` in zone ``tz``."""
    h = int(hh)
    m = round((hh - h) * 60)
    if m == 60:
        h, m = h + 1, 0
    local = datetime.combine(d, time(min(h, 23), m), tzinfo=ZoneInfo(tz))
    return local.astimezone(UTC)


def biz_ts(d: date, rng: np.random.Generator, tz: str = "America/New_York", lo: float = 8.0, hi: float = 18.0) -> datetime:
    return at_local(d, float(rng.uniform(lo, hi)), tz)


def to_local_naive(ts: Any, tz: str) -> datetime:
    return as_utc(ts).astimezone(ZoneInfo(tz)).replace(tzinfo=None)


@lru_cache(maxsize=64)
def _holidays(country: str, year: int) -> Any:
    return holidays_lib.country_holidays(country, years=year)


def is_business_day(d: date, country: str = "US") -> bool:
    return d.weekday() < 5 and d not in _holidays(country, d.year)


def first_business_day(y: int, m: int, country: str = "US") -> date:
    d = date(y, m, 1)
    while not is_business_day(d, country):
        d += timedelta(days=1)
    return d


def last_business_day(y: int, m: int, country: str = "US") -> date:
    d = month_end(date(y, m, 1))
    while not is_business_day(d, country):
        d -= timedelta(days=1)
    return d


def add_business_days(d: date, n: int, country: str = "US") -> date:
    step = 1 if n >= 0 else -1
    left = abs(n)
    while left:
        d += timedelta(days=step)
        if is_business_day(d, country):
            left -= 1
    return d


def module_state(ctx: Any, name: str) -> dict[str, Any]:
    return ctx.world.setdefault(name, {})


def shared_state(ctx: Any) -> dict[str, Any]:
    """Cross-module finance-crm state (churn overrides, scenario rate increases, schema drift flags)."""
    return ctx.world.setdefault("fincrm", {"churn": {}, "extra_versions": {}, "drift": {}})


def hist_end(ctx: Any) -> date:
    return as_utc(ctx.history_end).date()


def fx_per_usd(ctx: Any, d: Any, ccy: str) -> float:
    """Units of ``ccy`` per 1 USD on ``d`` (ECB-like market rate from the engine's signals)."""
    if ccy == "USD":
        return 1.0
    return float(ctx.signals.fx(to_date(d), ccy))


def diesel(ctx: Any, d: Any) -> float:
    return float(ctx.signals.diesel(to_date(d)))


def preset_warehouses(ctx: Any) -> dict[str, dict[str, Any]]:
    return {w["code"]: w for w in ctx.rules["warehouses"] if ctx.preset in w.get("presets", [ctx.preset])}


def warehouse_opened(ctx: Any, wh: dict[str, Any]) -> date:
    if wh.get("opened"):
        return date.fromisoformat(str(wh["opened"]))
    return month_start(hist_end(ctx), int(wh.get("opened_month", -999)))


def sim_clients(ctx: Any) -> list[dict[str, Any]]:
    """Clients served by at least one warehouse of the preset (warehouse list narrowed to the preset)."""
    whs = preset_warehouses(ctx)
    out = []
    for c in ctx.rules["clients"]:
        ws = [w for w in c.get("warehouses", []) if w in whs]
        if ws:
            out.append({**c, "warehouses": ws})
    return out


def entity_of_warehouse(ctx: Any, code: str) -> str:
    for w in ctx.rules["warehouses"]:
        if w["code"] == code:
            return w["legal_entity"]
    return "us01"


_DATES_CACHE: dict[tuple, Any] = {}


def dates_of(ctx: Any, client: dict[str, Any]) -> Any:
    key = (ctx.seed, hist_end(ctx), client["code"], client.get("go_live_date"), client.get("onboard_month"),
           client.get("churn_month"))
    hit = _DATES_CACHE.get(key)
    if hit is None:
        hit = client_dates(client, ctx.seed, hist_end(ctx))
        _DATES_CACHE[key] = hit
    return hit


def churn_of(ctx: Any, client: dict[str, Any]) -> date | None:
    override = shared_state(ctx)["churn"].get(client["code"])
    if override:
        return date.fromisoformat(override)
    return dates_of(ctx, client).churn


_RATES_CACHE: dict[tuple, list[RateVersion]] = {}


def rate_versions(ctx: Any, client: dict[str, Any]) -> list[RateVersion]:
    """Contracted rate versions (commercial.contract_rates) cut at a runtime churn and extended by scenario increases."""
    key = (ctx.seed, hist_end(ctx), client["code"], client.get("go_live_date"))
    base = _RATES_CACHE.get(key)
    if base is None:
        base = contract_rates(ctx.rules, client, ctx.seed, hist_end(ctx), lambda d, c: fx_per_usd(ctx, d, c))
        _RATES_CACHE[key] = base
    versions = [RateVersion(v.version, v.effective_date, v.end_date, v.currency, v.increase_pct, dict(v.rates),
                            v.monthly_minimum) for v in base]
    for extra in shared_state(ctx)["extra_versions"].get(client["code"], []):
        eff = date.fromisoformat(extra["effective"])
        prev = rate_on_date(versions, eff) or versions[-1]
        versions = [v for v in versions if v.effective_date < eff]
        prev.end_date = eff - timedelta(days=1)
        pct = float(extra["pct"])
        new = {k: (v if k in ("FRT-RCV", "MIN-ADJ") else round(v * (1 + pct / 100), 4 if v < 2 else 2)) for k, v in prev.rates.items()}
        versions.append(RateVersion(len(versions) + 1, eff, None, prev.currency, pct, new, round(prev.monthly_minimum * (1 + pct / 100), 2)))
    churn = churn_of(ctx, client)
    if churn:
        versions = [v for v in versions if v.effective_date <= churn]
        if versions:
            versions[-1].end_date = churn
    return versions


def rate_on_date(versions: list[RateVersion], d: date) -> RateVersion | None:
    for v in reversed(versions):
        if v.effective_date <= d and (v.end_date is None or d <= v.end_date):
            return v
    return None


def payment_behaviour(ctx: Any, code: str) -> str:
    cb = (ctx.planted or {}).get("client_behaviour", {})
    return cb.get("payment", {}).get(code, cb.get("default_payment", "on_time"))


def churn_signal(ctx: Any, code: str) -> dict[str, Any] | None:
    return (ctx.planted or {}).get("client_behaviour", {}).get("churn_signals", {}).get(code)


def dispute_share(seed: int, invoiceid: str, behaviour: str) -> float:
    """Share of an invoice a 'disputer' client short-pays (0 when the invoice is not disputed). Used by crm and ax."""
    if behaviour != "disputer":
        return 0.0
    r = np.random.default_rng(derive_seed(seed, "dispute", invoiceid))
    hit = r.random() < 0.08
    share = float(r.uniform(0.05, 0.3))
    return round(share, 4) if hit else 0.0


def sla_terms(seed: int, rules: dict[str, Any], client: dict[str, Any]) -> tuple[float, float]:
    """Contract SLA: (ship-within hours, on-time target %). Shared by crm (Contract fields) and ax (SLA credits)."""
    r = np.random.default_rng(derive_seed(seed, "sla", client["code"]))
    b2c = rules["verticals"][client["vertical"]]["b2c_share"]
    hours = 24.0 if b2c >= 0.5 else 48.0
    target = float(r.choice([98.0, 98.5, 99.0, 99.5]))
    return hours, target


def copy_df(ctx: Any, db: str, table: str, df: pd.DataFrame) -> int:
    """Bulk insert with COPY inside the run's transaction (NaN/None -> NULL)."""
    if df is None or df.empty:
        return 0
    buf = io.StringIO()
    df.to_csv(buf, index=False, header=False, na_rep="\\N")
    cols = ", ".join(df.columns)
    with ctx.conn(db).cursor() as cur, cur.copy(f"COPY {table} ({cols}) FROM STDIN WITH (FORMAT csv, NULL '\\N')") as cp:
        cp.write(buf.getvalue())
    return len(df)


_TMP_SEQ = itertools.count()


def bulk_update(ctx: Any, db: str, table: str, df: pd.DataFrame, keys: list[str], set_sql: str | None = None,
                types: dict[str, str] | None = None) -> int:
    """UPDATE ``table`` from a DataFrame via a temp table. ``set_sql`` may use aliases t (target) and u (new values)."""
    if df is None or df.empty:
        return 0
    conn = ctx.conn(db)
    tmp = f"_fc_upd_{next(_TMP_SEQ)}"
    cols = list(df.columns)
    if types:
        conn.execute(f"CREATE TEMP TABLE {tmp} ({', '.join(f'{c} {types[c]}' for c in cols)}) ON COMMIT DROP")
    else:
        conn.execute(f"CREATE TEMP TABLE {tmp} ON COMMIT DROP AS SELECT {', '.join(cols)} FROM {table} WHERE false")
    copy_df(ctx, db, tmp, df)
    sets = set_sql or ", ".join(f"{c} = u.{c}" for c in cols if c not in keys)
    where = " AND ".join(f"t.{k} = u.{k}" for k in keys)
    n = conn.execute(f"UPDATE {table} t SET {sets} FROM {tmp} u WHERE {where}").rowcount
    conn.execute(f"DROP TABLE {tmp}")
    return int(n or 0)


def ascii_slug(text: str) -> str:
    norm = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return "".join(ch for ch in norm.lower() if ch.isalnum())


def mojibake(text: str) -> str:
    return text.encode("utf-8").decode("cp1252", errors="replace")


# Curated HQ locations: (city, state, postal, country ISO2, lat, lon, phone area code)
_HQ_CITIES: dict[str, list[tuple[str, str, str, str, float, float, str]]] = {
    "US": [
        ("Atlanta", "GA", "30309", "US", 33.7984, -84.3883, "404"), ("Charlotte", "NC", "28202", "US", 35.2271, -80.8431, "704"),
        ("Nashville", "TN", "37203", "US", 36.1500, -86.7900, "615"), ("Austin", "TX", "78701", "US", 30.2711, -97.7437, "512"),
        ("Dallas", "TX", "75201", "US", 32.7876, -96.7994, "214"), ("Houston", "TX", "77002", "US", 29.7569, -95.3625, "713"),
        ("Denver", "CO", "80202", "US", 39.7525, -104.9995, "303"), ("Phoenix", "AZ", "85004", "US", 33.4510, -112.0687, "602"),
        ("Salt Lake City", "UT", "84101", "US", 40.7566, -111.8992, "801"), ("Portland", "OR", "97204", "US", 45.5185, -122.6755, "503"),
        ("Seattle", "WA", "98101", "US", 47.6114, -122.3305, "206"), ("San Francisco", "CA", "94105", "US", 37.7898, -122.3942, "415"),
        ("Oakland", "CA", "94612", "US", 37.8085, -122.2700, "510"), ("Los Angeles", "CA", "90013", "US", 34.0453, -118.2437, "213"),
        ("San Diego", "CA", "92101", "US", 32.7194, -117.1628, "619"), ("Irvine", "CA", "92618", "US", 33.6670, -117.7451, "949"),
        ("Chicago", "IL", "60606", "US", 41.8825, -87.6378, "312"), ("Minneapolis", "MN", "55401", "US", 44.9850, -93.2695, "612"),
        ("Milwaukee", "WI", "53202", "US", 43.0450, -87.9010, "414"), ("Columbus", "OH", "43215", "US", 39.9650, -83.0050, "614"),
        ("Cincinnati", "OH", "45202", "US", 39.1035, -84.5125, "513"), ("Detroit", "MI", "48226", "US", 42.3315, -83.0466, "313"),
        ("Indianapolis", "IN", "46204", "US", 39.7700, -86.1580, "317"), ("Kansas City", "MO", "64105", "US", 39.1030, -94.5830, "816"),
        ("St. Louis", "MO", "63101", "US", 38.6316, -90.1920, "314"), ("New York", "NY", "10001", "US", 40.7506, -73.9972, "212"),
        ("Brooklyn", "NY", "11201", "US", 40.6940, -73.9903, "718"), ("Boston", "MA", "02110", "US", 42.3570, -71.0530, "617"),
        ("Philadelphia", "PA", "19103", "US", 39.9526, -75.1740, "215"), ("Pittsburgh", "PA", "15222", "US", 40.4470, -79.9930, "412"),
        ("Baltimore", "MD", "21202", "US", 39.2905, -76.6100, "410"), ("Newark", "NJ", "07102", "US", 40.7357, -74.1724, "973"),
        ("Jersey City", "NJ", "07302", "US", 40.7196, -74.0431, "201"), ("Miami", "FL", "33131", "US", 25.7650, -80.1910, "305"),
        ("Tampa", "FL", "33602", "US", 27.9510, -82.4570, "813"), ("Orlando", "FL", "32801", "US", 28.5420, -81.3790, "407"),
        ("Raleigh", "NC", "27601", "US", 35.7730, -78.6390, "919"), ("Richmond", "VA", "23219", "US", 37.5410, -77.4360, "804"),
        ("Louisville", "KY", "40202", "US", 38.2540, -85.7590, "502"), ("Memphis", "TN", "38103", "US", 35.1450, -90.0520, "901"),
        ("Boise", "ID", "83702", "US", 43.6150, -116.2020, "208"), ("Reno", "NV", "89501", "US", 39.5260, -119.8130, "775"),
        ("Las Vegas", "NV", "89101", "US", 36.1720, -115.1420, "702"), ("Omaha", "NE", "68102", "US", 41.2590, -95.9370, "402"),
        ("Bentonville", "AR", "72712", "US", 36.3720, -94.2090, "479"),
    ],
    "CA": [
        ("Toronto", "ON", "M5V 2T6", "CA", 43.6450, -79.3910, "416"), ("Mississauga", "ON", "L5B 3C2", "CA", 43.5890, -79.6440, "905"),
        ("Montreal", "QC", "H3B 4W8", "CA", 45.5010, -73.5670, "514"), ("Vancouver", "BC", "V6B 1A1", "CA", 49.2820, -123.1170, "604"),
        ("Calgary", "AB", "T2P 1J9", "CA", 51.0450, -114.0630, "403"), ("Ottawa", "ON", "K1P 5G4", "CA", 45.4210, -75.6970, "613"),
    ],
    "MX": [
        ("Monterrey", "NL", "64000", "MX", 25.6710, -100.3090, "81"), ("San Pedro Garza García", "NL", "66220", "MX", 25.6570, -100.4020, "81"),
        ("Ciudad de México", "CMX", "06600", "MX", 19.4270, -99.1680, "55"), ("Guadalajara", "JAL", "44100", "MX", 20.6770, -103.3470, "33"),
        ("Querétaro", "QRO", "76000", "MX", 20.5880, -100.3880, "442"),
    ],
    "EU": [
        ("Paris", "", "75003", "FR", 48.8630, 2.3600, "1"), ("Lyon", "", "69002", "FR", 45.7600, 4.8330, "4"),
        ("Hamburg", "", "20457", "DE", 53.5430, 9.9890, "40"), ("Amsterdam", "", "1017 CE", "NL", 52.3630, 4.8890, "20"),
    ],
    "GB": [("London", "", "EC2A 4NE", "GB", 51.5230, -0.0800, "20"), ("Manchester", "", "M1 1AE", "GB", 53.4810, -2.2370, "161")],
}
_PINNED_HQ = {"BRUNE": ("EU", 0), "LUMIERE": ("EU", 1), "NORDHS": ("EU", 2), "HARROW": ("GB", 0)}
_STREETS = {
    "US": ["Peachtree St NE", "Commerce Dr", "Market St", "Industrial Blvd", "Main St", "Lakeview Pkwy", "Harbor Way",
           "Oak Ridge Rd", "Corporate Center Dr", "Innovation Way", "Riverfront Ave", "Elm St", "Union Ave", "Mill Rd"],
    "CA": ["King St W", "Bay St", "Rue Sainte-Catherine O", "Granville St", "Front St W", "Queen St E"],
    "MX": ["Av. Constitución", "Calz. del Valle", "Av. Insurgentes Sur", "Blvd. Díaz Ordaz", "Av. Revolución"],
    "FR": ["Rue de Bretagne", "Rue de la République", "Boulevard Haussmann"],
    "DE": ["Am Sandtorkai", "Großer Burstah", "Brooktorkai"],
    "NL": ["Herengracht", "Keizersgracht"],
    "GB": ["Shoreditch High St", "Great Eastern St", "Deansgate"],
}
COUNTRY_NAME = {"US": "United States", "CA": "Canada", "MX": "Mexico", "FR": "France", "DE": "Germany", "NL": "Netherlands",
                "GB": "United Kingdom"}
ISO3 = {"US": "USA", "CA": "CAN", "MX": "MEX", "FR": "FRA", "DE": "DEU", "NL": "NLD", "GB": "GBR"}


def region_key(client: dict[str, Any]) -> str:
    ccy = client.get("currency", "USD")
    return {"CAD": "CA", "MXN": "MX", "EUR": "EU", "GBP": "GB"}.get(ccy, "US")


def hq_address(seed: int, code: str, region: str, variant: int = 0) -> dict[str, Any]:
    """Deterministic head-office address (city/state/ZIP/lat/lon always agree)."""
    r = np.random.default_rng(derive_seed(seed, "hq", code, variant))
    if variant == 0 and code in _PINNED_HQ:
        region, idx = _PINNED_HQ[code]
        city = _HQ_CITIES[region][idx]
    else:
        pool = _HQ_CITIES[region]
        city = pool[int(r.integers(0, len(pool)))]
    name, state, postal, iso2, lat, lon, area = city
    streets = _STREETS.get(iso2, _STREETS["US"])
    street = streets[int(r.integers(0, len(streets)))]
    num = int(r.integers(10, 9800))
    if iso2 in ("FR", "DE", "NL", "MX"):
        line = f"{street} {num % 200 + 1}" if iso2 != "FR" else f"{num % 200 + 1} {street}"
    else:
        line = f"{num} {street}"
        if r.random() < 0.35:
            line += f", Suite {int(r.integers(100, 1900))}"
    return {"street": line, "city": name, "state": state, "postal": postal, "country": iso2, "lat": lat, "lon": lon,
            "area": area, "number": str(num)}


def phone_for(area: str, country: str, r: np.random.Generator) -> str:
    n = int(r.integers(100, 200))
    if country == "US":
        return f"({area}) 555-0{n}"
    if country == "CA":
        return f"+1 {area} 555 0{n}"
    if country == "MX":
        return f"+52 {area} 5555 0{n}"
    cc = {"FR": "33", "DE": "49", "NL": "31", "GB": "44"}[country]
    return f"+{cc} {area} 5550 {n}"


# ===================================================================================================== Salesforce

ORG_START = date(2019, 3, 4)
STAGES: dict[str, tuple[int, str]] = {
    "Prospecting": (10, "Pipeline"), "Qualification": (10, "Pipeline"), "Needs Analysis": (20, "Pipeline"),
    "Value Proposition": (50, "Best Case"), "Id. Decision Makers": (60, "Best Case"), "Perception Analysis": (70, "Best Case"),
    "Proposal/Price Quote": (75, "Commit"), "Negotiation/Review": (90, "Commit"), "Closed Won": (100, "Closed"),
    "Closed Lost": (0, "Omitted"),
}
OPEN_STAGES = list(STAGES)[:8]
INDUSTRY = {"apparel": "Apparel", "electronics": "Consumer Electronics", "food_bev": "Food & Beverage",
            "health_beauty": "Health & Beauty", "home_goods": "Home Goods", "auto_parts": "Automotive", "pet": "Pet Products",
            "supplements": "Nutrition", "b2b_general": "Wholesale"}
REGION = {"ATL1": "US East", "NJ1": "US East", "DFW1": "US Central", "CHI1": "US Central", "RNO1": "US West",
          "LAX1": "US West", "TOR1": "Canada", "MTY1": "Mexico"}
DEPT = {"EXEC": "Executive", "FIN": "Finance", "OPS": "Operations", "SLS": "Sales", "CS": "Client Success", "QA": "Quality",
        "IT": "Information Technology"}
USER_TZ = {"chloe.martin": "America/Toronto", "diego.salinas": "America/Mexico_City", "wes.harrington": "America/Los_Angeles",
           "priscilla.nunez": "America/Chicago", "lena.fischer": "America/Chicago", "travis.boone": "America/Chicago",
           "holly.brennan": "America/Los_Angeles", "dwayne.pruitt": "America/Chicago", "victor.ochoa": "America/Los_Angeles",
           "natalie.gagnon": "America/Toronto", "rodrigo.trevino": "America/Mexico_City"}
MANAGER = {"jordan.ellis": "hannah.price", "priscilla.nunez": "hannah.price", "wes.harrington": "hannah.price",
           "chloe.martin": "hannah.price", "diego.salinas": "hannah.price", "tara.whitlock": "omar.haddad",
           "ben.osei": "omar.haddad", "lena.fischer": "omar.haddad", "dana.whitfield": "hannah.price",
           "hannah.price": "daniel.reyes", "omar.haddad": "daniel.reyes", "rebecca.lindqvist": "daniel.reyes",
           "luis.moreno": "daniel.reyes", "sam.okafor": "rebecca.lindqvist", "alan.brooks": "rebecca.lindqvist",
           "iris.novak": "daniel.reyes", "joanne.pike": "alan.brooks", "shanice.carter": "luis.moreno",
           **{gm: "luis.moreno" for gm in ("travis.boone", "holly.brennan", "dwayne.pruitt", "angela.russo", "victor.ochoa",
                                          "natalie.gagnon", "rodrigo.trevino")}}
US_AES = ["jordan.ellis", "priscilla.nunez", "wes.harrington"]
CSMS = ["tara.whitlock", "ben.osei", "lena.fischer"]
FAMILY = {"STOR-PLT": "Storage", "STOR-BIN": "Storage", "COLD-SUR": "Storage", "RCV-PLT": "Handling", "RCV-CTN": "Handling",
          "ORD-B2C": "Handling", "ORD-B2B": "Handling", "PICK-ADD": "Handling", "PICK-CTN": "Handling",
          "PACK-MAT": "Value-Added Services", "RET-PROC": "Value-Added Services", "LBL-ACC": "Value-Added Services",
          "KIT-ASM": "Value-Added Services", "ACCT-MGT": "Fees", "MIN-ADJ": "Fees", "FRT-RCV": "Freight"}
CURRENCIES = ["USD", "CAD", "MXN", "EUR", "GBP"]
TIER_PALLETS = {"S": 120, "M": 450, "L": 1500, "XL": 4500}
LOSS_REASONS = (["Price", "Went with competitor", "Location", "Capabilities", "Timing", "No decision"],
                [0.30, 0.25, 0.12, 0.10, 0.13, 0.10])
CONTACT_TITLES = ["Operations Manager", "Director of Logistics", "Accounts Payable Specialist", "VP Supply Chain",
                  "Customer Service Manager", "E-commerce Manager", "Inventory Planner"]
PROSPECT_TITLES = ["VP Operations", "Supply Chain Manager", "Chief Operating Officer", "Founder & CEO", "Head of Fulfillment"]
FAKER_LOCALE = {"US": "en_US", "CA": "en_CA", "MX": "es_MX", "EU": "fr_FR", "GB": "en_GB"}
PROSPECT_WORDS = ["Granite", "Bluebird", "Copperline", "Riverbend", "Northwind", "Sagebrush", "Lighthouse", "Ironwood",
                  "Meadowlark", "Silverpine", "Redwood", "Brightwater", "Cedar Point", "Golden Gate", "Prairie", "Stonebridge",
                  "Evergreen", "Fox Hollow", "Wildflower", "High Desert", "Blue Harbor", "Juniper", "Coastline", "Thistle",
                  "Driftwood", "Magnolia", "Ridgeline", "Starling", "Tidewater", "Willow Creek", "Amberly", "Kingfisher",
                  "Larkspur", "Mosaic", "Pinnacle", "Quarry", "Saltbox", "Summit Ridge", "Trailblazer", "Vantage"]
PROSPECT_NOUNS = {"apparel": ["Apparel", "Outfitters", "Clothing Co.", "Threads"], "electronics": ["Electronics", "Audio", "Labs", "Devices"],
                  "food_bev": ["Foods", "Provisions", "Beverage Co.", "Pantry"], "health_beauty": ["Beauty", "Skincare", "Cosmetics", "Wellness"],
                  "home_goods": ["Home", "Living", "Interiors", "Housewares"], "auto_parts": ["Auto Parts", "Automotive", "Parts Supply"],
                  "pet": ["Pet Co.", "Pet Supply", "Pet Foods"], "supplements": ["Nutrition", "Vitamins", "Wellness Labs"],
                  "b2b_general": ["Supply", "Distributors", "Wholesale", "Industrial"]}
SPAM_SUBJECTS = ["Increase your website traffic by 300%", "Partnership opportunity - guaranteed SEO results",
                 "RE: Invoice overdue - click to view", "Get more leads for Harborline today", "Cheap freight brokerage quotes",
                 "Your mailbox storage is full", "Exclusive offer: LED warehouse lighting"]


@lru_cache(maxsize=1)
def sf_model() -> dict[str, Any]:
    return yaml.safe_load((SCHEMA_DIR / "salesforce_objects.yaml").read_text(encoding="utf-8"))


@cache
def sf_fields(obj: str) -> tuple[str, ...]:
    return tuple(f["name"] for f in sf_model()["objects"][obj]["fields"])


@lru_cache(maxsize=1)
def obj_prefix() -> dict[str, str]:
    return {o: str(spec["key_prefix"]) for o, spec in sf_model()["objects"].items()}


@lru_cache(maxsize=1)
def prefix_obj() -> dict[str, str]:
    return {p: o for o, p in obj_prefix().items()}


_FAKERS: dict[str, Faker] = {}


def faker_for(region: str, seed: int) -> Faker:
    loc = FAKER_LOCALE.get(region, "en_US")
    fk = _FAKERS.get(loc)
    if fk is None:
        fk = Faker(loc)
        _FAKERS[loc] = fk
    fk.seed_instance(seed)
    return fk


def est_monthly_qty(rules: dict[str, Any], client: dict[str, Any], codes: list[str]) -> dict[str, float]:
    v = rules["verticals"][client["vertical"]]
    orders = rules["volume_tiers"][client["tier"]] * 30
    b2c = v["b2c_share"]
    pallets = TIER_PALLETS[client["tier"]]
    q = {"STOR-PLT": pallets, "STOR-BIN": pallets * 2, "COLD-SUR": pallets, "RCV-PLT": pallets * 0.45,
         "RCV-CTN": pallets * 0.45 * 30, "ORD-B2C": orders * b2c, "ORD-B2B": orders * (1 - b2c), "PICK-ADD": orders * b2c * 1.3,
         "PICK-CTN": orders * (1 - b2c) * 14, "PACK-MAT": orders * b2c, "RET-PROC": orders * b2c * v["return_rate"] * 1.4,
         "LBL-ACC": orders * 0.6, "KIT-ASM": orders * 0.08, "ACCT-MGT": 1}
    return {c: round(q[c]) for c in codes if c in q}


class SfStore:
    """Buffers Salesforce inserts / updates / deletes for one run and writes them with COPY + one UPDATE."""

    def __init__(self, ctx: Any, st: dict[str, Any]) -> None:
        self.ctx, self.st = ctx, st
        self.ins: dict[str, dict[str, Any]] = {}
        self.upd: dict[str, dict[str, Any]] = {}

    def new_id(self, obj: str) -> str:
        prefix = obj_prefix()[obj]
        ctr = self.st.setdefault("ctr", {})
        n = ctr.get(prefix) or (1_200_000 + derive_seed("sfctr", prefix) % 900_000)
        ctr[prefix] = n + 1
        return sf_id(prefix, n)

    def insert(self, obj: str, fields: dict[str, Any], ts: datetime, by: str, owner: str | None = None) -> str:
        rid = self.new_id(obj)
        data: dict[str, Any] = dict.fromkeys(sf_fields(obj))
        stamp = sf_dt(ts)
        data.update(Id=rid, IsDeleted=False, CreatedDate=stamp, CreatedById=by, LastModifiedDate=stamp,
                    LastModifiedById=by, SystemModstamp=stamp)
        if sf_model()["objects"][obj].get("has_owner"):
            data["OwnerId"] = owner or by
        data.update(fields)
        self.ins[rid] = {"object": obj, "data": data, "created": ts, "lm": ts, "sm": ts, "del": None, "master": None}
        return rid

    def update(self, rid: str, fields: dict[str, Any], ts: datetime, by: str, system_only: bool = False) -> None:
        rec = self.ins.get(rid)
        stamp = sf_dt(ts)
        if rec is not None:
            rec["data"].update(fields)
            rec["data"]["SystemModstamp"] = stamp
            rec["sm"] = max(rec["sm"], ts)
            if not system_only:
                rec["data"].update(LastModifiedDate=stamp, LastModifiedById=by)
                rec["lm"] = max(rec["lm"], ts)
            return
        u = self.upd.setdefault(rid, {"patch": {}, "lm": None, "sm": ts, "del": None, "master": None})
        u["patch"].update(fields)
        u["sm"] = max(u["sm"], ts)
        u["patch"]["SystemModstamp"] = sf_dt(u["sm"])
        if not system_only:
            u["lm"] = ts if u["lm"] is None else max(u["lm"], ts)
            u["patch"].update(LastModifiedDate=sf_dt(u["lm"]), LastModifiedById=by)

    def delete(self, rid: str, ts: datetime, by: str, master: str | None = None) -> None:
        fields: dict[str, Any] = {"IsDeleted": True}
        if master:
            fields["MasterRecordId"] = master
        self.update(rid, fields, ts, by)
        rec = self.ins.get(rid) or self.upd[rid]
        rec["del"] = ts
        rec["master"] = master

    def flush(self) -> None:
        counts: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
        if self.ins:
            rows = []
            for rid, rec in self.ins.items():
                rows.append((rid, rec["object"], json.dumps(rec["data"], ensure_ascii=False), rec["created"], rec["lm"],
                             rec["sm"], rec["del"] is not None, rec["del"], rec["master"]))
                counts[rec["object"]][0] += 1
                if rec["del"] is not None:
                    counts[rec["object"]][2] += 1
            df = pd.DataFrame(rows, columns=["id", "object", "data", "created_date", "last_modified_date", "system_modstamp",
                                             "is_deleted", "deleted_date", "master_record_id"])
            copy_df(self.ctx, "sim_internal", "sfdc.record", df)
        if self.upd:
            rows = []
            for rid, u in self.upd.items():
                rows.append((rid, json.dumps(u["patch"], ensure_ascii=False), u["lm"], u["sm"], u["del"], u["master"]))
                obj = prefix_obj().get(rid[:3], "Unknown")
                counts[obj][2 if u["del"] is not None else 1] += 1
            df = pd.DataFrame(rows, columns=["id", "patch", "lm", "sm", "del", "master"])
            bulk_update(
                self.ctx, "sim_internal", "sfdc.record", df, ["id"],
                set_sql="data = t.data || u.patch, last_modified_date = coalesce(u.lm, t.last_modified_date), "
                        "system_modstamp = u.sm, is_deleted = (t.is_deleted OR u.del IS NOT NULL), "
                        "deleted_date = coalesce(u.del, t.deleted_date), master_record_id = coalesce(u.master, t.master_record_id)",
                types={"id": "char(18)", "patch": "jsonb", "lm": "timestamptz", "sm": "timestamptz", "del": "timestamptz",
                       "master": "char(18)"},
            )
        for obj, (i, u, d) in counts.items():
            self.ctx.changes.add("salesforce", obj, ins=i, upd=u, dele=d)
        ctr = self.st.get("ctr", {})
        if ctr:
            with self.ctx.conn("sim_internal").cursor() as cur:
                cur.executemany(
                    "INSERT INTO sfdc.id_counter (prefix, next_value) VALUES (%s, %s) ON CONFLICT (prefix) DO UPDATE "
                    "SET next_value = GREATEST(sfdc.id_counter.next_value, EXCLUDED.next_value)",
                    sorted(ctr.items()),
                )
        self.ins.clear()
        self.upd.clear()


class Crm:
    """One run's view of the CRM: the world state, an event queue and the record store."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.st = module_state(ctx, "crm")
        self.shared = shared_state(ctx)
        self.store = SfStore(ctx, self.st)
        self.seed = ctx.seed
        self.hend = hist_end(ctx)
        self.hstart = as_utc(ctx.history_start).date()
        self.clients = {c["code"]: c for c in sim_clients(ctx)}
        self.floor: datetime | None = None
        self.lad: dict[str, str] = {}
        for key in ("q", "users", "acct", "pros", "opp", "contract", "rc", "products", "cases", "dups", "known", "lad_acct",
                    "asn_expected", "nums", "client_owner"):
            self.st.setdefault(key, [] if key in ("q", "dups") else {})
        self.st.setdefault("qseq", 0)

    # ------------------------------------------------------------------ helpers
    def r(self, *parts: Any) -> np.random.Generator:
        return np.random.default_rng(derive_seed(self.seed, "crm", *parts))

    def uid(self, key: str) -> str:
        return self.st["users"].get(key) or self.st["users"]["integration.user"]

    def num(self, kind: str, start: int) -> int:
        n = self.st["nums"].get(kind, start)
        self.st["nums"][kind] = n + 1
        return n

    def schedule(self, ts: datetime, kind: str, payload: dict[str, Any]) -> None:
        self.st["qseq"] += 1
        heapq.heappush(self.st["q"], (as_utc(ts).timestamp(), self.st["qseq"], kind, payload))

    def run_queue(self, until: datetime) -> None:
        limit = until.timestamp()
        q = self.st["q"]
        while q and q[0][0] < limit:
            ts_f, _, kind, payload = heapq.heappop(q)
            ts = datetime.fromtimestamp(ts_f, UTC)
            if self.floor is not None and ts < self.floor:
                ts = self.floor
            getattr(self, f"ev_{kind}")(ts, payload)

    def dq(self, category: str, rng: np.random.Generator) -> bool:
        return self.ctx.dq.hit(category, rng)

    def log(self, category: str, obj: str, key: str | None, column: str | None, before: Any, after: Any, ts: datetime,
            note: str | None = None) -> None:
        self.ctx.dq.log(category, "salesforce", obj, key, column, before, after, note, ts)

    def ae_for(self, code: str, region: str, salt: str = "") -> str:
        if region == "CA":
            return "chloe.martin"
        if region == "MX":
            return "diego.salinas"
        if region in ("EU", "GB"):
            return "priscilla.nunez"
        return US_AES[derive_seed(self.seed, "ae", code, salt) % len(US_AES)]

    def csm_for(self, code: str) -> str:
        return CSMS[derive_seed(self.seed, "csm", code) % len(CSMS)]

    def client_fake(self, code: str, region: str, salt: str) -> Faker:
        return faker_for(region, derive_seed(self.seed, "faker", code, salt) % (2**31))

    # ------------------------------------------------------------------ bootstrap
    def bootstrap(self) -> None:
        rows = self.ctx.conn("sim_internal").execute("SELECT prefix, next_value FROM sfdc.id_counter").fetchall()
        for prefix, nv in rows:
            self.st.setdefault("ctr", {})[str(prefix).strip()] = int(nv)
        org = min(ORG_START, self.hstart - timedelta(days=500))
        self.create_users(at_local(org, 9.5, "America/New_York"))
        self.create_catalog(at_local(org + timedelta(days=12), 10.0, "America/New_York"))
        for c in self.clients.values():
            self.schedule_client(c)
        self.schedule_prospects()
        self.schedule_globals()
        self.run_queue(as_utc(self.ctx.history_start))

    def create_users(self, ts: datetime) -> None:
        domain = self.ctx.rules["company"]["email_domain"]
        people = self.ctx.rules["people"]
        for i, p in enumerate(people):
            first, _, last = p["name"].replace("Dr. ", "").partition(" ")
            slug = f"{ascii_slug(first)}.{ascii_slug(last)}"
            is_int = p["key"] == "integration.user"
            tz = USER_TZ.get(p["key"], "America/New_York")
            fields = {
                "Username": f"{slug}@{domain}", "FirstName": first, "LastName": last, "Name": f"{first} {last}",
                "Alias": (ascii_slug(first)[:1] + ascii_slug(last))[:8], "Email": f"{slug}@{domain}", "Title": p["title"],
                "Department": DEPT.get(p["dept"], p["dept"]), "CompanyName": self.ctx.rules["company"]["name"],
                "EmployeeNumber": None if is_int else f"E{1040 + i * 7:05d}", "IsActive": True,
                "UserType": "Standard", "TimeZoneSidKey": tz,
                "LocaleSidKey": {"America/Toronto": "en_CA", "America/Mexico_City": "es_MX"}.get(tz, "en_US"),
                "DefaultCurrencyIsoCode": {"chloe.martin": "CAD", "diego.salinas": "MXN"}.get(p["key"], "USD"),
                "LastLoginDate": None,
            }
            created = ts + timedelta(minutes=7 * i)
            uid = self.store.insert("User", fields, created, self.st["users"].get("dana.whitfield", "005000000000000AAA"))
            self.st["users"][p["key"]] = uid
        admin = self.st["users"]["dana.whitfield"]
        for key, uid in self.st["users"].items():
            rec = self.store.ins[uid]
            rec["data"]["CreatedById"] = admin
            rec["data"]["LastModifiedById"] = admin
            if key in MANAGER:
                rec["data"]["ManagerId"] = self.st["users"].get(MANAGER[key])

    def create_catalog(self, ts: datetime) -> None:
        admin = self.uid("dana.whitfield")
        pb = self.store.insert("Pricebook2", {"Name": "Standard Price Book", "Description": "Harborline 3PL services list prices",
                                              "IsActive": True, "IsStandard": True}, ts, admin)
        self.st["pricebook"] = pb
        d0 = ts.date()
        for i, cc in enumerate(self.ctx.rules["charge_codes"]):
            t = ts + timedelta(minutes=2 * i + 1)
            pid = self.store.insert("Product2", {
                "Name": cc["name"], "ProductCode": cc["code"], "Family": FAMILY.get(cc["code"], "Fees"),
                "QuantityUnitOfMeasure": cc["uom"], "Description": f"{cc['name']} (GL {cc['gl']})", "IsActive": True,
                "CurrencyIsoCode": "USD"}, t, admin)
            pbe = {}
            for ccy in CURRENCIES:
                price = cc["list_rate"] * fx_per_usd(self.ctx, d0, ccy)
                pbe[ccy] = self.store.insert("PricebookEntry", {
                    "Pricebook2Id": pb, "Product2Id": pid, "Name": cc["name"], "ProductCode": cc["code"],
                    "UnitPrice": round(price, 4 if price < 2 else 2), "IsActive": True, "UseStandardPrice": False,
                    "CurrencyIsoCode": ccy}, t, admin)
            self.st["products"][cc["code"]] = {"id": pid, "name": cc["name"], "uom": cc["uom"], "list": cc["list_rate"], "pbe": pbe}

    # ------------------------------------------------------------------ client lifecycle (pre-computed events)
    def schedule_client(self, c: dict[str, Any], floor_date: date | None = None) -> None:
        code = c["code"]
        if code in self.st["known"]:
            return
        self.st["known"][code] = True
        d = dates_of(self.ctx, c)
        r = self.r("client", code)
        region = region_key(c)
        tz = {"CA": "America/Toronto", "MX": "America/Mexico_City"}.get(region, "America/New_York")
        ae = self.ae_for(code, region)
        self.st["client_owner"][code] = ae
        churn = churn_of(self.ctx, c)
        sched = self.schedule
        sched(biz_ts(d.sf_first_touch, r, tz), "client_account", {"code": code})
        t0 = d.sf_first_touch + timedelta(days=int(r.integers(0, 10)))
        t_end = d.sf_closed_won
        stages = ["Prospecting", "Qualification", "Needs Analysis", "Value Proposition"]
        if r.random() < 0.5:
            stages.append("Id. Decision Makers")
        if r.random() < 0.4:
            stages.append("Perception Analysis")
        stages += ["Proposal/Price Quote", "Negotiation/Review"]
        span = max(1, (t_end - t0).days)
        fracs = sorted(r.uniform(0.05, 0.95, len(stages) - 1))
        amount, _ = self.client_quote(c)
        first_close = t_end + timedelta(days=int(r.integers(-35, 10)))
        sched(biz_ts(t0, r, tz), "opp_create", {
            "key": f"{code}:NB", "code": code, "kind": "NB", "stage": "Prospecting", "close": first_close.isoformat(),
            "amount": round(amount * float(r.uniform(0.7, 1.3)), -2), "owner": ae})
        slip_at = int(r.integers(2, len(stages)))
        for i, (stage, f) in enumerate(zip(stages[1:], fracs, strict=True), start=1):
            p: dict[str, Any] = {"key": f"{code}:NB", "stage": stage}
            if i == slip_at:
                p["close"] = t_end.isoformat()
            ts_stage = biz_ts(t0 + timedelta(days=int(span * f)), r, tz)
            sched(ts_stage, "opp_stage", p)
            if stage == "Proposal/Price Quote":
                sched(ts_stage + timedelta(minutes=20), "opp_lines", {"key": f"{code}:NB", "code": code})
        won_ts = biz_ts(t_end, r, tz, 10, 17)
        sched(won_ts, "opp_close", {"key": f"{code}:NB", "won": True})
        sched(won_ts + timedelta(hours=float(r.uniform(2, 40))), "contract_create", {"code": code})
        sched(won_ts + timedelta(days=float(r.uniform(3, 6))), "contract_status", {"code": code, "status": "In Approval Process"})
        sign = d.sf_closed_won + timedelta(days=int(r.integers(6, max(7, (d.go_live - d.sf_closed_won).days - 3))))
        sched(biz_ts(sign, r, tz), "contract_signed", {"code": code, "date": sign.isoformat()})
        sched(at_local(d.go_live, 7.0, tz), "contract_status", {"code": code, "status": "Activated"})
        sched(at_local(d.wms_created + timedelta(days=int(r.integers(1, 8))), 11.0, tz), "client_keys", {"code": code, "key": "wms"})
        sched(at_local(d.ax_created + timedelta(days=int(r.integers(1, 5))), 15.0, tz), "client_keys", {"code": code, "key": "erp"})
        for v in rate_versions(self.ctx, c):
            if v.version == 1:
                created = won_ts + timedelta(days=float(r.uniform(3, 10)))
            else:
                created = biz_ts(v.effective_date - timedelta(days=int(r.integers(25, 40))), r, tz)
                sched(created - timedelta(days=int(r.integers(10, 20))), "task", {
                    "code": code, "owner": ae, "subtype": "Email", "subject": f"Rate increase notice ({v.increase_pct:.1f}%) sent",
                    "desc": f"Annual increase of {v.increase_pct:.1f}% effective {v.effective_date.isoformat()} per contract."})
            sched(created, "ratecard_create", {"code": code, "version": v.version})
            sched(at_local(v.effective_date, 6.0, tz), "ratecard_activate", {"code": code, "version": v.version})
        end = add_months(d.contract_start, 36) - timedelta(days=1)
        if churn is None or churn > end:
            self.schedule_renewal(code, end, tz)
        r_exp = self.r("expansion", code)
        if r_exp.random() < 0.4:
            lo = d.go_live + timedelta(days=120)
            hi = min(churn or self.hend + timedelta(days=400), self.hend + timedelta(days=400))
            if hi > lo:
                when = lo + timedelta(days=int(r_exp.integers(0, (hi - lo).days)))
                sched(biz_ts(when, r_exp, tz), "expansion", {"code": code})
        if churn:
            sched(biz_ts(churn - timedelta(days=int(r.integers(30, 60))), r, tz), "task", {
                "code": code, "owner": ae, "subtype": "Call", "subject": "Call: client notice of termination",
                "desc": "Client informed us they will not continue after the current term. Offboarding plan requested."})
            sched(at_local(churn, 17.0, tz), "churn", {"code": code})

    def schedule_renewal(self, code: str, end: date, tz: str) -> None:
        r = self.r("renewal", code, end.isoformat())
        start = end - timedelta(days=int(r.integers(80, 110)))
        self.schedule(biz_ts(start, r, tz), "renewal", {"code": code, "end": end.isoformat()})

    def schedule_prospects(self) -> None:
        n = int(self.ctx.rules.get("prospect_count", 140))
        r = self.r("prospects")
        lo = self.hstart - timedelta(days=270)
        span = (self.hend - lo).days - 7
        for i in range(n):
            when = lo + timedelta(days=int(r.integers(0, max(1, span))))
            self.schedule(biz_ts(when, r), "prospect_create", {"pid": f"P{i + 1:04d}"})
        self.st["next_pid"] = n + 1

    def schedule_globals(self) -> None:
        r = self.r("globals")
        realign = month_start(self.hend, -18) + timedelta(days=3)
        self.schedule(biz_ts(realign, r), "realign", {"from": "wes.harrington", "to": "jordan.ellis", "share": 0.35})
        codes = sorted(self.clients)
        for code in [codes[i] for i in r.choice(len(codes), size=min(4, len(codes)), replace=False)]:
            d = dates_of(self.ctx, self.clients[code])
            lo = d.sf_closed_won + timedelta(days=60)
            hi = self.hend - timedelta(days=20)
            if hi <= lo:
                continue
            when = lo + timedelta(days=int(r.integers(0, (hi - lo).days)))
            self.schedule(biz_ts(when, r), "duplicate", {"code": code, "merge_after": int(r.integers(20, 240)) if r.random() < 0.5 else None})
        gaps = self.crosswalk_gaps()
        self.st["gaps"] = gaps

    def crosswalk_gaps(self) -> dict[str, dict[str, str]]:
        """Planted crm_crosswalk_gaps: 4 blank + 3 mistyped WMS_Owner_Code__c; 2 blank ERP numbers (systematic, not logged)."""
        r = self.r("crosswalk")
        codes = sorted(self.clients)
        order = [codes[i] for i in r.permutation(len(codes))]
        wms: dict[str, str] = {code: "" for code in order[:4]}
        rest = order[4:]
        typo_o = next((c for c in rest if "O" in c), rest[0] if rest else None)
        others = [c for c in rest if c != typo_o]
        if typo_o:
            wms[typo_o] = typo_o.replace("O", "0", 1) if "O" in typo_o else typo_o.lower()
        if others[:1]:
            wms[others[0]] = others[0].lower()
        if others[1:2]:
            wms[others[1]] = others[1] + " "
        order = [c for c in order if c not in wms] + [c for c in order if c in wms]
        erp = {c: "" for c in order[:2]}
        return {"wms": wms, "erp": erp}

    # ------------------------------------------------------------------ quotes
    def client_quote(self, c: dict[str, Any]) -> tuple[float, list[tuple[str, float, float]]]:
        versions = rate_versions(self.ctx, c)
        rates = versions[0].rates if versions else {}
        qty = est_monthly_qty(self.ctx.rules, c, list(rates))
        lines = [(code, qty[code] * 12, rates[code]) for code in qty if rates.get(code)]
        return round(sum(q * p for _, q, p in lines), 2), lines

    def prospect_quote(self, p: dict[str, Any], d: date) -> tuple[float, list[tuple[str, float, float]]]:
        codes = applicable_charge_codes(self.ctx.rules, p, self.seed)
        disc = self.ctx.rules["tier_discounts"][p["tier"]]
        fx = fx_per_usd(self.ctx, d, p["currency"])
        by = {cc["code"]: cc for cc in self.ctx.rules["charge_codes"]}
        qty = est_monthly_qty(self.ctx.rules, p, codes)
        lines = []
        for code, q in qty.items():
            price = by[code]["list_rate"] * (1 - disc) * fx
            if price > 0:
                lines.append((code, q * 12, round(price, 4 if price < 2 else 2)))
        return round(sum(q * pr for _, q, pr in lines), 2), lines

    # ------------------------------------------------------------------ record builders
    def new_account(self, ts: datetime, name: str, region: str, vertical: str, tier: str, currency: str, owner: str,
                    acct_type: str, key: str, whs: list[str], variant: int = 0) -> tuple[str, dict[str, Any]]:
        r = self.r("acct", key, variant)
        addr = hq_address(self.seed, key, region, variant)
        if self.dq("mojibake", r) and any(ord(ch) > 127 for ch in name):
            bad = mojibake(name)
            self.log("mojibake", "Account", key, "Name", name, bad, ts)
            name = bad
        country = COUNTRY_NAME[addr["country"]]
        if addr["country"] == "US":
            country = str(r.choice(["United States", "United States", "United States", "USA", "US"]))
        rev = {"S": (4e6, 30e6), "M": (25e6, 120e6), "L": (100e6, 600e6), "XL": (500e6, 3e9)}[tier]
        slug = ascii_slug(name)[:24] or "client"
        fields = {
            "Name": name, "Type": acct_type, "Industry": INDUSTRY.get(vertical), "Rating": str(r.choice(["Hot", "Warm", "Cold"], p=[0.25, 0.5, 0.25])),
            "AccountSource": str(r.choice(["Web", "Referral", "Trade Show", "Partner", "Outbound", "Other"], p=[0.2, 0.25, 0.2, 0.1, 0.2, 0.05])),
            "AnnualRevenue": round(float(r.uniform(*rev)), -4), "NumberOfEmployees": int(r.integers(20, 4000)),
            "Phone": phone_for(addr["area"], addr["country"], r), "Website": f"https://www.{slug}.example",
            "BillingStreet": addr["street"], "BillingCity": addr["city"], "BillingState": addr["state"] or None,
            "BillingPostalCode": addr["postal"], "BillingCountry": country, "BillingLatitude": addr["lat"],
            "BillingLongitude": addr["lon"], "ShippingStreet": addr["street"], "ShippingCity": addr["city"],
            "ShippingState": addr["state"] or None, "ShippingPostalCode": addr["postal"], "ShippingCountry": country,
            "Description": f"{INDUSTRY.get(vertical, vertical)} brand; interested in {', '.join(whs)} fulfillment.",
            "CurrencyIsoCode": currency, "Vertical__c": vertical, "Region__c": REGION.get(whs[0]) if whs else None,
        }
        aid = self.store.insert("Account", fields, ts, self.uid(owner), self.uid(owner))
        return aid, {"slug": slug, "region": region, "addr": addr}

    def new_contacts(self, ts: datetime, aid: str, key: str, region: str, slug: str, n: int, titles: list[str],
                     owner: str, source: str | None) -> list[str]:
        fk = self.client_fake(key, region, f"contacts{n}{ts.date()}")
        r = self.r("contacts", key, ts.isoformat())
        out = []
        addr = hq_address(self.seed, key, region)
        for i in range(n):
            first, last = fk.first_name(), fk.last_name()
            email = f"{ascii_slug(first)}.{ascii_slug(last)}@{slug}.example"
            fields = {
                "AccountId": aid, "Salutation": None, "FirstName": first, "LastName": last, "Name": f"{first} {last}",
                "Title": titles[i % len(titles)], "Department": "Operations" if i % 3 != 2 else "Finance", "Email": email,
                "Phone": phone_for(addr["area"], addr["country"], r), "MobilePhone": phone_for(addr["area"], addr["country"], r) if r.random() < 0.6 else None,
                "MailingStreet": addr["street"], "MailingCity": addr["city"], "MailingState": addr["state"] or None,
                "MailingPostalCode": addr["postal"], "MailingCountry": COUNTRY_NAME[addr["country"]], "LeadSource": source,
                "HasOptedOutOfEmail": bool(r.random() < 0.05), "CurrencyIsoCode": None,
            }
            if any(ord(ch) > 127 for ch in last) and self.dq("mojibake", r):
                bad = mojibake(last)
                self.log("mojibake", "Contact", key, "LastName", last, bad, ts)
                fields["LastName"], fields["Name"] = bad, f"{first} {bad}"
            cid = self.store.insert("Contact", fields, ts + timedelta(minutes=3 * i), self.uid(owner), self.uid(owner))
            self.st.setdefault("cinfo", {})[cid] = [fields["Name"], email]
            out.append(cid)
        return out

    def opp_history(self, oid: str, o: dict[str, Any], ts: datetime, by: str) -> None:
        self.store.insert("OpportunityHistory", {
            "OpportunityId": oid, "StageName": o["stage"], "Amount": o["amount"], "Probability": STAGES[o["stage"]][0],
            "CloseDate": o["close"], "ForecastCategory": STAGES[o["stage"]][1]}, ts, by)

    def opp_fields(self, o: dict[str, Any]) -> dict[str, Any]:
        prob, fc = STAGES[o["stage"]]
        close = date.fromisoformat(o["close"]) if o["close"][:4] < "9000" else date(2099, 12, 31)
        return {"StageName": o["stage"], "Amount": o["amount"], "Probability": prob,
                "ExpectedRevenue": round((o["amount"] or 0) * prob / 100, 2), "CloseDate": o["close"],
                "ForecastCategoryName": fc, "IsClosed": o["stage"].startswith("Closed"), "IsWon": o["stage"] == "Closed Won",
                "FiscalYear": close.year, "FiscalQuarter": (close.month - 1) // 3 + 1}

    # ------------------------------------------------------------------ events: clients
    def ev_client_account(self, ts: datetime, p: dict[str, Any]) -> None:
        code = p["code"]
        c = self.clients.get(code)
        if c is None or code in self.st["acct"]:
            return
        region = region_key(c)
        owner = self.st["client_owner"][code]
        aid, meta = self.new_account(ts, c["name"], region, c["vertical"], c["tier"], c["currency"], owner, "Prospect", code, c["warehouses"])
        source = self.store.ins[aid]["data"]["AccountSource"]
        n = int(self.r("ncontacts", code).integers(3, 6))
        contacts = self.new_contacts(ts + timedelta(minutes=10), aid, code, region, meta["slug"], n, CONTACT_TITLES, owner, source)
        self.st["acct"][code] = {"id": aid, "owner": owner, "contacts": contacts, "csm": self.csm_for(code), "name": c["name"],
                                 "currency": c["currency"], "region": region, "slug": meta["slug"], "source": source}
        self.add_task(ts + timedelta(hours=1), code, owner, "Call", "Call: intro / discovery", "Discovery call - current 3PL, volumes, pain points.")

    def ev_opp_create(self, ts: datetime, p: dict[str, Any]) -> None:
        key = p["key"]
        if key in self.st["opp"]:
            return
        code = p.get("code")
        acct = self.st["acct"].get(code) if code else self.st["pros"].get(p.get("pid", ""))
        if acct is None:
            return
        kind = p["kind"]
        owner = p.get("owner") or acct["owner"]
        label = {"NB": "New Business", "EXP": "Existing Business - Expansion", "REN": "Existing Business - Renewal"}[kind]
        c = self.clients.get(code) or acct
        whs = c.get("warehouses") or acct.get("warehouses", [])
        name = acct["name"]
        title = {"NB": f"{name} - 3PL Fulfillment {ts.year}", "EXP": f"{name} - Expansion ({p.get('wh', 'VAS')})",
                 "REN": f"{name} - Renewal {ts.year}"}[kind]
        o = {"stage": p["stage"], "amount": float(p["amount"]), "close": p["close"], "owner": owner, "acct": acct["id"],
             "closed": False, "kind": kind, "code": code, "pid": p.get("pid"), "stalls": 0, "currency": acct["currency"]}
        r = self.r("oppf", key)
        tier = c.get("tier", "M")
        fields = {"AccountId": acct["id"], "Name": title, "Type": label, "LeadSource": acct.get("source"),
                  "NextStep": "Discovery call", "Description": None, "Pricebook2Id": None, "ContractId": None,
                  "HasOpportunityLineItem": False, "Loss_Reason__c": None,
                  "Est_Monthly_Orders__c": float(self.ctx.rules["volume_tiers"][tier] * 30 * float(r.uniform(0.8, 1.2))),
                  "Warehouses_Requested__c": ";".join(p.get("whs") or whs) or None, "CurrencyIsoCode": acct["currency"],
                  **self.opp_fields(o)}
        if kind == "NB" and p.get("pid") and self.dq("future_date", r):
            bad = str(int(o["close"][:4]) + 10) + o["close"][4:]
            self.log("future_date", "Opportunity", title, "CloseDate", o["close"], bad, ts)
            o["close"] = bad
            fields.update(self.opp_fields(o))
        oid = self.store.insert("Opportunity", fields, ts, self.uid(owner), self.uid(owner))
        o["id"] = oid
        self.st["opp"][key] = o
        self.opp_history(oid, o, ts, self.uid(owner))

    def ev_opp_stage(self, ts: datetime, p: dict[str, Any]) -> None:
        o = self.st["opp"].get(p["key"])
        if o is None or o["closed"]:
            return
        o["stage"] = p.get("stage", o["stage"])
        if p.get("close"):
            o["close"] = p["close"]
        if p.get("amount") is not None:
            o["amount"] = float(p["amount"])
        nxt = {"Qualification": "Needs analysis meeting", "Needs Analysis": "Solution design / site visit",
               "Value Proposition": "Present solution", "Id. Decision Makers": "Meet CFO", "Perception Analysis": "Reference calls",
               "Proposal/Price Quote": "Send pricing proposal", "Negotiation/Review": "Redline MSA"}.get(o["stage"], None)
        fields = self.opp_fields(o)
        if nxt:
            fields["NextStep"] = nxt
        self.store.update(o["id"], fields, ts, self.uid(o["owner"]))
        self.opp_history(o["id"], o, ts, self.uid(o["owner"]))

    def ev_opp_lines(self, ts: datetime, p: dict[str, Any]) -> None:
        o = self.st["opp"].get(p["key"])
        if o is None or o["closed"]:
            return
        if p.get("code"):
            amount, lines = self.client_quote(self.clients[p["code"]])
            service = dates_of(self.ctx, self.clients[p["code"]]).go_live.isoformat()
        else:
            pros = self.st["pros"][o["pid"]]
            amount, lines = self.prospect_quote(pros, ts.date())
            service = o["close"]
        by = self.uid(o["owner"])
        for i, (code, q, price) in enumerate(lines):
            prod = self.st["products"][code]
            self.store.insert("OpportunityLineItem", {
                "OpportunityId": o["id"], "PricebookEntryId": prod["pbe"].get(o["currency"]), "Product2Id": prod["id"],
                "Name": f"{self.store_name(o)} {prod['name']}", "ProductCode": code, "Quantity": float(q), "UnitPrice": price,
                "ListPrice": round(prod["list"] * fx_per_usd(self.ctx, ts.date(), o["currency"]), 4),
                "TotalPrice": round(q * price, 2), "Discount": None, "ServiceDate": service, "CurrencyIsoCode": o["currency"]},
                ts + timedelta(seconds=5 * i), by)
        o["amount"] = amount
        self.store.update(o["id"], {"HasOpportunityLineItem": True, "Pricebook2Id": self.st.get("pricebook"), **self.opp_fields(o)}, ts, by)
        self.opp_history(o["id"], o, ts, by)

    @staticmethod
    def store_name(o: dict[str, Any]) -> str:
        return {"NB": "New", "EXP": "Expansion", "REN": "Renewal"}[o["kind"]]

    def ev_opp_close(self, ts: datetime, p: dict[str, Any]) -> None:
        o = self.st["opp"].get(p["key"])
        if o is None or o["closed"]:
            return
        o["closed"] = True
        o["stage"] = "Closed Won" if p.get("won") else "Closed Lost"
        if p.get("won"):
            o["close"] = ts.date().isoformat()
        by = self.uid(o["owner"])
        fields = self.opp_fields(o)
        fields["NextStep"] = None
        if not p.get("won"):
            r = self.r("loss", p["key"])
            fields["Loss_Reason__c"] = p.get("reason") or str(r.choice(LOSS_REASONS[0], p=LOSS_REASONS[1]))
            o["close"] = ts.date().isoformat() if o["close"] > ts.date().isoformat() or o["close"][:4] > "2090" else o["close"]
            fields.update(CloseDate=o["close"])
        self.store.update(o["id"], fields, ts, by)
        self.opp_history(o["id"], o, ts, by)
        if p.get("won") and o["kind"] == "NB" and o.get("code"):
            acct = self.st["acct"][o["code"]]
            typ = "Customer - Channel" if acct.get("source") == "Partner" else "Customer - Direct"
            self.store.update(acct["id"], {"Type": typ, "Rating": "Hot"}, ts, by)

    def ev_contract_create(self, ts: datetime, p: dict[str, Any]) -> None:
        code = p["code"]
        c = self.clients[code]
        acct = self.st["acct"].get(code)
        if acct is None or code in self.st["contract"]:
            return
        d = dates_of(self.ctx, c)
        hours, target = sla_terms(self.seed, self.ctx.rules, c)
        versions = rate_versions(self.ctx, c)
        incr = next((v.increase_pct for v in versions if v.version == 2), None)
        lo, hi = self.ctx.rules["annual_rate_increase_pct"]
        end = add_months(d.contract_start, 36) - timedelta(days=1)
        number = f"{self.num('contract', 100):08d}"
        r = self.r("contract", code)
        cid = self.store.insert("Contract", {
            "AccountId": acct["id"], "ContractNumber": number, "Status": "Draft", "StatusCode": "Draft",
            "StartDate": d.contract_start.isoformat(), "EndDate": end.isoformat(), "ContractTerm": 36,
            "SpecialTerms": f"Monthly minimum applies. Rates increase {lo:g}-{hi:g}% on each contract anniversary.",
            "Description": f"Master services agreement - warehousing and fulfillment ({', '.join(c['warehouses'])}).",
            "Auto_Renew__c": bool(r.random() < 0.75), "Annual_Increase_Pct__c": incr if incr is not None else float(lo),
            "SLA_Ship_Hours__c": hours, "SLA_Target_Pct__c": target, "CurrencyIsoCode": c["currency"]},
            ts, self.uid(acct["owner"]), self.uid(acct["owner"]))
        self.st["contract"][code] = {"id": cid, "number": number, "end": end.isoformat(), "status": "Draft"}
        o = self.st["opp"].get(f"{code}:NB")
        if o:
            self.store.update(o["id"], {"ContractId": cid}, ts, self.uid(acct["owner"]))

    def ev_contract_status(self, ts: datetime, p: dict[str, Any]) -> None:
        k = self.st["contract"].get(p["code"])
        if k is None or k["status"] in ("Expired",):
            return
        status = p["status"]
        if status == "Activated" and k["status"] == "Activated":
            return
        k["status"] = status
        fields: dict[str, Any] = {"Status": status, "StatusCode": {"In Approval Process": "InApproval"}.get(status, status)}
        if status == "Activated":
            fields["ActivatedDate"] = sf_dt(ts)
        if p.get("end"):
            fields["EndDate"] = p["end"]
            k["end"] = p["end"]
        owner = self.st["acct"][p["code"]]["owner"]
        self.store.update(k["id"], fields, ts, self.uid(owner if status != "Activated" else "hannah.price"))

    def ev_contract_signed(self, ts: datetime, p: dict[str, Any]) -> None:
        k = self.st["contract"].get(p["code"])
        if k is None:
            return
        r = self.r("signed", p["code"])
        signed = date.fromisoformat(p["date"])
        self.store.update(k["id"], {"CustomerSignedDate": signed.isoformat(),
                                    "CompanySignedDate": (signed + timedelta(days=int(r.integers(0, 4)))).isoformat()},
                          ts, self.uid("hannah.price"))

    def ev_client_keys(self, ts: datetime, p: dict[str, Any]) -> None:
        code = p["code"]
        acct = self.st["acct"].get(code)
        if acct is None:
            return
        gaps = self.st.get("gaps", {"wms": {}, "erp": {}})
        r = self.r("keys", code, p["key"])
        if p["key"] == "wms":
            value = gaps["wms"].get(code, code)
            d = dates_of(self.ctx, self.clients[code])
            fields: dict[str, Any] = {"Client_Since__c": d.go_live.isoformat()}
            if value != "":
                fields["WMS_Owner_Code__c"] = value
            self.store.update(acct["id"], fields, ts, self.uid(acct["owner"]))
        else:
            value = gaps["erp"].get(code)
            if value == "":
                return
            value = ax_account_num(self.ctx.rules, code)
            if self.dq("key_whitespace_case", r):
                bad = value.lower() if r.random() < 0.5 else value + " "
                self.log("key_whitespace_case", "Account", acct["id"], "ERP_Account_Number__c", value, bad, ts)
                value = bad
            self.store.update(acct["id"], {"ERP_Account_Number__c": value}, ts, self.uid("dana.whitfield"))

    def ev_ratecard_create(self, ts: datetime, p: dict[str, Any]) -> None:
        code = p["code"]
        acct = self.st["acct"].get(code)
        if acct is None:
            return
        c = self.clients[code]
        versions = {v.version: v for v in rate_versions(self.ctx, c)}
        v = versions.get(p["version"])
        cards = self.st["rc"].setdefault(code, [])
        if v is None or any(x["version"] == v.version for x in cards):
            return
        by = self.uid(acct["owner"])
        k = self.st["contract"].get(code)
        if cards and cards[-1]["status"] in ("Active", "Draft"):
            prev = cards[-1]
            prev["end"] = (v.effective_date - timedelta(days=1)).isoformat()
            self.store.update(prev["id"], {"End_Date__c": prev["end"]}, ts, by)
        name = f"RC-{self.num('rc', 1):04d}"
        rid = self.store.insert("Rate_Card__c", {
            "Name": name, "Account__c": acct["id"], "Contract__c": k["id"] if k else None,
            "Effective_Date__c": v.effective_date.isoformat(), "End_Date__c": None,
            "Status__c": "Draft", "CurrencyIsoCode": v.currency}, ts, by, by)
        by_code = {cc["code"]: cc for cc in self.ctx.rules["charge_codes"]}
        for i, (charge, rate) in enumerate(sorted(v.rates.items())):
            cc = by_code[charge]
            self.store.insert("Rate_Card_Line__c", {
                "Name": f"RCL-{self.num('rcl', 1):06d}", "Rate_Card__c": rid, "Charge_Code__c": charge,
                "Description__c": cc["name"] + (" (markup %)" if charge == "FRT-RCV" else ""), "Rate__c": rate,
                "Unit_of_Measure__c": "PCT" if charge == "FRT-RCV" else cc["uom"],
                "Monthly_Minimum__c": v.monthly_minimum if charge == "MIN-ADJ" else None, "CurrencyIsoCode": v.currency},
                ts + timedelta(seconds=2 * i + 1), by)
        cards.append({"id": rid, "version": v.version, "eff": v.effective_date.isoformat(), "status": "Draft", "end": None})

    def ev_ratecard_activate(self, ts: datetime, p: dict[str, Any]) -> None:
        code = p["code"]
        cards = self.st["rc"].get(code, [])
        card = next((x for x in cards if x["version"] == p["version"]), None)
        if card is None or card["status"] != "Draft":
            return
        by = self.uid("hannah.price")
        for prev in cards:
            if prev["status"] == "Active":
                prev["status"] = "Superseded"
                self.store.update(prev["id"], {"Status__c": "Superseded"}, ts, by)
        card["status"] = "Active"
        self.store.update(card["id"], {"Status__c": "Active"}, ts, by)
        k = self.st["contract"].get(code)
        versions = {v.version: v for v in rate_versions(self.ctx, self.clients[code])}
        v = versions.get(p["version"])
        if k and v and v.version > 1:
            self.store.update(k["id"], {"Annual_Increase_Pct__c": v.increase_pct}, ts, by)

    def ev_renewal(self, ts: datetime, p: dict[str, Any]) -> None:
        code = p["code"]
        acct = self.st["acct"].get(code)
        c = self.clients.get(code)
        if acct is None or c is None:
            return
        churn = churn_of(self.ctx, c)
        end = date.fromisoformat(p["end"])
        if churn and churn <= end:
            return
        r = self.r("ren", code, p["end"])
        amount, _ = self.client_quote(c)
        key = f"{code}:REN:{end.year}"
        tz = "America/New_York"
        self.ev_opp_create(ts, {"key": key, "code": code, "kind": "REN", "stage": "Qualification", "close": (end - timedelta(days=15)).isoformat(),
                                "amount": round(amount * float(r.uniform(1.05, 1.2)), -2)})
        mid = ts + (as_utc(at_local(end, 12, tz)) - ts) * 0.5
        self.schedule(mid, "opp_stage", {"key": key, "stage": "Negotiation/Review"})
        close_ts = biz_ts(end - timedelta(days=int(r.integers(8, 30))), r)
        self.schedule(close_ts, "opp_close", {"key": key, "won": True})
        new_end = add_months(end + timedelta(days=1), 36) - timedelta(days=1)
        self.schedule(at_local(end, 23.0, tz), "contract_extend", {"code": code, "end": new_end.isoformat()})

    def ev_contract_extend(self, ts: datetime, p: dict[str, Any]) -> None:
        k = self.st["contract"].get(p["code"])
        if k is None or k["status"] == "Expired":
            return
        k["end"] = p["end"]
        self.store.update(k["id"], {"EndDate": p["end"], "ContractTerm": 36}, ts, self.uid("hannah.price"))
        c = self.clients.get(p["code"])
        churn = churn_of(self.ctx, c) if c else None
        end = date.fromisoformat(p["end"])
        if churn is None or churn > end:
            self.schedule_renewal(p["code"], end, "America/New_York")

    def ev_expansion(self, ts: datetime, p: dict[str, Any]) -> None:
        code = p["code"]
        acct = self.st["acct"].get(code)
        c = self.clients.get(code)
        if acct is None or c is None or (churn_of(self.ctx, c) and churn_of(self.ctx, c) <= ts.date()):
            return
        r = self.r("exp", code)
        base, _ = self.client_quote(c)
        key = f"{code}:EXP"
        wh = str(r.choice(["ATL1", "DFW1", "RNO1", "NJ1", "LAX1", "CHI1"]))
        days = int(r.integers(30, 120))
        self.ev_opp_create(ts, {"key": key, "code": code, "kind": "EXP", "stage": "Qualification", "wh": wh, "whs": [wh],
                                "close": (ts.date() + timedelta(days=days)).isoformat(), "amount": round(base * float(r.uniform(0.15, 0.4)), -2)})
        steps = ["Needs Analysis", "Proposal/Price Quote", "Negotiation/Review"]
        for i, st in enumerate(steps, start=1):
            self.schedule(ts + timedelta(days=days * i / 4), "opp_stage", {"key": key, "stage": st})
        self.schedule(ts + timedelta(days=days), "opp_close", {"key": key, "won": bool(r.random() < 0.6)})

    def ev_churn(self, ts: datetime, p: dict[str, Any]) -> None:
        code = p["code"]
        acct = self.st["acct"].get(code)
        if acct is None or acct.get("former"):
            return
        acct["former"] = True
        by = self.uid(acct["owner"])
        self.store.update(acct["id"], {"Type": "Former Customer", "Rating": "Cold"}, ts, by)
        k = self.st["contract"].get(code)
        if k and k["status"] != "Expired":
            k["status"] = "Expired"
            self.store.update(k["id"], {"Status": "Expired", "StatusCode": "Expired", "EndDate": ts.date().isoformat()}, ts, by)
        for card in self.st["rc"].get(code, []):
            if card["status"] in ("Active", "Draft"):
                card["status"] = "Expired"
                self.store.update(card["id"], {"Status__c": "Expired", "End_Date__c": ts.date().isoformat()}, ts, by)
        for key, o in self.st["opp"].items():
            if o.get("code") == code and not o["closed"]:
                self.ev_opp_close(ts, {"key": key, "won": False, "reason": "Went with competitor"})

    def ev_realign(self, ts: datetime, p: dict[str, Any]) -> None:
        r = self.r("realign", ts.date().isoformat())
        for code, acct in sorted(self.st["acct"].items()):
            if acct["owner"] != p["from"] or r.random() >= p["share"]:
                continue
            self.reassign(code, acct, p["to"], ts)

    def reassign(self, code: str, acct: dict[str, Any], new_owner: str, ts: datetime) -> None:
        by = self.uid("hannah.price")
        acct["owner"] = new_owner
        self.st["client_owner"][code] = new_owner
        self.store.update(acct["id"], {"OwnerId": self.uid(new_owner)}, ts, by)
        for cid in acct["contacts"]:
            self.store.update(cid, {"OwnerId": self.uid(new_owner)}, ts, by)
        for o in self.st["opp"].values():
            if o.get("code") == code and not o["closed"]:
                o["owner"] = new_owner
                self.store.update(o["id"], {"OwnerId": self.uid(new_owner)}, ts, by)
        k = self.st["contract"].get(code)
        if k:
            self.store.update(k["id"], {"OwnerId": self.uid(new_owner)}, ts, by)

    # ------------------------------------------------------------------ duplicates & merges
    def ev_duplicate(self, ts: datetime, p: dict[str, Any]) -> str | None:
        code = p["code"]
        acct = self.st["acct"].get(code)
        c = self.clients.get(code)
        if acct is None or c is None:
            return None
        r = self.r("dup", code, ts.isoformat())
        base = c["name"]
        variants = [f"{base} Inc.", base.upper(), f"{base} ({c['warehouses'][0]})", base.replace("e", "", 1) if "e" in base else base + " LLC",
                    f"{base} - AP"]
        name = variants[int(r.integers(0, len(variants)))]
        owner = str(r.choice(US_AES + CSMS))
        aid, _meta = self.new_account(ts, name, acct["region"], c["vertical"], c["tier"], c["currency"], owner, "Customer - Direct",
                                     code, c["warehouses"], variant=1)
        contacts = self.new_contacts(ts + timedelta(minutes=5), aid, code + "-dup", acct["region"], acct["slug"], 1,
                                     ["Accounts Payable Specialist"], owner, None)
        self.log("duplicate_account", "Account", aid, "Name", acct["id"], name, ts, f"duplicate of {code} account {acct['id']}")
        dup = {"id": aid, "master": acct["id"], "code": code, "merged": False, "contacts": contacts}
        self.st["dups"].append(dup)
        if p.get("merge_after"):
            self.schedule(ts + timedelta(days=int(p["merge_after"])), "merge", {"id": aid})
        return aid

    def ev_merge(self, ts: datetime, p: dict[str, Any]) -> None:
        dup = next((d for d in self.st["dups"] if d["id"] == p["id"]), None)
        if dup is None or dup["merged"]:
            return
        dup["merged"] = True
        by = self.uid("dana.whitfield")
        acct = self.st["acct"].get(dup["code"])
        for cid in dup["contacts"]:
            self.store.update(cid, {"AccountId": dup["master"]}, ts, by)
            if acct:
                acct["contacts"].append(cid)
        self.store.delete(dup["id"], ts, by, master=dup["master"])

    # ------------------------------------------------------------------ prospects
    def ev_prospect_create(self, ts: datetime, p: dict[str, Any]) -> None:
        pid = p["pid"]
        if pid in self.st["pros"]:
            return
        r = self.r("pros", pid)
        vertical = str(r.choice(list(PROSPECT_NOUNS)))
        tier = str(r.choice(["S", "M", "L", "XL"], p=[0.35, 0.4, 0.2, 0.05]))
        region = str(r.choice(["US", "CA", "MX", "EU", "GB"], p=[0.8, 0.1, 0.06, 0.03, 0.01]))
        currency = {"US": "USD", "CA": "CAD", "MX": "MXN", "EU": "EUR", "GB": "GBP"}[region]
        pool = {"US": ["ATL1", "DFW1", "RNO1", "CHI1", "NJ1", "LAX1"], "CA": ["TOR1"], "MX": ["MTY1"], "EU": ["NJ1"], "GB": ["NJ1"]}[region]
        whs = sorted({str(x) for x in r.choice(pool, size=min(len(pool), int(r.integers(1, 3))), replace=False)})
        name = f"{PROSPECT_WORDS[int(r.integers(0, len(PROSPECT_WORDS)))]} {r.choice(PROSPECT_NOUNS[vertical])!s}"
        taken = {a["name"] for a in self.st["pros"].values()} | {c["name"] for c in self.ctx.rules["clients"]}
        if name in taken:
            name = f"{name} {r.choice(['Group', 'Brands', 'International', 'Holdings', 'USA'])!s}"
        owner = self.ae_for(pid, region)
        aid, meta = self.new_account(ts, name, region, vertical, tier, currency, owner,
                                     "Other" if r.random() < 0.06 else "Prospect", pid, whs)
        contacts = self.new_contacts(ts + timedelta(minutes=8), aid, pid, region, meta["slug"], int(r.integers(1, 4)),
                                     PROSPECT_TITLES, owner, self.store.ins[aid]["data"]["AccountSource"])
        self.st["pros"][pid] = {"id": aid, "owner": owner, "contacts": contacts, "name": name, "currency": currency,
                                "vertical": vertical, "tier": tier, "region": region, "warehouses": whs, "code": pid,
                                "slug": meta["slug"], "source": self.store.ins[aid]["data"]["AccountSource"]}
        if self.dq("duplicate_account", r):
            dup_id, _ = self.new_account(ts + timedelta(days=1), name.upper(), region, vertical, tier, currency, owner, "Prospect",
                                         pid, whs, variant=2)
            self.log("duplicate_account", "Account", dup_id, "Name", aid, name.upper(), ts, f"duplicate prospect of {aid}")
        if r.random() < 0.85:
            self.schedule(ts + timedelta(days=float(r.uniform(0.5, 20))), "prospect_opp", {"pid": pid})

    def ev_prospect_opp(self, ts: datetime, p: dict[str, Any]) -> None:
        pid = p["pid"]
        pros = self.st["pros"].get(pid)
        if pros is None:
            return
        r = self.r("popp", pid)
        amount, _ = self.prospect_quote(pros, ts.date())
        key = f"{pid}:NB"
        self.ev_opp_create(ts, {"key": key, "pid": pid, "kind": "NB", "stage": str(r.choice(["Prospecting", "Qualification"])),
                                "close": (ts.date() + timedelta(days=int(r.integers(60, 150)))).isoformat(),
                                "amount": round(amount * float(r.uniform(0.6, 1.4)), -2), "whs": pros["warehouses"]})
        self.schedule(ts + timedelta(days=float(r.uniform(7, 35))), "prospect_advance", {"key": key, "n": 0})

    def ev_prospect_advance(self, ts: datetime, p: dict[str, Any]) -> None:
        key = p["key"]
        o = self.st["opp"].get(key)
        if o is None or o["closed"]:
            return
        n = int(p.get("n", 0))
        r = self.r("adv", key, n)
        idx = OPEN_STAGES.index(o["stage"])
        if r.random() < 0.10 + 0.02 * idx:
            self.ev_opp_close(ts, {"key": key, "won": False})
            return
        today = ts.date()
        if idx >= OPEN_STAGES.index("Negotiation/Review"):
            o["stalls"] += 1
            if o["stalls"] > int(r.integers(2, 5)):
                self.ev_opp_close(ts, {"key": key, "won": False, "reason": str(r.choice(["No decision", "Timing", "Price"]))})
                return
            new_close = (max(today, date.fromisoformat(o["close"][:10]) if o["close"][:4] < "2090" else today)
                         + timedelta(days=int(r.integers(30, 90)))).isoformat()
            self.ev_opp_stage(ts, {"key": key, "close": new_close})
        else:
            step = 2 if r.random() < 0.2 and idx + 2 < len(OPEN_STAGES) else 1
            stage = OPEN_STAGES[min(idx + step, len(OPEN_STAGES) - 1)]
            upd: dict[str, Any] = {"key": key, "stage": stage}
            if r.random() < 0.3:
                upd["amount"] = round(o["amount"] * float(r.uniform(0.8, 1.2)), -2)
            if o["close"][:4] < "2090" and date.fromisoformat(o["close"][:10]) < today + timedelta(days=14):
                upd["close"] = (today + timedelta(days=int(r.integers(21, 75)))).isoformat()
            self.ev_opp_stage(ts, upd)
            if stage == "Proposal/Price Quote":
                self.ev_opp_lines(ts + timedelta(minutes=30), {"key": key})
        self.add_task(ts + timedelta(minutes=45), o["pid"], o["owner"], str(r.choice(["Call", "Email"])),
                      f"Follow-up: {o['stage'].lower()}", None, what=o["id"], pros=True)
        self.schedule(ts + timedelta(days=float(r.uniform(10, 45))), "prospect_advance", {"key": key, "n": n + 1})

    # ------------------------------------------------------------------ tasks
    def add_task(self, ts: datetime, code: str, owner: str, subtype: str, subject: str, desc: str | None,
                 what: str | None = None, pros: bool = False, status: str = "Completed", due: date | None = None) -> str | None:
        acct = (self.st["pros"] if pros else self.st["acct"]).get(code)
        if acct is None:
            return None
        r = self.r("task", code, ts.isoformat(), subject)
        who = acct["contacts"][int(r.integers(0, len(acct["contacts"])))] if acct["contacts"] else None
        done = status == "Completed"
        fields = {"WhoId": who, "WhatId": what or acct["id"], "AccountId": acct["id"],
                  "Subject": subject if subtype == "Task" else (subject if subject.startswith(subtype) else f"{subtype}: {subject}"),
                  "Status": status, "Priority": "Normal" if r.random() < 0.85 else "High", "TaskSubtype": subtype,
                  "ActivityDate": (due or ts.date()).isoformat(), "CompletedDateTime": sf_dt(ts) if done else None,
                  "IsClosed": done, "CallDurationInSeconds": int(r.integers(180, 2400)) if subtype == "Call" and done else None,
                  "Description": desc}
        tid = self.store.insert("Task", fields, ts, self.uid(owner), self.uid(owner))
        if done:
            self.lad[acct["id"]] = max(self.lad.get(acct["id"], ""), fields["ActivityDate"])
        return tid

    def ev_task(self, ts: datetime, p: dict[str, Any]) -> None:
        self.add_task(ts, p["code"], p["owner"], p["subtype"], p["subject"], p.get("desc"), pros=p.get("pros", False),
                      status=p.get("status", "Completed"), due=date.fromisoformat(p["due"]) if p.get("due") else None)

    def ev_task_done(self, ts: datetime, p: dict[str, Any]) -> None:
        status = p["status"]
        fields: dict[str, Any] = {"Status": status, "IsClosed": status in ("Completed", "Deferred")}
        if status == "Completed":
            fields["CompletedDateTime"] = sf_dt(ts)
            self.lad[p["acct"]] = max(self.lad.get(p["acct"], ""), ts.date().isoformat())
        self.store.update(p["id"], fields, ts, self.uid(p["owner"]))

    def ev_delete(self, ts: datetime, p: dict[str, Any]) -> None:
        self.store.delete(p["id"], ts, self.uid(p.get("by", "dana.whitfield")))

    def background(self, win: Window, r: np.random.Generator) -> None:
        """Calls, emails, QBRs, prospect follow-ups and junk tasks for one window (Poisson)."""
        days = win.hours / 24
        today = win.start.date()
        signal_start: dict[str, date] = {}
        for code, cs in ((self.ctx.planted or {}).get("client_behaviour", {}).get("churn_signals", {}) or {}).items():
            signal_start[code] = month_start(self.hend, int(cs["from_month"]))
        for code, acct in sorted(self.st["acct"].items()):
            c = self.clients.get(code)
            if c is None or acct.get("former"):
                continue
            d = dates_of(self.ctx, c)
            if today < d.sf_closed_won:
                continue
            boost = 2.5 if code in signal_start and today >= signal_start[code] else 1.0
            for subtype, owner, every, subjects in (
                ("Call", acct["csm"], 9 / boost, ["Weekly ops check-in", "Inventory review", "Peak planning", "Escalation follow-up"]),
                ("Email", acct["csm"], 4 / boost, ["Order status update", "Inbound schedule", "Monthly KPI pack", "Re: open cases"]),
                ("Email", acct["owner"], 25, ["Account review", "Contract question", "Rate discussion"]),
            ):
                for _ in range(int(r.poisson(days / every))):
                    ts = win.start + timedelta(seconds=float(r.uniform(0, win.hours * 3600)))
                    subject = str(r.choice(subjects))
                    if boost > 1 and r.random() < 0.3:
                        subject = "Escalation: service issues"
                    self.add_task(ts, code, owner, subtype, subject, None)
                    if subtype == "Email" and r.random() < 0.015:
                        jid = self.add_task(ts + timedelta(minutes=float(r.uniform(0, 5))), code, owner, subtype, subject, None)
                        if jid:
                            self.schedule(ts + timedelta(hours=float(r.uniform(2, 48))), "delete", {"id": jid, "by": owner})
                    if subtype == "Call" and r.random() < 0.15:
                        due = (ts + timedelta(days=float(r.uniform(3, 10)))).date()
                        fid = self.add_task(ts + timedelta(minutes=2), code, owner, "Task", "Send follow-up summary", None,
                                            status="Not Started", due=due)
                        if fid:
                            self.schedule(at_local(due, 14.0, "America/New_York"), "task_done",
                                          {"id": fid, "owner": owner, "acct": acct["id"],
                                           "status": "Completed" if r.random() < 0.8 else "Deferred"})
        for key, o in sorted(self.st["opp"].items()):
            if o["closed"] or not o.get("pid"):
                continue
            for _ in range(int(r.poisson(days / 9))):
                ts = win.start + timedelta(seconds=float(r.uniform(0, win.hours * 3600)))
                self.add_task(ts, o["pid"], o["owner"], str(r.choice(["Call", "Email"])), str(r.choice(
                    ["Prospecting touch", "Send case study", "Pricing follow-up", "Schedule site visit"])), None, what=o["id"], pros=True)
        for code, acct in sorted(self.st["acct"].items()):
            c = self.clients.get(code)
            if c is None or acct.get("former") or c["tier"] not in ("L", "XL"):
                continue
            for ts in win.crosses_local_time("America/New_York", 9, 0):
                if ts.day == 1 and ts.month in (2, 5, 8, 11) and today >= dates_of(self.ctx, c).go_live:
                    due = ts.date() + timedelta(days=14)
                    q = (ts.month - 1) // 3
                    tid = self.add_task(ts, code, acct["owner"], "Task", f"QBR - Q{q if q else 4} {ts.year if q else ts.year - 1}",
                                        "Quarterly business review deck and KPI pack", status="Not Started", due=due)
                    if tid:
                        self.schedule(at_local(due, 15.0, "America/New_York"), "task_done",
                                      {"id": tid, "owner": acct["owner"], "acct": acct["id"], "status": "Completed"})
        for _ in range(int(r.poisson(days * 0.3))):
            ts = win.start + timedelta(seconds=float(r.uniform(0, win.hours * 3600)))
            self.schedule(ts, "spam_case", {})
        if win.start >= as_utc(self.ctx.history_end):
            rate = int(self.ctx.rules.get("prospect_count", 140)) / (30.4 * (self.months() + 9))
            for _ in range(int(r.poisson(days * rate))):
                pid = f"P{self.st.get('next_pid', 1):04d}"
                self.st["next_pid"] = self.st.get("next_pid", 1) + 1
                self.schedule(win.start + timedelta(seconds=float(r.uniform(0, win.hours * 3600))), "prospect_create", {"pid": pid})
            for code, acct in sorted(self.st["acct"].items()):
                if r.random() < days * 0.08 / 30:
                    region_aes = US_AES if acct["region"] == "US" else [acct["owner"]]
                    new = str(r.choice(region_aes))
                    if new != acct["owner"]:
                        self.reassign(code, acct, new, win.start + timedelta(seconds=float(r.uniform(0, win.hours * 3600))))

    def months(self) -> int:
        return int(self.ctx.rules["size_presets"].get(self.ctx.preset, {"months": 36})["months"])

    # ------------------------------------------------------------------ cases
    def ev_spam_case(self, ts: datetime, p: dict[str, Any]) -> None:
        r = self.r("spam", ts.isoformat())
        fk = faker_for("US", derive_seed(self.seed, "spam", ts.isoformat()) % (2**31))
        cid = self.store.insert("Case", {
            "CaseNumber": f"{self.num('case', 12001):08d}", "Subject": str(r.choice(SPAM_SUBJECTS)),
            "Description": "Dear sir/madam, " + fk.sentence(nb_words=14), "Status": "New", "Priority": "Low", "Origin": "Web",
            "Type": None, "Reason": None, "IsClosed": False, "IsEscalated": False, "SuppliedEmail": fk.email(),
            "SuppliedName": fk.name()}, ts, self.uid("integration.user"), self.uid("integration.user"))
        self.schedule(ts + timedelta(hours=float(r.uniform(2, 30))), "delete", {"id": cid, "by": "dana.whitfield"})

    def new_case(self, ts: datetime, p: dict[str, Any]) -> None:
        code = p["code"]
        acct = self.st["acct"].get(code)
        if acct is None:
            return
        r = self.r("case", p["salt"])
        origin = str(r.choice(["Email", "Client Portal", "Phone", "Web"], p=[0.45, 0.3, 0.15, 0.1]))
        contact = acct["contacts"][int(r.integers(0, len(acct["contacts"])))] if acct["contacts"] else None
        csm = acct["csm"]
        number = f"{self.num('case', 12001):08d}"
        fields: dict[str, Any] = {
            "CaseNumber": number, "AccountId": acct["id"], "ContactId": contact, "Subject": p["subject"][:255],
            "Description": p["desc"], "Status": "New", "Priority": p.get("priority", "Medium"), "Origin": origin,
            "Reason": p["reason"], "Type": p.get("type", "Problem"), "IsClosed": False, "IsEscalated": False,
            "WMS_Order_Number__c": p.get("order_nbr"), "Tracking_Number__c": p.get("tracking"),
            "Warehouse__c": p.get("wh"), "Root_Cause__c": None, "Credit_Amount__c": None, "CurrencyIsoCode": acct["currency"],
        }
        if origin in ("Email", "Web") and contact:
            name, email = self.st.get("cinfo", {}).get(contact, [None, f"ops@{acct.get('slug', 'client')}.example"])
            fields["SuppliedEmail"], fields["SuppliedName"] = email, name
        if self.shared["drift"].get("case_claim") and p["reason"] == "Damaged Goods":
            fields["Carrier_Claim_Number__c"] = f"CLM-{int(r.integers(100000, 999999))}"
        if self.dq("null_required", r):
            self.log("null_required", "Case", number, "AccountId", acct["id"], None, ts)
            fields["AccountId"] = None
        elif contact and self.dq("orphan_fk", r):
            fake = sf_id("003", int(r.integers(10**8, 10**9)))
            self.log("orphan_fk", "Case", number, "ContactId", contact, fake, ts)
            fields["ContactId"] = fake
        creator = "integration.user" if origin in ("Email", "Web", "Client Portal") else csm
        cid = self.store.insert("Case", fields, ts, self.uid(creator), self.uid(csm))
        self.st["cases"][cid] = {"code": code, "owner": csm, "reason": p["reason"]}
        t = ts + timedelta(hours=float(r.uniform(0.3, 6)))
        self.schedule(t, "case_status", {"id": cid, "status": "Working"})
        if r.random() < (0.2 if p.get("priority") == "High" else 0.06):
            t += timedelta(hours=float(r.uniform(3, 24)))
            self.schedule(t, "case_status", {"id": cid, "status": "Escalated"})
        if r.random() < 0.18:
            t += timedelta(days=float(r.uniform(0.5, 2)))
            self.schedule(t, "case_status", {"id": cid, "status": "Waiting on Client"})
            t += timedelta(days=float(r.uniform(0.5, 3)))
            self.schedule(t, "case_status", {"id": cid, "status": "Working"})
        close: dict[str, Any] = {"id": cid, "status": "Closed", "root": p.get("root") or str(r.choice(
            ["Carrier", "Warehouse Error", "Client Data", "Inventory", "System", "Unknown"], p=[0.3, 0.25, 0.15, 0.1, 0.05, 0.15]))}
        if p["reason"] == "Damaged Goods" and r.random() < 0.4:
            close["credit"] = round(float(r.uniform(25, 500)), 2)
        elif p["reason"] in ("Late Shipment", "Mis-ship", "Short Ship") and r.random() < 0.12:
            close["credit"] = round(float(r.uniform(10, 150)), 2)
        self.schedule(t + timedelta(days=float(r.uniform(0.5, 9))), "case_status", close)
        if r.random() < 0.4:
            self.add_task(ts + timedelta(hours=float(r.uniform(1, 8))), code, csm, "Email", f"Re: {p['subject'][:80]}", None, what=cid)

    def ev_case_create(self, ts: datetime, p: dict[str, Any]) -> None:
        self.new_case(ts, p)

    def ev_case_status(self, ts: datetime, p: dict[str, Any]) -> None:
        case = self.st["cases"].get(p["id"])
        if case is None:
            return
        status = p["status"]
        fields: dict[str, Any] = {"Status": status}
        if status == "Escalated":
            fields["IsEscalated"] = True
            fields["Priority"] = "High"
        if status == "Closed":
            fields.update(IsClosed=True, ClosedDate=sf_dt(ts), Root_Cause__c=p.get("root"))
            if p.get("credit"):
                fields["Credit_Amount__c"] = p["credit"]
            del self.st["cases"][p["id"]]
        self.store.update(p["id"], fields, ts, self.uid(case["owner"]))

    def case_multiplier(self, code: str, d: date) -> float:
        cs = churn_signal(self.ctx, code)
        if cs and d >= month_start(self.hend, int(cs["from_month"])):
            return float(cs["case_multiplier"])
        return 1.0

    def ingest_bus(self, win: Window, r: np.random.Generator) -> None:
        bus = self.ctx.bus
        cases: list[tuple[datetime, dict[str, Any]]] = []
        out = bus.get("delivery_outcomes")
        if len(out):
            out = out[out["owner_code"].isin(list(self.st["acct"]))].drop_duplicates("tracking_nbr")
            exc = out["exception_code"].fillna("").astype(str) if "exception_code" in out else pd.Series("", index=out.index)
            late = out["is_late"].fillna(False).astype(bool)
            p = np.where(exc == "DAMAGED", 0.12, np.where(exc.isin(["ADDRESS", "REFUSED"]), 0.03, np.where(late, 0.015, 0.0)))
            mult = np.array([self.case_multiplier(c, win.start.date()) for c in out["owner_code"]]) if len(out) else np.array([])
            pick = out[r.random(len(out)) < p * mult] if len(out) else out
            if len(pick):
                info = self.order_info(list(pick["order_nbr"].astype(str)))
                for row in pick.itertuples(index=False):
                    oi = info.get(str(row.order_nbr), {})
                    exc_code = (row.exception_code or "") if isinstance(row.exception_code, str) else ""
                    reason = "Damaged Goods" if exc_code == "DAMAGED" else ("Returns" if exc_code == "REFUSED" else "Late Shipment")
                    base = row.delivered_ts if isinstance(row.delivered_ts, (pd.Timestamp, datetime)) and not pd.isna(row.delivered_ts) else None
                    base = as_utc(base) if base is not None else at_local(to_date(row.promised_date) + timedelta(days=1), 15, "America/New_York")
                    cases.append((base + timedelta(hours=float(r.uniform(2, 72))), self.case_text(r, reason, row.owner_code, row, oi)))
        ships = bus.get("shipments_shipped")
        lines = bus.get("shipment_lines")
        if len(ships):
            ships = ships[ships["owner_code"].isin(list(self.st["acct"]))].drop_duplicates("shipment_id")
            if len(lines):
                short = lines[lines["shipped_qty"] < lines["ordered_qty"]]
                short_ids = set(short["shipment_id"].tolist())
                cand = ships[ships["shipment_id"].isin(short_ids)]
                cand = cand[r.random(len(cand)) < 0.05 * np.array([self.case_multiplier(c, win.start.date()) for c in cand["owner_code"]])] if len(cand) else cand
                for row in cand.itertuples(index=False):
                    sl = short[short["shipment_id"] == row.shipment_id].iloc[0]
                    extra = {"sku": sl["sku"], "ordered": int(sl["ordered_qty"]), "shipped": int(sl["shipped_qty"])}
                    cases.append((as_utc(row.ship_ts) + timedelta(days=float(r.uniform(2, 6))),
                                  self.case_text(r, "Short Ship", row.owner_code, row, {"client_order_ref": row.client_order_ref,
                                                                                        "wh": row.warehouse_code, **extra})))
            mis = ships[r.random(len(ships)) < 0.0004]
            for row in mis.itertuples(index=False):
                cases.append((as_utc(row.ship_ts) + timedelta(days=float(r.uniform(3, 7))),
                              self.case_text(r, "Mis-ship", row.owner_code, row, {"client_order_ref": row.client_order_ref,
                                                                                    "wh": row.warehouse_code})))
        asns = bus.get("asns_created")
        exp = self.st["asn_expected"]
        if len(asns):
            for row in asns.itertuples(index=False):
                exp[str(row.asn_nbr)] = (str(to_date(row.expected_date)), row.supplier_name)
        rec = bus.get("receipts_completed")
        if len(rec):
            for row in rec.itertuples(index=False):
                e = exp.pop(str(row.asn_nbr), None) if isinstance(row.asn_nbr, str) else None
                if e is None or row.owner_code not in self.st["acct"]:
                    continue
                if to_date(row.completed_ts) > date.fromisoformat(e[0]) + timedelta(days=2) and r.random() < 0.15:
                    cases.append((as_utc(row.completed_ts) - timedelta(hours=float(r.uniform(4, 30))), self.case_text(
                        r, "Receiving Delay", row.owner_code, row, {"asn": row.asn_nbr, "expected": e[0], "supplier": e[1],
                                                                    "wh": row.warehouse_code})))
        if len(exp) > 20000:
            for k in list(exp)[: len(exp) - 20000]:
                exp.pop(k)
        ret = bus.get("returns_received")
        if len(ret):
            ret = ret[ret["owner_code"].isin(list(self.st["acct"])) & ret["reason_code"].isin(["DAMAGED", "DEFECTIVE"])]
            for row in ret[r.random(len(ret)) < 0.01].itertuples(index=False):
                cases.append((as_utc(row.received_ts) + timedelta(days=float(r.uniform(1, 4))), self.case_text(
                    r, "Returns", row.owner_code, row, {"rma": row.rma_nbr, "orig": row.original_order_nbr, "wh": row.warehouse_code})))
        holds = bus.get("inventory_holds")
        if len(holds):
            holds = holds[holds["owner_code"].isin(list(self.st["acct"]))]
            if "released_ts" in holds:
                holds = holds[holds["released_ts"].isna()]
            holds = holds.drop_duplicates(["owner_code", "warehouse_code"])
            for row in holds[r.random(len(holds)) < 0.08].itertuples(index=False):
                cases.append((as_utc(row.hold_ts) + timedelta(hours=float(r.uniform(6, 48))), self.case_text(
                    r, "Inventory Discrepancy", row.owner_code, row, {"lot": row.lot_nbr, "sku": getattr(row, "sku", None),
                                                                       "wh": row.warehouse_code, "reason_txt": row.hold_reason})))
        for code, acct in sorted(self.st["acct"].items()):
            c = self.clients.get(code)
            if c is None or acct.get("former") or win.start.date() < dates_of(self.ctx, c).go_live:
                continue
            for _ in range(int(r.poisson(win.hours / 24 / 30 * self.case_multiplier(code, win.start.date())))):
                ts = win.start + timedelta(seconds=float(r.uniform(0, win.hours * 3600)))
                cases.append((ts, self.case_text(r, "Other", code, None, {"wh": c["warehouses"][0]})))
        for ts, payload in cases:
            ts = max(as_utc(ts), win.start)
            self.schedule(ts, "case_create", payload)

    def order_info(self, order_nbrs: list[str]) -> dict[str, dict[str, Any]]:
        if not order_nbrs:
            return {}
        rows = self.ctx.conn("wms").execute(
            "SELECT o.order_nbr, o.client_order_ref, w.warehouse_code, o.ship_to_name, o.ship_to_company, o.ship_to_city, "
            "o.ship_to_state FROM wms.order_header o JOIN wms.warehouse w ON w.warehouse_id = o.warehouse_id "
            "WHERE o.order_nbr = ANY(%s)", (order_nbrs,)).fetchall()
        return {r[0]: {"client_order_ref": r[1], "wh": r[2], "name": r[3], "company": r[4], "city": r[5], "state": r[6]} for r in rows}

    def case_text(self, r: np.random.Generator, reason: str, code: str, row: Any, info: dict[str, Any]) -> dict[str, Any]:
        acct = self.st["acct"][code]
        po = info.get("client_order_ref") or getattr(row, "client_order_ref", None)
        order_nbr = getattr(row, "order_nbr", None)
        trk = getattr(row, "tracking_nbr", None)
        scac = getattr(row, "scac", None)
        wh = info.get("wh")
        sign = faker_for(acct["region"], derive_seed(self.seed, "sig", code) % (2**31)).first_name()
        who = info.get("company") or info.get("name") or "our customer"
        where = f"{info['city']}, {info['state']}" if info.get("city") else None
        p: dict[str, Any] = {"code": code, "reason": reason, "wh": wh, "salt": f"{code}{po}{trk}{order_nbr}{r.integers(1 << 30)}"}
        ref = str(r.choice(["po", "order", "trk"], p=[0.45, 0.25, 0.3])) if (po or order_nbr or trk) else "none"
        refs = {"po": f"PO {po}" if po else f"order {order_nbr}", "order": f"order {order_nbr}" if order_nbr else f"PO {po}",
                "trk": f"tracking {trk}" if trk else f"PO {po}", "none": ""}
        ref_txt = refs[ref]
        if reason == "Late Shipment":
            subj = str(r.choice([f"Late delivery - {ref_txt}", f"WISMO {ref_txt}", f"{ref_txt.capitalize()} not delivered yet",
                                 f"Customer complaint: late {ref_txt}"]))
            desc = (f"Hi team,\n\n{who.capitalize() if who == 'our customer' else who}{' in ' + where if where else ''} says "
                    f"{ref_txt} was due {to_date(row.promised_date).isoformat()} and still has not arrived"
                    f"{' (carrier ' + scac + ')' if scac else ''}. Can you check with the carrier?\n\nThanks,\n{sign}")
            p.update(priority=str(r.choice(["Medium", "High"], p=[0.75, 0.25])))
        elif reason == "Damaged Goods":
            subj = str(r.choice([f"Damaged shipment - {ref_txt}", f"Items arrived damaged ({ref_txt})", f"Damage claim {ref_txt}"]))
            desc = (f"Our customer received {ref_txt} with crushed cartons and {int(r.integers(1, 12))} damaged units. "
                    f"Photos attached. Please file a claim with {scac or 'the carrier'}.\n{sign}")
            p.update(priority="High", root="Carrier" if r.random() < 0.7 else "Warehouse Error")
        elif reason == "Returns":
            if "rma" in info:
                subj = f"Return {info['rma']} received damaged"
                desc = f"RMA {info['rma']} for original order {info.get('orig') or 'n/a'} came back damaged - please advise disposition."
            else:
                subj = f"Shipment refused - {ref_txt}"
                desc = f"{ref_txt.capitalize()} was refused at delivery and is returning to {wh}. Please hold and confirm.\n{sign}"
        elif reason == "Short Ship":
            subj = str(r.choice([f"Short shipment on PO {po}", f"Missing units - PO {po}", f"PO {po} arrived short"]))
            desc = (f"PO {po}: SKU {info['sku']} ordered {info['ordered']}, received {info['shipped']}. "
                    f"Please confirm whether the balance is backordered.\n{sign}")
            p.update(order_nbr=order_nbr if r.random() < 0.3 else None, root="Inventory")
        elif reason == "Mis-ship":
            subj = str(r.choice([f"Wrong item received - order {order_nbr}", f"Mis-ship on PO {po}"]))
            desc = f"End customer received the wrong SKU on {('PO ' + po) if po else order_nbr}. Please reship and arrange a return label.\n{sign}"
            p.update(priority="High", root="Warehouse Error")
        elif reason == "Receiving Delay":
            subj = f"ASN {info['asn']} not received yet"
            desc = (f"Our inbound {info['asn']} from {info.get('supplier') or 'our supplier'} was expected {info['expected']} "
                    f"but is not showing as available at {wh}. Please expedite.\n{sign}")
            p.update(root="Warehouse Error")
        elif reason == "Inventory Discrepancy":
            subj = f"Lot {info.get('lot') or ''} on hold at {wh}?".replace("Lot  ", "Inventory ")
            desc = f"We see inventory on hold ({info.get('reason_txt') or 'QA hold'}). What happened and when will it be released?\n{sign}"
            p.update(type="Question", root="Inventory")
        else:
            subj, typ, desc = [
                ("Request: add users to client portal", "Request",
                 f"Please create client portal logins for two new planners on our team. Same permissions as {sign}."),
                ("Question about storage charges", "Question",
                 "Our storage line went up this month although inbound was flat. Can you share the pallet count by week?"),
                ("Kitting setup for new SKU", "Request",
                 "We are launching a 3-pack bundle next month. Please quote kitting and confirm the component SKUs are in stock."),
                ("Need weekly inventory snapshot report", "Request",
                 "Can we get an on-hand snapshot by SKU and lot every Monday morning, as CSV to our ops mailbox?"),
                ("Holiday cut-off times?", "Question",
                 f"What are the order cut-off times at {info.get('wh') or 'your site'} for the holiday week?"),
                ("Update ship-from address on labels", "Request",
                 "Our return address changed. Please update the ship-from block on packing slips and labels."),
            ][int(r.integers(0, 6))]
            desc = f"Hi team,\n\n{desc}\n\nThanks,\n{sign}"
            p.update(type=typ, priority="Low")
        p.update(subject=subj, desc=desc)
        if order_nbr and reason in ("Late Shipment", "Damaged Goods", "Mis-ship") and r.random() < 0.35:
            p["order_nbr"] = order_nbr
        if trk and reason in ("Late Shipment", "Damaged Goods") and r.random() < 0.5:
            p["tracking"] = trk
        return p

    def billing_cases(self, win: Window, r: np.random.Generator) -> None:
        for ts in win.crosses_local_time("America/New_York", 10, 0):
            if ts.day not in (6, 7, 8) or ts.weekday() >= 5 or self.st.get("billing_scan") == f"{ts.year}-{ts.month}":
                continue
            self.st["billing_scan"] = f"{ts.year}-{ts.month}"
            rows = self.ctx.conn("ax2012").execute(
                "SELECT j.invoiceid, j.invoiceaccount, j.invoiceamount, j.currencycode FROM dbo.custinvoicejour j "
                "WHERE j.invoicedate >= %s AND j.invoicedate < %s AND j.invoiceamount > 0",
                (add_months(ts.date().replace(day=1), -1), ts.date().replace(day=1))).fetchall()
            by_acct = {ax_account_num(self.ctx.rules, code): code for code in self.st["acct"]}
            for inv, acctnum, amount, ccy in rows:
                code = by_acct.get(str(acctnum).strip())
                if code is None:
                    continue
                share = dispute_share(self.seed, inv, payment_behaviour(self.ctx, code))
                if share <= 0 and r.random() >= 0.05:
                    continue
                when = ts + timedelta(days=float(r.uniform(0, 10)))
                charge = str(r.choice(["storage", "pick", "receiving", "freight recovery", "minimum fee"]))
                subj = f"Dispute - invoice {inv}" if share > 0 else f"Question on invoice {inv}"
                desc = (f"We are short-paying invoice {inv} ({float(amount):,.2f} {str(ccy).strip()}) by {share:.0%} - the {charge} "
                        f"charges do not match our records." if share > 0 else
                        f"Can you send backup for the {charge} charges on invoice {inv}? Our AP team needs detail by SKU.")
                self.schedule(when, "case_create", {"code": code, "reason": "Billing Question", "subject": subj, "desc": desc,
                                                    "type": "Question", "priority": "High" if share > 0 else "Medium",
                                                    "root": "Client Data" if share <= 0 else "System",
                                                    "salt": f"bill{inv}"})

    # ------------------------------------------------------------------ run
    def finish(self, ts: datetime) -> None:
        for aid, d in self.lad.items():
            if d and d > self.st["lad_acct"].get(aid, ""):
                self.st["lad_acct"][aid] = d
                self.store.update(aid, {"LastActivityDate": d}, ts, self.uid("integration.user"), system_only=True)
        self.lad.clear()
        self.store.flush()

    def step(self, win: Window) -> None:
        r = self.ctx.rng("crm", win, "step")
        self.floor = win.start
        for c in self.clients.values():
            if c["code"] not in self.st["known"]:
                self.schedule_client(c)
        self.ingest_bus(win, r)
        self.background(win, r)
        self.billing_cases(win, r)
        self.run_queue(win.end)
        for ts in win.crosses_local_time("UTC", 3, 0):
            self.ctx.conn("sim_internal").execute(
                "DELETE FROM sfdc.record WHERE is_deleted AND deleted_date < %s", (ts - timedelta(days=15),))
        self.finish(win.end - timedelta(seconds=1))


class CrmModule:
    name = "crm"

    def bootstrap(self, ctx: Any) -> None:
        crm = Crm(ctx)
        crm.bootstrap()
        crm.finish(as_utc(ctx.history_start))

    def step(self, ctx: Any, win: Window) -> None:
        Crm(ctx).step(win)

    def apply_scenario(self, ctx: Any, scenario_id: str, params: dict[str, Any], win: Window) -> str | None:
        crm = Crm(ctx)
        crm.floor = win.start
        ts = win.start
        msg: str | None = None
        code = scenario_client(params)
        if scenario_id == "onboard_client":
            new = [c for c in crm.clients.values() if c["code"] not in crm.st["known"]]
            for c in new:
                crm.schedule_client(c)
            crm.run_queue(ts + timedelta(seconds=1))
            msg = f"account, closed-won opportunity, contract and rate card for {', '.join(c['code'] for c in new) or 'no new client'}"
        elif scenario_id == "offboard_client" and code in crm.st["acct"]:
            eff = to_date(params.get("end_date") or params.get("effective_date") or ts.date())
            crm.shared["churn"][code] = eff.isoformat()
            crm.schedule(max(ts, at_local(eff, 17.0, "America/New_York")), "churn", {"code": code})
            crm.add_task(ts, code, crm.st["acct"][code]["owner"], "Call", "Call: client notice of termination",
                         f"Client gave notice; offboarding effective {eff.isoformat()}.")
            crm.run_queue(ts + timedelta(seconds=1))
            msg = f"{code} becomes Former Customer on {eff.isoformat()}"
        elif scenario_id == "rate_increase":
            code = code or next((c for c, a in sorted(crm.st["acct"].items()) if not a.get("former")), None)
            if code in crm.st["acct"] and code in crm.clients:
                pct = float(params.get("pct") or 4.0)
                eff = to_date(params.get("effective_date") or ts.date())
                crm.shared["extra_versions"].setdefault(code, []).append({"effective": eff.isoformat(), "pct": pct})
                version = rate_versions(ctx, crm.clients[code])[-1].version
                crm.ev_ratecard_create(ts, {"code": code, "version": version})
                crm.schedule(max(ts + timedelta(minutes=5), at_local(eff, 6.0, "America/New_York")), "ratecard_activate",
                             {"code": code, "version": version})
                k = crm.st["contract"].get(code)
                if k:
                    crm.store.update(k["id"], {"Annual_Increase_Pct__c": pct, "Description": f"Amended {ts.date().isoformat()}: "
                                               f"{pct:.1f}% rate increase effective {eff.isoformat()}."}, ts, crm.uid("hannah.price"))
                crm.run_queue(ts + timedelta(minutes=6))
                msg = f"rate card amendment +{pct:.1f}% for {code} effective {eff.isoformat()}"
        elif scenario_id == "sf_account_merge":
            done = []
            for i in range(max(1, int(params.get("count") or 1))):
                t = ts + timedelta(minutes=3 * i)
                dup = next((d for d in crm.st["dups"] if not d["merged"] and (code is None or d["code"] == code)), None)
                if dup is None:
                    pick = code if code in crm.st["acct"] else next(
                        (c for c in sorted(crm.st["acct"]) if not any(d["code"] == c for d in crm.st["dups"])), None)
                    if pick is None:
                        break
                    crm.ev_duplicate(t, {"code": pick})
                    dup = crm.st["dups"][-1]
                crm.ev_merge(t + timedelta(minutes=2), {"id": dup["id"]})
                done.append(f"{dup['id']} -> {dup['master']} ({dup['code']})")
            msg = f"merged duplicate account(s) {'; '.join(done)}" if done else None
        elif scenario_id == "schema_drift_release" and params.get("systems", "all") in ("all", "salesforce"):
            crm.shared["drift"]["case_claim"] = ts.isoformat()
            ctx.conn("sim_internal").execute(
                "INSERT INTO sfdc.custom_field (object, api_name, definition, added_at) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                ("Case", "Carrier_Claim_Number__c", json.dumps({"name": "Carrier_Claim_Number__c", "type": "string", "length": 30,
                                                                "custom": True, "label": "Carrier Claim Number"}), ts))
            ctx.dq.log("schema_drift", "salesforce", "Case", None, "Carrier_Claim_Number__c", None, "string(30)",
                       "scenario schema_drift_release: new custom field", ts)
            msg = "new custom field Case.Carrier_Claim_Number__c"
        crm.finish(ts)
        return msg


def scenario_client(params: dict[str, Any]) -> str | None:
    code = params.get("client")
    if isinstance(code, dict):
        code = code.get("code")
    return None if code in (None, "", "AUTO", "ALL") else str(code)


MODULE = CrmModule()
