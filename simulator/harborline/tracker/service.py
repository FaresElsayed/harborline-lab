"""Tracker operations used by the HTTP routes. Every change he makes is written to ``tracker.history``."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.rows import dict_row

from harborline.tracker import jira_export, loader
from harborline.tracker.jql import FROM_SQL, PRIORITIES, PROJECT_KEY, STATUSES, TYPES, compile_jql

PROJECT_NAME = "Harborline Data Platform"
AUTHOR = "me"


class TrackerError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _jsonable(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _row(row: dict[str, Any]) -> dict[str, Any]:
    return {k: _jsonable(v) for k, v in row.items()}


def slug(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(s) <= limit:
        return s
    cut = s[: limit + 1].rsplit("-", 1)[0]  # trim at a word boundary, never mid-word
    return cut if cut else s[:limit]


def branch_name(key: str, issue_type: str, summary: str) -> str:
    prefix = "fix" if issue_type == "Bug" else "feat"
    return f"{prefix}/{key}-{slug(summary)}"


_LIST_SQL = """
SELECT i.key, i.num, i.source_id, i.origin, i.removed, i.project, i.type, i.summary, i.status, i.priority,
       i.reporter, i.assignee, i.department, i.epic_key, i.parent_key, i.sprint_id, s.name AS sprint_name,
       s.state AS sprint_state, i.story_points, i.estimate_hours, i.due_date, i.start_date, i.labels, i.components,
       i.attachments, i.dashboards, i.pr_url, i.created_at, i.updated_at, i.resolved_at, r.rank,
       (SELECT count(*) FROM tracker.ac_check a WHERE a.issue_key = i.key) AS ac_total,
       (SELECT count(*) FROM tracker.ac_check a WHERE a.issue_key = i.key AND a.checked) AS ac_done,
       (SELECT coalesce(sum(w.hours), 0) FROM tracker.worklog w WHERE w.issue_key = i.key) AS logged_hours,
       (SELECT count(*) FROM tracker.comment c WHERE c.issue_key = i.key) AS comment_count,
       (SELECT count(*) FROM tracker.link l JOIN tracker.issue b ON b.key = l.from_key
         WHERE l.to_key = i.key AND b.status <> 'Done') AS blocked_by_open
