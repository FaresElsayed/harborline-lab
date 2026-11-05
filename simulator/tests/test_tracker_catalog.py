"""Ticket catalog validation, flattening order, working-day calendar (no database needed)."""

from __future__ import annotations

import copy
import re
from collections import defaultdict
from datetime import UTC, date, datetime
from fractions import Fraction
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
import yaml

from harborline.settings import get_settings
from harborline.tracker import loader, moves, workdays
from harborline.tracker.catalog import CatalogIssue, read_catalog

PEOPLE = {"sam.okafor", "luis.moreno", "beth.kowalczyk", "me"}


def _task(tid: str, hours: float, due: int, kind: str = "Task", **extra: Any) -> dict[str, Any]:
    return {"id": tid, "type": kind, "summary": f"{kind} {tid}", "estimate_hours": hours, "due_day": due,
            "priority": "Medium", "components": ["Ingestion"], "labels": [], "description": "What and why.",
            "acceptance_criteria": [f"{tid} works"], "depends_on": [], **extra}


def sample_catalog() -> list[dict[str, Any]]:
    """Two small projects (14 tickets). File order: P1, P1.S1, its 3 tasks, P1.S2, its 2 tasks, P2, ..."""
    p1 = {
        "project": 1,
        "epic": {"id": "P1", "summary": "Data Platform Foundation", "department": "IT", "reporter": "sam.okafor",
                 "priority": "High", "labels": ["platform"], "description": "Foundation.", "dashboards": ["P1-D1"]},
        "stories": [
            {"id": "P1.S1", "summary": "Land WMS orders", "type": "Story", "reporter": "sam.okafor",
             "priority": "High", "story_points": 5, "sprint": 1, "due_day": 5, "components": ["Ingestion"],
             "labels": ["wms", "data quality"], "description": "Freshness matters.",
             "acceptance_criteria": ["Orders land hourly", "Row counts reconcile"], "depends_on": [],
             "attachments": ["wireframes/P1-D1.svg"],
             "tasks": [
                 _task("P1.S1.T1", 8, 2),
                 _task("P1.S1.T2", 16, 5, depends_on=["P1.S1.T1"], labels=["dq"]),
                 _task("P1.S1.B1", 4, 4, kind="Bug", priority="Highest"),
             ]},
            {"id": "P1.S2", "summary": "Platform health dashboard", "type": "Story", "reporter": "sam.okafor",
             "priority": "Medium", "story_points": 8, "sprint": 2, "due_day": 15, "components": ["Dashboards"],
             "labels": ["dashboards"], "description": "", "acceptance_criteria": ["KPI definitions agreed"],
             "depends_on": ["P1.S1"],
             "tasks": [_task("P1.S2.T1", 12, 12), _task("P1.S2.T2", 6, 15, kind="Spike")]},
        ],
    }
    p2 = {
        "project": 2,
        "epic": {"id": "P2", "summary": "Warehouse Performance", "department": "Operations",
                 "reporter": "luis.moreno", "dashboards": ["P2-D1"]},
        "stories": [
            {"id": "P2.S1", "summary": "OTIF model", "reporter": "luis.moreno", "story_points": 3, "sprint": 1,
             "due_day": 8, "acceptance_criteria": ["OTIF matches Ops definition"],
             "tasks": [_task("P2.S1.T1", 10, 8, depends_on=["P1.S2.T1"])]},
            {"id": "P2.S2", "summary": "Ops scorecard", "reporter": "luis.moreno", "story_points": 13, "sprint": 2,
             "due_day": 20, "acceptance_criteria": ["Scorecard live"],
             "tasks": [_task("P2.S2.T1", 20, 18), _task("P2.S2.T2", 8, 20)]},
        ],
    }
    return [p1, p2]


