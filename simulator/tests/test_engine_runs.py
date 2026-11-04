"""Build / batch / stream / scenario runs through the real Engine with a fake module."""

from __future__ import annotations

import pickle
import time
from datetime import timedelta

import pytest

from harborline.db import get_db
from harborline.sim.engine import Engine, EngineBusy, get_engine, reset_engine
from harborline.sim.testing import TEST_HISTORY_END


def _run_log(kind: str) -> list[tuple]:
    return get_db().fetchall("sim_internal", "SELECT run_id, status, changes, sim_from, sim_to FROM sim.run_log "
                                             "WHERE kind = %s ORDER BY run_id", (kind,))


def _changes(summary: dict) -> dict[tuple[str, str], dict]:
    return {(c["system"], c["table"]): c for c in summary["changes"]}


def _txn_count() -> int:
    return get_db().fetchall("wms", "SELECT count(*) FROM wms.inventory_txn")[0][0]


def test_batch_requires_a_build(fake_engine: Engine) -> None:
    with pytest.raises(EngineBusy, match="Build first"):
        fake_engine.batch(1)


def test_build_runs_bootstrap_and_every_day(built_engine: Engine, fake_module) -> None:
    eng = built_engine
    assert eng.state == "idle"
    assert eng.sim_clock == eng.history_end and eng.history_end.date() == TEST_HISTORY_END
    days = (eng.history_end - eng.history_start).days
    assert eng.world["fake"]["windows"] == days
    (_run_id, status, changes, sim_from, sim_to), = _run_log("build")
    assert status == "ok" and sim_from == eng.history_start and sim_to == eng.history_end
    by_table = {(c["system"], c["table"]): c for c in changes}
    assert by_table[("wms", "inventory_txn")]["ins"] == days * 96
    assert by_table[("wms", "inventory_txn")]["del"] == days
    assert by_table[("wms", "warehouse")] == {"system": "wms", "table": "warehouse", "ins": 1, "upd": days, "del": 0}
    assert by_table[("files", "fake_report.csv")]["ins"] == days
    assert eng.settings.anomalies_csv.read_text().count("negative_qty") == days
    indexes = get_db().fetchall("wms", "SELECT count(*) FROM pg_indexes WHERE indexname LIKE 'hlx_%'")[0][0]
    assert indexes > 20
    status = eng.status()
    assert {"state", "preset", "seed", "sim_clock", "real_clock", "history_start", "history_end", "built_at", "dq",
            "stream", "build", "totals", "db_size_bytes", "landing", "last_run"} <= status.keys()
    assert any(t["system"] == "wms" and t["table"] == "inventory_txn" and t["rows"] > 0 for t in status["totals"])


def test_batch_splits_windows_and_checkpoints_the_world(built_engine: Engine) -> None:
    eng = built_engine
    start, txns = eng.sim_clock, _txn_count()
    summary = eng.batch(30)
    assert summary["status"] == "ok" and summary["kind"] == "batch"
    assert eng.sim_clock == start + timedelta(hours=30)
    changes = _changes(summary)
    assert changes[("wms", "inventory_txn")]["ins"] == 30 * 4 and changes[("wms", "inventory_txn")]["del"] == 2
    assert changes[("wms", "warehouse")]["upd"] == 2
    assert _txn_count() == txns + 120 - 2
    blob, clock = get_db().fetchall("sim_internal", "SELECT world, sim_clock FROM sim.state WHERE id = 1")[0]
    assert pickle.loads(blob)["fake"]["windows"] == eng.world["fake"]["windows"] and clock == eng.sim_clock
    assert _run_log("batch")[-1][1] == "ok"


def test_failed_window_rolls_back_data_world_and_clock(built_engine: Engine, fake_module) -> None:
    eng = built_engine
    eng.batch(24)
    clock, txns, ids = eng.sim_clock, _txn_count(), dict(eng.world["_ids"])
    windows = eng.world["fake"]["windows"]
    fake_module.fail = True
    summary = eng.batch(48)
    assert summary["status"] == "error" and "fake module failure" in summary["error"]
    assert eng.sim_clock == clock and eng.world["_ids"] == ids and eng.world["fake"]["windows"] == windows
    assert _txn_count() == txns
    assert eng.state == "error"
    fake_module.fail = False
    assert eng.batch(1)["status"] == "ok" and eng.state == "idle"


def test_state_survives_a_restart(built_engine: Engine, fake_module) -> None:
    built_engine.batch(5)
    ids, clock = dict(built_engine.world["_ids"]), built_engine.sim_clock
    built_engine.shutdown()
    reset_engine()
    again = get_engine()
    again.load()
    assert again.sim_clock == clock and again.world["_ids"] == ids and again.preset == "small"
    assert again.batch(1)["status"] == "ok"
    again.shutdown()


