"""Ticket catalog validation, flattening order, working-day calendar (no database needed)."""

from __future__ import annotations

import copy
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

from harborline.settings import get_settings
from harborline.tracker import loader, workdays
from harborline.tracker.catalog import read_catalog

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
