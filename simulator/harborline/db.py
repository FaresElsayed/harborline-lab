"""Postgres access: connections, pools and the idempotent bootstrap.

Bootstrap (runs at app start and after a rebuild wipe):
  1. as superuser: make sure the roles and databases exist (same as ``db/init``), sync role passwords to .env;
  2. as ``harborline_app``: apply the DDL contracts (``schema/*.sql``, the tracker's ``tracker.sql``) and the
     engine's own additive migration. Every ``CREATE`` is made idempotent, so re-running is a no-op;
  3. as superuser: grants for ``etl_reader`` and ``uat_reviewer`` (Iris Novak's read-only login) and the CDC publications.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Literal

import psycopg
from psycopg import errors, sql
from psycopg.conninfo import make_conninfo
from psycopg_pool import ConnectionPool

from .settings import SCHEMA_DIR, TRACKER_SQL, Settings, get_settings

log = logging.getLogger("harborline.db")

Role = Literal["app", "super", "etl", "reviewer"]

DATABASES = ("wms", "ax2012", "iot", "tracker", "sim_internal")
SOURCE_DBS = ("wms", "ax2012", "iot", "sim_internal")
SEARCH_PATH = {
    "wms": "wms,public",
    "ax2012": "dbo,public",
    "iot": "telemetry,public",
    "sim_internal": "sim,sfdc,carrier,public",
    "tracker": "tracker,public",
    "postgres": "public",
}
SCHEMA_FILES = {"wms": "wms.sql", "ax2012": "ax2012.sql", "iot": "iot.sql", "sim_internal": "sim_internal.sql"}
# Schemas dropped by a rebuild. sim_internal keeps schema ``sim`` (state, run log, handbook progress, caches).
SOURCE_SCHEMAS = {"wms": ["wms"], "ax2012": ["dbo"], "iot": ["telemetry"], "sim_internal": ["sfdc", "carrier"]}
READER_DBS = ("wms", "ax2012", "iot", "tracker")
REVIEWER_DBS = ("wms", "ax2012", "iot")  # the source systems only: no tracker, never sim_internal
# Guard rails for the reviewer login: reconciliation queries must not hold up the simulator.
REVIEWER_ROLE_SETTINGS = {"default_transaction_read_only": "on", "statement_timeout": "5min",
                          "idle_in_transaction_session_timeout": "1min"}
PUBLICATIONS = {"wms": ("hl_wms_cdc", "wms"), "ax2012": ("hl_ax_cdc", "dbo"), "iot": ("hl_iot_cdc", "telemetry")}
SYSTEM_OF_SCHEMA = {"wms": "wms", "dbo": "ax2012", "telemetry": "iot", "carrier": "carrier"}

# Engine-owned additive migration for sim_internal (kept out of the shared contract file).
ENGINE_DDL = """
ALTER TABLE sim.state ADD COLUMN IF NOT EXISTS history_end timestamptz;
ALTER TABLE sim.state ADD COLUMN IF NOT EXISTS id_counters jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE sim.state ADD COLUMN IF NOT EXISTS stream jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE sim.state ADD COLUMN IF NOT EXISTS last_error text;
CREATE TABLE IF NOT EXISTS sim.signal_cache (
  kind        varchar(20) NOT NULL,
  key         varchar(40) NOT NULL,
  source      varchar(20) NOT NULL,
  payload     jsonb       NOT NULL,
  fetched_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (kind, key)
);
ALTER TABLE sim.run_log ADD COLUMN IF NOT EXISTS notes text;
ALTER TABLE sim.anomaly_log ALTER COLUMN object TYPE varchar(240);
CREATE INDEX IF NOT EXISTS run_log_started ON sim.run_log (started_at DESC);
"""

# Secondary indexes created after a build (a build loads faster without them). Skipped when a column is missing.
SECONDARY_INDEXES: dict[str, list[tuple[str, tuple[str, ...]]]] = {
    "wms": [
        ("order_header", ("owner_id",)), ("order_header", ("client_order_ref",)), ("order_header", ("received_ts",)),
        ("order_line", ("material_id",)),
        ("inventory", ("owner_id", "material_id")), ("inventory", ("location_id",)), ("inventory", ("lot_nbr",)),
        ("inventory_txn", ("txn_ts",)), ("inventory_txn", ("material_id",)),
        ("shipment", ("order_id",)), ("shipment", ("tracking_nbr",)), ("shipment", ("ship_ts",)),
        ("shipment_carton", ("shipment_id",)), ("shipment_carton", ("tracking_nbr",)),
        ("task", ("order_id",)), ("task", ("assigned_user_id", "completed_ts")), ("task", ("created_ts",)),
        ("asn_header", ("owner_id",)), ("receipt_header", ("asn_id",)), ("receipt_line", ("material_id",)),
        ("billing_record", ("owner_id", "activity_date")), ("billing_record", ("invoice_ref",)),
        ("billing_rate", ("owner_id", "charge_code")), ("material", ("upc",)),
        ("return_header", ("original_order_id",)), ("cycle_count", ("location_id",)), ("app_user", ("personnel_nbr",)),
    ],
    "ax2012": [
        ("custtable", ("accountnum",)), ("custtable", ("party",)), ("vendtable", ("accountnum",)),
        ("custinvoicejour", ("invoiceid",)), ("custinvoicetrans", ("invoiceid",)),
        ("custtrans", ("accountnum",)), ("custtrans", ("voucher",)), ("custsettlement", ("transrecid",)),
        ("vendtrans", ("accountnum",)), ("vendtrans", ("voucher",)),
        ("vendinvoicejour", ("invoiceid",)), ("vendinvoicetrans", ("invoiceid",)),
        ("generaljournalentry", ("accountingdate",)), ("generaljournalaccountentry", ("generaljournalentry",)),
        ("generaljournalaccountentry", ("ledgerdimension",)), ("ledgerjournaltrans", ("journalnum",)),
        ("dirpartylocation", ("party",)), ("logisticspostaladdress", ("location",)),
        ("exchangerate", ("exchangeratecurrencypair",)), ("hcmworker", ("personnelnumber",)), ("dirpersonname", ("person",)),
    ],
    "iot": [
        ("sensor_reading", ("sensor_id", "device_ts_ms")), ("sensor_reading", ("ingested_at",)),
        ("dock_door_event", ("warehouse_code", "device_ts_ms")), ("dock_door_event", ("ingested_at",)),
    ],
}
# Change-tracking columns indexed on every table that has them (incremental extraction).
AUTO_INDEX_COLUMNS = ("mod_ts", "modifieddatetime", "updated_at", "system_modstamp")
INDEX_SCHEMA = {"wms": "wms", "ax2012": "dbo", "iot": "telemetry"}

_IGNORABLE = (
    errors.DuplicateTable, errors.DuplicateObject, errors.DuplicateSchema, errors.DuplicateColumn,
    errors.DuplicateFunction, errors.UniqueViolation,
)
_IDEMPOTENT = [
    (re.compile(r"\bCREATE\s+TABLE\s+(?!IF\s+NOT\s+EXISTS)", re.IGNORECASE), "CREATE TABLE IF NOT EXISTS "),
    (re.compile(r"\bCREATE\s+SCHEMA\s+(?!IF\s+NOT\s+EXISTS)", re.IGNORECASE), "CREATE SCHEMA IF NOT EXISTS "),
    (re.compile(r"\bCREATE\s+SEQUENCE\s+(?!IF\s+NOT\s+EXISTS)", re.IGNORECASE), "CREATE SEQUENCE IF NOT EXISTS "),
    (re.compile(r"\bCREATE\s+(UNIQUE\s+)?INDEX\s+(?!IF\s+NOT\s+EXISTS|CONCURRENTLY|ON\s)", re.IGNORECASE), r"CREATE \1INDEX IF NOT EXISTS "),
    (re.compile(r"\bCREATE\s+(VIEW|FUNCTION|TRIGGER|PROCEDURE)\b", re.IGNORECASE), r"CREATE OR REPLACE \1"),
]


def idempotent_ddl(text: str) -> str:
    for pattern, repl in _IDEMPOTENT:
        text = pattern.sub(repl, text)
    return text


def split_sql(text: str) -> list[str]:
    """Split a SQL script on top-level semicolons (aware of quotes, comments and dollar quoting)."""
    out: list[str] = []
    buf: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "-" and text.startswith("--", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            buf.append(text[i:j])
            i = j
            continue
        if ch == "/" and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            buf.append(text[i:j])
            i = j
            continue
        if ch in ("'", '"'):
            j = i + 1
            while j < n and text[j] != ch:
                j += 1
            buf.append(text[i : j + 1])
            i = j + 1
            continue
        if ch == "$":
            m = re.match(r"\$[A-Za-z_]*\$", text[i:])
            if m:
                tag = m.group(0)
                j = text.find(tag, i + len(tag))
                j = n if j < 0 else j + len(tag)
                buf.append(text[i:j])
                i = j
                continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if _has_code(stmt):
                out.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    stmt = "".join(buf).strip()
    if _has_code(stmt):
        out.append(stmt)
    return out


def _has_code(stmt: str) -> bool:
    return any(line.strip() and not line.strip().startswith("--") for line in stmt.splitlines())


class Database:
    """Connections to the Harborline cluster. One instance per process (see ``get_db``)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._pools: dict[str, ConnectionPool] = {}
        self._lock = threading.Lock()

    # ----------------------------------------------------------------- connections
    def conninfo(self, db: str, role: Role = "app", extra_options: str = "") -> str:
        s = self.settings
        user, password = {
            "app": (s.app_user, s.app_password),
            "super": (s.pg_superuser, s.pg_superpassword),
            "etl": (s.etl_user, s.etl_password),
            "reviewer": (s.reviewer_user, s.reviewer_password),
        }[role]
        options = f"-c search_path={SEARCH_PATH.get(db, 'public')} -c TimeZone=UTC {extra_options}".strip()
        return make_conninfo(
            host=s.pg_host, port=s.pg_port, dbname=db, user=user, password=password,
            options=options, application_name=f"harborline-{role}", connect_timeout=10,
        )

    def connect(self, db: str, role: Role = "app", autocommit: bool = False, extra_options: str = "") -> psycopg.Connection:
        return psycopg.connect(self.conninfo(db, role, extra_options), autocommit=autocommit)

    def pool(self, db: str) -> ConnectionPool:
        """Shared pool (app role) for API handlers. ``with db.pool("tracker").connection() as conn: ...``"""
        with self._lock:
            p = self._pools.get(db)
            if p is None:
                p = ConnectionPool(self.conninfo(db), min_size=1, max_size=8, open=True, name=f"hl-{db}", timeout=15)
                self._pools[db] = p
            return p

    @contextmanager
    def cursor(self, db: str) -> Iterator[psycopg.Cursor]:
        with self.pool(db).connection() as conn, conn.cursor() as cur:
            yield cur

    def fetchall(self, db: str, query: str | sql.Composable, params: Any = None) -> list[tuple]:
        with self.cursor(db) as cur:
            cur.execute(query, params)
            return cur.fetchall() if cur.description else []

    def close(self) -> None:
        with self._lock:
            for p in self._pools.values():
                p.close()
            self._pools.clear()

    def ping(self) -> bool:
        try:
            with self.connect("sim_internal", autocommit=True) as conn:
                conn.execute("SELECT 1")
            return True
        except psycopg.Error:
            return False

    def wait_until_ready(self, timeout_s: float = 120) -> None:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                with self.connect("postgres", "super", autocommit=True) as conn:
                    conn.execute("SELECT 1")
                return
            except psycopg.OperationalError as exc:
                if time.monotonic() > deadline:
                    raise RuntimeError(f"Postgres not reachable at {self.settings.pg_host}:{self.settings.pg_port}") from exc
                time.sleep(1.5)

    # ----------------------------------------------------------------- bootstrap
    def bootstrap(self) -> None:
        """Idempotent: roles, databases, DDL, grants, publications."""
        self.wait_until_ready()
        self.ensure_cluster()
        self.apply_schemas()
        self.apply_grants_and_publications()

    def ensure_cluster(self) -> None:
        s = self.settings
        with self.connect("postgres", "super", autocommit=True) as conn:
            for role, password, extra in ((s.app_user, s.app_password, ""), (s.etl_user, s.etl_password, " REPLICATION")):
                exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
                verb = "ALTER" if exists else "CREATE"
                conn.execute(sql.SQL(verb + " ROLE {} LOGIN" + extra + " PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(password)))
            for name in DATABASES:
                if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
                    conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(name), sql.Identifier(s.app_user)))
            conn.execute("REVOKE CONNECT ON DATABASE sim_internal FROM PUBLIC")
            for name in READER_DBS:
                conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(name), sql.Identifier(s.etl_user)))
            self._ensure_reviewer(conn)

    def _ensure_reviewer(self, conn: psycopg.Connection) -> None:
        s = self.settings
        role = sql.Identifier(s.reviewer_user)
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (s.reviewer_user,)).fetchone()
        conn.execute(sql.SQL(("ALTER" if exists else "CREATE") + " ROLE {} LOGIN PASSWORD {}").format(
            role, sql.Literal(s.reviewer_password)))
        for key, value in REVIEWER_ROLE_SETTINGS.items():
            conn.execute(sql.SQL("ALTER ROLE {} SET {} = {}").format(role, sql.Identifier(key), sql.Literal(value)))
        for name in REVIEWER_DBS:
            conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(name), role))

    def apply_schemas(self) -> None:
        for db, file in SCHEMA_FILES.items():
            self.apply_sql(db, (SCHEMA_DIR / file).read_text(encoding="utf-8"))
        self.apply_sql("sim_internal", ENGINE_DDL)
        if TRACKER_SQL.exists():
            self.apply_sql("tracker", TRACKER_SQL.read_text(encoding="utf-8"))

    def apply_sql(self, db: str, text: str) -> None:
        """Run a DDL script as the app role, statement by statement, ignoring 'already exists' errors."""
        with self.connect(db) as conn:
            for stmt in split_sql(idempotent_ddl(text)):
                try:
                    with conn.transaction():
                        conn.execute(stmt)
                except _IGNORABLE:
                    pass
            conn.commit()

    def apply_grants_and_publications(self) -> None:
        s = self.settings
        owner = sql.Identifier(s.app_user)
        for db in READER_DBS:
            readers = [s.etl_user, *([s.reviewer_user] if db in REVIEWER_DBS else [])]
            with self.connect(db, "super", autocommit=True) as conn:
                for schema in self._user_schemas(conn):
                    ident = sql.Identifier(schema)
                    for reader in map(sql.Identifier, readers):
                        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(ident, reader))
                        conn.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(ident, reader))
                        conn.execute(sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} GRANT SELECT ON TABLES TO {}").format(
                            owner, ident, reader))
                if db in PUBLICATIONS:
                    self._ensure_publication(conn, *PUBLICATIONS[db])

    @staticmethod
    def _user_schemas(conn: psycopg.Connection) -> list[str]:
        rows = conn.execute(
            "SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg\\_%' AND nspname <> 'information_schema' "
            "AND (nspname <> 'public' OR EXISTS (SELECT 1 FROM pg_tables WHERE schemaname = 'public'))"
        ).fetchall()
        return [r[0] for r in rows]

    @staticmethod
    def _ensure_publication(conn: psycopg.Connection, pub: str, schema: str) -> None:
        exists = conn.execute("SELECT 1 FROM pg_publication WHERE pubname = %s", (pub,)).fetchone()
        if not exists:
            conn.execute(sql.SQL("CREATE PUBLICATION {} FOR TABLES IN SCHEMA {}").format(sql.Identifier(pub), sql.Identifier(schema)))
            return
        member = conn.execute(
            "SELECT 1 FROM pg_publication p JOIN pg_publication_namespace pn ON pn.pnpubid = p.oid "
            "JOIN pg_namespace n ON n.oid = pn.pnnspid WHERE p.pubname = %s AND n.nspname = %s",
            (pub, schema),
        ).fetchone()
        if not member:
            conn.execute(sql.SQL("ALTER PUBLICATION {} ADD TABLES IN SCHEMA {}").format(sql.Identifier(pub), sql.Identifier(schema)))

    # ----------------------------------------------------------------- rebuild support
    def wipe_source_data(self) -> None:
        """Drop all simulated source data (not the tracker, run log, handbook progress or signal cache), then re-bootstrap."""
        for db, schemas in SOURCE_SCHEMAS.items():
            with self.connect(db, "super", autocommit=True) as conn:
                for schema in schemas:
                    self._drop_schema(conn, db, schema)
                if db == "sim_internal":
                    conn.execute("TRUNCATE sim.anomaly_log RESTART IDENTITY")
        self.apply_schemas()
        self.apply_grants_and_publications()

    def _drop_schema(self, conn: psycopg.Connection, db: str, schema: str) -> None:
        stmt = sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
        conn.execute("SET lock_timeout = '15s'")
        try:
            conn.execute(stmt)
        except errors.LockNotAvailable:
            log.warning("%s.%s is locked by another session; terminating idle readers and retrying", db, schema)
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid() "
                "AND backend_type = 'client backend'",
                (db,),
            )
            conn.execute("SET lock_timeout = '60s'")
            conn.execute(stmt)

    def create_secondary_indexes(self, on_progress: Callable[[int, int], None] | None = None) -> int:
        """Create post-build indexes (parallel across databases). Returns the number of indexes created or present."""
        plans = {db: self._index_plan(db) for db in INDEX_SCHEMA}
        total = sum(len(p) for p in plans.values())
        done = 0
        lock = threading.Lock()

        def run(db: str) -> None:
            nonlocal done
            with self.connect(db, autocommit=True) as conn:
                conn.execute("SET maintenance_work_mem = '512MB'")
                for stmt in plans[db]:
                    conn.execute(stmt)
                    with lock:
                        done += 1
                        if on_progress:
                            on_progress(done, total)

        with ThreadPoolExecutor(max_workers=len(plans)) as pool:
            list(pool.map(run, plans))
        return total

    def _index_plan(self, db: str) -> list[sql.Composed]:
        schema = INDEX_SCHEMA[db]
        with self.connect(db, autocommit=True) as conn:
            cols: dict[str, set[str]] = {}
            for table, column in conn.execute(
                "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = %s", (schema,)
            ).fetchall():
                cols.setdefault(table, set()).add(column)
        wanted = list(SECONDARY_INDEXES.get(db, []))
        for table, columns in sorted(cols.items()):
            wanted += [(table, (c,)) for c in AUTO_INDEX_COLUMNS if c in columns]
        plan, seen = [], set()
        for table, columns in wanted:
            if table not in cols or not set(columns) <= cols[table] or (table, columns) in seen:
                continue
            seen.add((table, columns))
            name = f"hlx_{table}_{'_'.join(columns)}"[:63]
            plan.append(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {}.{} ({})").format(
                sql.Identifier(name), sql.Identifier(schema), sql.Identifier(table),
                sql.SQL(", ").join(sql.Identifier(c) for c in columns)))
        return plan

    def analyze(self) -> None:
        def run(db: str) -> None:
            # As the superuser: the app role logs a warning for every shared catalog, and those logs go into setup's reports.
            with self.connect(db, "super", autocommit=True) as conn:
                conn.execute("ANALYZE")

        with ThreadPoolExecutor(max_workers=len(SOURCE_DBS)) as pool:
            list(pool.map(run, SOURCE_DBS))

    # ----------------------------------------------------------------- stats
    def table_rows(self) -> list[dict[str, Any]]:
        """Live row counts per source table (statistics-based for big tables; exact per Salesforce object)."""
        out: list[dict[str, Any]] = []
        for db in ("wms", "ax2012", "iot", "sim_internal"):
            schemas = ("carrier",) if db == "sim_internal" else (INDEX_SCHEMA[db],)
            for schema, table, rows in self.fetchall(
                db,
                "SELECT schemaname, relname, n_live_tup FROM pg_stat_user_tables WHERE schemaname = ANY(%s) ORDER BY 1, 2",
                (list(schemas),),
            ):
                out.append({"system": SYSTEM_OF_SCHEMA[schema], "table": table, "rows": int(rows)})
        for obj, rows in self.fetchall(
            "sim_internal", "SELECT object, count(*) FROM sfdc.record WHERE NOT is_deleted GROUP BY object ORDER BY object"
        ):
            out.append({"system": "salesforce", "table": obj, "rows": int(rows)})
        return out

    def size_bytes(self) -> int:
        rows = self.fetchall("sim_internal", "SELECT coalesce(sum(pg_database_size(datname)), 0) FROM pg_database WHERE datname = ANY(%s)",
                             (list(DATABASES),))
        return int(rows[0][0]) if rows else 0


_db: Database | None = None
_db_lock = threading.Lock()


def get_db() -> Database:
    global _db
    with _db_lock:
        if _db is None:
            _db = Database(get_settings())
        return _db


def reset_db() -> None:
    global _db
    with _db_lock:
        if _db is not None:
            _db.close()
        _db = None
