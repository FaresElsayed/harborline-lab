"""Geography helpers: curated cities, addresses, people names, distances, time zones and weather.

Everything here is pure (no database). Arrays are vectorised so a day of orders costs microseconds
per row. The curated data lives in ``harborline/sim/data`` (cities.csv, people.json, retailers.json).
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from functools import cache, lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ..core import derive_seed

DATA_DIR = Path(__file__).resolve().parent / "data"
EARTH_MI = 3958.8
_TZ_Y0, _TZ_Y1 = 2010, 2040


# --------------------------------------------------------------------------------------------- data
@lru_cache(maxsize=1)
def cities() -> pd.DataFrame:
    df = pd.read_csv(DATA_DIR / "cities.csv", dtype={"postal": str, "phone": str})
    df["postal_list"] = df["postal"].str.split("|")
    df["phone_list"] = df["phone"].str.split("|")
    df["locale"] = np.where(df["country"] == "MX", "es", np.where(df["state"] == "QC", "fr", "en"))
    return df


@lru_cache(maxsize=1)
def people() -> dict[str, Any]:
    return json.loads((DATA_DIR / "people.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def retailers() -> dict[str, Any]:
    return json.loads((DATA_DIR / "retailers.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def products() -> dict[str, Any]:
    return json.loads((DATA_DIR / "products.json").read_text(encoding="utf-8"))


@cache
def _country_weights(country: str) -> tuple[np.ndarray, np.ndarray]:
    c = cities()
    idx = np.flatnonzero(c["country"].to_numpy() == country)
    w = c["pop_k"].to_numpy(float)[idx] ** 0.85   # flatten big-city dominance a little
    return idx, w / w.sum()


def city_index(city: str, state: str, country: str) -> int:
    c = cities()
    hit = np.flatnonzero((c["city"] == city).to_numpy() & (c["state"] == state).to_numpy() & (c["country"] == country).to_numpy())
    return int(hit[0]) if len(hit) else -1


def sample_cities(rng: np.random.Generator, countries: np.ndarray) -> np.ndarray:
    """Population-weighted city index for every element of ``countries`` (US/CA/MX)."""
    out = np.empty(len(countries), dtype=np.int32)
    for ctry in ("US", "CA", "MX"):
        m = countries == ctry
        if m.any():
            idx, w = _country_weights(ctry)
            out[m] = idx[rng.choice(len(idx), size=int(m.sum()), p=w)]
    return out


# --------------------------------------------------------------------------------------------- distance
def haversine_mi(lat1: np.ndarray, lon1: np.ndarray, lat2: np.ndarray, lon2: np.ndarray) -> np.ndarray:
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp, dl = p2 - p1, np.radians(np.asarray(lon2) - np.asarray(lon1))
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * EARTH_MI * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def parcel_zone(dist_mi: np.ndarray) -> np.ndarray:
    """US-style parcel zone (2-8) from origin-destination distance."""
    edges = np.array([150, 300, 600, 1000, 1400, 1800])
    return (np.searchsorted(edges, dist_mi, side="right") + 2).astype(np.int16)


# --------------------------------------------------------------------------------------------- addresses
def _pick(rng: np.random.Generator, items: list[str], n: int) -> np.ndarray:
    return np.asarray(items, dtype=object)[rng.integers(0, len(items), n)]


def names(rng: np.random.Generator, locales: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    p = people()
    n = len(locales)
    first = np.empty(n, dtype=object)
    last = np.empty(n, dtype=object)
    for loc in ("en", "fr", "es"):
        m = locales == loc
        k = int(m.sum())
        if k:
            first[m] = _pick(rng, p["first_names"][loc], k)
            last[m] = _pick(rng, p["last_names"][loc], k)
    # Canadians outside Quebec and Americans have plenty of Spanish / French surnames too.
    mix = (locales == "en") & (rng.random(n) < 0.12)
    if mix.any():
        last[mix] = _pick(rng, p["last_names"]["es"], int(mix.sum()))
    return first, last


def postal_codes(rng: np.random.Generator, city_idx: np.ndarray) -> np.ndarray:
    """A postal code that belongs to the city: real US ZIPs, CA FSA + random LDU, MX CP prefix + 2 digits."""
    c = cities()
    plist = c["postal_list"].to_numpy()
    ctry = c["country"].to_numpy()[city_idx]
    out = np.empty(len(city_idx), dtype=object)
    choice = rng.random(len(city_idx))
    for i, ci in enumerate(city_idx):           # small per-order cost (string assembly)
        opts = plist[ci]
        base = opts[int(choice[i] * len(opts))]
        out[i] = base
    ca = ctry == "CA"
    if ca.any():
        k = int(ca.sum())
        d1 = rng.integers(0, 10, k)
        letters = np.array(list("ABCEGHJKLMNPRSTVWXYZ"))
        l1 = letters[rng.integers(0, len(letters), k)]
        d2 = rng.integers(0, 10, k)
        out[ca] = [f"{b} {a}{x}{y}" for b, a, x, y in zip(out[ca], d1, l1, d2)]
    mx = ctry == "MX"
    if mx.any():
        tail = rng.integers(0, 100, int(mx.sum()))
        out[mx] = [f"{b}{t:02d}" for b, t in zip(out[mx], tail)]
    return out


def street_lines(rng: np.random.Generator, city_idx: np.ndarray, residential: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    c = cities()
    p = people()["streets"]
    loc = c["locale"].to_numpy()[city_idx]
    n = len(city_idx)
    a1 = np.empty(n, dtype=object)
    a2 = np.full(n, None, dtype=object)
    num = rng.integers(10, 9900, n)
    en = loc == "en"
    if en.any():
        k = int(en.sum())
        s = _pick(rng, p["en"], k)
        suf = _pick(rng, p["en_suffix"], k)
        dirs = np.where(rng.random(k) < 0.15, _pick(rng, ["N ", "S ", "E ", "W "], k), "")
        a1[en] = [f"{n_} {d}{s_} {x}" for n_, d, s_, x in zip(num[en], dirs, s, suf)]
    fr = loc == "fr"
    if fr.any():
        a1[fr] = [f"{n_} {s_}" for n_, s_ in zip(num[fr], _pick(rng, p["fr"], int(fr.sum())))]
    es = loc == "es"
    if es.any():
        k = int(es.sum())
        a1[es] = [f"{s_} {n_}" for s_, n_ in zip(_pick(rng, p["es"], k), num[es])]
        a2[es] = ["Col. " + x for x in _pick(rng, p["es_colonia"], k)]
    unit = residential & (loc != "es") & (rng.random(n) < 0.22)
    if unit.any():
        k = int(unit.sum())
        pre = _pick(rng, people()["unit_prefixes"], k)
        a2[unit] = [f"{x} {y}" if x != "#" else f"#{y}" for x, y in zip(pre, rng.integers(1, 2400, k))]
    return a1, a2


def phones(rng: np.random.Generator, city_idx: np.ndarray) -> np.ndarray:
    c = cities()
    pl = c["phone_list"].to_numpy()
    ctry = c["country"].to_numpy()[city_idx]
    n = len(city_idx)
    pick = rng.random(n)
    mid = rng.integers(200, 1000, n)
    end = rng.integers(0, 10000, n)
    mx8 = rng.integers(10_000_000, 99_999_999, n)
    out = np.empty(n, dtype=object)
    for i in range(n):
        opts = pl[city_idx[i]]
        ac = opts[int(pick[i] * len(opts))]
        if ctry[i] == "MX":
            body = f"{mx8[i]:08d}" if len(ac) == 2 else f"{mx8[i] % 10_000_000:07d}"
            out[i] = f"+52 {ac} {body[:4]} {body[4:]}" if len(ac) == 2 else f"+52 {ac} {body[:3]} {body[3:]}"
        else:
            out[i] = f"({ac}) {mid[i]:03d}-{end[i]:04d}"
    return out


def emails(rng: np.random.Generator, first: np.ndarray, last: np.ndarray) -> np.ndarray:
    doms = people()["email_domains"]
    d = _pick(rng, doms, len(first))
    k = rng.integers(1, 99, len(first))
    style = rng.random(len(first))
    out = np.empty(len(first), dtype=object)
    for i in range(len(first)):
        f = _ascii(first[i]).lower().replace(" ", "")
        lname = _ascii(last[i]).lower().replace(" ", "").replace("'", "")
        if style[i] < 0.4:
            out[i] = f"{f}.{lname}@{d[i]}"
        elif style[i] < 0.75:
            out[i] = f"{f[0]}{lname}{k[i]}@{d[i]}"
        else:
            out[i] = f"{f}{k[i]}@{d[i]}"
    return out


_ASCII = str.maketrans("áéíóúñüÁÉÍÓÚÑÜàèìòùâêîôûçÀÈÌÒÙÂÊÎÔÛÇëïË", "aeiounuAEIOUNUaeiouaeioucAEIOUAEIOUCeiE")


def _ascii(s: str) -> str:
    return s.translate(_ASCII)


def mojibake(s: str) -> str:
    """UTF-8 text decoded as cp1252 - the classic 'MÃ©xico' defect."""
    try:
        return s.encode("utf-8").decode("cp1252", errors="replace")
    except UnicodeError:
        return s


# --------------------------------------------------------------------------------------------- time zones
@cache
def _tz_table(tz: str) -> tuple[int, np.ndarray]:
    idx = pd.date_range(f"{_TZ_Y0}-01-01", f"{_TZ_Y1}-12-31 23:00", freq="h", tz="UTC", unit="s")
    local = idx.tz_convert(tz).tz_localize(None)
    off_min = ((local.as_unit("s").asi8 - idx.tz_localize(None).as_unit("s").asi8) // 60).astype(np.int16)
    base = int(idx[0].timestamp())
    return base, off_min


def utc_offset_s(tz: str, utc_s: np.ndarray) -> np.ndarray:
    base, off = _tz_table(tz)
    h = np.clip((np.asarray(utc_s, dtype=np.int64) - base) // 3600, 0, len(off) - 1)
    return off[h].astype(np.int64) * 60


def utc_to_local(tz: str, utc_s: np.ndarray) -> np.ndarray:
    """Epoch seconds (UTC) -> naive local wall-clock seconds."""
    u = np.asarray(utc_s, dtype=np.int64)
    return u + utc_offset_s(tz, u)


def local_to_utc(tz: str, local_s: np.ndarray) -> np.ndarray:
    loc = np.asarray(local_s, dtype=np.int64)
    guess = loc - utc_offset_s(tz, loc)
    return loc - utc_offset_s(tz, guess)


def utc_to_local_many(tz: np.ndarray, utc_s: np.ndarray) -> np.ndarray:
    """Like ``utc_to_local`` with one zone per element."""
    u = np.asarray(utc_s, dtype=np.int64)
    out = np.empty(len(u), dtype=np.int64)
    tz = np.asarray(tz, dtype=object)
    for z in pd.unique(tz):
        m = tz == z
        out[m] = utc_to_local(z, u[m])
    return out


def local_to_utc_many(tz: np.ndarray, local_s: np.ndarray) -> np.ndarray:
    loc = np.asarray(local_s, dtype=np.int64)
    out = np.empty(len(loc), dtype=np.int64)
    tz = np.asarray(tz, dtype=object)
    for z in pd.unique(tz):
        m = tz == z
        out[m] = local_to_utc(z, loc[m])
    return out


def iso_with_offset(local_s: np.ndarray, off_s: np.ndarray) -> np.ndarray:
    """ISO-8601 strings with the local offset, e.g. 2026-10-30T14:03:00-05:00."""
    body = np.datetime_as_string(np.asarray(local_s, dtype=np.int64).astype("datetime64[s]"), unit="s")
    off = np.asarray(off_s, dtype=np.int64)
    uniq, inv = np.unique(off, return_inverse=True)
    sfx = np.array([f"{'-' if o < 0 else '+'}{abs(int(o)) // 3600:02d}:{(abs(int(o)) % 3600) // 60:02d}" for o in uniq])
    return np.char.add(body, sfx[inv]).astype(object)


def epoch_day(d: date) -> int:
    return (d - date(1970, 1, 1)).days


def day_date(day: int) -> date:
    return date(1970, 1, 1) + timedelta(days=int(day))


# --------------------------------------------------------------------------------------------- weather
def synthetic_weather(seed: int, key: str, lat: float, d: date) -> dict[str, float]:
    """Deterministic plausible daily weather when the real archive is not available."""
    rng = np.random.default_rng(derive_seed(seed, "wx", key, d.isoformat()))
    doy = d.timetuple().tm_yday
    mean = 34.0 - 0.52 * lat
    amp = max(4.0, 0.42 * lat - 2.5)
    season = -np.cos(2 * np.pi * (doy - 15) / 365.25)          # -1 mid-Jan, +1 mid-Jul
    tmax = mean + amp * season + rng.normal(0, 3.2)
    precip = float(rng.gamma(0.6, 6.0)) if rng.random() < 0.32 else 0.0
    snow = 0.0
    if tmax < 3.0 and precip > 0:
        snow = round(precip * rng.uniform(0.8, 1.6), 1)
    return {"tmax_c": round(float(tmax), 1), "tmin_c": round(float(tmax - rng.uniform(7, 12)), 1),
            "precip_mm": round(precip, 1), "snow_cm": snow}


def weather(ctx: Any, key: str, lat: float, lon: float, d: date) -> dict[str, float]:
    """Daily weather for a site: the engine's signal (real Open-Meteo archive) or the synthetic fallback."""
    sig = getattr(ctx, "signals", None)
    fn = getattr(sig, "weather", None)
    if fn is not None:
        cache = _wx_cache(ctx)
        ck = (key, d)
        if ck not in cache:
            try:
                res = fn(key, d)
            except TypeError:
                res = fn(lat, lon, d)
            cache[ck] = _normalise_wx(res) or synthetic_weather(ctx.seed, key, lat, d)
        return cache[ck]
    return synthetic_weather(ctx.seed, key, lat, d)


