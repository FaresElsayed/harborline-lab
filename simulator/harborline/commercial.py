"""Client lifecycle dates and contracted rates.

Salesforce (Rate_Card__c / Rate_Card_Line__c) is the commercial source of truth. The WMS
billing_rate table is keyed by hand from it (with the planted keying mistakes applied by the
WMS module). Both modules call these functions so they agree on every date and rate.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import numpy as np

from .core import derive_seed

FxFn = Callable[[date, str], float]   # units of `currency` per 1 USD on a date


def month_start(d: date, offset_months: int) -> date:
    y, m = divmod((d.year * 12 + d.month - 1) + offset_months, 12)
    return date(y, m + 1, 1)


@dataclass
class ClientDates:
    code: str
    sf_first_touch: date
    sf_closed_won: date
    contract_start: date        # == go-live
    ax_created: date
    wms_created: date           # owner row created (ONBOARDING) before go-live
    go_live: date
    churn: date | None
    anniversaries: list[date] = field(default_factory=list)


def client_dates(client: dict[str, Any], seed: int, history_end: date) -> ClientDates:
    rng = np.random.default_rng(derive_seed(seed, "client_dates", client["code"]))
    if client.get("go_live_date"):  # clients onboarded at runtime by the onboard_client scenario
        go_live = date.fromisoformat(str(client["go_live_date"]))
    else:
        go_live = month_start(history_end, client["onboard_month"]) + timedelta(days=int(rng.integers(0, 20)))
    closed_won = go_live - timedelta(days=int(rng.integers(35, 75)))
    first_touch = closed_won - timedelta(days=int(rng.integers(45, 160)))
    ax_created = go_live - timedelta(days=int(rng.integers(7, 22)))
    wms_created = go_live - timedelta(days=int(rng.integers(10, 30)))
    churn = None
    if client.get("churn_month") is not None:
        churn = month_start(history_end, client["churn_month"]) + timedelta(days=int(rng.integers(0, 25)))
    anniversaries = []
    k = 1
    while True:
        a = date(go_live.year + k, go_live.month, min(go_live.day, 28))
        if a > history_end + timedelta(days=730) or (churn and a >= churn):
            break
        anniversaries.append(a)
        k += 1
    return ClientDates(client["code"], first_touch, closed_won, go_live, ax_created, wms_created, go_live, churn, anniversaries)


def applicable_charge_codes(rules: dict[str, Any], client: dict[str, Any], seed: int) -> list[str]:
    v = rules["verticals"][client["vertical"]]
    temp = client.get("temp", v["temp"])
    rng = np.random.default_rng(derive_seed(seed, "charge_codes", client["code"]))
    b2c = v["b2c_share"]
    codes = ["STOR-PLT", "RCV-PLT", "RCV-CTN", "MIN-ADJ"]
    if client["vertical"] in ("apparel", "health_beauty", "electronics", "supplements", "pet"):
        codes.append("STOR-BIN")
    if temp in ("COOLER", "FREEZER"):
        codes.append("COLD-SUR")
    if b2c > 0.2:
        codes += ["ORD-B2C", "PICK-ADD", "PACK-MAT"]
    if b2c < 0.95:
        codes += ["ORD-B2B", "PICK-CTN"]
    if v["return_rate"] > 0.02:
        codes.append("RET-PROC")
    if client["vertical"] in ("auto_parts", "b2b_general") or rng.random() < 0.25:
        codes.append("LBL-ACC")
    if client["vertical"] in ("health_beauty", "supplements", "electronics") and rng.random() < 0.5:
        codes.append("KIT-ASM")
    if client["tier"] in ("M", "L", "XL"):
        codes.append("ACCT-MGT")
    if b2c > 0.5 and rng.random() < 0.6:
        codes.append("FRT-RCV")
    return codes


@dataclass
class RateVersion:
    version: int
    effective_date: date
    end_date: date | None
    currency: str
    increase_pct: float                 # applied vs previous version (0 for the first)
    rates: dict[str, float]             # charge_code -> rate in `currency` (FRT-RCV holds the markup %)
    monthly_minimum: float


def contract_rates(rules: dict[str, Any], client: dict[str, Any], seed: int, history_end: date, fx: FxFn) -> list[RateVersion]:
    """All contracted rate versions for a client: the original rate card plus one per anniversary."""
    dates = client_dates(client, seed, history_end)
    rng = np.random.default_rng(derive_seed(seed, "rates", client["code"]))
    cur = client["currency"]
    to_ccy = fx(dates.contract_start, cur) if cur != "USD" else 1.0
    disc = rules["tier_discounts"][client["tier"]]
    by_code = {c["code"]: c for c in rules["charge_codes"]}
    base: dict[str, float] = {}
    for code in applicable_charge_codes(rules, client, seed):
        cc = by_code[code]
        if code == "FRT-RCV":
            base[code] = round(cc.get("markup_pct", 0.12) * 100 * float(rng.uniform(0.8, 1.2)), 2)
            continue
        if code == "MIN-ADJ":
            base[code] = 0.0
            continue
        usd = cc["list_rate"] * (1 - disc) * float(rng.uniform(0.96, 1.04))
        nd = 4 if usd < 2 else 2
        base[code] = round(usd * to_ccy, nd)
    minimum = round(rules["monthly_minimums"][client["tier"]] * to_ccy, 2)
    lo, hi = rules["annual_rate_increase_pct"]
    versions = [RateVersion(1, dates.contract_start, None, cur, 0.0, base, minimum)]
    for i, ann in enumerate(dates.anniversaries, start=2):
        pct = round(float(rng.uniform(lo, hi)), 1)
        prev = versions[-1]
        prev.end_date = ann - timedelta(days=1)
        new = {k: (v if k in ("FRT-RCV", "MIN-ADJ") else round(v * (1 + pct / 100), 4 if v < 2 else 2)) for k, v in prev.rates.items()}
        versions.append(RateVersion(i, ann, None, cur, pct, new, round(prev.monthly_minimum * (1 + pct / 100), 2)))
    if dates.churn:
        versions[-1].end_date = dates.churn
    return versions


def rate_on(versions: list[RateVersion], d: date) -> RateVersion | None:
    for v in reversed(versions):
        if v.effective_date <= d and (v.end_date is None or d <= v.end_date):
            return v
    return None


def ax_account_num(rules: dict[str, Any], code: str) -> str:
    """AX custtable.accountnum for a client: 'C' + 5 digits, the same number in every legal entity.

    Numbers follow the order of ``rules['clients']`` (runtime clients are appended, so existing numbers never move).
    """
    for i, c in enumerate(rules["clients"]):
        if c["code"] == code:
            return f"C{10010 + i * 10:05d}"
    raise KeyError(code)
