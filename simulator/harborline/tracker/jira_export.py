"""Jira Cloud CSV export (Settings > System > External system import > CSV).

Column names follow the importer's field names so they auto-map: Issue Id / Parent Id build the hierarchy
(the importer requires parents to appear before their children, so rows are written epics first, then
standard issues, then sub-tasks), repeated ``Labels`` and ``Component/s`` columns carry multiple values,
``Original Estimate`` is in seconds, and dates use the importer's default SimpleDateFormat
``dd/MMM/yy h:mm a`` (e.g. ``02/Nov/26 12:00 AM``), so the "Date format" step needs no change.

Type mapping: Epic -> Epic, Story -> Story, Task -> Sub-task (under its story), Bug -> Bug and
Spike -> Task (both under the epic, because Jira does not allow them below a story).
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date
from typing import Any

JIRA_DATE_FORMAT = "dd/MMM/yy h:mm a"
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_LEVEL = {"Epic": 0, "Story": 1, "Bug": 1, "Task": 1, "Sub-task": 2}


def jira_date(d: date | None) -> str:
    """Date in Jira's default import format, with an English month regardless of locale."""
    if d is None:
        return ""
    return f"{d.day:02d}/{_MONTHS[d.month - 1]}/{d.year % 100:02d} 12:00 AM"


def jira_type(issue: dict[str, Any]) -> str:
    kind = issue["type"]
    if kind == "Task":
        return "Sub-task" if issue.get("parent_type") == "Story" else "Task"
    if kind == "Spike":
        return "Task"
    return kind


def _label(text: str) -> str:
    return re.sub(r"\s+", "-", text.strip())


def _description(issue: dict[str, Any]) -> str:
    parts = [issue.get("description") or ""]
    ac = issue.get("acceptance_criteria") or []
    if ac:
        parts.append("h3. Acceptance criteria\n" + "\n".join(f"* {line}" for line in ac))
    dod = issue.get("definition_of_done") or []
    if dod:
        parts.append("h3. Definition of Done\n" + "\n".join(f"* {line}" for line in dod))
    if issue.get("blocked_by"):
        parts.append("Depends on: " + ", ".join(issue["blocked_by"]))
    origin = f"Harborline Tracker key: {issue['key']}"
    if issue.get("source_id"):
        origin += f" ({issue['source_id']})"
    if issue["type"] in ("Bug", "Spike") and issue.get("parent_type") == "Story":
        origin += f"; originally under story {issue['parent_key']}"
    parts.append(origin)
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def export_csv(issues: list[dict[str, Any]]) -> str:
    """``issues``: dicts with key, num, type, summary, description, priority, labels, components, due_date,
    estimate_hours, story_points, sprint_name, status, epic_key, parent_key, parent_type, source_id,
    acceptance_criteria, definition_of_done, blocked_by."""
    by_key = {i["key"]: i for i in issues}
    rows = []
    for issue in issues:
        kind = jira_type(issue)
        if kind == "Sub-task":
            parent = by_key.get(issue.get("parent_key") or "")
        elif kind == "Epic":
            parent = None
        else:
            parent = by_key.get(issue.get("epic_key") or "")
        rows.append((issue, kind, parent))
    rows.sort(key=lambda r: (_LEVEL.get(r[1], 1), r[0]["num"]))
    max_labels = max((len(i.get("labels") or []) for i in issues), default=0)
    max_components = max((len(i.get("components") or []) for i in issues), default=0)
    header = ["Issue Id", "Parent Id", "Issue Type", "Summary", "Description", "Priority",
              *(["Labels"] * max(1, max_labels)), *(["Component/s"] * max(1, max_components)),
              "Due date", "Original Estimate", "Story point estimate", "Sprint", "Status"]
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(header)
    for issue, kind, parent in rows:
        labels = [_label(x) for x in issue.get("labels") or []]
        components = list(issue.get("components") or [])
        estimate = issue.get("estimate_hours")
        points = issue.get("story_points")
        sprint = issue.get("sprint_name") if kind in ("Story", "Task", "Bug") else None
        writer.writerow([
            issue["num"],
            parent["num"] if parent else "",
            kind,
            issue["summary"],
            _description(issue),
            issue.get("priority") or "Medium",
            *(labels + [""] * (max(1, max_labels) - len(labels))),
            *(components + [""] * (max(1, max_components) - len(components))),
            jira_date(issue.get("due_date")),
            round(float(estimate) * 3600) if estimate is not None and kind != "Epic" else "",
            f"{float(points):g}" if points is not None and kind == "Story" else "",
            sprint or "",
            issue["status"],
        ])
    return buf.getvalue()