def _wx_cache(ctx: Any) -> dict:
    c = getattr(ctx, "_hl_wx_cache", None)
    if c is None:
        c = {}
        try:
            ctx._hl_wx_cache = c
        except AttributeError:
            pass
    if len(c) > 20000:
        c.clear()
    return c


def _normalise_wx(res: Any) -> dict[str, float] | None:
    if res is None:
        return None
    if not isinstance(res, dict):
        res = {k: getattr(res, k) for k in dir(res) if not k.startswith("_")}
    pick = {}
    for want, alts in {"tmax_c": ("tmax_c", "temperature_2m_max", "tmax", "temp_max_c"),
                       "tmin_c": ("tmin_c", "temperature_2m_min", "tmin", "temp_min_c"),
                       "precip_mm": ("precip_mm", "precipitation_sum", "precip"),
                       "snow_cm": ("snow_cm", "snowfall_sum", "snowfall_cm", "snow")}.items():
        for a in alts:
            if a in res and res[a] is not None:
                pick[want] = float(res[a])
                break
    if "tmax_c" not in pick:
        return None
    pick.setdefault("tmin_c", pick["tmax_c"] - 9)
    pick.setdefault("precip_mm", 0.0)
    pick.setdefault("snow_cm", 0.0)
    return pick


# --------------------------------------------------------------------------------------------- fast COPY
_COL_TYPES: dict[tuple[str, str, str], dict[str, str]] = {}
_INT_TYPES = {"int2", "int4", "int8"}


