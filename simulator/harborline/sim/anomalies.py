"""Data-quality defect injection (implements ``core.DataQuality``).

A module asks ``ctx.dq.mask(category, n, rng)`` for the rows to corrupt, corrupts them, then calls ``ctx.dq.log``
once per defect. Logged defects are written to ``sim.anomaly_log`` inside the window's transaction and mirrored to
``answer_key/anomalies.csv`` after the commit. The rate is 1 / denominator per category per eligible record.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import psycopg

from ..core import System

CSV_COLUMNS = (
    "anomaly_id", "injected_at", "sim_ts", "category", "system", "object", "record_key",
    "column_name", "before_value", "after_value", "note",
)
MIN_DENOMINATOR, MAX_DENOMINATOR = 100, 100_000


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and np.isnan(value):
        return None
    return str(value)


class DataQualityInjector:
    def __init__(self, all_categories: list[str], denominator: int = 10_000, categories: dict[str, bool] | None = None,
                 csv_path: Path | None = None) -> None:
        self.all_categories = list(all_categories)
        self.denominator = denominator
        self.categories = {c: True for c in self.all_categories}
        self.csv_path = csv_path
        self.sim_ts: datetime | None = None       # set by the engine to the current window start
        self._pending: list[dict[str, Any]] = []
        self.update(denominator, categories or {})

    # ----------------------------------------------------------------- config
    def update(self, denominator: int | None = None, categories: dict[str, bool] | None = None) -> None:
        if denominator is not None:
            self.denominator = int(min(MAX_DENOMINATOR, max(MIN_DENOMINATOR, int(denominator))))
        for name, on in (categories or {}).items():
            if name in self.categories:
                self.categories[name] = bool(on)

    def config(self) -> dict[str, Any]:
        return {"denominator": self.denominator, "categories": dict(self.categories)}

    # ----------------------------------------------------------------- DataQuality protocol
    def enabled(self, category: str) -> bool:
        return self.categories.get(category, True)

    def mask(self, category: str, n: int, rng: np.random.Generator) -> np.ndarray:
        # Always draw, so toggling a category never shifts the module's random stream.
        draws = rng.random(int(n))
        if not self.enabled(category):
            return np.zeros(int(n), dtype=bool)
        return draws < 1.0 / self.denominator

    def hit(self, category: str, rng: np.random.Generator) -> bool:
        return bool(self.mask(category, 1, rng)[0])

    def log(self, category: str, system: System, obj: str, record_key: str | None, column: str | None = None,
            before: Any = None, after: Any = None, note: str | None = None, sim_ts: datetime | None = None) -> None:
        self._pending.append({
            "sim_ts": sim_ts or self.sim_ts, "category": category, "system": system, "object": obj,
            "record_key": _text(record_key), "column_name": column, "before_value": _text(before),
            "after_value": _text(after), "note": note,
        })

    # ----------------------------------------------------------------- engine side
    @property
    def pending(self) -> int:
        return len(self._pending)

    def discard(self) -> None:
        self._pending.clear()

    def flush(self, conn: psycopg.Connection) -> list[dict[str, Any]]:
        """Write pending defects into sim.anomaly_log (caller commits). Returns the rows with their ids."""
        if not self._pending:
            return []
        rows, self._pending = self._pending, []
        ids = conn.execute(
            "SELECT nextval(pg_get_serial_sequence('sim.anomaly_log', 'anomaly_id')) FROM generate_series(1, %s)",
            (len(rows),),
        ).fetchall()
        injected = datetime.now(UTC)
        cols = CSV_COLUMNS
        with conn.cursor() as cur, cur.copy(f"COPY sim.anomaly_log ({', '.join(cols)}) FROM STDIN") as cp:
            for (aid,), row in zip(ids, rows, strict=True):
                row["anomaly_id"], row["injected_at"] = aid, injected
                cp.write_row([row[c] for c in cols])
        return rows

    def mirror(self, rows: list[dict[str, Any]]) -> None:
        """Append committed defects to answer_key/anomalies.csv (header written once)."""
        if not rows or self.csv_path is None:
            return
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.csv_path.exists() or self.csv_path.stat().st_size == 0
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        if new:
            writer.writerow(CSV_COLUMNS)
        for row in rows:
            writer.writerow([_csv_value(row[c]) for c in CSV_COLUMNS])
        with self.csv_path.open("a", encoding="utf-8", newline="") as fh:
            fh.write(buf.getvalue())


def _csv_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return "" if value is None else value
