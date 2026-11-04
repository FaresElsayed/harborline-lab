"""Harborline Tracker API (prefix ``/api/tracker``; data in database ``tracker``, schema ``tracker``)."""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

import psycopg
from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from harborline.tracker import backup as backup_mod
from harborline.tracker import jira_export, loader, reports, runtime, service
from harborline.tracker.catalog import read_catalog
from harborline.tracker.jql import JqlError
from harborline.tracker.service import TrackerError

DB = "tracker"
log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/tracker", tags=["tracker"])

_schema_lock = threading.Lock()
_schema_ready = False


def _now() -> datetime:
    """Wall clock (tracker work happens in real time). Tests patch this."""
    return datetime.now(UTC)


@contextmanager
def _db() -> Iterator[psycopg.Connection]:
    global _schema_ready
    with runtime.connection(DB) as conn:
        if not _schema_ready:
            with _schema_lock:
                if not _schema_ready:
                    loader.ensure_schema(conn)
                    _schema_ready = True
        try:
            yield conn
        except TrackerError as exc:
            raise HTTPException(exc.status, exc.message) from None
        except JqlError as exc:
            raise HTTPException(400, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None


def startup() -> None:
    """Call once at app start: create the schema and load (or refresh) the ticket catalog."""
    try:
        with _db() as conn:
            loader.ensure_loaded(conn)
    except (psycopg.Error, HTTPException) as exc:
        log.warning("tracker startup skipped: %s", exc)


def reset_schema_flag() -> None:
    global _schema_ready
    _schema_ready = False


# --------------------------------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------------------------------


class SetupIn(BaseModel):
    start_date: date


class AcItem(BaseModel):
    idx: int
    checked: bool


class IssuePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str | None = None
    description: str | None = None
    status: str | None = None
    priority: str | None = None
    assignee: str | None = None
    sprint_id: int | None = None
    labels: list[str] | None = None
    components: list[str] | None = None
    story_points: float | None = None
    estimate_hours: float | None = None
    due_date: date | None = None
    pr_url: str | None = None
    epic_key: str | None = None
    parent_key: str | None = None
    acceptance_criteria: list[AcItem] | None = None


class IssueCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["Epic", "Story", "Task", "Bug", "Spike"]
    summary: str = Field(min_length=1, max_length=255)
    description: str = ""
    status: str = "To Do"
    priority: str = "Medium"
    assignee: str | None = "me"
    epic_key: str | None = None
    parent_key: str | None = None
    sprint_id: int | None = None
    labels: list[str] = []
    components: list[str] = []
    estimate_hours: float | None = Field(default=None, gt=0)
    story_points: float | None = Field(default=None, ge=0)
    due_date: date | None = None
    acceptance_criteria: list[str] = []
    blocked_by: list[str] = []


class CommentIn(BaseModel):
    body: str = Field(min_length=1)


class WorklogIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    hours: float
    work_date: date | None = Field(default=None, alias="date")
    note: str | None = None


class RankIn(BaseModel):
    before: str | None = None
    after: str | None = None


class CompleteIn(BaseModel):
    move_open_to: int | Literal["backlog"] | None = None


# --------------------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------------------


@router.get("/meta")
def get_meta() -> dict[str, Any]:
    with _db() as conn:
        loader.ensure_loaded(conn)
        return service.meta(conn, _now())


@router.post("/setup")
def setup(body: SetupIn) -> dict[str, Any]:
    with _db() as conn:
        loader.ensure_loaded(conn)
        loader.apply_start_date(conn, body.start_date)
        return service.meta(conn, _now())


@router.get("/catalog")
def catalog_check() -> dict[str, Any]:
    """Validate the ticket YAML files without loading them."""
    tickets = runtime.tracker_dir() / "tickets"
    people = {p["key"] for p in loader.people()}
    return read_catalog(tickets, people, runtime.brand_dir()).summary()


@router.post("/catalog/reload")
def catalog_reload() -> JSONResponse:
    with _db() as conn:
        try:
            report, result = loader.load_catalog(conn)
        except loader.CatalogInvalid as exc:
            return JSONResponse({"loaded": False, **exc.report.summary()}, status_code=422)
        return JSONResponse({"loaded": True, **report.summary(), "result": result.as_dict()})


@router.get("/issues")
def list_issues(jql: str | None = None, sprint: str | None = None, epic: str | None = None,
                status: str | None = None, type: str | None = None) -> dict[str, Any]:
    with _db() as conn:
        issues = service.list_issues(conn, _now(), jql=jql, sprint=sprint, epic=epic, status=status, issue_type=type)
        return {"total": len(issues), "issues": issues}


@router.post("/issues", status_code=201)
def create_issue(body: IssueCreate) -> dict[str, Any]:
    with _db() as conn:
        return service.create_issue(conn, body.model_dump(), _now())


@router.get("/issues/{key}")
def get_issue(key: str) -> dict[str, Any]:
    with _db() as conn:
        return service.get_issue(conn, key, _now())


@router.patch("/issues/{key}")
def patch_issue(key: str, body: IssuePatch) -> dict[str, Any]:
    changes = body.model_dump(exclude_unset=True)
    with _db() as conn:
        return service.patch_issue(conn, key, changes, _now())


@router.post("/issues/{key}/comments", status_code=201)
def add_comment(key: str, body: CommentIn) -> dict[str, Any]:
    with _db() as conn:
        return service.add_comment(conn, key, body.body, _now())


@router.post("/issues/{key}/worklog", status_code=201)
def add_worklog(key: str, body: WorklogIn) -> dict[str, Any]:
    now = _now()
    with _db() as conn:
        return service.add_worklog(conn, key, body.hours, body.work_date or now.date(), body.note, now)


@router.get("/issues/{key}/history")
def get_history(key: str) -> list[dict[str, Any]]:
    with _db() as conn:
        return service.history(conn, key)


@router.post("/issues/{key}/rank")
def rank(key: str, body: RankIn) -> dict[str, Any]:
    with _db() as conn:
        return service.rank_issue(conn, key, body.before, body.after, _now())


@router.get("/sprints")
def sprints() -> list[dict[str, Any]]:
    with _db() as conn:
        return service.list_sprints(conn)


@router.post("/sprints/{sprint_id}/start")
def start_sprint(sprint_id: int) -> dict[str, Any]:
    with _db() as conn:
        return service.start_sprint(conn, sprint_id, _now())


@router.post("/sprints/{sprint_id}/complete")
def complete_sprint(sprint_id: int, body: CompleteIn | None = None) -> dict[str, Any]:
    target = body.move_open_to if body else None
    with _db() as conn:
        return service.complete_sprint(conn, sprint_id, None if target == "backlog" else target, _now())


@router.get("/reports/burndown")
def burndown(sprint: int, metric: Literal["hours", "points"] = "hours") -> dict[str, Any]:
    with _db() as conn:
        return reports.burndown(conn, sprint, _now(), metric)


@router.get("/reports/velocity")
def velocity() -> dict[str, Any]:
    with _db() as conn:
        return reports.velocity(conn, _now())


@router.get("/reports/cfd")
def cfd(from_: Annotated[date | None, Query(alias="from")] = None, to: date | None = None, epic: str | None = None,
        sprint: int | None = None) -> dict[str, Any]:
    with _db() as conn:
        return reports.cfd(conn, _now(), from_, to, epic, sprint)


@router.get("/export/jira.csv")
def export_jira() -> Response:
    with _db() as conn:
        body = jira_export.export_csv(service.jira_rows(conn))
    headers = {"Content-Disposition": 'attachment; filename="harborline-tracker-jira.csv"',
               "X-Jira-Date-Format": jira_export.JIRA_DATE_FORMAT}
    return Response(body, media_type="text/csv; charset=utf-8", headers=headers)


@router.get("/backup")
def get_backup() -> JSONResponse:
    with _db() as conn:
        doc = backup_mod.backup(conn)
    stamp = _now().strftime("%Y%m%d-%H%M")
    return JSONResponse(doc, headers={"Content-Disposition": f'attachment; filename="tracker-backup-{stamp}.json"'})


@router.post("/restore")
async def restore(request: Request) -> dict[str, Any]:
    try:
        doc = await request.json()
    except ValueError:
        raise HTTPException(400, "body must be a JSON backup document") from None
    def run() -> dict[str, Any]:
        with _db() as conn:
            return {"restored": backup_mod.restore(conn, doc)}

    return await run_in_threadpool(run)


@router.get("/attachments/{path:path}")
def attachment(path: str) -> FileResponse:
    root = runtime.brand_dir().resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(404, f"attachment {path} not found")
    return FileResponse(target)
