"""Load the ticket catalog into the ``tracker`` database.

Keys are ``DATA-<n>`` in file order (epic, then each story followed by its tasks). Re-loading is
idempotent and never touches his work: status, assignee, sprint moves, ranks, comments, work log,
history and checked acceptance criteria are preserved, and a ticket that got a new id in a release keeps
its work (moves.py). While he has not started working (no history, comments, work log or own tickets) a
changed catalog is simply re-numbered in file order; afterwards existing keys stay stable and new catalog
tickets get the next free numbers.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from difflib import SequenceMatcher
from functools import cache
from pathlib import Path
from typing import Any

import psycopg
import yaml

from harborline.tracker import runtime, workdays
from harborline.tracker.catalog import CatalogIssue, CatalogReport, read_catalog, signature
from harborline.tracker.moves import follow_moves, has_work

DDL_PATH = Path(__file__).with_name("tracker.sql")
ME = {"key": "me", "name": "You", "title": "Data Engineer", "dept": "IT"}
RANK_STEP = 1000


class CatalogInvalid(Exception):
    def __init__(self, report: CatalogReport):
        super().__init__(f"ticket catalog has {len(report.errors)} error(s): " + "; ".join(report.errors[:5]))
        self.report = report


@dataclass
class SyncResult:
    inserted: int = 0
    updated: int = 0
    removed: int = 0
    kept_removed: int = 0
    renumbered: bool = False

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def ensure_schema(conn: psycopg.Connection) -> None:
    conn.execute(DDL_PATH.read_text(encoding="utf-8"))


@cache
def _people_cached(path: str) -> tuple[dict[str, str], ...]:
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return tuple({"key": p["key"], "name": p["name"], "title": p.get("title", ""), "dept": p.get("dept", "")}
                 for p in doc.get("people", []))


def people(config_dir: Path | None = None) -> list[dict[str, str]]:
    path = (config_dir or runtime.config_dir()) / "business_rules.yaml"
    return [*_people_cached(str(path)), ME]


def get_setting(conn: psycopg.Connection, key: str) -> Any:
    row = conn.execute("SELECT value FROM tracker.settings WHERE key = %s", [key]).fetchone()
    return row[0] if row else None


def put_setting(conn: psycopg.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO tracker.settings (key, value, updated_at) VALUES (%s, %s, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        [key, json.dumps(value)],
    )


def start_date(conn: psycopg.Connection) -> date | None:
    value = get_setting(conn, "start_date")
    return date.fromisoformat(value) if value else None


def has_user_state(conn: psycopg.Connection) -> bool:
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM tracker.history) OR EXISTS (SELECT 1 FROM tracker.comment) "
        "OR EXISTS (SELECT 1 FROM tracker.worklog) OR EXISTS (SELECT 1 FROM tracker.ac_check WHERE checked) "
        "OR EXISTS (SELECT 1 FROM tracker.link WHERE origin = 'user') "
        "OR EXISTS (SELECT 1 FROM tracker.sprint WHERE state <> 'future') "
        "OR EXISTS (SELECT 1 FROM tracker.issue WHERE origin = 'user' OR status <> 'To Do' OR pr_url IS NOT NULL)"
    ).fetchone()
    return bool(row[0])


def _assign_numbers(conn: psycopg.Connection, issues: list[CatalogIssue], result: SyncResult) -> dict[str, int]:
    existing = dict(conn.execute("SELECT source_id, num FROM tracker.issue WHERE origin = 'catalog'").fetchall())
    desired = {issue.source_id: i + 1 for i, issue in enumerate(issues)}
    if existing == desired:
        return desired
    if not existing or not has_user_state(conn):
        if existing:
            conn.execute("DELETE FROM tracker.issue WHERE origin = 'catalog'")
            result.renumbered = True
        return desired
    next_num = int(conn.execute("SELECT coalesce(max(num), 0) + 1 FROM tracker.issue").fetchone()[0])
    out: dict[str, int] = {}
    for issue in issues:
        if issue.source_id in existing:
            out[issue.source_id] = existing[issue.source_id]
        else:
            out[issue.source_id] = next_num
            next_num += 1
    return out


def _dates(issue: CatalogIssue, start: date | None) -> tuple[date | None, date | None]:
    if start is None:
        return None, None
    return (workdays.workday(start, workdays.program_day(issue.project, issue.due_day)),
            workdays.workday(start, workdays.program_day(issue.project, issue.start_day)))


_CATALOG_COLUMNS = ("project", "type", "summary", "description", "priority", "reporter", "department", "epic_key",
                    "parent_key", "story_points", "estimate_hours", "due_day", "start_day", "due_date", "start_date",
                    "labels", "components", "attachments", "dashboards", "definition_of_done", "removed")


def _upsert_issues(conn: psycopg.Connection, issues: list[CatalogIssue], nums: dict[str, int], start: date | None,
                   result: SyncResult) -> None:
    keys = {sid: f"DATA-{n}" for sid, n in nums.items()}
    cols = ("key", "num", "source_id", "origin", "status", "assignee", "sprint_id", *_CATALOG_COLUMNS)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in _CATALOG_COLUMNS)
    changed = " OR ".join(f"tracker.issue.{c} IS DISTINCT FROM EXCLUDED.{c}" for c in _CATALOG_COLUMNS)
    sql = (f"INSERT INTO tracker.issue ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
           f"ON CONFLICT (key) DO UPDATE SET {updates}, updated_at = now() WHERE {changed} "
           "RETURNING (xmax = 0)")
    for issue in issues:
        due, begin = _dates(issue, start)
        row = conn.execute(sql, [
            keys[issue.source_id], nums[issue.source_id], issue.source_id, "catalog", "To Do",
            None if issue.type == "Epic" else "me", issue.sprint,
            issue.project, issue.type, issue.summary, issue.description.rstrip(), issue.priority, issue.reporter,
            issue.department, keys.get(issue.epic_source or ""), keys.get(issue.parent_source or ""),
            _num(issue.story_points), _num(issue.estimate_hours), issue.due_day, issue.start_day, due, begin,
            issue.labels, issue.components, issue.attachments, issue.dashboards,
            json.dumps(issue.definition_of_done), False,
        ]).fetchone()
        if row is not None:
            if row[0]:
                result.inserted += 1
            else:
                result.updated += 1


def _num(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


Tick = tuple[bool, datetime | None]
_WORD = re.compile(r"\w+")
_SAME_BOX = 0.6


def _likeness(a: str, b: str) -> float:
    """How alike two boxes read: 1.0 when the shorter one's words are all in the longer one."""
    a, b = a.lower(), b.lower()
    words_a, words_b = set(_WORD.findall(a)), set(_WORD.findall(b))
    shared = len(words_a & words_b) / (min(len(words_a), len(words_b)) or 1)
    return max(shared, SequenceMatcher(None, a, b, autojunk=False).ratio())


