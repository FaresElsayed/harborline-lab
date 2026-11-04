"""Window math, DST handling, deterministic randomness and id allocation (no database needed)."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from harborline.core import Window
from harborline.settings import get_settings
from harborline.sim.anomalies import DataQualityInjector
from harborline.sim.engine import EngineCtx, add_months, day_windows, split_windows
from harborline.sim.signals import Signals

T0 = datetime(2026, 3, 1, tzinfo=UTC)


def _ctx(seed: int = 1) -> EngineCtx:
    return EngineCtx(db=None, settings=get_settings(), mode="build", seed=seed, preset="small", rules={}, planted={},
                     history_start=T0, history_end=T0 + timedelta(days=30), world={}, dq=DataQualityInjector([]),
                     signals=Signals({}, offline=True))


def _tiles(windows: list[Window], start: datetime, end: datetime) -> bool:
    return windows[0].start == start and windows[-1].end == end and all(
        a.end == b.start for a, b in itertools.pairwise(windows))


def test_split_windows_caps_each_window_at_24h() -> None:
    wins = split_windows(T0, 60)
    assert [w.hours for w in wins] == [24, 24, 12]
    assert _tiles(wins, T0, T0 + timedelta(hours=60))
    assert [w.hours for w in split_windows(T0, 0.5)] == [0.5]
    assert [w.hours for w in split_windows(T0, 24)] == [24]


def test_day_windows_tile_the_history() -> None:
    end = datetime(2026, 10, 1, tzinfo=UTC)
    start = add_months(end, -6)
    wins = day_windows(start, end)
    assert len(wins) == (end - start).days == 183
    assert _tiles(wins, start, end)
    assert all(w.hours == 24 for w in wins)


@pytest.mark.parametrize(("dt", "months", "expected"), [
    (datetime(2026, 3, 31, tzinfo=UTC), -1, datetime(2026, 2, 28, tzinfo=UTC)),
    (datetime(2024, 3, 31, tzinfo=UTC), -1, datetime(2024, 2, 29, tzinfo=UTC)),
    (datetime(2026, 10, 31, tzinfo=UTC), -36, datetime(2023, 10, 31, tzinfo=UTC)),
    (datetime(2026, 1, 15, tzinfo=UTC), -6, datetime(2025, 7, 15, tzinfo=UTC)),
])
def test_add_months(dt: datetime, months: int, expected: datetime) -> None:
    assert add_months(dt, months) == expected


@pytest.mark.parametrize("tz", ["America/New_York", "America/Chicago", "America/Los_Angeles", "America/Toronto",
                                "America/Monterrey"])
@pytest.mark.parametrize(("hh", "mm"), [(2, 0), (1, 30), (9, 0)])
def test_nightly_job_runs_exactly_once_per_local_day_across_dst(tz: str, hh: int, mm: int) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    hits = [t for w in day_windows(start, start + timedelta(days=365)) for t in w.crosses_local_time(tz, hh, mm)]
    local_days = [t.astimezone(ZoneInfo(tz)).date() for t in hits]
    assert len(local_days) == len(set(local_days)), "a job ran twice on one local day"
    assert len(hits) in (364, 365)       # the first/last local day can fall outside the UTC range


def test_spring_forward_and_fall_back_utc_instants() -> None:
    def at(day: datetime, hh: int, mm: int = 0) -> list[datetime]:
        return Window(day, day + timedelta(days=1)).crosses_local_time("America/New_York", hh, mm)

    assert at(datetime(2026, 3, 8, tzinfo=UTC), 2) == [datetime(2026, 3, 8, 7, tzinfo=UTC)]     # 02:00 does not exist
    assert at(datetime(2026, 3, 9, tzinfo=UTC), 2) == [datetime(2026, 3, 9, 6, tzinfo=UTC)]     # EDT
    assert at(datetime(2026, 11, 1, tzinfo=UTC), 1, 30) == [datetime(2026, 11, 1, 5, 30, tzinfo=UTC)]  # first 01:30


def test_short_stream_windows_find_jobs_once() -> None:
    start = datetime(2026, 3, 8, 0, tzinfo=UTC)
    wins = split_windows(start, 24, max_hours=5 / 60)       # 5-minute stream ticks
    assert len(wins) == 288
    hits = [t for w in wins for t in w.crosses_local_time("America/Chicago", 2)]
    assert hits == [datetime(2026, 3, 8, 8, tzinfo=UTC)]


def test_rng_is_deterministic_per_seed_module_window_salt() -> None:
    win = Window(T0, T0 + timedelta(days=1))
    a = _ctx(1).rng("wms", win, "orders").random(5)
    assert (a == _ctx(1).rng("wms", win, "orders").random(5)).all()
    assert not (a == _ctx(1).rng("wms", win, "lines").random(5)).all()
    assert not (a == _ctx(2).rng("wms", win, "orders").random(5)).all()
    assert not (a == _ctx(1).rng("ax", win, "orders").random(5)).all()


def test_next_ids_are_consecutive_and_persist_in_world() -> None:
    ctx = _ctx()
    assert list(ctx.next_ids("order", 3)) == [1, 2, 3]
    assert list(ctx.next_ids("order", 2)) == [4, 5]
    assert list(ctx.next_ids("recid.custtrans", 2, start=5637144576)) == [5637144576, 5637144577]
    assert list(ctx.next_ids("recid.custtrans", 1, start=1)) == [5637144578]
    assert ctx.world["_ids"] == {"order": 6, "recid.custtrans": 5637144579}
    assert len(ctx.next_ids("empty", 0)) == 0
