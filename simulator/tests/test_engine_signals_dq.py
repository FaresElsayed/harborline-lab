"""Signals (offline fallback), DQ injection, scenario catalog / onboarding, and the DQ scoring CLI."""

from __future__ import annotations

import itertools
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
import yaml

from harborline.settings import PACKAGE_ROOT
from harborline.sim import scenarios
from harborline.sim.anomalies import CSV_COLUMNS, DataQualityInjector
from harborline.sim.signals import Signals
from harborline.tools import score_dq

RULES = yaml.safe_load((PACKAGE_ROOT / "config" / "business_rules.yaml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def signals() -> Signals:
    return Signals(RULES, offline=True)


@pytest.mark.parametrize(("ccy", "lo", "hi"), [("CAD", 1.2, 1.5), ("EUR", 0.8, 1.05), ("GBP", 0.7, 0.92), ("MXN", 16, 25)])
def test_fx_fallback_is_plausible_and_deterministic(signals: Signals, ccy: str, lo: float, hi: float) -> None:
    days = [date(2023, 10, 1) + timedelta(days=i) for i in range(0, 1100, 7)]
    values = [signals.fx(d, ccy) for d in days]
    assert all(lo < v < hi for v in values), (min(values), max(values))
    fresh = Signals(RULES, offline=True)
    assert values == [fresh.fx(d, ccy) for d in days]


def test_fx_weekends_carry_friday_and_future_dates_keep_moving(signals: Signals) -> None:
    friday = date(2026, 6, 5)
    assert signals.fx(friday + timedelta(days=1), "EUR") == signals.fx(friday, "EUR") == signals.fx(
        friday + timedelta(days=2), "EUR")
    future = [signals.fx(date(2027, 3, 1) + timedelta(days=i), "CAD") for i in range(10)]
    assert len(set(future)) > 3 and all(1.0 < v < 2.0 for v in future)
    assert signals.fx(datetime(2026, 6, 5, 23, tzinfo=UTC), "USD") == 1.0
    assert signals.fx(friday, "XXX") == 1.0


def test_holidays(signals: Signals) -> None:
    assert signals.is_holiday("US", date(2026, 7, 4))
    assert signals.is_holiday("us01", date(2026, 11, 26))           # Thanksgiving, by legal entity
    assert signals.is_holiday("CA", date(2026, 7, 1))
    assert signals.is_holiday("MX", date(2026, 9, 16))
    assert not signals.is_holiday("US", date(2026, 7, 7))


def test_weather_is_seasonal_and_snows_in_chicago_winters(signals: Signals) -> None:
    jan = [signals.weather("CHI1", date(2026, 1, 1) + timedelta(days=i)) for i in range(31)]
    jul = [signals.weather("CHI1", date(2026, 7, 1) + timedelta(days=i)) for i in range(31)]
    assert np.mean([w["temp_max_c"] for w in jan]) + 15 < np.mean([w["temp_max_c"] for w in jul])
    winter = [signals.weather("CHI1", date(y, m, 1) + timedelta(days=i)) for y in (2024, 2025) for m in (1, 2) for i in range(28)]
    assert sum(w["snow_cm"] > 0 for w in winter) >= 3
    assert all(w["snow_cm"] == 0 for w in jul)
    assert set(jan[0]) == {"snow_cm", "temp_max_c", "precip_mm"}
    assert signals.weather("ZZZ9", date(2026, 1, 1))["temp_max_c"] is not None


def test_diesel_is_weekly_smooth_and_in_range(signals: Signals) -> None:
    mondays = [date(2023, 10, 2) + timedelta(weeks=i) for i in range(160)]
    prices = [signals.diesel(d) for d in mondays]
    assert 3.4 <= min(prices) and max(prices) <= 4.2 and max(prices) - min(prices) > 0.3
    assert max(abs(a - b) for a, b in itertools.pairwise(prices)) < 0.08
    assert signals.diesel(date(2026, 6, 1)) == signals.diesel(date(2026, 6, 7))       # constant Mon..Sun


def test_dq_mask_rate_toggle_and_stable_random_stream() -> None:
    dq = DataQualityInjector(["orphan_fk", "mojibake"], denominator=100)
    rng = np.random.default_rng(1)
    rate = dq.mask("orphan_fk", 200_000, rng).mean()
    assert 0.008 < rate < 0.012
    dq.update(categories={"orphan_fk": False})
    a, b = np.random.default_rng(5), np.random.default_rng(5)
    assert not dq.mask("orphan_fk", 1000, a).any()
    dq.mask("mojibake", 1000, b)
    assert a.random() == b.random()                         # disabled categories still consume the same draws
    dq.update(denominator=5)
    assert dq.denominator == 100                            # clamped to the slider range
    dq.update(denominator=10**9)
    assert dq.denominator == 100_000


def test_dq_mirror_writes_header_once(tmp_path: Path) -> None:
    dq = DataQualityInjector(["negative_qty"], csv_path=tmp_path / "anomalies.csv")
    row = {"anomaly_id": 1, "injected_at": datetime(2026, 1, 1, tzinfo=UTC), "sim_ts": None, "category": "negative_qty",
           "system": "wms", "object": "order_line", "record_key": "1|2", "column_name": "ordered_qty",
           "before_value": "3", "after_value": "-3", "note": None}
    dq.mirror([row])
    dq.mirror([{**row, "anomaly_id": 2}])
    lines = (tmp_path / "anomalies.csv").read_text().splitlines()
    assert lines[0] == ",".join(CSV_COLUMNS) and len(lines) == 3


def test_scenario_catalog_and_validation() -> None:
    catalog = scenarios.catalog(RULES)
    assert {s["id"] for s in catalog} == set(scenarios.SCENARIO_IDS)
    rate = scenarios.get("rate_increase")
    clean = scenarios.validate(rate, {"client": "GLOWTH", "pct": "4.5"}, RULES)
    assert clean == {"client": "GLOWTH", "pct": 4.5, "keyed_in_wms": "late", "delay_days": 21}
    with pytest.raises(ValueError):
        scenarios.validate(rate, {"client": "URBLOOM"}, RULES)         # churned clients are not offered
    with pytest.raises(ValueError):
        scenarios.validate(scenarios.get("storm"), {"warehouse": "XXX1"}, RULES)
    with pytest.raises(KeyError):
        scenarios.get("nope")


def test_onboard_client_is_fictional_unique_and_shaped_like_rules() -> None:
    params = scenarios.validate(scenarios.get("onboard_client"), {"vertical": "food_bev", "warehouse": "MTY1",
                                                                  "currency": "MXN", "go_live_days": 20}, RULES)
    start = datetime(2026, 10, 15, tzinfo=UTC)
    client = scenarios.make_client(RULES, params, start, datetime(2026, 10, 31, tzinfo=UTC), np.random.default_rng(3))
    existing = RULES["clients"]
    assert client["code"] not in {c["code"] for c in existing} and len(client["code"]) <= 7
    assert client["name"].split()[0].lower() not in {c["name"].split()[0].lower() for c in existing}
    assert client["entity"] == "mx01" and client["go_live_date"] == "2026-11-04" and client["onboard_month"] == 1
    assert {"code", "name", "vertical", "tier", "currency", "entity", "warehouses", "onboard_month"} <= client.keys()
    with pytest.raises(ValueError):
        scenarios.make_client(RULES, {**params, "name": "Lumen Activewear"}, start, start, np.random.default_rng(1))


def test_score_dq_precision_recall(tmp_path: Path) -> None:
    anomalies = tmp_path / "anomalies.csv"
    anomalies.write_text(
        "anomaly_id,injected_at,sim_ts,category,system,object,record_key,column_name,before_value,after_value,note\n"
        "1,x,2026-05-01T00:00:00+00:00,negative_qty,wms,order_line,100|1,ordered_qty,3,-3,\n"
        "2,x,2026-05-02T00:00:00+00:00,negative_qty,wms,order_line,101|2,ordered_qty,3,-3,\n"
        "3,x,2026-05-03T00:00:00+00:00,orphan_fk,ax2012,custtrans,5637144999,accountnum,C10001,C99999,\n"
        "4,x,2026-09-01T00:00:00+00:00,mojibake,salesforce,Account,001000000000001AAA,Name,Café,CafÃ©,\n")
    findings = tmp_path / "findings.csv"
    findings.write_text("System,Table,Key,Category\n"
                        "WMS,wms.order_line, 100|1 ,negative_qty\n"
                        "ax2012,dbo.CUSTTRANS,5637144999,duplicate_row\n"
                        "wms,order_header,HL0000000001,null_required\n")
    result = score_dq.score(score_dq.read_findings(findings), score_dq.anomalies_from_csv(anomalies))
    assert result["matched_findings"] == 2 and result["precision"] == pytest.approx(2 / 3)
    assert result["recall"] == pytest.approx(0.5)
    cats = result["categories"]
    assert cats["negative_qty"]["found"] == 1 and cats["negative_qty"]["recall"] == 0.5
    assert cats["negative_qty"]["precision"] == 1.0 and cats["duplicate_row"]["precision"] == 0.0
    assert cats["mojibake"]["found"] == 0
    assert score_dq.main([str(findings), "--anomalies", str(anomalies), "--until", "2026-06-01", "--show", "2"]) == 0