def carry_ticks(old: list[tuple[str, bool, datetime | None]], new: list[str], done: bool = False) -> list[Tick]:
    """What each new box inherits when a release rewords them.

    The same words keep their tick. Then each leftover box takes the tick of the closest rewording, and a last pair
    left over is the same box said differently. A genuinely new requirement starts unticked, except on a ticket he
    finished with every box ticked: finished stays finished.
    """
    ticks: list[Tick | None] = [None] * len(new)
    left = list(old)
    for i, text in enumerate(new):
        hit = next((o for o in left if o[0] == text), None)
        if hit is not None:
            left.remove(hit)
            ticks[i] = hit[1], hit[2]
    open_new = [i for i, tick in enumerate(ticks) if tick is None]
    pairs = sorted(((_likeness(o[0], new[i]), n, i) for n, o in enumerate(left) for i in open_new),
                   key=lambda p: (-p[0], p[1], p[2]))
    used_old: set[int] = set()
    for score, n, i in pairs:
        if score < _SAME_BOX:
            break
        if n not in used_old and ticks[i] is None:
            used_old.add(n)
            ticks[i] = left[n][1], left[n][2]
    rest_old = [o for n, o in enumerate(left) if n not in used_old]
    rest_new = [i for i in open_new if ticks[i] is None]
    if len(rest_old) == 1 and len(rest_new) == 1:
        ticks[rest_new[0]] = rest_old[0][1], rest_old[0][2]
    if done and old and all(o[1] for o in old):
        stamps = [o[2] for o in old if o[2] is not None]
        latest = max(stamps) if stamps else None
        return [tick if tick is not None and tick[0] else (True, latest) for tick in ticks]
    return [tick if tick is not None else (False, None) for tick in ticks]


