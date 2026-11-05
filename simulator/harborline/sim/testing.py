"""Helpers that give module tests a real ``EngineCtx`` against a throwaway Postgres.

Start a database once (pick your own port), then point the tests at it::

    export HL_TEST_PG_PASSWORD="$(openssl rand -hex 16)"
    docker run -d --name hl-pg-<you> -p <port>:5432 -e POSTGRES_PASSWORD="$HL_TEST_PG_PASSWORD" postgres:17 -c wal_level=logical
    HL_TEST_PG_PORT=<port> .venv/bin/python -m pytest tests/<yours>

Optional: HL_TEST_PG_HOST (localhost). Landing / answer-key folders go to a temp dir,
public APIs are never called (HL_OFFLINE=1) and the history ends on ``TEST_HISTORY_END``, so runs are deterministic.

Stable API::

    make_ctx(preset="small", mode="build", *, seed=TEST_SEED, history_end=TEST_HISTORY_END, wipe=True) -> EngineCtx
    run_window(ctx, module, win) -> ChangeCounter        # one module, one window, committed
    bootstrap_modules(ctx, modules=("wms",)) -> ChangeCounter
    run_days(ctx, days, modules=MODULE_ORDER, start=None) -> ChangeCounter
    day_window(ctx, day=0) -> Window                    # [history_start + day, +1 day)
    fetch_df(ctx, db, query, params=None) -> pandas.DataFrame
    close_ctx(ctx)
    pg_configured() -> bool; use_test_database() -> Settings
"""

from __future__ import annotations

import os
import secrets
import shutil
import tempfile
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from psycopg.pq import TransactionStatus

from ..core import MODULE_ORDER, ChangeCounter, Module, Window
from ..db import get_db, reset_db
from ..settings import Settings, get_settings, reset_settings
from .anomalies import DataQualityInjector
from .engine import (
    EngineCtx,
    add_months,
    load_yaml,
    merge_changes,
    reset_engine,
    resolve_module,
    utc_midnight,
)
from .signals import Signals

TEST_SEED = 20261031
TEST_HISTORY_END = date(2026, 10, 1)
GENERATED_KEYS = ("HL_APP_PASSWORD", "HL_ETL_PASSWORD", "HL_REVIEWER_PASSWORD", "SF_CLIENT_ID", "SF_CLIENT_SECRET", "CARRIER_API_KEY")

_state: dict[str, Any] = {"configured": False, "tmp": None}


def pg_configured() -> bool:
    return bool(os.environ.get("HL_TEST_PG_PORT"))


def use_test_database() -> Settings:
    """Point the whole process (settings, db, engine singletons) at the test Postgres and bootstrap it once."""
    if not pg_configured():
        raise RuntimeError("HL_TEST_PG_PORT is not set (see harborline/sim/testing.py)")
    if _state["configured"]:
        return get_settings()
    password = os.environ.get("HL_TEST_PG_PASSWORD")
    if not password:
        raise RuntimeError("HL_TEST_PG_PASSWORD is not set (see harborline/sim/testing.py)")
    tmp = Path(os.environ.get("HL_TEST_TMP") or tempfile.mkdtemp(prefix="harborline-test-"))
    landing, answer_key = tmp / "landing", tmp / "answer_key"
    landing.mkdir(parents=True, exist_ok=True)
    answer_key.mkdir(parents=True, exist_ok=True)
    planted = get_settings().planted_path
    if planted.exists():
        shutil.copy(planted, answer_key / planted.name)
    os.environ.update({
        "HL_PG_HOST": os.environ.get("HL_TEST_PG_HOST", "localhost"),
        "HL_PG_PORT": os.environ["HL_TEST_PG_PORT"],
        "POSTGRES_USER": "postgres",
        "POSTGRES_PASSWORD": password,
        # The test database gets its own keys each run, like an install does: nothing here may depend on their values.
        **{name: os.environ.get(name) or secrets.token_hex(24) for name in GENERATED_KEYS},
        "HL_OFFLINE": "1",
        "HL_LANDING_DIR": str(landing),
        "HL_ANSWER_KEY_DIR": str(answer_key),
        "HL_HISTORY_END": TEST_HISTORY_END.isoformat(),
        "HL_STREAM_CHECKPOINT_S": "0",
    })
    reset_settings()
    reset_db()
    reset_engine()
    get_db().bootstrap()
    _state.update(configured=True, tmp=tmp)
    return get_settings()


