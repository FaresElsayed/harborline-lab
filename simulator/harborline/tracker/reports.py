"""Agile reports rebuilt from ``tracker.history``: sprint burndown, velocity and cumulative flow.

The value of a field at instant t is the ``from_value`` of the first change after t, or the current value
when nothing changed after t. Hours = original estimates of Tasks/Bugs/Spikes; points = story points.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row

from harborline.tracker import workdays
from harborline.tracker.jql import STATUSES
from harborline.tracker.service import TrackerError

MAX_CFD_DAYS = 366


@dataclass
class _Issue:
    key: str
    type: str
    status: str
    sprint_id: int | None
    epic_key: str | None
    points: float
    hours: float
    created_at: datetime
    changes: dict[str, list[tuple[datetime, str | None]]] = field(default_factory=dict)

    def value_at(self, name: str, t: datetime) -> str | None:
        for at, before in self.changes.get(name, []):
            if at > t:
                return before
        current = self.status if name == "status" else self.sprint_id
        return None if current is None else str(current)

    def in_sprint(self, sprint: int, t: datetime) -> bool:
        return self.created_at <= t and self.value_at("sprint", t) == str(sprint)

    def done(self, t: datetime) -> bool:
        return self.value_at("status", t) == "Done"

    def metric(self, name: str) -> float:
        return self.points if name == "points" else self.hours


def _load(conn: psycopg.Connection) -> list[_Issue]:
    issues: dict[str, _Issue] = {}
    with conn.cursor(row_factory=dict_row) as cur:
        for r in cur.execute("SELECT key, type, status, sprint_id, epic_key, story_points, estimate_hours, created_at "
                             "FROM tracker.issue WHERE NOT removed AND type <> 'Epic'"):
            hours = float(r["estimate_hours"] or 0) if r["type"] in ("Task", "Bug", "Spike") else 0.0
            issues[r["key"]] = _Issue(r["key"], r["type"], r["status"], r["sprint_id"], r["epic_key"],
                                      float(r["story_points"] or 0), hours, r["created_at"])
        for r in cur.execute("SELECT issue_key, field, from_value, changed_at FROM tracker.history "
                             "WHERE field IN ('status', 'sprint') ORDER BY changed_at, id"):
            issue = issues.get(r["issue_key"])
            if issue is not None:
                issue.changes.setdefault(r["field"], []).append((r["changed_at"], r["from_value"] or None))
    return list(issues.values())


def _sprint(conn: psycopg.Connection, sprint_id: int) -> dict[str, Any]:
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute("SELECT * FROM tracker.sprint WHERE id = %s", [sprint_id]).fetchone()
    if row is None:
        raise TrackerError(404, f"Sprint {sprint_id} does not exist")
    return row


def _sprint_json(s: dict[str, Any]) -> dict[str, Any]:
    return {k: (v.isoformat() if isinstance(v, (date, datetime)) else v) for k, v in s.items()}


def _midnight(d: date) -> datetime:
    return datetime.combine(d, time(0), UTC)


def _window(s: dict[str, Any], now: datetime) -> tuple[datetime, datetime]:
    begin = s["started_at"] or _midnight(s["start_date"])
    end = s["completed_at"] or now
    return begin, end


def burndown(conn: psycopg.Connection, sprint_id: int, now: datetime, metric: str = "hours") -> dict[str, Any]:
    if metric not in ("hours", "points"):
        raise ValueError("metric must be 'hours' or 'points'")
    s = _sprint(conn, sprint_id)
    if s["start_date"] is None:
        raise TrackerError(409, "Set the program start date first (POST /api/tracker/setup)")
    issues = _load(conn)
    begin, end_cut = _window(s, now)
    first_day = begin.date()
    last_day = max(s["end_date"], s["completed_at"].date() if s["completed_at"] else s["end_date"])
    committed = sum(i.metric(metric) for i in issues if i.in_sprint(sprint_id, begin))
    working = [d for d in workdays.days_between(first_day, last_day) if workdays.is_workday(d)]
    total_work = max(1, len(working) - 1)
    days, elapsed = [], 0
    for d in workdays.days_between(first_day, last_day):
        cutoff = min(_midnight(d + timedelta(days=1)) - timedelta(microseconds=1), end_cut)
        if workdays.is_workday(d) and d != first_day:
            elapsed += 1
        ideal = round(committed * max(0.0, 1 - elapsed / total_work), 2)
        remaining = None
        completed = None
        if cutoff >= begin and d <= now.date():
            remaining = sum(i.metric(metric) for i in issues if i.in_sprint(sprint_id, cutoff) and not i.done(cutoff))
            day_start = max(begin, _midnight(d))
            completed = sum(i.metric(metric) for i in issues
                            if i.in_sprint(sprint_id, cutoff) and i.done(cutoff) and not i.done(day_start))
        days.append({"date": d.isoformat(), "working": workdays.is_workday(d), "ideal": ideal,
                     "remaining": remaining, "completed": completed})
    return {
        "sprint": _sprint_json(s),
        "metric": metric,
        "committed": committed,
        "remaining": next((d["remaining"] for d in reversed(days) if d["remaining"] is not None), committed),
        "days": days,
        "scope_changes": _scope_changes(issues, sprint_id, begin, end_cut, metric),
    }


def _scope_changes(issues: list[_Issue], sprint_id: int, begin: datetime, end: datetime,
                   metric: str) -> list[dict[str, Any]]:
    out = []
    target = str(sprint_id)
    for issue in issues:
        if begin < issue.created_at <= end and issue.in_sprint(sprint_id, issue.created_at):
            out.append({"at": issue.created_at.isoformat(), "key": issue.key, "change": "added",
                        "value": issue.metric(metric)})
        for at, before in issue.changes.get("sprint", []):
            if not begin < at <= end:
                continue
            after = issue.value_at("sprint", at)
            if after == target and before != target:
                out.append({"at": at.isoformat(), "key": issue.key, "change": "added", "value": issue.metric(metric)})
            elif before == target and after != target:
                out.append({"at": at.isoformat(), "key": issue.key, "change": "removed",
                            "value": issue.metric(metric)})
    return sorted(out, key=lambda c: c["at"])


def velocity(conn: psycopg.Connection, now: datetime) -> dict[str, Any]:
    issues = _load(conn)
    with conn.cursor(row_factory=dict_row) as cur:
        sprints = cur.execute("SELECT * FROM tracker.sprint ORDER BY id").fetchall()
    rows = []
    for s in sprints:
        entry: dict[str, Any] = {"id": s["id"], "name": s["name"], "state": s["state"], "project": s["project"]}
        if s["state"] == "future":
            planned = [i for i in issues if i.sprint_id == s["id"]]
            entry |= {"committed_points": sum(i.points for i in planned), "completed_points": None,
                      "committed_hours": sum(i.hours for i in planned), "completed_hours": None}
        else:
            begin, end = _window(s, now)
            start_scope = [i for i in issues if i.in_sprint(s["id"], begin)]
            finished = [i for i in issues if i.in_sprint(s["id"], end) and i.done(end)]
            entry |= {"committed_points": sum(i.points for i in start_scope),
                      "completed_points": sum(i.points for i in finished),
                      "committed_hours": sum(i.hours for i in start_scope),
                      "completed_hours": sum(i.hours for i in finished)}
        rows.append(entry)
    closed = [r for r in rows if r["state"] == "closed"][-3:]
    avg = (sum(r["completed_points"] for r in closed) / len(closed)) if closed else None
    return {"sprints": rows, "average_completed_points_last_3": avg}


def cfd(conn: psycopg.Connection, now: datetime, start: date | None = None, end: date | None = None,
        epic: str | None = None, sprint: int | None = None) -> dict[str, Any]:
    issues = _load(conn)
    if epic:
        issues = [i for i in issues if (i.epic_key or "").upper() == epic.upper()]
    if sprint is not None:
        issues = [i for i in issues if i.sprint_id == sprint]
    end = min(end or now.date(), now.date())
    if start is None:
        firsts = [i.created_at.date() for i in issues]
        firsts += [at.date() for i in issues for ch in i.changes.values() for at, _ in ch]
        start = min(firsts, default=end)
    start = max(start, end - timedelta(days=MAX_CFD_DAYS))
    days = []
    for d in workdays.days_between(start, end):
        cutoff = min(_midnight(d + timedelta(days=1)) - timedelta(microseconds=1), now)
        counts = dict.fromkeys(STATUSES, 0)
        for issue in issues:
            if issue.created_at <= cutoff:
                status = issue.value_at("status", cutoff) or "To Do"
                counts[status] = counts.get(status, 0) + 1
        days.append({"date": d.isoformat(), **counts})
    return {"statuses": list(STATUSES), "days": days}