def write_catalog(folder: Path, docs: list[dict[str, Any]]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("*.yaml"):
        old.unlink()
    slugs = {1: "platform", 2: "warehouse"}
    for doc in docs:
        path = folder / f"p{doc['project']:02d}_{slugs.get(doc['project'], 'x')}.yaml"
        path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return folder


def test_valid_catalog_flattens_in_file_order(tmp_path: Path) -> None:
    report = read_catalog(write_catalog(tmp_path, sample_catalog()), PEOPLE)
    assert report.errors == []
    assert [i.source_id for i in report.issues] == [
        "P1", "P1.S1", "P1.S1.T1", "P1.S1.T2", "P1.S1.B1", "P1.S2", "P1.S2.T1", "P1.S2.T2",
        "P2", "P2.S1", "P2.S1.T1", "P2.S2", "P2.S2.T1", "P2.S2.T2",
    ]
    by_id = {i.source_id: i for i in report.issues}
    assert by_id["P1.S1.B1"].type == "Bug" and by_id["P1.S1.B1"].sprint == 1
    assert by_id["P2.S1.T1"].sprint == 3 and by_id["P2.S2"].sprint == 4  # project p owns sprints 2p-1, 2p
    assert by_id["P1.S1.T2"].start_day == 4  # 16 h = 2 working days ending on day 5
    assert by_id["P1.S1.T1"].reporter == "sam.okafor"  # inherited from the story
    assert any("hours" in w for w in report.warnings)
    assert any("'data quality' contains whitespace" in w for w in report.warnings)


def test_errors_are_collected_with_locations(tmp_path: Path) -> None:
    docs = sample_catalog()
    bad = copy.deepcopy(docs)
    s1 = bad[0]["stories"][0]
    s1["reporter"] = "nobody.here"
    s1["tasks"][0]["due_day"] = 9  # after the story's due_day 5
    s1["tasks"][1]["estimate_hours"] = -1
    s1["tasks"][2]["owner"] = "x"  # unknown key
    bad[0]["stories"][1]["due_day"] = 4  # sprint 2 story must be due on days 11-20
    bad[1]["stories"][0]["tasks"][0]["depends_on"] = ["P9.S1"]
    report = read_catalog(write_catalog(tmp_path, bad), PEOPLE)
    text = "\n".join(report.errors)
    assert "p01_platform.yaml: stories[0].tasks[1].estimate_hours: Input should be greater than 0" in text
    assert "p01_platform.yaml: stories[0].tasks[2].owner: Extra inputs are not permitted" in text
    assert not report.ok and report.issues  # p02 still parsed


def test_semantic_errors(tmp_path: Path) -> None:
    docs = sample_catalog()
    docs[0]["stories"][0]["reporter"] = "nobody.here"
    docs[0]["stories"][0]["tasks"][0]["due_day"] = 9
    docs[0]["stories"][1]["due_day"] = 4
    docs[0]["stories"][1]["tasks"] = [_task("P1.S2.T1", 2, 3)]
    docs[1]["stories"][0]["tasks"][0]["depends_on"] = ["P9.S1"]
    report = read_catalog(write_catalog(tmp_path, docs), PEOPLE)
    text = "\n".join(report.errors)
    assert "stories[0].reporter: unknown people key 'nobody.here'" in text
    assert "stories[0].tasks[0].due_day: 9 is after its story's due_day 5" in text
    assert "stories[1].due_day: 4 is outside sprint 2 (days 11-20)" in text
    assert "P2.S1.T1.depends_on: unknown ticket id 'P9.S1'" in text


def test_cycles_duplicates_yaml_and_project_mismatch(tmp_path: Path) -> None:
    docs = sample_catalog()
    docs[0]["stories"][0]["tasks"][0]["depends_on"] = ["P1.S2.T1"]
    docs[0]["stories"][1]["tasks"][0]["depends_on"] = ["P1.S1"]
    docs[0]["stories"][0]["depends_on"] = ["P1.S1.T1"]
    report = read_catalog(write_catalog(tmp_path, docs), PEOPLE)
    assert any(e.startswith("dependency cycle: ") for e in report.errors), report.errors

    docs = sample_catalog()
    docs[1]["stories"][0]["tasks"][0]["id"] = "P2.S1.T1"
    docs[1]["stories"][1]["tasks"][0]["id"] = "P2.S2.T1"
    docs[1]["stories"][1]["tasks"][1]["id"] = "P2.S2.T1"
    report = read_catalog(write_catalog(tmp_path, docs), PEOPLE)
    assert any("duplicate ticket id 'P2.S2.T1'" in e for e in report.errors)

    folder = write_catalog(tmp_path, sample_catalog())
    (folder / "p02_warehouse.yaml").write_text("project: 2\nepic: [unclosed\n", encoding="utf-8")
    (folder / "p03_mismatch.yaml").write_text(yaml.safe_dump(sample_catalog()[0]), encoding="utf-8")
    report = read_catalog(folder, PEOPLE)
    assert any(e.startswith("p02_warehouse.yaml: invalid YAML") for e in report.errors)
    assert any("p03_mismatch.yaml: project: 1 does not match the file name prefix p03" in e for e in report.errors)
    assert any("project 1 is already defined in p01_platform.yaml" in e for e in report.errors)


def test_empty_folder_is_an_error(tmp_path: Path) -> None:
    assert read_catalog(tmp_path, PEOPLE).errors == [f"no ticket files (*.yaml) found in {tmp_path}"]


def test_working_days_skip_weekends() -> None:
    monday = date(2026, 11, 2)
    assert workdays.workday(monday, 1) == monday
    assert workdays.workday(date(2026, 10, 31), 1) == monday  # Saturday start rolls to Monday
    assert workdays.workday(monday, 5) == date(2026, 11, 6)
    assert workdays.workday(monday, 6) == date(2026, 11, 9)
    assert workdays.workday(monday, 15) == date(2026, 11, 20)
    assert workdays.workday(date(2026, 11, 4), 4) == date(2026, 11, 9)  # Wed start
    assert workdays.sprint_dates(monday, 1) == (monday, date(2026, 11, 13))
    assert workdays.sprint_dates(monday, 16) == (date(2027, 5, 31), date(2027, 6, 11))
    assert workdays.program_day(2, 8) == 28 and workdays.sprint_id(8, 2) == 16
    assert [workdays.sprint_project(s) for s in (1, 2, 3, 16)] == [1, 1, 2, 8]


def test_shipped_catalog_validates() -> None:
    tickets = get_settings().tracker_dir / "tickets"
    if not any(tickets.glob("*.yaml")):
        pytest.skip("ticket catalog not written yet")
    people = {p["key"] for p in loader.people(get_settings().config_dir)}
    report = read_catalog(tickets, people)
    assert report.errors == []
    assert sum(1 for i in report.issues if i.type == "Epic") == 8
    sprints = {i.sprint for i in report.issues if i.sprint}
    assert sprints <= set(range(1, workdays.SPRINTS + 1))


# ---------------------------------------------------------------- the difficulty ramp (see the top of p01)

LEVEL_LABEL = re.compile(r"level-(\d+)")
START_HERE = "Where to start"


def test_ticks_follow_reworded_boxes() -> None:
    when = datetime(2026, 11, 3, tzinfo=UTC)
    on, off = (True, when), (False, None)
    carry = loader.carry_ticks
    old = [("Each late package attributed", True, when), ("Row counts reconcile", False, None)]
    assert carry(old, ["Row counts reconcile", "Each late package attributed, with both causes"]) == [off, on]
    # Rewritten beyond recognition, but the only box left on each side: the same box.
    assert carry([("Heartbeat proves liveness", True, when)], ["A test shows a stalled feed reads as stale"]) == [on]
    # Two rewrites that share no words could be anything, so nothing is claimed.
    two = [("Alpha beta gamma", True, when), ("Delta epsilon zeta", True, when)]
    assert carry(two, ["Totally different one", "Nothing alike here"]) == [off, off]
    # Added boxes start empty. A wholly new one is not ticked by a neighbour.
    assert carry([("Decision documented", True, when)], ["Decision and edge cases documented", "Review it with Sam"]) == [on, off]
    # Duplicate wording keeps its own ticks, in order.
    twins = [("Same words", True, when), ("Same words", False, None)]
    assert carry(twins, ["Same words", "Same words"]) == [on, off]


def test_ticks_on_a_finished_ticket() -> None:
    when = datetime(2026, 11, 3, tzinfo=UTC)
    all_on = [("One box", True, when), ("Two box", True, when)]
    assert loader.carry_ticks(all_on, ["Nothing like it", "Also unrelated", "Third"], done=True) == [(True, when)] * 3
    some = [("One box", True, when), ("Two box", False, None)]
    assert loader.carry_ticks(some, ["One box", "Two box", "A new requirement"], done=True) == [
        (True, when), (False, None), (False, None)]
    assert loader.carry_ticks(all_on, ["One box", "Two box", "A new requirement"], done=False)[2] == (False, None)


def test_moved_tickets_point_at_real_tickets_and_each_old_id_moves_once() -> None:
    shipped = {i.source_id: i.summary for i in _shipped()}
    was = [m.was for m in moves.MOVED]
    assert len(was) == len(set(was))
    for move in moves.MOVED:
        assert move.now is None or move.now in shipped, move
        assert move.summary, move
    # An entry only applies to the ticket as it was, so it can't fire on anything in the current catalog by mistake:
    # the one ticket that kept both its id and its summary would have nowhere to go.
    assert not [m for m in moves.MOVED if shipped.get(m.was) == m.summary]


def _shipped() -> list[CatalogIssue]:
    tickets = get_settings().tracker_dir / "tickets"
    if not any(tickets.glob("*.yaml")):
        pytest.skip("ticket catalog not written yet")
    people = {p["key"] for p in loader.people(get_settings().config_dir)}
    report = read_catalog(tickets, people)
    assert report.errors == []
    return report.issues


def _level(issue: CatalogIssue) -> int:
    found = [m for m in map(LEVEL_LABEL.fullmatch, issue.labels) if m]
    assert len(found) == 1, f"{issue.source_id}: expected exactly one level-N label, got {issue.labels}"
    return int(found[0].group(1))


def _averages(issues: list[CatalogIssue], key: Any) -> list[tuple[int, Fraction]]:
    """Mean level per group, in group order. Exact fractions, so a tie is never read as a fall."""
    groups: dict[int, list[int]] = defaultdict(list)
    for issue in issues:
        if issue.type != "Epic":
            groups[key(issue)].append(_level(issue))
    return [(k, Fraction(sum(v), len(v))) for k, v in sorted(groups.items())]


def _falls(averages: list[tuple[int, Fraction]]) -> list[int]:
    return [b[0] for a, b in pairwise(averages) if b[1] < a[1]]


def _sprint_falls(issues: list[CatalogIssue]) -> list[tuple[int, list[int]]]:
    out = []
    for project in sorted({i.project for i in issues}):
        falls = _falls(_averages([i for i in issues if i.project == project and i.sprint], lambda i: i.sprint))
        if falls:
            out.append((project, falls))
    return out


def test_every_non_epic_ticket_has_exactly_one_level_label() -> None:
    issues = _shipped()
    for issue in issues:
        found = [label for label in issue.labels if LEVEL_LABEL.fullmatch(label)]
        if issue.type == "Epic":
            assert found == [], issue.source_id
        else:
            assert len(found) == 1 and found[0] in {f"level-{n}" for n in range(1, 6)}, (issue.source_id, issue.labels)


def test_average_level_never_falls_from_one_project_to_the_next() -> None:
    averages = _averages(_shipped(), lambda i: i.project)
    assert [k for k, _ in averages] == list(range(1, workdays.PROJECTS + 1))
    assert _falls(averages) == [], averages


def test_average_level_never_falls_between_sprints_of_a_project() -> None:
    assert _sprint_falls(_shipped()) == []


def test_the_ramp_checks_catch_a_fall(tmp_path: Path) -> None:
    docs = sample_catalog()
    for doc in docs:
        for story in doc["stories"]:
            story["labels"] = ["level-3"] if story["sprint"] == 1 else ["level-2"]
            for task in story["tasks"]:
                task["labels"] = list(story["labels"])
    report = read_catalog(write_catalog(tmp_path, docs), PEOPLE)
    assert _sprint_falls(report.issues) == [(1, [2]), (2, [4])]
    assert _falls([(1, Fraction(3)), (2, Fraction(2))]) == [2]
    assert _falls([(1, Fraction(2)), (2, Fraction(2))]) == []


def test_project_one_is_gentle_and_project_five_onward_has_no_level_one() -> None:
    issues = [i for i in _shipped() if i.type != "Epic"]
    first_two_sprints = [i.source_id for i in issues if i.project == 1 and i.sprint in (1, 2) and _level(i) >= 4]
    assert first_two_sprints == []
    assert [i.source_id for i in issues if i.project >= 5 and _level(i) == 1] == []


def test_support_fades_with_the_level() -> None:
    for issue in _shipped():
        if issue.type == "Epic":
            continue
        assert (START_HERE in issue.description) == (_level(issue) <= 2), issue.source_id
        assert issue.acceptance_criteria, f"{issue.source_id}: every level needs checkable acceptance criteria"


def test_shipped_catalog_keeps_the_published_numbers() -> None:
    """The landing page and the brief quote these. Changing them is a decision, not a side effect."""
    issues = _shipped()
    counts: dict[str, int] = defaultdict(int)
    hours: dict[int, float] = defaultdict(float)
    for issue in issues:
        counts[issue.type] += 1
        hours[issue.project] += issue.estimate_hours or 0
    assert dict(counts) == {"Epic": 8, "Story": 48, "Task": 313, "Spike": 9, "Bug": 15}
    assert all(155 <= h <= 159 for h in hours.values()), dict(hours)
    epics = [n for n, issue in enumerate(issues, start=1) if issue.type == "Epic"]
    assert epics == [1, 52, 100, 150, 200, 248, 297, 346]  # the Project brief links each project to its epic key


def test_each_project_runs_in_sprint_order_and_ends_in_dashboards_and_sign_off() -> None:
    issues = _shipped()
    for project in range(1, workdays.PROJECTS + 1):
        stories = [i for i in issues if i.project == project and i.type == "Story"]
        assert [s.sprint for s in stories] == sorted(s.sprint for s in stories), project
        last = stories[-1]
        tasks = [i for i in issues if i.parent_source == last.source_id]
        assert "dashboard" in last.summary.lower() or "Dashboard" in last.components, last.source_id
        sign_off = [t for t in tasks if t.summary.startswith("UAT sign-off from Iris Novak")]
        assert len(sign_off) == 1 and sign_off[0].due_day == workdays.DAYS_PER_PROJECT, last.source_id