def make_ctx(preset: str = "small", mode: str = "build", *, seed: int = TEST_SEED,
             history_end: date = TEST_HISTORY_END, wipe: bool = True) -> EngineCtx:
    """A fresh context over an empty set of source databases (``wipe=False`` keeps existing rows)."""
    settings = use_test_database()
    db = get_db()
    if wipe:
        db.wipe_source_data()
        for child in settings.landing_dir.iterdir():
            shutil.rmtree(child) if child.is_dir() else child.unlink()
        settings.anomalies_csv.unlink(missing_ok=True)
    rules = load_yaml(settings.rules_path)
    planted = load_yaml(settings.planted_path) if settings.planted_path.exists() else {}
    months = int(rules["size_presets"][preset]["months"])
    end = utc_midnight(history_end)
    start = add_months(end, -months)
    dq_rules = rules.get("data_quality", {})
    dq = DataQualityInjector(dq_rules.get("categories", []), dq_rules.get("default_rate_denominator", 10_000),
                             csv_path=settings.anomalies_csv)
    ctx = EngineCtx(
        db=db, settings=settings, mode=mode, seed=seed, preset=preset, rules=rules, planted=planted,
        history_start=start, history_end=end, world={"dynamic_clients": []}, dq=dq, signals=Signals(rules, offline=True),
    )
    ctx.window = Window(start, start)
    return ctx


def _commit(ctx: EngineCtx) -> ChangeCounter:
    try:
        measured, anomalies = ctx.flush_window()
        ctx.commit()
    except BaseException:
        ctx.rollback()
        ctx.dq.discard()
        raise
    ctx.dq.mirror(anomalies)
    return measured


def _delta(before: dict[tuple[str, str], list[int]], after: ChangeCounter) -> ChangeCounter:
    out = ChangeCounter()
    for key, (ins, upd, dele) in after.rows.items():
        b = before.get(key, [0, 0, 0])
        if (ins, upd, dele) != tuple(b):
            out.add(key[0], key[1], ins - b[0], upd - b[1], dele - b[2])
    return out


def run_window(ctx: EngineCtx, module: Module | str, win: Window) -> ChangeCounter:
    """Run one module's ``step`` for ``win`` and commit. The bus is kept while consecutive calls use the same
    window, so ``run_window(ctx, "wms", w); run_window(ctx, "crm", w)`` hands wms frames to crm."""
    mod = resolve_module(module)
    before = {k: list(v) for k, v in ctx.changes.rows.items()}
    ctx.begin_window(win, clear_bus=ctx.window != win)
    try:
        mod.step(ctx, win)
    except BaseException:
        ctx.rollback()
        ctx.dq.discard()
        raise
    measured = _commit(ctx)
    return merge_changes(_delta(before, ctx.changes), measured)


def bootstrap_modules(ctx: EngineCtx, modules: Iterable[Module | str] = ("wms",)) -> ChangeCounter:
    """Call ``bootstrap`` on the given modules (in order, sharing one bus) at ``history_start`` and commit."""
    before = {k: list(v) for k, v in ctx.changes.rows.items()}
    ctx.begin_window(Window(ctx.history_start, ctx.history_start))
    try:
        for module in modules:
            resolve_module(module).bootstrap(ctx)
    except BaseException:
        ctx.rollback()
        ctx.dq.discard()
        raise
    measured = _commit(ctx)
    return merge_changes(_delta(before, ctx.changes), measured)


def day_window(ctx: EngineCtx, day: int = 0) -> Window:
    start = ctx.history_start + timedelta(days=day)
    return Window(start, start + timedelta(days=1))


def run_days(ctx: EngineCtx, days: int, modules: Iterable[Module | str] = MODULE_ORDER,
             start: datetime | None = None) -> ChangeCounter:
    """Run ``days`` one-day windows through ``modules`` (one transaction per window, like a build)."""
    mods = [resolve_module(m) for m in modules]
    total = ChangeCounter()
    cur = start or (ctx.window.end if ctx.window else ctx.history_start)
    for _ in range(days):
        win = Window(cur, cur + timedelta(days=1))
        for mod in mods:
            for (system, table), (ins, upd, dele) in run_window(ctx, mod, win).rows.items():
                total.add(system, table, ins, upd, dele)
        cur = win.end
    # A build writes its EDI archives at each month end, and a short run can stop before one.
    if any(getattr(mod, "name", "") == "edi" for mod in mods):
        from .edi import Edi
        Edi(ctx).flush_archives()
    return total


def fetch_df(ctx: EngineCtx, db: str, query: str, params: Any = None) -> pd.DataFrame:
    """Read between windows. Ends the read-only transaction so an idle test session holds no locks that a later
    wipe/rebuild in the same pytest session would wait on."""
    df = ctx.read_df(db, query, params)
    conn = ctx.conn(db)
    if conn.info.transaction_status == TransactionStatus.INTRANS:
        conn.rollback()
    return df


def close_ctx(ctx: EngineCtx) -> None:
    ctx.close()
