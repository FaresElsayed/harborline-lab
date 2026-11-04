"""Simulation engine: the ``Ctx`` implementation, module registry, build / batch / stream / scenario runs.

One run at a time (``_run_lock``). Every window runs each module's ``step`` in ``MODULE_ORDER`` inside one
transaction per database, then commits (sim_internal first, so the clock and id counters never lag the data).
Row changes are measured from ``pg_stat_xact_user_tables`` for wms / ax2012 / iot / carrier and merged with what
modules report through ``ctx.changes`` (Salesforce objects, EDI and business files).
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import pickle
import shutil
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import psycopg
import yaml
from psycopg import sql

from ..core import MODULE_ORDER, Bus, ChangeCounter, Module, Window, derive_seed
from ..db import Database, get_db
from ..settings import Settings, get_settings
from .anomalies import DataQualityInjector
from .signals import Signals

log = logging.getLogger("harborline.engine")

DEFAULT_SCHEMA = {"wms": "wms", "ax2012": "dbo", "iot": "telemetry", "sim_internal": "sim", "tracker": "tracker"}
MEASURED = {"wms": {"wms": "wms"}, "ax2012": {"dbo": "ax2012"}, "iot": {"telemetry": "iot"}, "sim_internal": {"carrier": "carrier"}}
COMMIT_ORDER = ("sim_internal", "wms", "ax2012", "iot", "tracker")
CONN_OPTIONS = "-c synchronous_commit=off"
COPY_CHUNK = 200_000
SMALL_WORLD_BYTES = 4 * 1024 * 1024
INT_TYPES = {"int2", "int4", "int8"}
JSON_TYPES = {"json", "jsonb"}
OUTDATED_DATA = ("This data was built by an earlier version, before a carrier was renamed. You can still read it, but "
                 "to generate more, press Build dataset once.")
LANDING_BLOCKED = ("The app cannot write to the landing folder. Restart it to repair the folder's owner: "
                   "docker compose restart app. If that does not help, check your antivirus")
# One table per module that a finished build must have filled.
BUILD_PROOF = {"wms": ("wms", "wms.order_header"), "ax": ("ax2012", "dbo.custinvoicejour"), "crm": ("sim_internal", "sfdc.record"),
               "iot": ("iot", "telemetry.sensor_reading"), "carrier": ("sim_internal", "carrier.tracking_event")}


class EngineBusy(RuntimeError):
    """Another run holds the lock (or the requested action needs a finished build)."""


class LandingBlocked(RuntimeError):
    """The landing folder cannot be written. Raised before a build touches anything, so old data and its stamp stay."""


class RunAborted(RuntimeError):
    pass


# --------------------------------------------------------------------------- small helpers
def utcnow() -> datetime:
    return datetime.now(UTC)


def utc_midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def add_months(dt: datetime, months: int) -> datetime:
    y, m = divmod(dt.year * 12 + dt.month - 1 + months, 12)
    m += 1
    last = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)).day
    return dt.replace(year=y, month=m, day=min(dt.day, last))


def day_windows(start: datetime, end: datetime) -> list[Window]:
    out, cur = [], start
    while cur < end:
        nxt = min(cur + timedelta(days=1), end)
        out.append(Window(cur, nxt))
        cur = nxt
    return out


def split_windows(start: datetime, hours: float, max_hours: float = 24.0) -> list[Window]:
    """Batch of ``hours`` sim hours from ``start``, in windows of at most ``max_hours``."""
    end = start + timedelta(hours=hours)
    out, cur = [], start
    while cur < end:
        nxt = min(cur + timedelta(hours=max_hours), end)
        out.append(Window(cur, nxt))
        cur = nxt
    return out


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def merge_dynamic_clients(rules: dict[str, Any], world: dict[str, Any] | None) -> dict[str, Any]:
    """Append runtime-onboarded clients (``world['dynamic_clients']``) to ``rules['clients']`` (in place)."""
    clients = rules.setdefault("clients", [])
    known = {c["code"] for c in clients}
    for client in (world or {}).get("dynamic_clients", []):
        if client["code"] not in known:
            clients.append(client)
            known.add(client["code"])
    return rules


def merge_changes(reported: ChangeCounter, measured: ChangeCounter) -> ChangeCounter:
    """Measured database counts win over module-reported counts for the same (system, table)."""
    out = ChangeCounter()
    for key, value in reported.rows.items():
        if key not in measured.rows:
            out.rows[key] = list(value)
    for key, value in measured.rows.items():
        out.rows[key] = list(value)
    return out


_warned_missing: set[str] = set()


def load_modules(names: tuple[str, ...] = MODULE_ORDER) -> tuple[list[Module], list[str]]:
    """Import ``harborline.sim.<name>.MODULE`` for each name. Missing modules are skipped (warned once)."""
    modules: list[Module] = []
    missing: list[str] = []
    for name in names:
        qualified = f"harborline.sim.{name}"
        try:
            mod = importlib.import_module(qualified)
        except ModuleNotFoundError as exc:
            if exc.name != qualified:
                raise
            if name not in _warned_missing:
                _warned_missing.add(name)
                log.warning("Simulator module %s is not available yet; skipping it", qualified)
            missing.append(name)
            continue
        module = getattr(mod, "MODULE", None)
        if module is None:
            log.warning("%s has no MODULE attribute; skipping it", qualified)
            missing.append(name)
            continue
        modules.append(module)
    return modules, missing


def resolve_module(module: Module | str) -> Module:
    if isinstance(module, str):
        return importlib.import_module(f"harborline.sim.{module}").MODULE
    return module


# --------------------------------------------------------------------------- events
class EventHub:
    """Fan-out of engine events to SSE subscribers. ``publish`` is thread-safe."""

    def __init__(self) -> None:
        self._subs: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._lock = threading.Lock()

    def subscribe(self, loop: asyncio.AbstractEventLoop | None = None) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        with self._lock:
            self._subs.append((loop or asyncio.get_running_loop(), queue))
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subs = [(lp, q) for lp, q in self._subs if q is not queue]

    def publish(self, event: str, data: dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subs)
        for loop, queue in subs:
            try:
                loop.call_soon_threadsafe(_offer, queue, (event, data))
            except RuntimeError:          # loop closed: drop the subscriber
                self.unsubscribe(queue)

    def log(self, msg: str, level: str = "info") -> None:
        getattr(log, "warning" if level == "warn" else level, log.info)(msg)
        self.publish("log", {"ts": utcnow().isoformat(), "level": level, "msg": msg})


def _offer(queue: asyncio.Queue, item: tuple[str, dict[str, Any]]) -> None:
    if queue.full():
        queue.get_nowait()
    queue.put_nowait(item)


# --------------------------------------------------------------------------- context
class LastKnown:
    """A slow figure for the status call: one read at a time runs in the background, and callers get the latest value.

    A caller waits a moment so fast machines see fresh numbers, but never longer. Status has to answer whatever the
    disk is doing: setup reads silence as a broken app, and every caller left waiting holds one of the API's threads.
    """

    def __init__(self, read: Callable[[], Any], initial: Any, min_age_s: float, patience_s: float = 1.0) -> None:
        self._read = read
        self.value = initial
        self._min_age_s = min_age_s
        self._patience_s = patience_s
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._read_at = 0.0
        self._took = 0.0
        self._stale = True

    def get(self) -> Any:
        deadline = time.monotonic() + self._patience_s
        while True:
            with self._lock:
                if self._thread is None and self._due():
                    self._stale = False
                    self._thread = threading.Thread(target=self._refresh, name="hl-status", daemon=True)
                    self._thread.start()
                thread = self._thread
            left = deadline - time.monotonic()
            if thread is None or left <= 0:
                return self.value
            thread.join(left)
            if thread.is_alive():
                return self.value

    def expire(self) -> None:
        with self._lock:
            self._stale = True

    def _due(self) -> bool:
        # A read that takes a minute competes with the build for the same disk, so it runs that much less often.
        return self._stale or time.monotonic() - self._read_at >= max(self._min_age_s, self._took * 20)

    def _refresh(self) -> None:
        started = time.monotonic()
        try:
            self.value = self._read()
        except Exception:  # noqa: BLE001 - status keeps the last value; it must never fail because of a figure
            log.exception("status figure not readable")
        finally:
            with self._lock:
                self._read_at = time.monotonic()
                self._took = self._read_at - started
                self._thread = None


class EngineCtx:
    """``core.Ctx`` implementation handed to every module."""

    def __init__(self, *, db: Database, settings: Settings, mode: str, seed: int, preset: str, rules: dict[str, Any],
                 planted: dict[str, Any], history_start: datetime, history_end: datetime, world: dict[str, Any],
                 dq: DataQualityInjector, signals: Signals, hub: EventHub | None = None,
                 conns: dict[str, psycopg.Connection] | None = None,
                 type_cache: dict[tuple[str, str, str], dict[str, str]] | None = None) -> None:
        self.db = db
        self.settings = settings
        self.mode = mode
        self.seed = seed
        self.preset = preset
        self.rules = rules
        self.planted = planted
        self.history_start = history_start
        self.history_end = history_end
        self.world = world
        self.dq = dq
        self.signals = signals
        self.landing_dir = str(settings.landing_dir)
        self.answer_key_dir = str(settings.answer_key_dir)
        self.bus = Bus()
        self.changes = ChangeCounter()
        self.window: Window | None = None
        self.hub = hub
        self._owns_conns = conns is None
        self._baseline: dict[str, dict[tuple[str, str], tuple[int, int, int]]] = {}
        self._conns: dict[str, psycopg.Connection] = {} if conns is None else conns
        self._types = {} if type_cache is None else type_cache

    # ----------------------------------------------------------------- Ctx protocol
    def conn(self, db: str) -> psycopg.Connection:
        conn = self._conns.get(db)
        if conn is None or conn.closed:
            conn = self.db.connect(db, extra_options=CONN_OPTIONS)
            self._conns[db] = conn
        return conn

    def copy_rows(self, db: str, table: str, df: pd.DataFrame) -> int:
        """COPY ``df`` into ``table`` (columns = df columns). NaN/None/NaT become NULL; float columns going into
        integer columns are rounded; dict/list values going into json columns are serialised. tz-aware datetimes
        written to ``timestamp`` (no tz) columns keep their wall-clock time (Postgres drops the offset).
        Counts are not added to ``ctx.changes``: the engine measures database changes itself."""
        if df is None or len(df) == 0:
            return 0
        schema, name = table.split(".", 1) if "." in table else (DEFAULT_SCHEMA[db], table)
        conn = self.conn(db)
        types = self._column_types(conn, db, schema, name, list(df.columns))
        frame = _prepare_frame(df, types)
        stmt = sql.SQL("COPY {}.{} ({}) FROM STDIN WITH (FORMAT csv, NULL '\\N')").format(
            sql.Identifier(schema), sql.Identifier(name), sql.SQL(", ").join(sql.Identifier(str(c)) for c in frame.columns))
        with conn.cursor() as cur, cur.copy(stmt) as copy:
            for lo in range(0, len(frame), COPY_CHUNK):
                copy.write(frame.iloc[lo : lo + COPY_CHUNK].to_csv(index=False, header=False, na_rep="\\N", lineterminator="\n"))
        return len(frame)

    def next_ids(self, sequence: str, n: int, start: int = 1) -> np.ndarray:
        """``n`` consecutive ids for ``sequence`` (``start`` only applies to a sequence's first allocation)."""
        counters = self.world.setdefault("_ids", {})
        first = int(counters.get(sequence, start))
        counters[sequence] = first + int(n)
        return np.arange(first, first + int(n), dtype=np.int64)

    def rng(self, module: str, win: Window, salt: str = "") -> np.random.Generator:
        return np.random.default_rng(derive_seed(self.seed, module, win.start.isoformat(), salt))

    # ----------------------------------------------------------------- conveniences for modules
    @property
    def sim_now(self) -> datetime:
        return self.window.end if self.window else self.history_end

    def read_df(self, db: str, query: str | sql.Composable, params: Any = None) -> pd.DataFrame:
        """Run a query on this run's connection (sees uncommitted rows of the current window)."""
        with self.conn(db).cursor() as cur:
            cur.execute(query, params)
            cols = [d.name for d in cur.description] if cur.description else []
            return pd.DataFrame(cur.fetchall(), columns=cols)

    def log(self, msg: str, level: str = "info") -> None:
        if self.hub:
            self.hub.log(msg, level)
        else:
            log.info(msg)

    # ----------------------------------------------------------------- engine side
    def begin_window(self, win: Window, clear_bus: bool = True) -> None:
        self.window = win
        if clear_bus:
            self.bus.clear()
        self.dq.sim_ts = win.start
        # pg_stat_xact_* also holds counts of earlier transactions the backend has not flushed yet (PG15+ flushes
        # lazily), so changes are measured as a delta from this snapshot taken at the start of the transaction.
        self._baseline = {db: self._xact_counts(db) for db in MEASURED if db in self._conns and not self._conns[db].closed}

    def _xact_counts(self, db: str) -> dict[tuple[str, str], tuple[int, int, int]]:
        rows = self._conns[db].execute(
            "SELECT schemaname, relname, n_tup_ins, n_tup_upd, n_tup_del FROM pg_stat_xact_user_tables "
            "WHERE schemaname = ANY(%s) AND n_tup_ins + n_tup_upd + n_tup_del > 0",
            (list(MEASURED[db]),),
        ).fetchall()
        return {(schema, table): (ins, upd, dele) for schema, table, ins, upd, dele in rows}

    def flush_window(self) -> tuple[ChangeCounter, list[dict[str, Any]]]:
        """Measure this window's row changes and write pending anomalies (both inside the open transactions)."""
        measured = ChangeCounter()
        for db, systems in MEASURED.items():
            conn = self._conns.get(db)
            if conn is None or conn.closed:
                continue
            base = self._baseline.get(db, {})
            for (schema, table), (ins, upd, dele) in self._xact_counts(db).items():
                b_ins, b_upd, b_del = base.get((schema, table), (0, 0, 0))
                if (ins - b_ins) or (upd - b_upd) or (dele - b_del):
                    measured.add(systems[schema], table, ins - b_ins, upd - b_upd, dele - b_del)
        anomalies = self.dq.flush(self.conn("sim_internal")) if self.dq.pending else []
        return measured, anomalies

    def commit(self) -> None:
        for db in sorted(self._conns, key=lambda d: COMMIT_ORDER.index(d) if d in COMMIT_ORDER else 99):
            conn = self._conns[db]
            if not conn.closed:
                conn.commit()

    def rollback(self) -> None:
        for conn in self._conns.values():
            if not conn.closed:
                try:
                    conn.rollback()
                except psycopg.Error:
                    conn.close()

    def close(self) -> None:
        self.rollback()
        if self._owns_conns:
            for conn in self._conns.values():
                conn.close()
            self._conns.clear()

    def _column_types(self, conn: psycopg.Connection, db: str, schema: str, table: str, columns: list[str]) -> dict[str, str]:
        key = (db, schema, table)
        types = self._types.get(key)
        if types is None or not set(columns) <= types.keys():
            rows = conn.execute(
                "SELECT a.attname, t.typname FROM pg_attribute a JOIN pg_type t ON t.oid = a.atttypid "
                "JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped",
                (schema, table),
            ).fetchall()
            types = {name: typ for name, typ in rows}
            self._types[key] = types
        unknown = [c for c in columns if c not in types]
        if not types:
            raise ValueError(f"copy_rows: table {schema}.{table} does not exist in database {db}")
        if unknown:
            raise ValueError(f"copy_rows: {schema}.{table} has no column(s) {unknown}")
        return types


def _json_or_none(value: Any) -> Any:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return value


def _prepare_frame(df: pd.DataFrame, types: dict[str, str]) -> pd.DataFrame:
    """Coerce pandas dtypes into text Postgres accepts (pandas 3: copy-on-write, so the input is never modified)."""
    changed: dict[str, pd.Series] = {}
    for col in df.columns:
        s = df[col]
        typ = types[col]
        if typ in INT_TYPES:
            if s.dtype == object:
                s = pd.to_numeric(s)
            if pd.api.types.is_float_dtype(s.dtype):
                changed[col] = s.round().astype("Int64")
            elif pd.api.types.is_bool_dtype(s.dtype):
                changed[col] = s.astype("int64")
        elif typ in JSON_TYPES and s.dtype == object:
            changed[col] = s.map(_json_or_none)
        elif typ == "bool" and pd.api.types.is_float_dtype(s.dtype):
            changed[col] = s.map(lambda v: None if np.isnan(v) else bool(v)).astype("boolean")
    if not changed:
        return df
    out = df.copy(deep=False)
    for col, series in changed.items():
        out[col] = series
    return out


# --------------------------------------------------------------------------- engine
class Engine:
    """Process-wide simulator service used by the control API (see ``get_engine``)."""

    def __init__(self, settings: Settings | None = None, db: Database | None = None) -> None:
        self.settings = settings or get_settings()
        self.db = db or get_db()
        self.hub = EventHub()
        self._run_lock = threading.Lock()
        self.lock_wait_s = 60.0                      # how long batch / scenario / build wait for a running tick
        self._activity: str | None = None          # building | running_batch | scenario | stream_tick
        self._cancel = threading.Event()
        self._cancel_by_user = False
        self._build_thread: threading.Thread | None = None
        self._build_ctx: EngineCtx | None = None
        self._stream_thread: threading.Thread | None = None
        self._stream_stop = threading.Event()
        self._stream_guard = threading.Lock()
        self._stream_conns: dict[str, psycopg.Connection] = {}
        self._resume_stream = False                  # set by shutdown(): resume streaming after a restart
        self._type_cache: dict[tuple[str, str, str], dict[str, str]] = {}
        self.stream_cfg = {"interval_s": 5.0, "speed": 60.0}
        self.ticks = 0
        self.error: str | None = None
        self.outdated_data = False
        self.build_progress: dict[str, Any] | None = None
        self.last_run: dict[str, Any] | None = None
        self.preset: str | None = None
        self.seed: int = self.settings.default_seed
        self.history_start: datetime | None = None
        self.history_end: datetime | None = None
        self.sim_clock: datetime | None = None
        self.built_at: datetime | None = None
        self.world: dict[str, Any] = {"dynamic_clients": []}
        self._world_blob: bytes | None = None
        self._world_dirty = False
        self._last_ckpt = time.monotonic()
        self.rules: dict[str, Any] = {}
        self.planted: dict[str, Any] = {}
        self.missing_modules: list[str] = []
        self._totals = LastKnown(self._read_db_totals, ([], 0), min_age_s=3)
        self._landing = LastKnown(self._read_landing, {"files": 0, "bytes": 0}, min_age_s=5, patience_s=0.25)
        self.dq = DataQualityInjector([], csv_path=self.settings.anomalies_csv)
        self.signals = Signals({}, offline=True)

    # ----------------------------------------------------------------- lifecycle
    def load(self) -> None:
        """Read rules, persisted state and caches; resume a stream that was running before a restart."""
        self.rules = load_yaml(self.settings.rules_path)
        self.planted = load_yaml(self.settings.planted_path) if self.settings.planted_path.exists() else {}
        dq_rules = self.rules.get("data_quality", {})
        self.dq = DataQualityInjector(dq_rules.get("categories", []), dq_rules.get("default_rate_denominator", 10_000),
                                      csv_path=self.settings.anomalies_csv)
        self.signals = Signals(self.rules, offline=self.settings.offline)
        with self.db.connect("sim_internal") as conn:
            conn.execute("UPDATE sim.run_log SET status = 'error', error = 'interrupted by a restart', finished_at = now() "
                         "WHERE status = 'running'")
            row = conn.execute(
                "SELECT preset, seed, history_start, history_end, sim_clock, built_at, dq_denominator, dq_categories, "
                "world, id_counters, stream, last_error FROM sim.state WHERE id = 1"
            ).fetchone()
            self.signals.load(conn)
            conn.commit()
        stream: dict[str, Any] = {}
        if row:
            (self.preset, seed, self.history_start, self.history_end, self.sim_clock, self.built_at, denom, cats,
             blob, counters, stream, last_error) = row
            self.seed = int(seed) if seed is not None else self.settings.default_seed
            self.dq.update(denom, cats or {})
            self.world = pickle.loads(blob) if blob else {"dynamic_clients": []}
            ids = self.world.setdefault("_ids", {})
            for key, value in (counters or {}).items():
                ids[key] = max(int(ids.get(key, 0)), int(value))
            self._world_blob = pickle.dumps(self.world, protocol=pickle.HIGHEST_PROTOCOL)
            self.error = last_error
        merge_dynamic_clients(self.rules, self.world)
        self.outdated_data = self._carriers_differ_from_rules()
        if self.outdated_data:
            self.error = OUTDATED_DATA
        self.last_run = next(iter(self.runs(1)), None)
        if self.preset and self.built_at is None and (self.last_run or {}).get("status") != "cancelled":
            self.error = "The last build did not finish. Run Build again."
        _, self.missing_modules = load_modules()
        if stream.get("running") and self.built_at and not self.outdated_data:
            self.start_stream(stream.get("interval_s", 5), stream.get("speed", 60))

    def shutdown(self) -> None:
        self._cancel.set()
        self._resume_stream = bool(self._stream_thread and self._stream_thread.is_alive())
        self.stop_stream(persist=False)
        if self._build_thread and self._build_thread.is_alive():
            self._build_thread.join(timeout=30)
        if self._world_dirty and self._run_lock.acquire(timeout=30):
            try:
                self._checkpoint()
            finally:
                self._run_lock.release()

    # ----------------------------------------------------------------- status
    @property
    def state(self) -> str:
        if self._activity == "building":
            return "building"
        if self._activity in ("running_batch", "scenario"):
            return "running_batch"
        if self._stream_thread and self._stream_thread.is_alive():
            return "streaming"
        return "error" if self.error else "idle"

    def status(self) -> dict[str, Any]:
        totals, size = self._totals.get()
        return {
            "state": self.state,
            "error": self.error,
            "preset": self.preset,
            "seed": self.seed,
            "sim_clock": iso(self.sim_clock),
            "real_clock": utcnow().isoformat(),
            "history_start": iso(self.history_start),
            "history_end": iso(self.history_end),
            "built_at": iso(self.built_at),
            "dq": self.dq.config(),
            "stream": {"running": bool(self._stream_thread and self._stream_thread.is_alive()), **self.stream_cfg,
                       "ticks": self.ticks},
            "build": self.build_progress if self._activity == "building" else None,
            "totals": totals,
            "db_size_bytes": size,
            "landing": self._landing.get(),
            "last_run": self.last_run,
            "default_preset": self.settings.default_preset,
            "presets": self.presets_info(),
            "modules": {"order": list(MODULE_ORDER), "missing": self.missing_modules},
        }

    def presets_info(self) -> dict[str, dict[str, Any]]:
        return {name: {"months": p["months"], "volume_scale": p["volume_scale"]}
                for name, p in self.rules.get("size_presets", {}).items()}

    def _read_db_totals(self) -> tuple[list[dict[str, Any]], int]:
        try:
            return self.db.table_rows(), self.db.size_bytes()
        except psycopg.Error as exc:     # e.g. schemas being re-created by a build
            (log.debug if self._activity == "building" else log.warning)("status: database not readable: %s", exc)
            return [], 0

    def _read_landing(self) -> dict[str, int]:
        files = size = 0
        for dirpath, _dirs, names in os.walk(self.settings.landing_dir):
            for name in names:
                if not name.startswith("."):
                    files += 1
                    try:
                        size += os.path.getsize(os.path.join(dirpath, name))
                    except OSError:
                        pass
        return {"files": files, "bytes": size}

    def expire_status(self) -> None:
        self._totals.expire()
        self._landing.expire()

    def emit_state(self) -> None:
        self._totals.expire()
        self.hub.publish("state", self.status())

    def runs(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            "sim_internal",
            "SELECT run_id, kind, label, started_at, finished_at, sim_from, sim_to, status, error, changes, notes "
            "FROM sim.run_log ORDER BY run_id DESC LIMIT %s",
            (int(limit),),
        )
        keys = ("run_id", "kind", "label", "started_at", "finished_at", "sim_from", "sim_to", "status", "error", "changes", "notes")
        out = []
        for row in rows:
            rec = dict(zip(keys, row, strict=True))
            for k in ("started_at", "finished_at", "sim_from", "sim_to"):
                rec[k] = iso(rec[k])
            out.append(rec)
        return out

    # ----------------------------------------------------------------- configuration
    def set_dq(self, denominator: int | None = None, categories: dict[str, bool] | None = None) -> dict[str, Any]:
        self.dq.update(denominator, categories)
        cfg = self.dq.config()
        with self.db.pool("sim_internal").connection() as conn:
            conn.execute(
                "INSERT INTO sim.state (id, dq_denominator, dq_categories) VALUES (1, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET dq_denominator = EXCLUDED.dq_denominator, "
                "dq_categories = EXCLUDED.dq_categories, updated_at = now()",
                (cfg["denominator"], json.dumps(cfg["categories"])),
            )
        self.emit_state()
        return cfg

    def _modules(self) -> list[Module]:
        modules, self.missing_modules = load_modules()
        return modules

    def _new_ctx(self, mode: str, conns: dict[str, psycopg.Connection] | None = None) -> EngineCtx:
        assert self.history_start and self.history_end and self.preset
        return EngineCtx(
            db=self.db, settings=self.settings, mode=mode, seed=self.seed, preset=self.preset, rules=self.rules,
            planted=self.planted, history_start=self.history_start, history_end=self.history_end, world=self.world,
            dq=self.dq, signals=self.signals, hub=self.hub, conns=conns, type_cache=self._type_cache,
        )

    def _require_built(self) -> None:
        if not self.built_at or self.sim_clock is None:
            raise EngineBusy("No finished build yet: run Build first.")
        if self.outdated_data:
            raise EngineBusy(OUTDATED_DATA)

    def _carriers_differ_from_rules(self) -> bool:
        """Modules keep carriers by code in their saved state, so data built before a rename can't generate any more."""
        if not self.built_at:
            return False
        with self.db.connect("wms") as conn:
            built = {row[0] for row in conn.execute("SELECT scac FROM wms.carrier")}
        return bool(built) and built != {c["scac"] for c in self.rules.get("carriers", [])}

    @contextmanager
    def _exclusive(self, activity: str, wait_s: float) -> Iterator[None]:
        if not self._run_lock.acquire(timeout=wait_s):
            raise EngineBusy(f"The simulator is busy ({self.state}). Try again when it is idle.")
        self._activity = activity
        try:
            yield
        finally:
            self._activity = None
            self._run_lock.release()

    # ----------------------------------------------------------------- state persistence
    def _write_state(self, conn: psycopg.Connection, world_blob: bytes | None = None, clear_world: bool = False) -> None:
        values = {
            "preset": self.preset, "seed": self.seed, "history_start": self.history_start, "history_end": self.history_end,
            "sim_clock": self.sim_clock, "built_at": self.built_at, "dq_denominator": self.dq.denominator,
            "dq_categories": json.dumps(self.dq.categories), "id_counters": json.dumps(self.world.get("_ids", {})),
            "stream": json.dumps(self._stream_state()), "last_error": self.error,
        }
        if world_blob is not None or clear_world:
            values["world"] = world_blob
        cols = list(values)
        conn.execute(
            sql.SQL("INSERT INTO sim.state (id, {cols}) VALUES (1, {vals}) ON CONFLICT (id) DO UPDATE SET {sets}, updated_at = now()").format(
                cols=sql.SQL(", ").join(map(sql.Identifier, cols)),
                vals=sql.SQL(", ").join(sql.Placeholder() for _ in cols),
                sets=sql.SQL(", ").join(sql.SQL("{0} = EXCLUDED.{0}").format(sql.Identifier(c)) for c in cols),
            ),
            list(values.values()),
        )

    def _stream_state(self) -> dict[str, Any]:
        alive = bool(self._stream_thread and self._stream_thread.is_alive() and not self._stream_stop.is_set())
        return {"running": alive or self._resume_stream, **self.stream_cfg}

    def _checkpoint(self) -> None:
        """Persist the in-memory world (last committed window) and the clock."""
        blob = self._world_blob or pickle.dumps(self.world, protocol=pickle.HIGHEST_PROTOCOL)
        with self.db.connect("sim_internal") as conn:
            self._write_state(conn, world_blob=blob)
            conn.commit()
        self._world_dirty = False
        self._last_ckpt = time.monotonic()

    def _persist_stream_flag(self) -> None:
        if self.preset is None:
            return
        with self.db.pool("sim_internal").connection() as conn:
            conn.execute("UPDATE sim.state SET stream = %s, updated_at = now() WHERE id = 1", (json.dumps(self._stream_state()),))

    # ----------------------------------------------------------------- run log
    def _start_run_log(self, kind: str, label: str, sim_from: datetime | None) -> int:
        with self.db.pool("sim_internal").connection() as conn:
            row = conn.execute(
                "INSERT INTO sim.run_log (kind, label, started_at, sim_from, status) VALUES (%s, %s, now(), %s, 'running') "
                "RETURNING run_id",
                (kind, label[:80], sim_from),
            ).fetchone()
        return int(row[0])

    def _finish_run(self, run_id: int | None, kind: str, label: str, started: datetime, sim_from: datetime | None,
                    sim_to: datetime | None, status: str, error: str | None, changes: ChangeCounter,
                    notes: list[str]) -> dict[str, Any]:
        finished = utcnow()
        payload = changes.as_list()
        note_text = "\n".join(notes) or None
        with self.db.pool("sim_internal").connection() as conn:
            if run_id is None:
                run_id = conn.execute(
                    "INSERT INTO sim.run_log (kind, label, started_at, finished_at, sim_from, sim_to, status, error, changes, notes) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING run_id",
                    (kind, label[:80], started, finished, sim_from, sim_to, status, error, json.dumps(payload), note_text),
                ).fetchone()[0]
            else:
                conn.execute(
                    "UPDATE sim.run_log SET finished_at = %s, sim_from = %s, sim_to = %s, status = %s, error = %s, "
                    "changes = %s, notes = %s WHERE run_id = %s",
                    (finished, sim_from, sim_to, status, error, json.dumps(payload), note_text, run_id),
                )
        summary = {
            "run_id": int(run_id), "kind": kind, "label": label[:80], "started_at": iso(started), "finished_at": iso(finished),
            "sim_from": iso(sim_from), "sim_to": iso(sim_to), "status": status, "error": error, "changes": payload,
            "notes": note_text,
        }
        self.last_run = summary
        self._totals.expire()
        self.hub.publish("run", summary)
        return summary

    # ----------------------------------------------------------------- windows (batch / stream / scenario)
    def _run_window(self, ctx: EngineCtx, win: Window, modules: list[Module], save_world: Callable[[bytes], bool],
                    before: Callable[[EngineCtx, Window], None] | None = None) -> ChangeCounter:
        pre = self._world_blob if self._world_blob is not None else pickle.dumps(self.world, protocol=pickle.HIGHEST_PROTOCOL)
        ctx.begin_window(win)
        try:
            if before:
                before(ctx, win)
            for module in modules:
                module.step(ctx, win)
            measured, anomalies = ctx.flush_window()
            blob = pickle.dumps(self.world, protocol=pickle.HIGHEST_PROTOCOL)
            keep = save_world(blob)
            self.sim_clock = win.end
            self._write_state(ctx.conn("sim_internal"), world_blob=blob if keep else None)
            ctx.commit()
        except BaseException:
            ctx.rollback()
            self.dq.discard()
            self.world = pickle.loads(pre)
            self.rules = merge_dynamic_clients(load_yaml(self.settings.rules_path), self.world)
            self.sim_clock = win.start
            raise
        self._world_blob = blob
        self._world_dirty = not keep
        if keep:
            self._last_ckpt = time.monotonic()
        self.dq.mirror(anomalies)
        return measured

    def _run(self, ctx: EngineCtx, kind: str, label: str, windows: list[Window],
             before: Callable[[EngineCtx, Window], None] | None = None, notes: list[str] | None = None) -> dict[str, Any]:
        started = utcnow()
        sim_from = sim_to = windows[0].start
        measured = ChangeCounter()
        status, error = "ok", None
        last = len(windows) - 1
        try:
            modules = self._modules()
            for i, win in enumerate(windows):
                policy = self._stream_policy if kind == "stream_tick" else (lambda _b, final=(i == last): final)
                win_measured = self._run_window(ctx, win, modules, policy, before if i == 0 else None)
                for (system, table), (ins, upd, dele) in win_measured.rows.items():
                    measured.add(system, table, ins, upd, dele)
                sim_to = win.end
            self.error = None
        except Exception as exc:  # noqa: BLE001 - any module failure ends the run with an error summary
            status, error = "error", f"{type(exc).__name__}: {exc}"
            self.error = error
            self.hub.log(f"{label} failed: {error}", "error")
            log.error("%s failed\n%s", label, traceback.format_exc())
            if self._world_dirty:
                self._checkpoint()
        return self._finish_run(None, kind, label, started, sim_from, sim_to, status, error,
                                merge_changes(ctx.changes, measured), notes or [])

    def _stream_policy(self, blob: bytes) -> bool:
        return len(blob) <= SMALL_WORLD_BYTES or time.monotonic() - self._last_ckpt >= self.settings.stream_checkpoint_s

    # ----------------------------------------------------------------- batch
    def batch(self, hours: float) -> dict[str, Any]:
        self._require_built()
        hours = float(hours)
        if not 0 < hours <= 24 * 92:
            raise ValueError("hours must be between 0 and 2208 (92 days)")
        with self._exclusive("running_batch", wait_s=self.lock_wait_s):
            self.emit_state()
            ctx = self._new_ctx("batch")
            try:
                windows = split_windows(self.sim_clock, hours)
                label = f"Batch +{hours:g} h"
                summary = self._run(ctx, "batch", label, windows)
            finally:
                ctx.close()
        self.emit_state()
        return summary

    # ----------------------------------------------------------------- scenario
    def run_scenario(self, scenario_id: str, params: dict[str, Any]) -> dict[str, Any]:
        from . import scenarios

        self._require_built()
        spec = scenarios.get(scenario_id)
        clean = scenarios.validate(spec, params or {}, self.rules)
        notes: list[str] = []

        def apply(ctx: EngineCtx, win: Window) -> None:
            clean["effective_date"] = win.start.date().isoformat()
            if scenario_id == "offboard_client":
                clean["end_date"] = (win.start + timedelta(days=clean["notice_days"])).date().isoformat()
            if scenario_id == "onboard_client":
                client = scenarios.make_client(self.rules, clean, win.start, self.history_end, ctx.rng("engine", win, "onboard"))
                self.world.setdefault("dynamic_clients", []).append(client)
                merge_dynamic_clients(self.rules, self.world)
                clean["client"] = client
                notes.append(f"engine: onboarding {client['name']} ({client['code']}), go-live {client['go_live_date']}")
            for module in self._modules():
                handler = getattr(module, "apply_scenario", None)
                if handler is not None:
                    msg = handler(ctx, scenario_id, clean, win)
                    if msg:
                        notes.append(f"{module.name}: {msg}")

        with self._exclusive("scenario", wait_s=self.lock_wait_s):
            self.emit_state()
            ctx = self._new_ctx("scenario")
            try:
                win = Window(self.sim_clock, self.sim_clock + timedelta(hours=1))
                summary = self._run(ctx, "scenario", f"Scenario: {spec['name']}", [win], before=apply, notes=notes)
            finally:
                ctx.close()
        self.emit_state()
        return summary

    # ----------------------------------------------------------------- stream
    def start_stream(self, interval_s: float = 5, speed: float = 60) -> dict[str, Any]:
        self._require_built()
        interval_s, speed = float(interval_s), float(speed)
        if not 1 <= interval_s <= 300 or not 0 < speed <= 3600:
            raise ValueError("interval_s must be 1-300 and speed 0-3600")
        if self._activity == "building":
            raise EngineBusy("A build is running.")
        with self._stream_guard:
            self.stream_cfg = {"interval_s": interval_s, "speed": speed}
            if not (self._stream_thread and self._stream_thread.is_alive()):
                self._stream_stop.clear()
                self.ticks = 0
                self._stream_thread = threading.Thread(target=self._stream_loop, name="hl-stream", daemon=True)
                self._stream_thread.start()
        self._persist_stream_flag()
        self.hub.log(f"Stream started: every {interval_s:g} s at {speed:g}x ({interval_s * speed:g} sim seconds per tick)")
        self.emit_state()
        return self.status()

    def stop_stream(self, persist: bool = True) -> dict[str, Any]:
        with self._stream_guard:
            thread = self._stream_thread
            self._stream_stop.set()
        if thread and thread.is_alive():
            thread.join(timeout=180)
            self.hub.log(f"Stream stopped after {self.ticks} ticks")
        if persist:
            self._persist_stream_flag()
        self.emit_state()
        return self.status()

    def _stream_loop(self) -> None:
        try:
            while not self._stream_stop.is_set():
                t0 = time.monotonic()
                if not self._run_lock.acquire(timeout=1):
                    continue
                try:
                    self._activity = "stream_tick"
                    interval, speed = self.stream_cfg["interval_s"], self.stream_cfg["speed"]
                    win = Window(self.sim_clock, self.sim_clock + timedelta(seconds=interval * speed))
                    ctx = self._new_ctx("stream", conns=self._stream_conns)
                    ok = self._run(ctx, "stream_tick", f"Stream tick {speed:g}x", [win])["status"] == "ok"
                    self.ticks += 1
                except Exception as exc:  # noqa: BLE001 - infrastructure failure outside a window (lost connection)
                    self.error = f"Stream stopped: {type(exc).__name__}: {exc}"
                    self.hub.log(self.error, "error")
                    ok = False
                finally:
                    self._activity = None
                    self._run_lock.release()
                if not ok:
                    break
                self._stream_stop.wait(max(0.0, interval - (time.monotonic() - t0)))
        finally:
            for conn in self._stream_conns.values():
                conn.close()
            self._stream_conns.clear()
            if self._world_dirty and self._run_lock.acquire(timeout=60):
                try:
                    self._checkpoint()
                finally:
                    self._run_lock.release()
            self._stream_stop.set()
            if self.error:
                self._persist_stream_flag()
                self.emit_state()

    # ----------------------------------------------------------------- build
    def start_build(self, preset: str, seed: int | None = None) -> int:
        presets = self.rules.get("size_presets", {})
        if preset not in presets:
            raise ValueError(f"preset must be one of {sorted(presets)}")
        if self._activity == "building":
            raise EngineBusy("A build is already running.")
        self.stop_stream()
        if not self._run_lock.acquire(timeout=self.lock_wait_s):
            raise EngineBusy(f"The simulator is busy ({self.state}).")
        try:
            seed = int(seed) if seed is not None else self.settings.default_seed
            run_id = self._start_run_log("build", f"Build {preset} (seed {seed})", None)
            self._activity = "building"
            self._cancel.clear()
            self._cancel_by_user = False
            self.build_progress = {"progress": 0.0, "eta_s": None, "phase": "starting"}
            self._build_thread = threading.Thread(target=self._build_thread_main, args=(run_id, preset, seed),
                                                  name="hl-build", daemon=True)
            self._build_thread.start()
        except BaseException:
            self._activity = None
            self._run_lock.release()
            raise
        self.emit_state()
        return run_id

    def cancel_build(self) -> dict[str, Any]:
        """Stop the running build at its next checkpoint. The data it was replacing went when it started."""
        if self._activity != "building":
            raise EngineBusy("No build is running.")
        self._cancel_by_user = True
        self._cancel.set()
        self.hub.log("Cancelling the build at the end of the current step")
        progress = self.build_progress or {"progress": 0.0}
        self.build_progress = {**progress, "eta_s": None, "phase": "cancelling"}
        self.hub.publish("progress", {"phase": "cancelling", "pct": progress["progress"], "eta_s": None})
        return self.status()

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise RunAborted("build cancelled")

    def _progress(self, run_id: int, phase: str, pct: float, eta_s: float | None) -> None:
        if self._cancel.is_set():
            phase = "cancelling"
        self.build_progress = {"progress": round(pct, 4), "eta_s": None if eta_s is None else round(eta_s), "phase": phase}
        self.hub.publish("progress", {"run_id": run_id, "phase": phase, "pct": round(pct, 4),
                                      "eta_s": None if eta_s is None else round(eta_s)})

    def _build_thread_main(self, run_id: int, preset: str, seed: int) -> None:
        started = utcnow()
        measured = ChangeCounter()
        status, error = "ok", None
        try:
            self._build(run_id, preset, seed, measured)
            self.error = None
        except RunAborted:
            if self._cancel_by_user:
                status, error = "cancelled", "Cancelled"
                self.error = None     # the person's own choice, not a fault: the run log and the missing stamp say it
            else:
                status, error = "error", "interrupted by a restart"
                self.error = "The last build did not finish. Run Build again."
            self.hub.log(self.error or "Build cancelled. The data is incomplete.", "warn")
            self._record_unfinished()
        except LandingBlocked as exc:
            status, error = "error", str(exc)
            self.error = str(exc)
            self.hub.log(self.error, "error")
        except Exception as exc:  # noqa: BLE001 - a failed build is reported, never raised in the thread
            status, error = "error", f"{type(exc).__name__}: {exc}"
            self.error = f"Build failed: {error}"
            self.hub.log(self.error, "error")
            log.error("build failed\n%s", traceback.format_exc())
            self._record_unfinished()
        finally:
            ctx, self._build_ctx = self._build_ctx, None
            reported = ctx.changes if ctx else ChangeCounter()
            if ctx is not None:
                ctx.close()
            try:
                self._finish_run(run_id, "build", f"Build {preset} (seed {seed})", started, self.history_start,
                                 self.sim_clock, status, error, merge_changes(reported, measured), [])
            finally:
                self._activity = None
                self.build_progress = None
                self._run_lock.release()
                self._landing.expire()
                self.emit_state()

    def _record_unfinished(self) -> None:
        self.built_at = None
        try:
            with self.db.connect("sim_internal") as conn:
                self._write_state(conn)
                conn.commit()
        except psycopg.Error:
            log.exception("could not record the unfinished build")

    def _build(self, run_id: int, preset: str, seed: int, measured: ChangeCounter) -> None:
        t_start = time.monotonic()
        self._check_landing_is_writable()
        self._progress(run_id, "wipe", 0.0, None)
        # built_at is the only proof of a finished build, so it goes before the data does: a crash at any point from
        # here on leaves "not built", never a finished stamp on wiped or half-written data.
        self.preset, self.seed, self.built_at, self.outdated_data = preset, seed, None, False
        with self.db.connect("sim_internal") as conn:
            self._write_state(conn)
            conn.commit()
        self.hub.log(f"Build started: preset {preset}, seed {seed}. Wiping source data")
        self.db.wipe_source_data()
        self._check_cancel()
        self._clear_landing()
        self._check_cancel()
        self.settings.anomalies_csv.unlink(missing_ok=True)
        self._type_cache.clear()

        rules = load_yaml(self.settings.rules_path)
        self.planted = load_yaml(self.settings.planted_path) if self.settings.planted_path.exists() else {}
        months = int(rules["size_presets"][preset]["months"])
        end_day = date.fromisoformat(self.settings.history_end) if self.settings.history_end else utcnow().date()
        self.history_end = utc_midnight(end_day)
        self.history_start = add_months(self.history_end, -months)
        self.sim_clock = self.history_start
        self.world = {"dynamic_clients": []}
        self._world_blob = None
        self.rules = rules

        self._progress(run_id, "signals", 0.01, None)
        signals = Signals(rules, offline=self.settings.offline)
        with self.db.connect("sim_internal") as conn:
            sources = signals.prepare(self.history_start.date() - timedelta(days=45), self.history_end.date(), conn)
            self._write_state(conn, clear_world=True)
            conn.commit()
        self.signals = signals
        fx_src = sorted({v for k, v in sources.items() if k.startswith("fx:")})
        wx_src = sorted({v for k, v in sources.items() if k.startswith("weather:")}) or ["synthetic"]
        self.hub.log(f"Signals ready: FX from {'/'.join(fx_src)}, weather from {'/'.join(wx_src)}")

        modules = self._modules()
        if self.missing_modules:
            self.hub.log(f"Modules not available (skipped): {', '.join(self.missing_modules)}", "warn")
        ctx = self._build_ctx = self._new_ctx("build")
        self._check_cancel()

        self._progress(run_id, "bootstrap", 0.02, None)
        ctx.begin_window(Window(self.history_start, self.history_start))
        for module in modules:
            module.bootstrap(ctx)
        self._commit_build_window(ctx, measured)

        windows = day_windows(self.history_start, self.history_end)
        t_sim, last_emit = time.monotonic(), 0.0
        for i, win in enumerate(windows):
            self._check_cancel()
            ctx.begin_window(win)
            for module in modules:
                module.step(ctx, win)
            self._commit_build_window(ctx, measured)
            self.sim_clock = win.end
            done = i + 1
            now = time.monotonic()
            if now - last_emit >= 0.5 or done == len(windows):
                last_emit = now
                per_window = (now - t_sim) / done
                eta = per_window * (len(windows) - done) * 1.08
                self._progress(run_id, "simulate", 0.03 + 0.87 * done / len(windows), eta)
            if win.end.day == 1:
                self.hub.log(f"Simulated through {win.start.date():%b %Y} ({done}/{len(windows)} days)")

        self._check_cancel()
        self._progress(run_id, "indexes", 0.9, None)
        self.hub.log("Creating secondary indexes")
        self.db.create_secondary_indexes(lambda d, t: self._progress(run_id, "indexes", 0.9 + 0.08 * d / max(t, 1), None))
        self.db.analyze()
        self._check_cancel()

        self._progress(run_id, "finalize", 0.99, 0)
        self._require_every_system_has_data(modules)
        self.sim_clock = self.history_end
        self.built_at = utcnow()
        self._world_blob = pickle.dumps(self.world, protocol=pickle.HIGHEST_PROTOCOL)
        with self.db.connect("sim_internal") as conn:
            self._write_state(conn, world_blob=self._world_blob)
            conn.commit()
        self._world_dirty = False
        rows = sum(v[0] for v in measured.rows.values())
        minutes = (time.monotonic() - t_start) / 60
        self.hub.log(f"Build finished in {minutes:.1f} min: {rows:,} rows inserted, world {len(self._world_blob) / 1e6:.1f} MB")

    def _require_every_system_has_data(self, modules: list[Module]) -> None:
        """The last check before the finished stamp: a build that left a source system empty did not finish."""
        if self.missing_modules:
            raise RuntimeError(f"Modules did not load: {', '.join(self.missing_modules)}")
        empty = []
        for module in modules:
            db, table = BUILD_PROOF.get(getattr(module, "name", ""), (None, None))
            if db is None:
                continue
            with self.db.connect(db) as conn:
                if not conn.execute(sql.SQL("SELECT EXISTS (SELECT 1 FROM {})").format(sql.SQL(table))).fetchone()[0]:
                    empty.append(table)
        if empty:
            raise RuntimeError(f"The build finished with empty tables: {', '.join(empty)}")

    @staticmethod
    def _commit_build_window(ctx: EngineCtx, measured: ChangeCounter) -> None:
        win_measured, anomalies = ctx.flush_window()
        ctx.commit()
        ctx.dq.mirror(anomalies)
        for (system, table), (ins, upd, dele) in win_measured.rows.items():
            measured.add(system, table, ins, upd, dele)

    def _check_landing_is_writable(self) -> None:
        """Before anything is wiped: a build that cannot write its files must fail while the old data still exists."""
        landing = self.settings.landing_dir
        probe = landing / f".hl-write-check-{os.getpid()}"
        try:
            landing.mkdir(parents=True, exist_ok=True)
            probe.mkdir(exist_ok=True)
            (probe / "probe").write_bytes(b"ok")
            (probe / "probe").unlink()
            probe.rmdir()
        except OSError as exc:
            raise LandingBlocked(f"{LANDING_BLOCKED} ({exc}). Nothing was wiped.") from exc

    def _clear_landing(self) -> None:
        landing = self.settings.landing_dir
        landing.mkdir(parents=True, exist_ok=True)
        if landing.name != "landing":
            log.warning("Not clearing %s: the landing folder must be named 'landing'", landing)
            return
        for child in landing.iterdir():
            if child.name.startswith("."):
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        self._landing.expire()


_engine: Engine | None = None
_engine_lock = threading.Lock()


def get_engine() -> Engine:
    """The process-wide engine (other API routers use it for the sim clock: ``get_engine().sim_clock``)."""
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = Engine()
        return _engine


def reset_engine() -> None:
    global _engine
    with _engine_lock:
        _engine = None
