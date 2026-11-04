"""Tracker API: catalog load, dates and sprints, JQL, edits + history, idempotent reload, reports, export, backup.

Needs the test Postgres (HL_TEST_PG_PORT, see harborline/sim/testing.py).
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_tracker_catalog import _task, sample_catalog, write_catalog

from harborline.api import tracker as tracker_api
from harborline.db import get_db
from harborline.settings import Settings
from harborline.tracker import backup, loader, runtime

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)  # Monday, program day 1
API = "/api/tracker"
TABLES = "settings, sprint, issue, rank, ac_check, comment, worklog, history, link"


@dataclass
class Env:
    client: TestClient
    conn: psycopg.Connection
    tickets: Path
    clock: dict[str, datetime]

    def at(self, when: datetime) -> None:
        self.clock["now"] = when

    def ok(self, method: str, path: str, **kw: Any) -> Any:
        r = self.client.request(method, f"{API}{path}", **kw)
        assert r.status_code < 300, r.text
        return r.json() if r.content and r.headers.get("content-type", "").startswith("application/json") else r

    def keys(self, jql: str) -> list[str]:
        return [i["key"] for i in self.ok("GET", "/issues", params={"jql": jql})["issues"]]


@pytest.fixture
def env(pg_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    tickets = write_catalog(tmp_path / "tracker" / "tickets", sample_catalog())
    brand = tmp_path / "brand"
    (brand / "wireframes").mkdir(parents=True)
    (brand / "wireframes" / "P1-D1.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    monkeypatch.setattr(runtime, "tracker_dir", lambda: tmp_path / "tracker")
    monkeypatch.setattr(runtime, "brand_dir", lambda: brand)
    clock = {"now": T0 - timedelta(days=3)}
    monkeypatch.setattr(tracker_api, "_now", lambda: clock["now"])
    with get_db().connect("tracker", autocommit=True) as conn:
        loader.ensure_schema(conn)
        conn.execute(f"TRUNCATE {', '.join('tracker.' + t.strip() for t in TABLES.split(','))} RESTART IDENTITY "
                     "CASCADE")
        app = FastAPI()
        app.include_router(tracker_api.router)
        yield Env(TestClient(app), conn, tickets, clock)


def _configure(env: Env) -> dict[str, Any]:
    meta = env.ok("POST", "/setup", json={"start_date": "2026-10-31"})  # a Saturday: day 1 is Monday Nov 2
    # The catalog load stamps tickets with the database's real clock; these tests run on the fake one.
    env.conn.execute("UPDATE tracker.issue SET created_at = %s", [env.clock["now"]])
    return meta


def test_meta_loads_catalog_lazily_and_setup_sets_dates(env: Env) -> None:
    meta = env.ok("GET", "/meta")
    assert meta["configured"] is False and meta["start_date"] is None
    assert meta["project"] == {"key": "DATA", "name": "Harborline Data Platform"}
    assert meta["statuses"] == ["To Do", "In Progress", "In Review", "Done"]
    assert meta["priorities"] == ["Highest", "High", "Medium", "Low", "Lowest"]
    assert {"me", "luis.moreno"} <= {p["key"] for p in meta["people"]}
    assert len(meta["sprints"]) == 16 and meta["catalog"]["issues"] == 14
    assert {"Ingestion", "Dashboards"} <= set(meta["components"])
    issues = env.ok("GET", "/issues")["issues"]
    assert [i["key"] for i in sorted(issues, key=lambda i: i["num"])] == [f"DATA-{n}" for n in range(1, 15)]
    assert all(i["due_date"] is None for i in issues)

    meta = _configure(env)
    assert meta["configured"] is True and meta["start_date"] == "2026-10-31"
    sprints = {s["id"]: s for s in meta["sprints"]}
    assert (sprints[1]["start_date"], sprints[1]["end_date"]) == ("2026-11-02", "2026-11-13")
    assert (sprints[16]["start_date"], sprints[16]["end_date"]) == ("2027-05-31", "2027-06-11")
    assert sprints[1]["goal"] == "Data Platform Foundation: Land WMS orders"
    by_source = {i["source_id"]: i for i in env.ok("GET", "/issues")["issues"]}
    assert by_source["P1.S1.T1"]["due_date"] == "2026-11-03"
    assert by_source["P1.S2"]["due_date"] == "2026-11-20"
    assert by_source["P2.S1.T1"]["due_date"] == "2026-12-09"  # program day 28
    assert by_source["P2"]["due_date"] == "2026-12-25"  # program day 40 (weekends skipped only)
    assert by_source["P1.S1.T2"]["start_date"] == "2026-11-05"
    assert by_source["P1.S1"]["sprint_id"] == 1 and by_source["P2.S2.T1"]["sprint_id"] == 4
    assert by_source["P1"]["sprint_id"] is None and by_source["P1"]["assignee"] is None
    assert by_source["P1.S1.T1"]["assignee"] == "me"


def test_issue_detail_links_ac_branch_and_attachments(env: Env) -> None:
    _configure(env)
    story = env.ok("GET", "/issues/P1.S1")
    assert story["key"] == "DATA-2" and story["branch_name"] == "feat/DATA-2-land-wms-orders"
    assert [a["text"] for a in story["acceptance_criteria"]] == ["Orders land hourly", "Row counts reconcile"]
    assert [c["key"] for c in story["children"]] == ["DATA-3", "DATA-4", "DATA-5"]
    assert story["attachment_urls"] == [{"name": "P1-D1.svg", "path": "wireframes/P1-D1.svg",
                                         "url": "/api/tracker/attachments/wireframes/P1-D1.svg"}]
    assert env.client.get(f"{API}/attachments/wireframes/P1-D1.svg").status_code == 200
    assert env.client.get(f"{API}/attachments/../tracker/tickets/p01_platform.yaml").status_code == 404
    t2 = env.ok("GET", "/issues/DATA-4")
    assert [b["key"] for b in t2["links"]["blocked_by"]] == ["DATA-3"]
    assert env.ok("GET", "/issues/DATA-3")["links"]["blocks"][0]["key"] == "DATA-4"
    bug = env.ok("GET", "/issues/DATA-5")
    assert bug["type"] == "Bug" and bug["branch_name"].startswith("fix/DATA-5-")
    assert env.client.get(f"{API}/issues/DATA-999").status_code == 404


def test_jql_lite(env: Env) -> None:
    _configure(env)
    env.at(datetime(2026, 11, 4, 12, 0, tzinfo=UTC))  # Wednesday
    assert env.keys('project = DATA AND type = Epic') == ["DATA-1", "DATA-9"]
    assert env.keys("issuetype in (Bug, Spike) ORDER BY key") == ["DATA-5", "DATA-8"]
    assert env.keys('status = "to do" AND sprint = 1 ORDER BY key') == ["DATA-2", "DATA-3", "DATA-4", "DATA-5"]
    assert env.keys('sprint = "DATA Sprint 3"') == ["DATA-10", "DATA-11"]
    assert env.keys("sprint is EMPTY") == ["DATA-1", "DATA-9"]
    assert env.keys("epic = P2 ORDER BY key") == ["DATA-10", "DATA-11", "DATA-12", "DATA-13", "DATA-14"]
    assert env.keys("parent = DATA-6") == ["DATA-7", "DATA-8"]
    assert env.keys("labels = DQ") == ["DATA-4"]
    assert env.keys('component = dashboards') == ["DATA-6"]
    assert env.keys('duedate < "2026-11-05"') == ["DATA-3"]
    assert env.keys('duedate <= "2026-11-05" ORDER BY duedate DESC') == ["DATA-5", "DATA-3"]
    assert env.keys("duedate <= endOfWeek() AND duedate >= startOfWeek() ORDER BY key") == [
        "DATA-2", "DATA-3", "DATA-4", "DATA-5"]
    assert env.keys("duedate > endOfWeek(1) AND duedate < startOfWeek(3) ORDER BY key") == ["DATA-6", "DATA-7", "DATA-8"]
    assert env.keys("duedate < now() AND status != Done") == ["DATA-3"]
    assert env.keys('text ~ "freshness"') == ["DATA-2"]
    assert env.keys("key in (DATA-2, P2.S1) ORDER BY key") == ["DATA-2", "DATA-10"]
    assert env.keys("priority >= High AND type != Epic ORDER BY key") == ["DATA-2", "DATA-5"]
    assert env.keys('reporter = "Luis Moreno" AND type = Story') == ["DATA-10", "DATA-12"]
    assert len(env.keys("assignee = currentUser()")) == 12
    assert env.keys("assignee is EMPTY") == ["DATA-1", "DATA-9"]
    assert env.keys("storypoints > 5 ORDER BY storypoints DESC") == ["DATA-12", "DATA-6"]
    bad = env.client.get(f"{API}/issues", params={"jql": "colour = red"})
    assert bad.status_code == 400 and "Field 'colour' does not exist" in bad.json()["detail"]
    assert env.client.get(f"{API}/issues", params={"jql": "status = Blocked"}).status_code == 400
    assert env.client.get(f"{API}/issues", params={"jql": "status = "}).status_code == 400
    filtered = env.ok("GET", "/issues", params={"sprint": "1", "type": "Task", "status": "To Do"})
    assert [i["key"] for i in filtered["issues"]] == ["DATA-3", "DATA-4"]


def test_patch_comments_worklog_history_and_rank(env: Env) -> None:
    _configure(env)
    env.at(T0)
    out = env.ok("PATCH", "/issues/DATA-3", json={"status": "In Progress", "pr_url": "https://github.com/me/x/pull/1",
                                                  "acceptance_criteria": [{"idx": 0, "checked": True}]})
    assert out["status"] == "In Progress" and out["acceptance_criteria"][0]["checked"] is True
    env.at(T0 + timedelta(hours=3))
    done = env.ok("PATCH", "/issues/DATA-3", json={"status": "Done"})
    assert done["resolved_at"] == (T0 + timedelta(hours=3)).isoformat()
    assert env.client.patch(f"{API}/issues/DATA-3", json={"status": "Blocked"}).status_code == 400
    assert env.client.patch(f"{API}/issues/DATA-3", json={"sprint_id": 2}).status_code == 400  # sub-task
    assert env.client.patch(f"{API}/issues/DATA-3", json={"colour": "red"}).status_code == 422
    env.ok("POST", "/issues/DATA-3/comments", json={"body": "Asked Marcus about purge timing."})
    log = env.ok("POST", "/issues/DATA-3/worklog", json={"hours": 2.5, "date": "2026-11-02", "note": "spike"})
    assert log["hours"] == 2.5 and log["work_date"] == "2026-11-02"
    history = env.ok("GET", "/issues/DATA-3/history")
    assert [(h["field"], h["from_value"], h["to_value"]) for h in history] == [
        ("status", "To Do", "In Progress"), ("pr_url", None, "https://github.com/me/x/pull/1"),
        ("ac[0]", "False", "True"), ("status", "In Progress", "Done")]
    detail = env.ok("GET", "/issues/DATA-3")
    assert detail["logged_hours"] == 2.5 and detail["comment_count"] == 1 and len(detail["comments"]) == 1
    env.ok("POST", "/issues/DATA-5/rank", json={"before": "DATA-2"})
    order = [i["key"] for i in env.ok("GET", "/issues", params={"sprint": "1"})["issues"]]
    assert order.index("DATA-5") < order.index("DATA-2")
    assert env.client.post(f"{API}/issues/DATA-5/rank", json={}).status_code == 400
    moved = env.ok("PATCH", "/issues/DATA-6", json={"sprint_id": 3})
    assert moved["sprint_id"] == 3
    assert {c["key"]: env.ok("GET", f"/issues/{c['key']}")["sprint_id"] for c in moved["children"]} == {
        "DATA-7": 3, "DATA-8": 3}


def test_reload_is_idempotent_and_preserves_his_work(env: Env) -> None:
    _configure(env)
    env.at(T0)
    env.ok("PATCH", "/issues/DATA-4", json={"status": "In Review"})
    env.ok("POST", "/issues/DATA-4/comments", json={"body": "note"})
    env.ok("POST", "/issues/DATA-7/worklog", json={"hours": 1})
    own = env.ok("POST", "/issues", json={"type": "Task", "summary": "My extra check", "parent_key": "DATA-2",
                                          "estimate_hours": 2})
    assert own["key"] == "DATA-15" and own["sprint_id"] == 1 and own["epic_key"] == "DATA-1"

    _report, result = loader.load_catalog(env.conn, env.tickets)
    assert (result.inserted, result.updated, result.removed) == (0, 0, 0)

    docs = sample_catalog()
    docs[0]["stories"][0]["tasks"][1]["summary"] = "Renamed by the catalog"
    docs[0]["stories"][0]["tasks"].append(_task("P1.S1.T3", 3, 5))
    docs[0]["stories"][1]["tasks"] = [docs[0]["stories"][1]["tasks"][0]]  # drops P1.S2.T2 (no work on it)
    docs[1]["stories"][0]["tasks"][0]["depends_on"] = []
    del docs[0]["stories"][1]["tasks"][0]  # drops P1.S2.T1 (has a work log) -> kept, flagged removed
    docs[0]["stories"][1]["tasks"] = [_task("P1.S2.T9", 4, 14)]
    write_catalog(env.tickets, docs)
    _, result = loader.load_catalog(env.conn, env.tickets)
    assert result.renumbered is False and result.removed == 1 and result.kept_removed == 1
    t2 = env.ok("GET", "/issues/DATA-4")
    assert t2["summary"] == "Renamed by the catalog" and t2["status"] == "In Review" and len(t2["comments"]) == 1
    new = env.ok("GET", "/issues/P1.S1.T3")
    assert new["key"] in ("DATA-16", "DATA-17") and new["status"] == "To Do"
    assert env.ok("GET", "/issues/DATA-7")["removed"] is True
    assert env.client.get(f"{API}/issues/DATA-8").status_code == 404
    assert "DATA-7" not in env.keys("sprint = 2")
    assert env.ok("GET", "/issues/DATA-15")["summary"] == "My extra check"


def test_catalog_renumbers_in_file_order_before_any_work(env: Env) -> None:
    env.ok("GET", "/meta")
    docs = sample_catalog()
    docs[0]["stories"][0]["tasks"].insert(0, _task("P1.S1.T0", 2, 1))
    write_catalog(env.tickets, docs)
    _, result = loader.load_catalog(env.conn, env.tickets)
    assert result.renumbered is True
    assert env.ok("GET", "/issues/P1.S1.T0")["key"] == "DATA-3"
    assert env.ok("GET", "/issues/P2")["key"] == "DATA-10"


def test_invalid_catalog_is_reported_and_not_loaded(env: Env) -> None:
    env.ok("GET", "/meta")
    docs = sample_catalog()
    docs[1]["stories"][0]["reporter"] = "ghost"
    write_catalog(env.tickets, docs)
    r = env.client.post(f"{API}/catalog/reload")
    assert r.status_code == 422 and r.json()["loaded"] is False
    assert any("unknown people key 'ghost'" in e for e in r.json()["errors"])
    meta = env.ok("GET", "/meta")
    assert meta["catalog"]["issues"] == 14 and any("ghost" in e for e in meta["catalog_errors"])
    assert env.ok("GET", "/catalog")["errors"]


def test_sprint_start_complete_burndown_velocity_cfd(env: Env) -> None:
    _configure(env)
    env.at(T0)
    started = env.ok("POST", "/sprints/1/start")
    assert started["state"] == "active" and started["started_at"] == T0.isoformat()
    assert env.client.post(f"{API}/sprints/3/start").status_code == 409  # one active sprint at a time
    assert env.keys("sprint in openSprints() AND type = Story") == ["DATA-2"]

    env.at(datetime(2026, 11, 3, 10, tzinfo=UTC))
    env.ok("PATCH", "/issues/DATA-3", json={"status": "Done"})  # 8 h
    env.at(datetime(2026, 11, 4, 10, tzinfo=UTC))
    env.ok("POST", "/issues", json={"type": "Task", "summary": "Scope creep", "parent_key": "DATA-2",
                                    "estimate_hours": 2})
    env.at(datetime(2026, 11, 5, 10, tzinfo=UTC))
    env.ok("PATCH", "/issues/DATA-5", json={"status": "Done"})  # 4 h
    env.at(datetime(2026, 11, 5, 18, tzinfo=UTC))

    bd = env.ok("GET", "/reports/burndown", params={"sprint": 1})
    assert bd["committed"] == 28 and bd["metric"] == "hours"
    by_day = {d["date"]: d for d in bd["days"]}
    assert [by_day[d]["remaining"] for d in ("2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05")] == [
        28, 20, 22, 18]
    assert by_day["2026-11-06"]["remaining"] is None
    assert by_day["2026-11-02"]["ideal"] == 28 and by_day["2026-11-13"]["ideal"] == 0
    assert by_day["2026-11-07"]["working"] is False
    assert [(c["key"], c["change"], c["value"]) for c in bd["scope_changes"]] == [("DATA-15", "added", 2.0)]
    points = env.ok("GET", "/reports/burndown", params={"sprint": 1, "metric": "points"})
    assert points["committed"] == 5

    env.at(datetime(2026, 11, 13, 16, tzinfo=UTC))
    env.ok("PATCH", "/issues/DATA-2", json={"status": "Done"})
    done = env.ok("POST", "/sprints/1/complete", json={"move_open_to": 2})
    assert done["state"] == "closed" and sorted(done["moved"]) == ["DATA-15", "DATA-4"]
    assert env.ok("GET", "/issues/DATA-4")["sprint_id"] == 2
    assert env.client.post(f"{API}/sprints/1/complete", json={}).status_code == 409
    velocity = env.ok("GET", "/reports/velocity")
    s1 = velocity["sprints"][0]
    assert (s1["committed_points"], s1["completed_points"]) == (5, 5)
    assert (s1["committed_hours"], s1["completed_hours"]) == (28, 12)
    assert velocity["sprints"][1]["completed_points"] is None and velocity["average_completed_points_last_3"] == 5

    cfd = env.ok("GET", "/reports/cfd", params={"from": "2026-11-02", "sprint": 2})
    day = {d["date"]: d for d in cfd["days"]}
    assert cfd["statuses"] == ["To Do", "In Progress", "In Review", "Done"]
    assert day["2026-11-13"]["To Do"] >= 2


def test_jira_csv_export(env: Env) -> None:
    _configure(env)
    r = env.client.get(f"{API}/export/jira.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.headers["X-Jira-Date-Format"] == "dd/MMM/yy h:mm a"
    rows = list(csv.reader(io.StringIO(r.text)))
    header = rows[0]
    assert header[:6] == ["Issue Id", "Parent Id", "Issue Type", "Summary", "Description", "Priority"]
    assert header.count("Labels") == 2 and header.count("Component/s") == 1
    assert header[-5:] == ["Due date", "Original Estimate", "Story point estimate", "Sprint", "Status"]
    data = [dict(zip(header, row, strict=True)) | {"_labels": row[6:8]} for row in rows[1:]]
    types = [d["Issue Type"] for d in data]
    assert types[:2] == ["Epic", "Epic"] and types.index("Sub-task") > types.index("Story")  # parents first
    by_id = {d["Issue Id"]: d for d in data}
    assert by_id["3"]["Issue Type"] == "Sub-task" and by_id["3"]["Parent Id"] == "2"
    assert by_id["3"]["Original Estimate"] == "28800" and by_id["3"]["Sprint"] == ""
    assert by_id["3"]["Due date"] == "03/Nov/26 12:00 AM"
    assert by_id["5"]["Issue Type"] == "Bug" and by_id["5"]["Parent Id"] == "1"
    assert "originally under story DATA-2" in by_id["5"]["Description"]
    assert by_id["8"]["Issue Type"] == "Task" and by_id["8"]["Parent Id"] == "1"  # Spike -> Task under the epic
    story = by_id["2"]
    assert story["Story point estimate"] == "5" and story["Sprint"] == "DATA Sprint 1"
    assert story["_labels"] == ["wms", "data-quality"]
    assert "h3. Acceptance criteria\n* Orders land hourly" in story["Description"]
    assert by_id["1"]["Original Estimate"] == "" and by_id["1"]["Parent Id"] == ""
    assert {d["Status"] for d in data} == {"To Do"}


def test_backup_and_restore_round_trip(env: Env) -> None:
    _configure(env)
    env.at(T0)
    env.ok("PATCH", "/issues/DATA-3", json={"status": "Done"})
    env.ok("POST", "/issues/DATA-3/comments", json={"body": "before backup"})
    doc = env.ok("GET", "/backup")
    assert doc["format"] == backup.FORMAT and len(doc["tables"]["issue"]) == 14
    env.ok("POST", "/issues/DATA-3/comments", json={"body": "after backup"})
    env.ok("PATCH", "/issues/DATA-3", json={"status": "To Do"})
    restored = env.ok("POST", "/restore", json=doc)["restored"]
    assert restored["issue"] == 14 and restored["comment"] == 1
    issue = env.ok("GET", "/issues/DATA-3")
    assert issue["status"] == "Done" and [c["body"] for c in issue["comments"]] == ["before backup"]
    assert env.ok("GET", "/meta")["start_date"] == "2026-10-31"
    env.ok("POST", "/issues/DATA-3/comments", json={"body": "sequence still works"})
    assert env.client.post(f"{API}/restore", json={"format": "other"}).status_code == 400
    assert yaml.safe_load(env.tickets.joinpath("p01_platform.yaml").read_text())["project"] == 1


def test_shipped_catalog_loads_and_exports(env: Env, pg_settings: Settings) -> None:
    tickets = pg_settings.tracker_dir / "tickets"
    if not any(tickets.glob("*.yaml")):
        pytest.skip("ticket catalog not written yet")
    report, result = loader.load_catalog(env.conn, tickets, pg_settings.config_dir, pg_settings.brand_dir)
    assert report.errors == [] and result.inserted == len(report.issues)
    _, again = loader.load_catalog(env.conn, tickets, pg_settings.config_dir, pg_settings.brand_dir)
    assert (again.inserted, again.updated, again.removed) == (0, 0, 0)
    loader.apply_start_date(env.conn, datetime(2026, 11, 2).date())
    issues = env.ok("GET", "/issues")["issues"]
    assert len(issues) == len(report.issues) and all(i["due_date"] for i in issues)
    sprints = {s["id"]: s for s in env.ok("GET", "/sprints")}
    for issue in issues:
        if issue["sprint_id"]:
            assert issue["type"] != "Epic"
            assert issue["project"] == sprints[issue["sprint_id"]]["project"]
    rows = list(csv.reader(io.StringIO(env.client.get(f"{API}/export/jira.csv").text)))
    assert len(rows) == len(issues) + 1