FROM {from_sql}
WHERE ({where}) {extra}
ORDER BY {order}
"""


def _decorate(row: dict[str, Any], today: date) -> dict[str, Any]:
    out = _row(row)
    out["overdue"] = bool(row["due_date"] and row["due_date"] < today and row["status"] != "Done")
    out["branch_name"] = branch_name(row["key"], row["type"], row["summary"])
    return out


def list_issues(conn: psycopg.Connection, now: datetime, jql: str | None = None, sprint: str | None = None,
                epic: str | None = None, status: str | None = None, issue_type: str | None = None,
                include_removed: bool = False) -> list[dict[str, Any]]:
    q = compile_jql(jql, now, loader.people())
    extra, params = [], list(q.params)
    if not include_removed:
        extra.append("NOT i.removed")
    if sprint:
        if sprint.lower() in ("backlog", "none"):
            extra.append("i.sprint_id IS NULL")
        elif sprint.isdigit():
            extra.append("i.sprint_id = %s")
            params.append(int(sprint))
        else:
            raise TrackerError(400, "sprint must be a sprint id or 'backlog'")
    if epic:
        extra.append("(upper(i.epic_key) = %s OR i.epic_key = (SELECT key FROM tracker.issue WHERE "
                     "upper(source_id) = %s))")
        params += [epic.upper(), epic.upper()]
    if status:
        extra.append("lower(i.status) = lower(%s)")
        params.append(status)
    if issue_type:
        extra.append("lower(i.type) = lower(%s)")
        params.append(issue_type)
    sql = _LIST_SQL.format(from_sql=FROM_SQL, where=q.where_sql,
                           extra="".join(f" AND {e}" for e in extra), order=q.order_sql)
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(sql, params).fetchall()
    return [_decorate(r, now.date()) for r in rows]


def _one(conn: psycopg.Connection, key: str, now: datetime) -> dict[str, Any]:
    """One issue by key (DATA-12) or catalog id (P2.S1.T3)."""
    rows = list_issues(conn, now, jql=f'key = "{key}"', include_removed=True) \
        if re.fullmatch(r"[A-Za-z0-9.\-]+", key) else []
    if not rows:
        raise TrackerError(404, f"Issue {key} does not exist")
    return rows[0]


def get_issue(conn: psycopg.Connection, key: str, now: datetime) -> dict[str, Any]:
    issue = _one(conn, key, now)
    key = issue["key"]
    with conn.cursor(row_factory=dict_row) as cur:
        detail = cur.execute("SELECT description, definition_of_done, due_day, start_day FROM tracker.issue "
                             "WHERE key = %s", [key]).fetchone()
        ac = cur.execute("SELECT idx, text, checked, checked_at FROM tracker.ac_check WHERE issue_key = %s "
                         "ORDER BY idx", [key]).fetchall()
        comments = cur.execute("SELECT id, author, body, created_at, updated_at FROM tracker.comment "
                               "WHERE issue_key = %s ORDER BY created_at, id", [key]).fetchall()
        worklogs = cur.execute("SELECT id, author, hours, work_date, note, created_at FROM tracker.worklog "
                               "WHERE issue_key = %s ORDER BY work_date, id", [key]).fetchall()
        blocks = cur.execute("SELECT l.to_key AS key, i.summary, i.status, i.type FROM tracker.link l JOIN "
                             "tracker.issue i ON i.key = l.to_key WHERE l.from_key = %s ORDER BY i.num",
                             [key]).fetchall()
        blocked_by = cur.execute("SELECT l.from_key AS key, i.summary, i.status, i.type FROM tracker.link l JOIN "
                                 "tracker.issue i ON i.key = l.from_key WHERE l.to_key = %s ORDER BY i.num",
                                 [key]).fetchall()
        children = cur.execute("SELECT key, type, summary, status, estimate_hours, story_points FROM tracker.issue "
                               "WHERE (parent_key = %s OR (epic_key = %s AND parent_key IS NULL)) AND NOT removed "
                               "ORDER BY num", [key, key]).fetchall()
    issue |= {
        "description": detail["description"],
        "definition_of_done": detail["definition_of_done"],
        "due_day": detail["due_day"],
        "start_day": detail["start_day"],
        "acceptance_criteria": [_row(a) for a in ac],
        "comments": [_row(c) for c in comments],
        "worklogs": [_row(w) for w in worklogs],
        "links": {"blocks": [_row(b) for b in blocks], "blocked_by": [_row(b) for b in blocked_by]},
        "children": [_row(c) for c in children],
        "attachment_urls": [{"name": a.rsplit("/", 1)[-1], "path": a, "url": f"/api/tracker/attachments/{a}"}
                            for a in issue["attachments"]],
    }
    return issue


def _log(conn: psycopg.Connection, key: str, fieldname: str, before: Any, after: Any, now: datetime) -> None:
    def text(v: Any) -> str | None:
        if v is None:
            return None
        if isinstance(v, (list, tuple)):
            return ", ".join(str(x) for x in v)
        return str(_jsonable(v))

    conn.execute("INSERT INTO tracker.history (issue_key, field, from_value, to_value, author, changed_at) "
                 "VALUES (%s, %s, %s, %s, %s, %s)", [key, fieldname, text(before), text(after), AUTHOR, now])


def _current(conn: psycopg.Connection, key: str, lock: bool = False) -> dict[str, Any]:
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute("SELECT * FROM tracker.issue WHERE upper(key) = upper(%s)" + (" FOR UPDATE" if lock else ""),
                          [key]).fetchone()
    if row is None:
        raise TrackerError(404, f"Issue {key} does not exist")
    return row


def _check_sprint(conn: psycopg.Connection, sprint_id: int | None) -> None:
    if sprint_id is None:
        return
    row = conn.execute("SELECT state FROM tracker.sprint WHERE id = %s", [sprint_id]).fetchone()
    if row is None:
        raise TrackerError(400, f"Sprint {sprint_id} does not exist")
    if row[0] == "closed":
        raise TrackerError(400, f"Sprint {sprint_id} is closed")


def _check_issue_ref(conn: psycopg.Connection, key: str | None, expected: tuple[str, ...]) -> str | None:
    if key is None:
        return None
    row = conn.execute("SELECT key, type FROM tracker.issue WHERE upper(key) = upper(%s)", [key]).fetchone()
    if row is None:
        raise TrackerError(400, f"Issue {key} does not exist")
    if row[1] not in expected:
        raise TrackerError(400, f"{row[0]} is a {row[1]}; expected {' or '.join(expected)}")
    return row[0]


EDITABLE = ("summary", "description", "status", "priority", "assignee", "sprint_id", "labels", "components",
            "story_points", "estimate_hours", "due_date", "pr_url", "epic_key", "parent_key")


def patch_issue(conn: psycopg.Connection, key: str, changes: dict[str, Any], now: datetime) -> dict[str, Any]:
    unknown = set(changes) - set(EDITABLE) - {"acceptance_criteria"}
    if unknown:
        raise TrackerError(400, f"Fields not editable: {', '.join(sorted(unknown))}")
    with conn.transaction():
        cur = _current(conn, key, lock=True)
        key = cur["key"]
        _validate_values(conn, cur, changes)
        sets: dict[str, Any] = {}
        for name in EDITABLE:
            if name in changes and changes[name] != _plain(cur[name]):
                sets[name] = changes[name]
        if "sprint_id" in sets and cur["parent_key"] and _is_story(conn, cur["parent_key"]):
            raise TrackerError(400, f"{key} is a sub-task of {cur['parent_key']}; move the story instead")
        if "status" in sets:
            sets["resolved_at"] = now if sets["status"] == "Done" else None
        if sets:
            assignments = ", ".join(f"{c} = %s" for c in sets)
            conn.execute(f"UPDATE tracker.issue SET {assignments}, updated_at = %s WHERE key = %s",
                         [*sets.values(), now, key])
            for name, value in sets.items():
                if name != "resolved_at":
                    _log(conn, key, "sprint" if name == "sprint_id" else name, cur[name], value, now)
        if "sprint_id" in sets and cur["type"] == "Story":
            _move_children(conn, key, sets["sprint_id"], now)
        if "acceptance_criteria" in changes:
            _apply_ac(conn, key, changes["acceptance_criteria"], now)
    return get_issue(conn, key, now)


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    return value


def _is_story(conn: psycopg.Connection, key: str) -> bool:
    row = conn.execute("SELECT type FROM tracker.issue WHERE key = %s", [key]).fetchone()
    return bool(row and row[0] == "Story")


def _validate_values(conn: psycopg.Connection, cur: dict[str, Any], changes: dict[str, Any]) -> None:
    if "status" in changes and changes["status"] not in STATUSES:
        raise TrackerError(400, f"status must be one of {', '.join(STATUSES)}")
    if "priority" in changes and changes["priority"] not in PRIORITIES:
        raise TrackerError(400, f"priority must be one of {', '.join(PRIORITIES)}")
    if "summary" in changes and not (changes["summary"] or "").strip():
        raise TrackerError(400, "summary cannot be empty")
    if "assignee" in changes and changes["assignee"] is not None:
        keys = {p["key"] for p in loader.people()}
        if changes["assignee"] not in keys:
            raise TrackerError(400, f"unknown assignee '{changes['assignee']}'")
    if "sprint_id" in changes:
        if cur["type"] == "Epic" and changes["sprint_id"] is not None:
            raise TrackerError(400, "Epics are not planned into sprints")
        _check_sprint(conn, changes["sprint_id"])
    if changes.get("epic_key") is not None:
        changes["epic_key"] = _check_issue_ref(conn, changes["epic_key"], ("Epic",))
    if changes.get("parent_key") is not None:
        changes["parent_key"] = _check_issue_ref(conn, changes["parent_key"], ("Story", "Epic"))
    if changes.get("pr_url") and not re.match(r"https?://", changes["pr_url"]):
        raise TrackerError(400, "pr_url must start with http:// or https://")


def _move_children(conn: psycopg.Connection, story_key: str, sprint_id: int | None, now: datetime) -> None:
    rows = conn.execute("SELECT key, sprint_id FROM tracker.issue WHERE parent_key = %s AND sprint_id IS DISTINCT "
                        "FROM %s", [story_key, sprint_id]).fetchall()
    for child, before in rows:
        conn.execute("UPDATE tracker.issue SET sprint_id = %s, updated_at = %s WHERE key = %s", [sprint_id, now, child])
        _log(conn, child, "sprint", before, sprint_id, now)


def _apply_ac(conn: psycopg.Connection, key: str, items: list[dict[str, Any]], now: datetime) -> None:
    for item in items:
        idx, checked = int(item["idx"]), bool(item["checked"])
        row = conn.execute("SELECT checked FROM tracker.ac_check WHERE issue_key = %s AND idx = %s",
                           [key, idx]).fetchone()
        if row is None:
            raise TrackerError(400, f"{key} has no acceptance criterion #{idx}")
        if row[0] != checked:
            conn.execute("UPDATE tracker.ac_check SET checked = %s, checked_at = %s WHERE issue_key = %s AND idx = %s",
                         [checked, now if checked else None, key, idx])
            _log(conn, key, f"ac[{idx}]", row[0], checked, now)


def create_issue(conn: psycopg.Connection, data: dict[str, Any], now: datetime) -> dict[str, Any]:
    if data.get("type") not in TYPES:
        raise TrackerError(400, f"type must be one of {', '.join(TYPES)}")
    if not (data.get("summary") or "").strip():
        raise TrackerError(400, "summary is required")
    with conn.transaction():
        conn.execute("LOCK TABLE tracker.issue IN SHARE ROW EXCLUSIVE MODE")
        _validate_values(conn, {"type": data["type"]}, data)
        parent = data.get("parent_key")
        epic = data.get("epic_key")
        sprint_id = data.get("sprint_id")
        if parent:
            prow = _current(conn, parent)
            epic = epic or (prow["key"] if prow["type"] == "Epic" else prow["epic_key"])
            if prow["type"] == "Story":
                sprint_id = prow["sprint_id"]
        num = int(conn.execute("SELECT coalesce(max(num), 0) + 1 FROM tracker.issue").fetchone()[0])
        key = f"{PROJECT_KEY}-{num}"
        project = None
        if epic:
            project = conn.execute("SELECT project FROM tracker.issue WHERE key = %s", [epic]).fetchone()[0]
        conn.execute(
            "INSERT INTO tracker.issue (key, num, origin, project, type, summary, description, status, priority, "
            "reporter, assignee, epic_key, parent_key, sprint_id, story_points, estimate_hours, due_date, labels, "
            "components, created_at, updated_at) VALUES (%s, %s, 'user', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "%s, %s, %s, %s, %s, %s, %s)",
            [key, num, project, data["type"], data["summary"].strip(), data.get("description") or "",
             data.get("status") or "To Do", data.get("priority") or "Medium", AUTHOR,
             data.get("assignee", AUTHOR), epic, parent, sprint_id, data.get("story_points"),
             data.get("estimate_hours"), data.get("due_date"), data.get("labels") or [], data.get("components") or [],
             now, now],
        )
        top = conn.execute("SELECT coalesce(max(rank), 0) + 1000 FROM tracker.rank").fetchone()[0]
        conn.execute("INSERT INTO tracker.rank (issue_key, rank) VALUES (%s, %s)", [key, top])
        for idx, text in enumerate(data.get("acceptance_criteria") or []):
            conn.execute("INSERT INTO tracker.ac_check (issue_key, idx, text) VALUES (%s, %s, %s)", [key, idx, text])
        for blocker in data.get("blocked_by") or []:
            ref = _check_issue_ref(conn, blocker, TYPES)
            conn.execute("INSERT INTO tracker.link (from_key, to_key, link_type, origin) VALUES (%s, %s, 'Blocks', "
                         "'user') ON CONFLICT DO NOTHING", [ref, key])
        _log(conn, key, "created", None, data["type"], now)
    return get_issue(conn, key, now)


def add_comment(conn: psycopg.Connection, key: str, body: str, now: datetime) -> dict[str, Any]:
    if not body.strip():
        raise TrackerError(400, "comment body is empty")
    issue = _current(conn, key)
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute("INSERT INTO tracker.comment (issue_key, author, body, created_at, updated_at) "
                          "VALUES (%s, %s, %s, %s, %s) RETURNING id, issue_key, author, body, created_at, updated_at",
                          [issue["key"], AUTHOR, body, now, now]).fetchone()
    conn.execute("UPDATE tracker.issue SET updated_at = %s WHERE key = %s", [now, issue["key"]])
    return _row(row)


def add_worklog(conn: psycopg.Connection, key: str, hours: float, work_date: date, note: str | None,
                now: datetime) -> dict[str, Any]:
    if not 0 < hours <= 24:
        raise TrackerError(400, "hours must be between 0 and 24")
    issue = _current(conn, key)
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute("INSERT INTO tracker.worklog (issue_key, author, hours, work_date, note, created_at) "
                          "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, issue_key, author, hours, work_date, note, "
                          "created_at", [issue["key"], AUTHOR, hours, work_date, note, now]).fetchone()
    conn.execute("UPDATE tracker.issue SET updated_at = %s WHERE key = %s", [now, issue["key"]])
    return _row(row)


def history(conn: psycopg.Connection, key: str) -> list[dict[str, Any]]:
    issue = _current(conn, key)
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute("SELECT id, field, from_value, to_value, author, changed_at FROM tracker.history "
                           "WHERE issue_key = %s ORDER BY changed_at, id", [issue["key"]]).fetchall()
    return [_row(r) for r in rows]


def rank_issue(conn: psycopg.Connection, key: str, before: str | None, after: str | None,
               now: datetime) -> dict[str, Any]:
    if bool(before) == bool(after):
        raise TrackerError(400, "give exactly one of 'before' or 'after'")
    with conn.transaction():
        conn.execute("LOCK TABLE tracker.rank IN SHARE ROW EXCLUSIVE MODE")
        issue = _current(conn, key)
        anchor = _current(conn, before or after or "")
        if anchor["key"] == issue["key"]:
            raise TrackerError(400, "an issue cannot be ranked relative to itself")
        ranks = {k: Decimal(r) for k, r in conn.execute("SELECT issue_key, rank FROM tracker.rank")}
        old = ranks.pop(issue["key"], None)
        pivot = ranks[anchor["key"]]
        if before:
            lower = [r for r in ranks.values() if r < pivot]
            new = (max(lower) + pivot) / 2 if lower else pivot - 1000
        else:
            higher = [r for r in ranks.values() if r > pivot]
            new = (min(higher) + pivot) / 2 if higher else pivot + 1000
        conn.execute("INSERT INTO tracker.rank (issue_key, rank) VALUES (%s, %s) ON CONFLICT (issue_key) "
                     "DO UPDATE SET rank = EXCLUDED.rank", [issue["key"], new])
        direction = "higher" if old is None or new < old else "lower"
        _log(conn, issue["key"], "rank", None, f"Ranked {direction} ({'before' if before else 'after'} "
             f"{anchor['key']})", now)
    return {"key": issue["key"], "rank": float(new)}


def list_sprints(conn: psycopg.Connection) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            "SELECT s.*, (SELECT count(*) FROM tracker.issue i WHERE i.sprint_id = s.id AND NOT i.removed) AS issues, "
            "(SELECT count(*) FROM tracker.issue i WHERE i.sprint_id = s.id AND i.status = 'Done' AND NOT i.removed) "
            "AS done, (SELECT coalesce(sum(story_points), 0) FROM tracker.issue i WHERE i.sprint_id = s.id AND "
            "NOT i.removed) AS story_points, (SELECT coalesce(sum(estimate_hours), 0) FROM tracker.issue i WHERE "
            "i.sprint_id = s.id AND i.type IN ('Task', 'Bug', 'Spike') AND NOT i.removed) AS estimate_hours "
            "FROM tracker.sprint s ORDER BY s.id"
        ).fetchall()
    return [_row(r) for r in rows]


def _sprint(conn: psycopg.Connection, sprint_id: int) -> dict[str, Any]:
    for sprint in list_sprints(conn):
        if sprint["id"] == sprint_id:
            return sprint
    raise TrackerError(404, f"Sprint {sprint_id} does not exist")


def start_sprint(conn: psycopg.Connection, sprint_id: int, now: datetime) -> dict[str, Any]:
    with conn.transaction():
        row = conn.execute("SELECT state FROM tracker.sprint WHERE id = %s FOR UPDATE", [sprint_id]).fetchone()
        if row is None:
            raise TrackerError(404, f"Sprint {sprint_id} does not exist")
        if row[0] != "future":
            raise TrackerError(409, f"Sprint {sprint_id} is already {row[0]}")
        active = conn.execute("SELECT id FROM tracker.sprint WHERE state = 'active'").fetchone()
        if active:
            raise TrackerError(409, f"DATA Sprint {active[0]} is still active; complete it first")
        conn.execute("UPDATE tracker.sprint SET state = 'active', started_at = %s WHERE id = %s", [now, sprint_id])
    return _sprint(conn, sprint_id)


def complete_sprint(conn: psycopg.Connection, sprint_id: int, move_open_to: int | None,
                    now: datetime) -> dict[str, Any]:
    with conn.transaction():
        row = conn.execute("SELECT state FROM tracker.sprint WHERE id = %s FOR UPDATE", [sprint_id]).fetchone()
        if row is None:
            raise TrackerError(404, f"Sprint {sprint_id} does not exist")
        if row[0] != "active":
            raise TrackerError(409, f"Sprint {sprint_id} is {row[0]}; only an active sprint can be completed")
        if move_open_to == sprint_id:
            raise TrackerError(400, "open issues must move to another sprint or the backlog")
        _check_sprint(conn, move_open_to)
        open_rows = conn.execute(
            "SELECT key FROM tracker.issue WHERE sprint_id = %s AND (status <> 'Done' OR parent_key IN "
            "(SELECT key FROM tracker.issue WHERE sprint_id = %s AND type = 'Story' AND status <> 'Done'))",
            [sprint_id, sprint_id],
        ).fetchall()
        for (key,) in open_rows:
            conn.execute("UPDATE tracker.issue SET sprint_id = %s, updated_at = %s WHERE key = %s",
                         [move_open_to, now, key])
            _log(conn, key, "sprint", sprint_id, move_open_to, now)
        conn.execute("UPDATE tracker.sprint SET state = 'closed', completed_at = %s WHERE id = %s", [now, sprint_id])
    out = _sprint(conn, sprint_id)
    out["moved"] = [k for (k,) in open_rows]
    return out


def meta(conn: psycopg.Connection, now: datetime) -> dict[str, Any]:
    start = loader.start_date(conn)
    labels = [r[0] for r in conn.execute("SELECT DISTINCT unnest(labels) AS l FROM tracker.issue ORDER BY l")]
    components = [r[0] for r in conn.execute("SELECT DISTINCT unnest(components) AS c FROM tracker.issue ORDER BY c")]
    catalog = loader.get_setting(conn, "catalog") or {}
    return {
        "project": {"key": PROJECT_KEY, "name": PROJECT_NAME},
        "statuses": list(STATUSES),
        "issue_types": list(TYPES),
        "priorities": list(reversed(PRIORITIES)),
        "people": loader.people(),
        "components": components,
        "labels": labels,
        "sprints": list_sprints(conn),
        "start_date": start.isoformat() if start else None,
        "configured": start is not None,
        "today": now.date().isoformat(),
        "catalog": {k: catalog.get(k) for k in ("files", "issues", "warnings", "loaded_at", "result")},
        "catalog_errors": (loader.get_setting(conn, "catalog_errors") or {}).get("errors", []),
        "jira_date_format": jira_export.JIRA_DATE_FORMAT,
    }


def jira_rows(conn: psycopg.Connection) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            "SELECT i.key, i.num, i.type, i.summary, i.description, i.priority, i.labels, i.components, i.due_date, "
            "i.estimate_hours, i.story_points, s.name AS sprint_name, i.status, i.epic_key, i.parent_key, "
            "p.type AS parent_type, i.source_id, i.definition_of_done, "
            "coalesce((SELECT array_agg(a.text ORDER BY a.idx) FROM tracker.ac_check a WHERE a.issue_key = i.key), "
            "'{}') AS acceptance_criteria, "
            "coalesce((SELECT array_agg(l.from_key ORDER BY l.from_key) FROM tracker.link l "
            "WHERE l.to_key = i.key), '{}') AS blocked_by "
            "FROM tracker.issue i LEFT JOIN tracker.sprint s ON s.id = i.sprint_id "
            "LEFT JOIN tracker.issue p ON p.key = i.parent_key WHERE NOT i.removed ORDER BY i.num"
        ).fetchall()
    return rows


def now_utc() -> datetime:
    return datetime.now(UTC)
