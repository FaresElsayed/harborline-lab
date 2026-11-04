"""Shared fixtures. Database tests need a throwaway Postgres: see harborline/sim/testing.py (HL_TEST_PG_PORT)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pandas as pd
import pytest

from harborline.core import Window
from harborline.db import get_db
from harborline.settings import Settings
from harborline.sim import engine as engine_mod
from harborline.sim import testing
from harborline.sim.engine import Engine, EngineCtx, get_engine, reset_engine


@pytest.fixture(scope="session")
def pg_settings() -> Settings:
    """Settings pointed at the test database (skips the test when HL_TEST_PG_PORT is not set)."""
    if not testing.pg_configured():
        pytest.skip("set HL_TEST_PG_PORT to run database tests (see harborline/sim/testing.py)")
    return testing.use_test_database()


@pytest.fixture
def ctx(pg_settings: Settings) -> Iterator[EngineCtx]:
    """A fresh Small-preset build context over empty source databases."""
    c = testing.make_ctx("small", "build")
    yield c
    testing.close_ctx(c)


@pytest.fixture
def make_ctx(pg_settings: Settings) -> Iterator[Callable[..., EngineCtx]]:
    """Factory: ``make_ctx(preset="small", mode="build", **kw)``; every context is closed at teardown."""
    created: list[EngineCtx] = []

    def factory(preset: str = "small", mode: str = "build", **kw) -> EngineCtx:
        c = testing.make_ctx(preset, mode, **kw)
        created.append(c)
        return c

    yield factory
    for c in created:
        testing.close_ctx(c)


# --------------------------------------------------------------------------- engine test doubles
class FakeModule:
    """Tiny simulator module for engine tests: every window it inserts, updates and deletes WMS rows."""

    name = "fake"

    def __init__(self) -> None:
        self.fail = False
        self.scenarios: list[tuple[str, dict[str, Any]]] = []

    def bootstrap(self, ctx: EngineCtx) -> None:
        ts = ctx.history_start.replace(tzinfo=None)
        ctx.copy_rows("wms", "warehouse", pd.DataFrame({
            "warehouse_id": [1], "warehouse_code": ["ATL1"], "warehouse_name": ["Atlanta"], "country_code": ["US"],
            "time_zone": ["America/New_York"], "weight_uom": ["LB"], "dim_uom": ["IN"], "create_ts": [ts], "mod_ts": [ts],
        }))
        ctx.world["fake"] = {"windows": 0}

    def step(self, ctx: EngineCtx, win: Window) -> None:
        n = max(1, int(win.hours * 4))
        rng = ctx.rng(self.name, win)
        ids = ctx.next_ids("fake.txn", n)
        ctx.copy_rows("wms", "inventory_txn", pd.DataFrame({
            "txn_id": ids, "txn_ts": win.start.replace(tzinfo=None), "warehouse_id": 1, "owner_id": 1,
            "material_id": rng.integers(1, 100, n), "txn_type": "RECEIVE", "qty": rng.integers(1, 9, n),
            "create_ts": win.start.replace(tzinfo=None),
        }))
        wms = ctx.conn("wms")
        wms.execute("UPDATE wms.warehouse SET mod_ts = %s WHERE warehouse_id = 1", (win.end.replace(tzinfo=None),))
        wms.execute("DELETE FROM wms.inventory_txn WHERE txn_id = (SELECT min(txn_id) FROM wms.inventory_txn)")
        ctx.dq.log("negative_qty", "wms", "inventory_txn", str(int(ids[0])), "qty", 3, -3)
        ctx.changes.add("files", "fake_report.csv", ins=1)
        ctx.world["fake"]["windows"] += 1
        if self.fail:
            raise RuntimeError("fake module failure")

    def apply_scenario(self, ctx: EngineCtx, scenario_id: str, params: dict[str, Any], win: Window) -> str:
        self.scenarios.append((scenario_id, params))
        return f"handled {scenario_id}"


@pytest.fixture
def fake_module() -> FakeModule:
    return FakeModule()


@pytest.fixture
def fake_engine(pg_settings: Settings, fake_module: FakeModule, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    """A fresh Engine (empty state and run log) whose module registry contains only ``FakeModule``."""
    monkeypatch.setattr(engine_mod, "load_modules", lambda names=None: ([fake_module], []))
    with get_db().connect("sim_internal") as conn:
        conn.execute("DELETE FROM sim.state")
        conn.execute("TRUNCATE sim.run_log, sim.anomaly_log RESTART IDENTITY")
        conn.commit()
    reset_engine()
    eng = get_engine()
    eng.lock_wait_s = 0.5
    eng.load()
    yield eng
    eng.shutdown()
    reset_engine()


def wait_for_build(eng: Engine, timeout_s: float = 300) -> None:
    thread = eng._build_thread
    assert thread is not None
    thread.join(timeout_s)
    assert not thread.is_alive(), "build did not finish in time"


@pytest.fixture
def built_engine(fake_engine: Engine) -> Engine:
    """``fake_engine`` after a finished Small build."""
    fake_engine.start_build("small", 7)
    wait_for_build(fake_engine)
    assert fake_engine.error is None, fake_engine.error
    return fake_engine
