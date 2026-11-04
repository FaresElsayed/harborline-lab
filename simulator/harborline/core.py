"""Engine contracts shared by every simulator module.

The engine advances a simulated clock in windows. For every window it calls each module's
``step`` in a fixed order (see ``MODULE_ORDER``), all inside one database transaction per
database, then commits. Modules never call each other: they communicate through ``ctx.bus``
(per-window DataFrames, contract in the build spec) and through the databases themselves.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

import numpy as np
import pandas as pd

Mode = Literal["build", "batch", "stream", "scenario"]
System = Literal["wms", "ax2012", "iot", "salesforce", "carrier", "edi", "files"]

MODULE_ORDER = ("wms", "iot", "carrier", "crm", "ax", "edi", "files")


@dataclass(frozen=True)
class Window:
    """Half-open simulated interval [start, end) in UTC (tz-aware)."""

    start: datetime
    end: datetime

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600

    def crosses_local_time(self, tz: str, hh: int, mm: int = 0) -> list[datetime]:
        """UTC instants inside the window at which local clock time hh:mm occurs in zone ``tz``."""
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(tz)
        out: list[datetime] = []
        day = self.start.astimezone(zone).date() - timedelta(days=1)
        last = self.end.astimezone(zone).date()
        while day <= last:
            local = datetime(day.year, day.month, day.day, hh, mm, tzinfo=zone)
            utc = local.astimezone(self.start.tzinfo)
            if self.start <= utc < self.end:
                out.append(utc)
            day += timedelta(days=1)
        return out


@dataclass
class ChangeCounter:
    """Accumulates inserted / updated / deleted row counts per (system, table) for the current run."""

    rows: dict[tuple[str, str], list[int]] = field(default_factory=dict)

    def add(self, system: System, table: str, ins: int = 0, upd: int = 0, dele: int = 0) -> None:
        cur = self.rows.setdefault((system, table), [0, 0, 0])
        cur[0] += int(ins)
        cur[1] += int(upd)
        cur[2] += int(dele)

    def as_list(self) -> list[dict[str, Any]]:
        return [
            {"system": s, "table": t, "ins": v[0], "upd": v[1], "del": v[2]}
            for (s, t), v in sorted(self.rows.items())
        ]


class DataQuality(Protocol):
    """Anomaly injector. Implemented in harborline/sim/anomalies.py."""

    denominator: int

    def enabled(self, category: str) -> bool: ...

    def mask(self, category: str, n: int, rng: np.random.Generator) -> np.ndarray:
        """Boolean array of length n; True where a defect of ``category`` should be injected."""

    def hit(self, category: str, rng: np.random.Generator) -> bool: ...

    def log(
        self,
        category: str,
        system: System,
        obj: str,
        record_key: str | None,
        column: str | None = None,
        before: Any = None,
        after: Any = None,
        note: str | None = None,
        sim_ts: datetime | None = None,
    ) -> None: ...


class Bus:
    """Per-window hand-off of DataFrames between modules (contract: build spec, section 'Bus')."""

    def __init__(self) -> None:
        self._frames: dict[str, list[pd.DataFrame]] = {}

    def publish(self, key: str, df: pd.DataFrame) -> None:
        if df is not None and len(df):
            self._frames.setdefault(key, []).append(df)

    def get(self, key: str) -> pd.DataFrame:
        frames = self._frames.get(key)
        if not frames:
            return pd.DataFrame()
        return frames[0] if len(frames) == 1 else pd.concat(frames, ignore_index=True)

    def clear(self) -> None:
        self._frames.clear()


class Ctx(Protocol):
    """Everything a module needs. Implemented by the engine (harborline/sim/engine.py)."""

    mode: Mode
    seed: int
    preset: str
    rules: dict[str, Any]          # parsed config/business_rules.yaml
    planted: dict[str, Any]        # parsed answer_key/planted_stories.yaml
    history_start: datetime
    history_end: datetime          # end of the initial build; batches/streams continue after it
    world: dict[str, Any]          # checkpointed module state; each module owns world[module_name]
    bus: Bus
    changes: ChangeCounter
    dq: DataQuality
    signals: Any                   # harborline/sim/signals.py: fx(), weather(), is_holiday(), diesel()
    landing_dir: str               # absolute path of the landing folder

    def conn(self, db: Literal["wms", "ax2012", "iot", "sim_internal"]) -> Any:
        """Open psycopg connection for this run (inside the run's transaction)."""

    def copy_rows(self, db: str, table: str, df: pd.DataFrame) -> int:
        """Bulk insert a DataFrame with COPY (columns = df columns). Returns row count."""

    def next_ids(self, sequence: str, n: int) -> np.ndarray:
        """Allocate n consecutive integer ids for a named sequence (persisted in the world)."""

    def rng(self, module: str, win: Window, salt: str = "") -> np.random.Generator:
        """Deterministic generator for (seed, module, window start, salt)."""


class Module(Protocol):
    name: str

    def bootstrap(self, ctx: Ctx) -> None:
        """Create reference / master data at the start of a build (history_start)."""

    def step(self, ctx: Ctx, win: Window) -> None:
        """Simulate everything that happens in ``win`` for this system."""


class ScenarioHandler(Protocol):
    def __call__(self, ctx: Ctx, params: dict[str, Any]) -> str:
        """Apply a one-shot scenario. Returns a short human-readable summary."""


def derive_seed(*parts: Any) -> int:
    h = hashlib.blake2b("|".join(str(p) for p in parts).encode(), digest_size=8)
    return int.from_bytes(h.digest(), "big")
