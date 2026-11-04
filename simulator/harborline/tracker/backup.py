"""JSON backup / restore of the whole tracker database (his work included)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

FORMAT = "harborline-tracker-backup"
VERSION = 1
# Insert order respects foreign keys.
TABLES = ("settings", "sprint", "issue", "rank", "ac_check", "comment", "worklog", "history", "link")
_SERIAL = {"comment": "id", "worklog": "id", "history": "id", "link": "id"}


def backup(conn: psycopg.Connection) -> dict[str, Any]:
    tables = {}
    for table in TABLES:
        row = conn.execute(f"SELECT coalesce(jsonb_agg(to_jsonb(t)), '[]'::jsonb) FROM tracker.{table} t").fetchone()
        tables[table] = row[0]
    return {"format": FORMAT, "version": VERSION, "exported_at": datetime.now(UTC).isoformat(), "tables": tables}


def restore(conn: psycopg.Connection, doc: Any) -> dict[str, int]:
    """Replace all tracker data with the backup. Raises ValueError on a malformed document."""
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise ValueError(f"not a {FORMAT} document")
    if doc.get("version") != VERSION:
        raise ValueError(f"unsupported backup version {doc.get('version')!r} (expected {VERSION})")
    tables = doc.get("tables")
    if not isinstance(tables, dict) or not all(isinstance(tables.get(t, []), list) for t in TABLES):
        raise ValueError("backup 'tables' must map table names to lists of rows")
    counts: dict[str, int] = {}
    with conn.transaction():
        conn.execute("TRUNCATE " + ", ".join(f"tracker.{t}" for t in TABLES) + " RESTART IDENTITY CASCADE")
        for table in TABLES:
            rows = tables.get(table, [])
            if rows:
                conn.execute(f"INSERT INTO tracker.{table} SELECT * FROM jsonb_populate_recordset("
                             f"NULL::tracker.{table}, %s)", [Jsonb(rows)])
            counts[table] = len(rows)
        for table, col in _SERIAL.items():
            conn.execute(f"SELECT setval(pg_get_serial_sequence('tracker.{table}', '{col}'), "
                         f"coalesce((SELECT max({col}) FROM tracker.{table}), 0) + 1, false)")
    return counts