def _sync_acceptance_criteria(conn: psycopg.Connection, issues: list[CatalogIssue], keys: dict[str, str]) -> None:
    current: dict[str, list[tuple[str, bool, datetime | None]]] = {}
    done = {key for (key,) in conn.execute("SELECT key FROM tracker.issue WHERE status = 'Done'")}
    for key, text, checked, checked_at in conn.execute(
        "SELECT issue_key, text, checked, checked_at FROM tracker.ac_check a JOIN tracker.issue i ON i.key = "
        "a.issue_key WHERE i.origin = 'catalog' ORDER BY issue_key, idx"
    ):
        current.setdefault(key, []).append((text, checked, checked_at))
    for issue in issues:
        key = keys[issue.source_id]
        old = current.get(key, [])
        if [t for t, _, _ in old] == issue.acceptance_criteria:
            continue
        ticks = carry_ticks(old, issue.acceptance_criteria, key in done)
        conn.execute("DELETE FROM tracker.ac_check WHERE issue_key = %s", [key])
        for idx, (text, (checked, checked_at)) in enumerate(zip(issue.acceptance_criteria, ticks, strict=True)):
            conn.execute("INSERT INTO tracker.ac_check (issue_key, idx, text, checked, checked_at) "
                         "VALUES (%s, %s, %s, %s, %s)", [key, idx, text, checked, checked_at])


def _sync_links(conn: psycopg.Connection, issues: list[CatalogIssue], keys: dict[str, str]) -> None:
    desired = {(keys[dep], keys[i.source_id]) for i in issues for dep in i.depends_on}
    current = set(conn.execute("SELECT from_key, to_key FROM tracker.link WHERE origin = 'catalog' "
                               "AND link_type = 'Blocks'").fetchall())
    for from_key, to_key in current - desired:
        conn.execute("DELETE FROM tracker.link WHERE from_key = %s AND to_key = %s AND origin = 'catalog'",
                     [from_key, to_key])
    for from_key, to_key in sorted(desired - current):
        conn.execute("INSERT INTO tracker.link (from_key, to_key, link_type, origin) VALUES (%s, %s, 'Blocks', "
                     "'catalog') ON CONFLICT DO NOTHING", [from_key, to_key])


def _sync_ranks(conn: psycopg.Connection, issues: list[CatalogIssue], keys: dict[str, str]) -> None:
    """Rank new tickets right after their predecessor in file order; existing ranks are his and stay."""
    ranks = {k: Decimal(r) for k, r in conn.execute("SELECT issue_key, rank FROM tracker.rank")}
    ordered = [keys[i.source_id] for i in issues]
    for pos, key in enumerate(ordered):
        if key in ranks:
            continue
        prev = next((ranks[k] for k in reversed(ordered[:pos]) if k in ranks), None)
        if prev is None:
            rank = min(ranks.values(), default=Decimal(2 * RANK_STEP)) - RANK_STEP
        else:
            higher = [r for r in ranks.values() if r > prev]
            rank = (prev + min(higher)) / 2 if higher else prev + RANK_STEP
        ranks[key] = rank
        conn.execute("INSERT INTO tracker.rank (issue_key, rank) VALUES (%s, %s)", [key, rank])


def _sync_removed(conn: psycopg.Connection, issues: list[CatalogIssue], result: SyncResult) -> None:
    wanted = {i.source_id for i in issues}
    rows = conn.execute("SELECT key, source_id FROM tracker.issue WHERE origin = 'catalog' AND NOT removed").fetchall()
    for key, source_id in rows:
        if source_id in wanted:
            continue
        if has_work(conn, key):
            conn.execute("UPDATE tracker.issue SET removed = true, updated_at = now() WHERE key = %s", [key])
            result.kept_removed += 1
        else:
            conn.execute("DELETE FROM tracker.issue WHERE key = %s", [key])
            result.removed += 1


def sprint_goal(issues: list[CatalogIssue], sprint: int) -> str | None:
    project = workdays.sprint_project(sprint)
    epic = next((i for i in issues if i.type == "Epic" and i.project == project), None)
    stories = [i.summary for i in issues if i.type == "Story" and i.sprint == sprint]
    if epic is None:
        return None
    goal = (f"{epic.summary}: " + "; ".join(stories)) if stories else epic.summary
    return goal if len(goal) <= 500 else goal[:497] + "..."


