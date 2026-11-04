"""Runtime accessors shared by the apis-tracker modules (Salesforce mock, carrier mock, tracker).

Connections are borrowed from the engine's pools (``harborline.db.get_db()``) and switched to autocommit
for the duration of the borrow; use ``with conn.transaction():`` for multi-statement writes. Paths and
credentials come from ``harborline.settings``. ``configure()`` lets tests inject a connection factory.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from harborline.db import get_db
from harborline.settings import Settings, get_settings

ConnFactory = Callable[[str], AbstractContextManager[psycopg.Connection]]

_factory: ConnFactory | None = None


def env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def settings() -> Settings:
    return get_settings()


def configure(factory: ConnFactory | None) -> None:
    """Install a connection factory ``factory(dbname) -> context manager yielding a Connection`` (None resets)."""
    global _factory
    _factory = factory


@contextmanager
def connection(dbname: str) -> Iterator[psycopg.Connection]:
    """Autocommit connection to ``dbname``."""
    if _factory is not None:
        with _factory(dbname) as conn:
            yield conn
        return
    with get_db().pool(dbname).connection() as conn:
        conn.autocommit = True
        try:
            yield conn
        finally:
            try:
                conn.autocommit = False  # hand it back to the pool the way the engine expects it
            except psycopg.Error:
                pass


def sim_now(conn: psycopg.Connection) -> datetime:
    """Simulator clock (end of the last simulated window, UTC). Falls back to wall clock before the first build."""
    row = conn.execute("SELECT sim_clock FROM sim.state WHERE id = 1").fetchone()
    if row and row[0] is not None:
        return row[0].astimezone(UTC)
    return datetime.now(UTC)


def tracker_dir() -> Path:
    """Folder holding tickets/*.yaml (and dashboards/)."""
    return settings().tracker_dir


def brand_dir() -> Path:
    return settings().brand_dir


def config_dir() -> Path:
    return settings().config_dir
