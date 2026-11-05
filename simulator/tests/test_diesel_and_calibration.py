"""The diesel series and signal sources the simulator publishes, and the probe calibration programme.

The file tests need no database. The calibration tests need a throwaway Postgres (HL_TEST_PG_PORT, see
harborline/sim/testing.py) and run the IoT module only, in windows before readings start, so years pass in seconds.
"""

from __future__ import annotations

import csv
import itertools
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from harborline.core import ChangeCounter, Window
from harborline.settings import get_settings
from harborline.sim import files as sim_files
from harborline.sim import iot, testing
from harborline.sim.engine import EngineCtx, load_yaml
from harborline.sim.signals import Signals
from harborline.uat_extract import _diesel_reference

START = datetime(2026, 4, 1, tzinfo=UTC)
needs_pg = pytest.mark.skipif(not testing.pg_configured(), reason="set HL_TEST_PG_PORT (see harborline/sim/testing.py)")


def files_ctx(tmp_path: Path, world: dict[str, Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(landing_dir=str(tmp_path), history_start=START, signals=Signals({}, offline=True),
                           changes=ChangeCounter(), world={} if world is None else world)


def series(tmp_path: Path) -> dict[date, float]:
    with (tmp_path / "files" / "reference" / "diesel_weekly.csv").open(newline="", encoding="ascii") as fh:
        return {date.fromisoformat(r["week_start"]): float(r["diesel_usd_per_gal"]) for r in csv.DictReader(fh)}


# ------------------------------------------------------------------------------------------------- the diesel file
def test_each_week_is_published_on_its_monday() -> None:
    monday = date(2026, 6, 1)
    assert sim_files.diesel_week_published(datetime(2026, 6, 1, 11, 59, tzinfo=UTC)) == monday - timedelta(days=7)
    assert sim_files.diesel_week_published(datetime(2026, 6, 1, 12, 0, tzinfo=UTC)) == monday
    assert sim_files.diesel_week_published(datetime(2026, 6, 7, 23, 0, tzinfo=UTC)) == monday


def test_the_file_is_exactly_the_signal_billing_reads(tmp_path: Path) -> None:
    ctx = files_ctx(tmp_path)
    sim_files.Files(ctx).publish_reference(datetime(2026, 7, 15, tzinfo=UTC))
    prices = series(tmp_path)
    weeks = sorted(prices)
    assert all(week.weekday() == 0 for week in weeks)
    assert all(b - a == timedelta(days=7) for a, b in itertools.pairwise(weeks))
    assert weeks[0] <= START.date() - timedelta(days=14) and weeks[-1] == date(2026, 7, 13)
    assert prices == {week: Signals.diesel(week) for week in weeks}


def test_the_file_grows_once_a_week_and_only_then(tmp_path: Path) -> None:
    ctx = files_ctx(tmp_path)
    publisher = sim_files.Files(ctx)
    publisher.publish_reference(datetime(2026, 7, 15, tzinfo=UTC))
    publisher.publish_reference(datetime(2026, 7, 16, tzinfo=UTC))
    assert ctx.changes.rows[("files", "diesel_weekly")] == [1, 0, 0]
    publisher.publish_reference(datetime(2026, 7, 21, tzinfo=UTC))
    assert ctx.changes.rows[("files", "diesel_weekly")] == [1, 1, 0]
    assert max(series(tmp_path)) == date(2026, 7, 20)


def test_a_build_made_before_the_file_existed_gets_the_same_file(tmp_path: Path) -> None:
    now = datetime(2026, 8, 3, 18, tzinfo=UTC)
    fresh, older = tmp_path / "fresh", tmp_path / "older"
    publisher = sim_files.Files(files_ctx(fresh))
    publisher.publish_reference(START)
    publisher.publish_reference(now)
    sim_files.Files(files_ctx(older, {"files": {"rate_cards": {"UPSN:2026": True}}})).publish_reference(now)
    for name in ("diesel_weekly.csv", "signal_sources.csv"):
        assert (older / "files" / "reference" / name).read_bytes() == (fresh / "files" / "reference" / name).read_bytes()


def test_the_review_extract_takes_the_file_only_when_it_is_there(tmp_path: Path) -> None:
    assert _diesel_reference(tmp_path, None) == []
    sim_files.write_diesel_series(files_ctx(tmp_path), date(2026, 5, 4))
    assert [p.name for p, _why in _diesel_reference(tmp_path / "files", None)] == ["diesel_weekly.csv"]


# ------------------------------------------------------------------------------------------------- signal sources
def test_signal_sources_say_when_nothing_was_real(tmp_path: Path) -> None:
    rules = {"warehouses": [{"code": "CHI1", "address": {"lat": 41.5, "lon": -88.1}}]}
    rows = Signals(rules, offline=True).source_rows()
    by_signal = {(r["signal"], r["key"]): r for r in rows}
    assert by_signal["diesel", "US"]["source"] == "simulator"
    assert by_signal["holidays", "US,CA,MX"]["source"] == "python-holidays"
    assert by_signal["weather", "CHI1"]["source"] == "synthetic"
    assert all(r["source"] == "fallback" and not r["real_from"] for r in rows if r["signal"] == "fx")
    assert all(set(r) == set(sim_files.SOURCES_HEADER) for r in rows)


def test_signal_sources_give_the_real_range_and_keep_it_past_later_lookups() -> None:
    rules = {"warehouses": [{"code": "CHI1", "address": {"lat": 41.5, "lon": -88.1}}]}
    signals = Signals(rules, offline=True)
    signals._apply({"CAD": {"2026-01-05": 1.37, "2026-03-31": 1.39}},
                   {"CHI1": {"2026-01-01": [3.0, 0.0, 0.0], "2026-03-29": [9.0, 1.0, 0.0]}})
    signals.fx(date(2026, 6, 1), "CAD")      # past the real range: the walk extends the series
    rows = {(r["signal"], r["key"]): r for r in signals.source_rows()}
    assert (rows["fx", "CAD"]["source"], rows["fx", "CAD"]["real_from"], rows["fx", "CAD"]["real_to"]) == \
        ("frankfurter", "2026-01-05", "2026-03-31")
    assert (rows["weather", "CHI1"]["source"], rows["weather", "CHI1"]["real_from"], rows["weather", "CHI1"]["real_to"]) == \
        ("open-meteo", "2026-01-01", "2026-03-29")
    assert rows["fx", "EUR"]["source"] == "fallback"


# ------------------------------------------------------------------------------------------------- calibration
def calibration_rules() -> dict[str, Any]:
    return load_yaml(get_settings().rules_path)["sensor_calibration"]


def iot_only(ctx: EngineCtx) -> EngineCtx:
    """Every warehouse open, IoT module bootstrapped, nothing else: the sensor list without a WMS underneath."""
    ctx.world["wms"] = {"wh_ready": set(range(len(ctx.rules["warehouses"])))}
    testing.bootstrap_modules(ctx, ("iot",))
    return ctx


def before_readings(ctx: EngineCtx) -> datetime:
    return ctx.history_end - timedelta(days=int(ctx.rules["size_presets"][ctx.preset]["iot_days"]))


def run_through(ctx: EngineCtx, until: datetime, step_days: int) -> None:
    cur = ctx.history_start
    while cur < until:
        end = min(cur + timedelta(days=step_days), until)
        testing.run_window(ctx, "iot", Window(cur, end))
        cur = end


def due_dates(ctx: EngineCtx) -> dict[str, date]:
    rows = testing.fetch_df(ctx, "iot", "SELECT sensor_id, calibration_due FROM telemetry.sensor WHERE sensor_type <> 'DOOR'")
    return dict(zip(rows["sensor_id"], rows["calibration_due"], strict=True))


@needs_pg
def test_a_long_history_keeps_a_small_share_overdue_and_no_probe_far_behind(make_ctx: Callable[..., EngineCtx]) -> None:
    ctx = iot_only(make_ctx("large"))
    cfg = ctx.rules["sensor_calibration"]
    shares, cur = [], ctx.history_start
    while cur + timedelta(days=30) <= before_readings(ctx):
        testing.run_window(ctx, "iot", Window(cur, cur + timedelta(days=30)))
        cur += timedelta(days=30)
        dues = due_dates(ctx)
        shares.append(sum(d < cur.date() for d in dues.values()) / len(dues))
        assert max((cur.date() - d).days for d in dues.values()) <= cfg["late_days"][1] + 3     # a weekend can add two
    assert len(shares) >= 30
    assert 0.02 <= sum(shares) / len(shares) <= 0.10
    assert max(shares) <= 0.15
    cycles = [p[1] for p in ctx.world["iot"]["calibration"]["probes"].values()]
    assert min(cycles) >= 2       # nearly three years: every probe has been back round
    assert testing.fetch_df(ctx, "iot", "SELECT count(*) AS n FROM telemetry.sensor WHERE sensor_type = 'DOOR' "
                                        "AND calibration_due IS NOT NULL")["n"].iloc[0] == 0


@needs_pg
def test_due_dates_do_not_depend_on_how_time_was_cut_into_windows(make_ctx: Callable[..., EngineCtx]) -> None:
    weekly = iot_only(make_ctx("large"))
    until = before_readings(weekly)
    run_through(weekly, until, 7)
    expected = due_dates(weekly)
    testing.close_ctx(weekly)
    quarterly = iot_only(make_ctx("large"))
    run_through(quarterly, until, 91)
    assert due_dates(quarterly) == expected


@needs_pg
def test_due_dates_change_with_the_seed(make_ctx: Callable[..., EngineCtx]) -> None:
    one = iot_only(make_ctx("large"))
    run_through(one, before_readings(one), 91)
    expected = due_dates(one)
    testing.close_ctx(one)
    other = iot_only(make_ctx("large", seed=testing.TEST_SEED + 1))
    run_through(other, before_readings(other), 91)
    assert due_dates(other) != expected


@needs_pg
def test_a_build_made_before_the_programme_catches_up_in_its_next_window(make_ctx: Callable[..., EngineCtx]) -> None:
    fresh = iot_only(make_ctx("large"))
    until = before_readings(fresh)
    run_through(fresh, until, 30)
    expected = due_dates(fresh)
    testing.close_ctx(fresh)

    older = iot_only(make_ctx("large"))
    state = older.world["iot"]
    del state["calibration"]      # what a world saved before this programme looks like, with its first due dates untouched
    stale = due_dates(older)
    assert sum(d < until.date() for d in stale.values()) / len(stale) > 0.9
    testing.run_window(older, "iot", Window(older.history_start, until))
    assert due_dates(older) == expected
    assert "calibration" in state


@needs_pg
def test_a_build_with_no_sensors_yet_starts_the_programme_without_trouble(make_ctx: Callable[..., EngineCtx]) -> None:
    ctx = make_ctx("small")
    ctx.world["wms"] = {"wh_ready": set()}
    testing.bootstrap_modules(ctx, ("iot",))
    del ctx.world["iot"]["calibration"]
    window = testing.day_window(ctx)
    testing.run_window(ctx, "iot", window)
    assert ctx.world["iot"]["calibration"] == {"probes": {}, "next": None}


@needs_pg
def test_no_visit_lands_inside_a_stuck_probe_window(make_ctx: Callable[..., EngineCtx]) -> None:
    ctx = make_ctx("small")
    t0, t1, sensor = iot._stuck_window(ctx)
    for day in range(t0 // iot.DAY - 130, t1 // iot.DAY + 30):
        for cycle in range(3):
            assert not t0 <= iot._calibration_visit(ctx, sensor, cycle, day) < t1


def test_the_rules_leave_a_small_share_overdue_on_paper() -> None:
    cfg = calibration_rules()
    late = cfg["late_share"] * sum(cfg["late_days"]) / 2
    early = (1 - cfg["late_share"]) * sum(cfg["early_days"]) / 2
    assert 0.03 <= late / (cfg["interval_days"] + late - early) <= 0.08