def copy_fast(ctx: Any, db: str, table: str, df: pd.DataFrame) -> int:
    """COPY a DataFrame through the run's connection (same transaction as ``ctx.copy_rows``), serialised with
    Arrow's C++ CSV writer, which is ~8x faster than pandas for the high-volume tables. NaN/None/NaT become NULL,
    float columns going into integer columns are rounded."""
    import io

    import pyarrow as pa
    import pyarrow.csv as pacsv
    from psycopg import sql

    if df is None or not len(df):
        return 0
    schema, name = table.split(".", 1)
    conn = ctx.conn(db)
    key = (db, schema, name)
    types = _COL_TYPES.get(key)
    if types is None or not set(df.columns) <= types.keys():
        rows = conn.execute(
            "SELECT a.attname, t.typname FROM pg_attribute a JOIN pg_type t ON t.oid = a.atttypid JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped",
            (schema, name)).fetchall()
        types = _COL_TYPES[key] = dict(rows)
    fixed = {}
    for col in df.columns:
        s = df[col]
        if types.get(col) in _INT_TYPES and pd.api.types.is_float_dtype(s.dtype):
            fixed[col] = s.round().astype("Int64")
        elif types.get(col) in _INT_TYPES and s.dtype == object:
            fixed[col] = pd.to_numeric(s).round().astype("Int64")
    if fixed:
        df = df.assign(**fixed)
    buf = io.BytesIO()
    pacsv.write_csv(pa.Table.from_pandas(df, preserve_index=False), buf, pacsv.WriteOptions(include_header=False))
    stmt = sql.SQL("COPY {}.{} ({}) FROM STDIN WITH (FORMAT csv)").format(
        sql.Identifier(schema), sql.Identifier(name), sql.SQL(", ").join(sql.Identifier(str(c)) for c in df.columns))
    with conn.cursor() as cur, cur.copy(stmt) as cp:
        cp.write(buf.getbuffer())
    return len(df)