def test_stream_ticks_advance_the_clock_and_log_runs(built_engine: Engine) -> None:
    eng = built_engine
    start = eng.sim_clock
    eng.start_stream(interval_s=1, speed=600)
    deadline = time.monotonic() + 20
    while eng.ticks < 3 and time.monotonic() < deadline:
        time.sleep(0.1)
    assert eng.state == "streaming"
    assert eng.batch(2)["status"] == "ok"          # a batch slips in between ticks
    eng.stop_stream()
    ticks = eng.ticks
    assert ticks >= 3 and eng.state == "idle"
    assert eng.sim_clock == start + timedelta(seconds=600 * ticks) + timedelta(hours=2)
    rows = _run_log("stream_tick")
    assert len(rows) == ticks and all(r[1] == "ok" for r in rows)
    first = {(c["system"], c["table"]): c for c in rows[0][2]}
    assert first[("wms", "inventory_txn")]["ins"] == 1 and first[("wms", "inventory_txn")]["del"] == 1


def test_build_stops_a_running_stream(built_engine: Engine) -> None:
    eng = built_engine
    eng.start_stream(interval_s=1, speed=60)
    time.sleep(0.3)
    eng.start_build("small", 7)
    assert not eng._stream_thread.is_alive()
    eng._build_thread.join(120)
    assert eng.state == "idle" and eng.ticks >= 1
    assert not (eng._stream_thread and eng._stream_thread.is_alive())


def test_batch_waits_for_or_refuses_while_another_run_holds_the_lock(built_engine: Engine) -> None:
    eng = built_engine
    assert eng._run_lock.acquire()
    try:
        with pytest.raises(EngineBusy):
            eng.batch(1)
    finally:
        eng._run_lock.release()
    assert eng.batch(1)["status"] == "ok"


def test_onboard_client_scenario_creates_a_dynamic_client(built_engine: Engine, fake_module) -> None:
    eng = built_engine
    clock = eng.sim_clock
    summary = eng.run_scenario("onboard_client", {"vertical": "pet", "tier": "S", "warehouse": "TOR1", "currency": "CAD"})
    assert summary["status"] == "ok" and summary["kind"] == "scenario"
    assert eng.sim_clock == clock + timedelta(hours=1)
    client = eng.world["dynamic_clients"][-1]
    assert client["entity"] == "ca01" and client["dynamic"] is True and client["warehouses"] == ["TOR1"]
    assert client["code"] in {c["code"] for c in eng.rules["clients"]}
    sid, params = fake_module.scenarios[-1]
    assert sid == "onboard_client" and params["client"]["code"] == client["code"]
    assert "handled onboard_client" in summary["notes"]
    eng.shutdown()
    reset_engine()
    again = get_engine()
    again.load()
    assert client["code"] in {c["code"] for c in again.rules["clients"]}
    again.shutdown()


def test_scenario_params_are_validated(built_engine: Engine) -> None:
    with pytest.raises(ValueError, match="pct"):
        built_engine.run_scenario("rate_increase", {"client": "NBOUT", "pct": 99})
    with pytest.raises(ValueError, match="client"):
        built_engine.run_scenario("offboard_client", {"client": "NOPE"})
    summary = built_engine.run_scenario("offboard_client", {"client": "NBOUT", "notice_days": 10})
    assert summary["status"] == "ok"


def test_dq_settings_persist(fake_engine: Engine) -> None:
    cfg = fake_engine.set_dq(2500, {"mojibake": False, "not_a_category": True})
    assert cfg["denominator"] == 2500 and cfg["categories"]["mojibake"] is False
    assert "not_a_category" not in cfg["categories"]
    reset_engine()
    again = get_engine()
    again.load()
    assert again.dq.denominator == 2500 and again.dq.enabled("mojibake") is False


def test_stream_resumes_after_a_restart(built_engine: Engine) -> None:
    built_engine.start_stream(interval_s=1, speed=30)
    time.sleep(0.5)
    built_engine.shutdown()                       # container stop: the stream flag is kept
    reset_engine()
    again = get_engine()
    again.lock_wait_s = 0.5
    again.load()
    try:
        assert again.state == "streaming" and again.stream_cfg == {"interval_s": 1.0, "speed": 30.0}
    finally:
        again.stop_stream()                       # explicit stop: the flag is cleared
    reset_engine()
    third = get_engine()
    third.load()
    assert third.state == "idle"
    third.shutdown()
