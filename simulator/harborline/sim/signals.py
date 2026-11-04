"""External signals the simulator embeds so the user's own API pulls correlate with the data.

* ``fx(date, ccy)``: units of ``ccy`` per 1 USD (ECB reference rates via Frankfurter; weekends carry Friday's rate).
* ``is_holiday(country, date)``: public holidays from the ``holidays`` package.
* ``weather(warehouse_code, date)``: daily ``snow_cm``, ``temp_max_c``, ``precip_mm`` from the Open-Meteo archive.
* ``diesel(date)``: weekly US on-highway diesel price (USD/gal), synthetic but realistic and smooth.

Public APIs are fetched once per build (``prepare``) and cached in ``sim.signal_cache``. Without internet (or with
HL_OFFLINE=1) a bundled fallback series is used. Dates past the fetched range continue deterministically.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timedelta
from typing import Any

import holidays as holidays_lib
import httpx
import numpy as np
import psycopg

from ..core import derive_seed

log = logging.getLogger("harborline.signals")

FX_CURRENCIES = ("CAD", "EUR", "GBP", "MXN")
FX_URLS = ("https://api.frankfurter.dev/v1", "https://api.frankfurter.app")
WEATHER_URL = "https://archive-api.open-meteo.com/v1/archive"
HTTP_TIMEOUT = httpx.Timeout(20.0, connect=5.0)

# Approximate monthly averages (units per USD), used when the FX API is unreachable. Keys are (year, month).
_FX_ANCHORS: dict[str, dict[tuple[int, int], float]] = {
    "CAD": {(2020, 1): 1.31, (2020, 4): 1.40, (2020, 9): 1.32, (2021, 1): 1.27, (2021, 6): 1.23, (2021, 12): 1.28,
            (2022, 6): 1.28, (2022, 10): 1.37, (2023, 3): 1.37, (2023, 7): 1.32, (2023, 10): 1.37, (2024, 1): 1.34,
            (2024, 6): 1.37, (2024, 9): 1.35, (2024, 12): 1.43, (2025, 2): 1.43, (2025, 5): 1.39, (2025, 8): 1.38,
            (2025, 11): 1.40, (2026, 4): 1.38, (2026, 10): 1.39},
    "EUR": {(2020, 1): 0.90, (2020, 4): 0.92, (2020, 9): 0.85, (2021, 1): 0.82, (2021, 6): 0.83, (2021, 12): 0.88,
            (2022, 6): 0.95, (2022, 10): 1.01, (2023, 3): 0.93, (2023, 7): 0.90, (2023, 10): 0.95, (2024, 1): 0.92,
            (2024, 6): 0.93, (2024, 9): 0.90, (2024, 12): 0.96, (2025, 2): 0.96, (2025, 5): 0.89, (2025, 8): 0.86,
            (2025, 11): 0.86, (2026, 4): 0.87, (2026, 10): 0.89},
    "GBP": {(2020, 1): 0.76, (2020, 4): 0.80, (2020, 9): 0.77, (2021, 1): 0.73, (2021, 6): 0.72, (2021, 12): 0.75,
            (2022, 6): 0.81, (2022, 10): 0.88, (2023, 3): 0.82, (2023, 7): 0.78, (2023, 10): 0.82, (2024, 1): 0.79,
            (2024, 6): 0.79, (2024, 9): 0.76, (2024, 12): 0.79, (2025, 2): 0.80, (2025, 5): 0.75, (2025, 8): 0.74,
            (2025, 11): 0.76, (2026, 4): 0.75, (2026, 10): 0.75},
    "MXN": {(2020, 1): 18.8, (2020, 4): 24.2, (2020, 9): 21.4, (2021, 1): 19.9, (2021, 6): 20.0, (2021, 12): 20.9,
            (2022, 6): 20.0, (2022, 10): 20.0, (2023, 3): 18.4, (2023, 7): 16.9, (2023, 10): 18.0, (2024, 1): 17.1,
            (2024, 6): 18.2, (2024, 9): 19.6, (2024, 12): 20.3, (2025, 2): 20.5, (2025, 5): 19.4, (2025, 8): 18.7,
            (2025, 11): 18.4, (2026, 4): 18.6, (2026, 10): 18.9},
}
_FX_DAILY_VOL = {"CAD": 0.0035, "EUR": 0.0045, "GBP": 0.0050, "MXN": 0.0070}

# Climate normals: mean daily max temperature (C) per month, chance of a wet day, mean wet-day precip (mm).
_CLIMATE: dict[str, tuple[tuple[float, ...], float, float]] = {
    "ATL1": ((12, 15, 19, 23, 27, 31, 32, 32, 29, 24, 18, 13), 0.31, 11.0),
    "DFW1": ((14, 17, 21, 25, 29, 33, 36, 36, 32, 26, 19, 15), 0.22, 11.5),
    "RNO1": ((7, 10, 14, 17, 22, 28, 33, 32, 27, 20, 12, 7), 0.10, 5.0),
    "CHI1": ((0, 2, 9, 16, 22, 27, 29, 28, 24, 17, 9, 2), 0.32, 7.5),
    "NJ1": ((4, 6, 11, 17, 23, 28, 31, 30, 26, 19, 13, 7), 0.32, 9.5),
    "LAX1": ((20, 20, 22, 24, 26, 30, 34, 35, 33, 28, 24, 20), 0.08, 9.0),
    "TOR1": ((-2, -1, 4, 11, 19, 24, 27, 26, 22, 14, 7, 1), 0.33, 6.5),
    "MTY1": ((21, 23, 27, 30, 32, 34, 35, 35, 32, 28, 24, 21), 0.18, 8.0),
}
_COUNTRY_ALIASES = {"USA": "US", "CAN": "CA", "MEX": "MX", "us01": "US", "ca01": "CA", "mx01": "MX"}


def as_date(value: date | datetime | Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if hasattr(value, "to_pydatetime"):          # pandas Timestamp
        return value.to_pydatetime().date()
    if isinstance(value, np.datetime64):
        return date.fromisoformat(str(value)[:10])
    return date.fromisoformat(str(value)[:10])


class _DailySeries:
    """Dense daily array from ``start``; forward-filled; extended past the end with a deterministic random walk."""

    def __init__(self, start: date, values: np.ndarray, key: str, vol: float) -> None:
        self.start = start
        self.values = list(map(float, values))
        self.key = key
        self.vol = vol

    @classmethod
    def from_points(cls, points: dict[date, float], key: str, vol: float) -> _DailySeries:
        days = sorted(points)
        start, end = days[0], days[-1]
        out = np.empty((end - start).days + 1)
        last = points[start]
        for i in range(len(out)):
            d = start + timedelta(days=i)
            last = points.get(d, last)
            out[i] = last
        return cls(start, out, key, vol)

    def get(self, d: date) -> float:
        i = (d - self.start).days
        if i < 0:
            return self.values[0]
        while i >= len(self.values):
            day = self.start + timedelta(days=len(self.values))
            if day.weekday() >= 5:
                self.values.append(self.values[-1])
                continue
            shock = np.random.default_rng(derive_seed("fx-walk", self.key, day.isoformat())).normal(0, self.vol)
            self.values.append(self.values[-1] * math.exp(shock))
        return self.values[i]


def _fallback_fx(ccy: str, start: date, end: date) -> dict[date, float]:
    anchors = sorted((date(y, m, 15), v) for (y, m), v in _FX_ANCHORS[ccy].items())
    xs = np.array([a.toordinal() for a, _ in anchors], dtype=float)
    ys = np.log([v for _, v in anchors])
    n = (end - start).days + 1
    ords = np.arange(start.toordinal(), start.toordinal() + n, dtype=float)
    base = np.interp(ords, xs, ys)
    rng = np.random.default_rng(derive_seed("fx-fallback", ccy))
    # Mean-reverting daily noise around the monthly path so the series looks like a market rate.
    noise = np.zeros(n)
    shocks = rng.normal(0, _FX_DAILY_VOL[ccy], n)
    for i in range(1, n):
        noise[i] = 0.93 * noise[i - 1] + shocks[i]
    out: dict[date, float] = {}
    for i in range(n):
        d = start + timedelta(days=i)
        if d.weekday() < 5:
            out[d] = round(float(math.exp(base[i] + noise[i])), 5)
    return out


class Signals:
    def __init__(self, rules: dict[str, Any], offline: bool = False) -> None:
        self.offline = offline
        self.warehouses = {w["code"]: w for w in rules.get("warehouses", [])}
        self.sources: dict[str, str] = {}
        self._fx: dict[str, _DailySeries] = {}
        self._weather: dict[str, dict[str, tuple[float | None, float | None, float | None]]] = {}
        self._holidays: dict[tuple[str, int], Any] = {}
        self._fallback_window = (date(2019, 1, 1), date.today() + timedelta(days=1))
        for ccy in FX_CURRENCIES:
            self._fx[ccy] = _DailySeries.from_points(_fallback_fx(ccy, *self._fallback_window), ccy, _FX_DAILY_VOL[ccy])
            self.sources[f"fx:{ccy}"] = "fallback"

    # ----------------------------------------------------------------- lifecycle
    def prepare(self, start: date, end: date, conn: psycopg.Connection | None = None) -> dict[str, str]:
        """Fetch FX + weather for [start, end] (once per build) and cache them. Falls back silently."""
        if not self.offline:
            fx = self._fetch_fx(start, end)
            # No FX answer usually means no internet: don't wait on eight more timeouts for the weather.
            weather = {code: self._fetch_weather(w, start, end) for code, w in self.warehouses.items()} if fx else {}
            if conn is not None:
                self._store(conn, fx, weather)
            self._apply(fx, weather)
        if conn is not None:
            self.load(conn)
        return dict(self.sources)

    def load(self, conn: psycopg.Connection) -> None:
        """Load whatever was cached by an earlier ``prepare`` (used at app start)."""
        try:
            rows = conn.execute("SELECT kind, key, payload FROM sim.signal_cache").fetchall()
        except psycopg.Error:
            conn.rollback()
            return
        fx = {key: payload for kind, key, payload in rows if kind == "fx"}
        weather = {key: payload for kind, key, payload in rows if kind == "weather"}
        self._apply(fx, weather)

    def _apply(self, fx: dict[str, dict[str, float]], weather: dict[str, dict[str, list]]) -> None:
        for ccy, points in fx.items():
            if ccy in self._fx and points:
                parsed = {date.fromisoformat(k): float(v) for k, v in points.items()}
                self._fx[ccy] = _DailySeries.from_points(parsed, ccy, _FX_DAILY_VOL[ccy])
                self.sources[f"fx:{ccy}"] = "frankfurter"
        for code, days in weather.items():
            if days:
                self._weather[code] = {k: tuple(v) for k, v in days.items()}
                self.sources[f"weather:{code}"] = "open-meteo"

    @staticmethod
    def _store(conn: psycopg.Connection, fx: dict[str, Any], weather: dict[str, Any]) -> None:
        for kind, source, payloads in (("fx", "frankfurter", fx), ("weather", "open-meteo", weather)):
            for key, payload in payloads.items():
                if payload:
                    conn.execute(
                        "INSERT INTO sim.signal_cache (kind, key, source, payload, fetched_at) VALUES (%s, %s, %s, %s, now()) "
                        "ON CONFLICT (kind, key) DO UPDATE SET source = EXCLUDED.source, payload = EXCLUDED.payload, "
                        "fetched_at = now()",
                        (kind, key, source, json.dumps(payload)),
                    )

    def _fetch_fx(self, start: date, end: date) -> dict[str, dict[str, float]]:
        params = {"base": "USD", "symbols": ",".join(FX_CURRENCIES)}
        for base_url in FX_URLS:
            try:
                resp = httpx.get(f"{base_url}/{start.isoformat()}..{end.isoformat()}", params=params, timeout=HTTP_TIMEOUT)
                resp.raise_for_status()
                rates = resp.json()["rates"]
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                log.warning("FX fetch from %s failed: %s", base_url, exc)
                continue
            out: dict[str, dict[str, float]] = {c: {} for c in FX_CURRENCIES}
            for day, by_ccy in rates.items():
                for ccy, rate in by_ccy.items():
                    if ccy in out:
                        out[ccy][day] = float(rate)
            return out
        return {}

    def _fetch_weather(self, wh: dict[str, Any], start: date, end: date) -> dict[str, list]:
        addr = wh.get("address", {})
        if "lat" not in addr:
            return {}
        last = min(end, date.today() - timedelta(days=2))
        params = {
            "latitude": addr["lat"], "longitude": addr["lon"], "start_date": start.isoformat(), "end_date": last.isoformat(),
            "daily": "temperature_2m_max,precipitation_sum,snowfall_sum", "timezone": wh.get("time_zone", "UTC"),
        }
        try:
            resp = httpx.get(WEATHER_URL, params=params, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            daily = resp.json()["daily"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            log.warning("Weather fetch for %s failed: %s", wh.get("code"), exc)
            return {}
        return {
            day: [tmax, precip, snow]
            for day, tmax, precip, snow in zip(daily["time"], daily["temperature_2m_max"], daily["precipitation_sum"],
                                               daily["snowfall_sum"], strict=False)
            if tmax is not None
        }

    # ----------------------------------------------------------------- signals
    def fx(self, d: date | datetime, ccy: str) -> float:
        """Units of ``ccy`` per 1 USD on ``d`` (1.0 for USD and unknown codes)."""
        series = self._fx.get(str(ccy).upper())
        return 1.0 if series is None else round(series.get(as_date(d)), 6)

    def is_holiday(self, country: str, d: date | datetime, subdiv: str | None = None) -> bool:
        return self.holiday_name(country, d, subdiv) is not None

    def holiday_name(self, country: str, d: date | datetime, subdiv: str | None = None) -> str | None:
        day = as_date(d)
        code = _COUNTRY_ALIASES.get(country, country).upper()
        key = (f"{code}:{subdiv or ''}", day.year)
        cal = self._holidays.get(key)
        if cal is None:
            cal = holidays_lib.country_holidays(code, subdiv=subdiv, years=day.year)
            self._holidays[key] = cal
        return cal.get(day)

    def weather(self, warehouse_code: str, d: date | datetime) -> dict[str, float]:
        day = as_date(d)
        cached = self._weather.get(warehouse_code, {}).get(day.isoformat())
        if cached is not None:
            tmax, precip, snow = cached
            return {"snow_cm": round(float(snow or 0.0), 2), "temp_max_c": round(float(tmax), 1),
                    "precip_mm": round(float(precip or 0.0), 2)}
        return self._synthetic_weather(warehouse_code, day)

    def _synthetic_weather(self, code: str, day: date) -> dict[str, float]:
        normals, wet_p, wet_mm = _CLIMATE.get(code) or self._climate_from_latitude(code)
        # Interpolate the monthly normal to the day so seasons change smoothly.
        pos = day.month - 1 + (day.day - 15) / 30.0
        lo = math.floor(pos)
        frac = pos - lo
        tnorm = normals[lo % 12] * (1 - frac) + normals[(lo + 1) % 12] * frac
        rng = np.random.default_rng(derive_seed("weather", code, day.isoformat()))
        tmax = tnorm + rng.normal(0, 4.0)
        precip = float(rng.exponential(wet_mm)) if rng.random() < wet_p else 0.0
        snow = precip * rng.uniform(0.8, 1.3) if tmax < 2.5 and precip > 0 else 0.0
        return {"snow_cm": round(snow, 2), "temp_max_c": round(float(tmax), 1), "precip_mm": round(precip, 2)}

    def _climate_from_latitude(self, code: str) -> tuple[tuple[float, ...], float, float]:
        lat = float(self.warehouses.get(code, {}).get("address", {}).get("lat", 35.0))
        mean, amp = 33.0 - 0.55 * lat, 0.35 * lat - 1.5
        return tuple(mean + amp * -math.cos(2 * math.pi * (m - 0.5) / 12) for m in range(12)), 0.25, 8.0

    @staticmethod
    def diesel(d: date | datetime) -> float:
        """US on-highway diesel, USD per gallon, published weekly (constant Monday..Sunday). Range ~3.45-4.15."""
        day = as_date(d)
        monday = day - timedelta(days=day.weekday())
        t = (monday - date(2020, 1, 6)).days / 7.0
        value = (3.80 + 0.22 * math.sin(2 * math.pi * t / 62 + 0.7) + 0.09 * math.sin(2 * math.pi * t / 23 + 2.1)
                 + 0.035 * math.sin(2 * math.pi * t / 9.5 + 0.3))
        return round(min(4.2, max(3.4, value)), 3)