def sync_sprints(conn: psycopg.Connection, issues: list[CatalogIssue] | None, start: date | None) -> None:
    for sprint in range(1, workdays.SPRINTS + 1):
        first, last = workdays.sprint_dates(start, sprint) if start else (None, None)
        goal = sprint_goal(issues, sprint) if issues is not None else None
        conn.execute(
            "INSERT INTO tracker.sprint (id, name, project, goal, start_date, end_date) VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, project = EXCLUDED.project, "
            "goal = coalesce(EXCLUDED.goal, tracker.sprint.goal), start_date = EXCLUDED.start_date, "
            "end_date = EXCLUDED.end_date",
            [sprint, f"DATA Sprint {sprint}", workdays.sprint_project(sprint), goal, first, last],
        )


def sync_catalog(conn: psycopg.Connection, report: CatalogReport) -> SyncResult:
    """Apply a validated catalog. Runs in one transaction."""
    if not report.ok:
        raise CatalogInvalid(report)
    result = SyncResult()
    with conn.transaction():
        conn.execute("LOCK TABLE tracker.issue IN SHARE ROW EXCLUSIVE MODE")
        start = start_date(conn)
        sync_sprints(conn, report.issues, start)
        follow_moves(conn, report.issues)
        nums = _assign_numbers(conn, report.issues, result)
        keys = {sid: f"DATA-{n}" for sid, n in nums.items()}
        _sync_removed(conn, report.issues, result)
        _upsert_issues(conn, report.issues, nums, start, result)
        _sync_acceptance_criteria(conn, report.issues, keys)
        _sync_links(conn, report.issues, keys)
        _sync_ranks(conn, report.issues, keys)
        put_setting(conn, "catalog", {**report.summary(), "loaded_at": datetime.now(UTC).isoformat(),
                                      "result": result.as_dict()})
    return result


def load_catalog(conn: psycopg.Connection, tickets_dir: Path | None = None, config_dir: Path | None = None,
                 brand_dir: Path | None = None) -> tuple[CatalogReport, SyncResult]:
    """Validate ``tickets_dir`` and sync it. Invalid catalogs are recorded in settings and raise CatalogInvalid."""
    tickets = tickets_dir or runtime.tracker_dir() / "tickets"
    keys = {p["key"] for p in people(config_dir)}
    report = read_catalog(tickets, keys, brand_dir or runtime.brand_dir())
    if not report.ok:
        put_setting(conn, "catalog_errors", {**report.summary(), "checked_at": datetime.now(UTC).isoformat()})
        raise CatalogInvalid(report)
    conn.execute("DELETE FROM tracker.settings WHERE key = 'catalog_errors'")
    result = sync_catalog(conn, report)
    put_setting(conn, "catalog_signature", signature(tickets))
    return report, result


def ensure_loaded(conn: psycopg.Connection, tickets_dir: Path | None = None) -> None:
    """Load the catalog when it was never loaded or the files changed since (cheap stat check)."""
    tickets = tickets_dir or runtime.tracker_dir() / "tickets"
    if not tickets.is_dir():
        return
    if get_setting(conn, "catalog_signature") == signature(tickets):
        return
    try:
        load_catalog(conn, tickets)
    except CatalogInvalid:
        put_setting(conn, "catalog_signature", signature(tickets))  # don't re-validate until the files change


def apply_start_date(conn: psycopg.Connection, start: date) -> None:
    """Set the program start date and recompute every catalog due/start date and sprint date."""
    with conn.transaction():
        put_setting(conn, "start_date", start.isoformat())
        sync_sprints(conn, None, start)
        rows = conn.execute("SELECT key, project, due_day, start_day FROM tracker.issue "
                            "WHERE origin = 'catalog' AND due_day IS NOT NULL").fetchall()
        for key, project, due_day, start_day in rows:
            due = workdays.workday(start, workdays.program_day(project, due_day))
            begin = workdays.workday(start, workdays.program_day(project, start_day or due_day))
            conn.execute("UPDATE tracker.issue SET due_date = %s, start_date = %s WHERE key = %s", [due, begin, key])
