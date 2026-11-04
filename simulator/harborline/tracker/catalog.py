"""Strict reader for the ticket catalog ``tracker/tickets/pNN_<slug>.yaml`` (build spec section 6).

``read_catalog`` validates every file, collects *all* problems (file + YAML path + message) and returns
the flattened issues in file order: epic, then each story followed by its tasks. Structural problems are
errors (the catalog is not loaded); pacing rules (hours per project, ticket counts, missing attachments)
are warnings.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator

from harborline.tracker import workdays

Priority = Literal["Highest", "High", "Medium", "Low", "Lowest"]
Summary = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
PROJECT_HOURS = (150, 160)
STORIES_PER_PROJECT = (5, 6)
TASKS_PER_PROJECT = (40, 50)


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("description", "acceptance_criteria", "definition_of_done", "labels", "components",
                     "depends_on", "attachments", mode="before", check_fields=False)
    @classmethod
    def _none_is_empty(cls, value: Any, info: Any) -> Any:
        if value is None:
            return "" if info.field_name == "description" else []
        if info.field_name == "definition_of_done" and isinstance(value, str):
            return [line.strip("-* ").strip() for line in value.splitlines() if line.strip()]
        return value


class TaskSpec(_Spec):
    id: str
    type: Literal["Task", "Bug", "Spike"] = "Task"
    summary: Summary
    estimate_hours: float = Field(gt=0, le=80)
    due_day: int = Field(ge=1, le=workdays.DAYS_PER_PROJECT)
    priority: Priority = "Medium"
    reporter: str | None = None
    components: list[str] = []
    labels: list[str] = []
    description: str = ""
    acceptance_criteria: list[str] = []
    definition_of_done: list[str] = []
    depends_on: list[str] = []
    attachments: list[str] = []


class StorySpec(_Spec):
    id: str
    type: Literal["Story"] = "Story"
    summary: Summary
    reporter: str
    priority: Priority = "Medium"
    story_points: float = Field(ge=0, le=100)
    sprint: Literal[1, 2]
    due_day: int = Field(ge=1, le=workdays.DAYS_PER_PROJECT)
    components: list[str] = []
    labels: list[str] = []
    description: str = ""
    acceptance_criteria: list[str] = []
    definition_of_done: list[str] = []
    depends_on: list[str] = []
    attachments: list[str] = []
    tasks: list[TaskSpec] = Field(min_length=1)


class EpicSpec(_Spec):
    id: str
    summary: Summary
    department: str
    reporter: str
    priority: Priority = "High"
    labels: list[str] = []
    components: list[str] = []
    description: str = ""
    acceptance_criteria: list[str] = []
    definition_of_done: list[str] = []
    dashboards: list[str] = []


class ProjectFile(_Spec):
    project: int = Field(ge=1, le=workdays.PROJECTS)
    epic: EpicSpec
    stories: list[StorySpec] = Field(min_length=1)


@dataclass
class CatalogIssue:
    source_id: str
    project: int
    type: str
    summary: str
    description: str
    priority: str
    reporter: str
    department: str
    epic_source: str | None
    parent_source: str | None
    sprint: int | None  # program sprint id (1..16); None for epics
    story_points: float | None
    estimate_hours: float | None
    due_day: int
    start_day: int
    labels: list[str]
    components: list[str]
    attachments: list[str]
    dashboards: list[str]
    definition_of_done: list[str]
    acceptance_criteria: list[str]
    depends_on: list[str]
    file: str


@dataclass
class CatalogReport:
    files: list[str] = field(default_factory=list)
    issues: list[CatalogIssue] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    digest: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.issues)

    def summary(self) -> dict[str, Any]:
        return {"files": self.files, "issues": len(self.issues), "errors": self.errors, "warnings": self.warnings,
                "digest": self.digest}


def catalog_files(tickets_dir: Path) -> list[Path]:
    return sorted(p for p in tickets_dir.glob("*.y*ml") if p.suffix in (".yaml", ".yml"))


def signature(tickets_dir: Path) -> str:
    """Cheap change detector (names, sizes, mtimes)."""
    parts = [f"{p.name}:{p.stat().st_size}:{p.stat().st_mtime_ns}" for p in catalog_files(tickets_dir)]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _loc(loc: tuple[Any, ...]) -> str:
    out = ""
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out or "(root)"


def _task_start(due_day: int, hours: float, sprint_first_day: int) -> int:
    span = max(1, math.ceil(hours / 8))
    return max(1, min(due_day, max(sprint_first_day, due_day - span + 1)))


def _flatten(doc: ProjectFile, file: str) -> list[CatalogIssue]:
    p, epic = doc.project, doc.epic
    out = [CatalogIssue(
        source_id=epic.id, project=p, type="Epic", summary=epic.summary, description=epic.description,
        priority=epic.priority, reporter=epic.reporter, department=epic.department, epic_source=None,
        parent_source=None, sprint=None, story_points=None, estimate_hours=None, due_day=workdays.DAYS_PER_PROJECT,
        start_day=1, labels=epic.labels, components=epic.components, attachments=[], dashboards=epic.dashboards,
        definition_of_done=epic.definition_of_done, acceptance_criteria=epic.acceptance_criteria, depends_on=[],
        file=file,
    )]
    for story in doc.stories:
        sprint_first = (story.sprint - 1) * workdays.DAYS_PER_SPRINT + 1
        tasks = []
        for task in story.tasks:
            tasks.append(CatalogIssue(
                source_id=task.id, project=p, type=task.type, summary=task.summary, description=task.description,
                priority=task.priority, reporter=task.reporter or story.reporter, department=epic.department,
                epic_source=epic.id, parent_source=story.id, sprint=workdays.sprint_id(p, story.sprint),
                story_points=None, estimate_hours=task.estimate_hours, due_day=task.due_day,
                start_day=_task_start(task.due_day, task.estimate_hours, sprint_first), labels=task.labels,
                components=task.components, attachments=task.attachments, dashboards=[],
                definition_of_done=task.definition_of_done, acceptance_criteria=task.acceptance_criteria,
                depends_on=task.depends_on, file=file,
            ))
        story_start = min([sprint_first, *(t.start_day for t in tasks)])
        out.append(CatalogIssue(
            source_id=story.id, project=p, type="Story", summary=story.summary, description=story.description,
            priority=story.priority, reporter=story.reporter, department=epic.department, epic_source=epic.id,
            parent_source=epic.id, sprint=workdays.sprint_id(p, story.sprint), story_points=story.story_points,
            estimate_hours=None, due_day=story.due_day, start_day=story_start, labels=story.labels,
            components=story.components, attachments=story.attachments, dashboards=[],
            definition_of_done=story.definition_of_done, acceptance_criteria=story.acceptance_criteria,
            depends_on=story.depends_on, file=file,
        ))
        out.extend(tasks)
    return out


def _check_file(doc: ProjectFile, file: str, people: set[str], brand_dir: Path | None,
                errors: list[str], warnings: list[str]) -> None:
    p = doc.project
    if doc.epic.id != f"P{p}":
        errors.append(f"{file}: epic.id: expected 'P{p}', got '{doc.epic.id}'")
    if doc.epic.reporter not in people:
        errors.append(f"{file}: epic.reporter: unknown people key '{doc.epic.reporter}'")
    if not doc.epic.dashboards:
        warnings.append(f"{file}: epic.dashboards: empty (every project should end in dashboards)")
    hours = 0.0
    task_count = 0
    for si, story in enumerate(doc.stories):
        sloc = f"{file}: stories[{si}]"
        if not re.fullmatch(rf"P{p}\.S\d+", story.id):
            errors.append(f"{sloc}.id: expected 'P{p}.S<n>', got '{story.id}'")
        if story.reporter not in people:
            errors.append(f"{sloc}.reporter: unknown people key '{story.reporter}'")
        first, last = (story.sprint - 1) * 10 + 1, story.sprint * 10
        if not first <= story.due_day <= last:
            errors.append(f"{sloc}.due_day: {story.due_day} is outside sprint {story.sprint} (days {first}-{last})")
        if not story.acceptance_criteria:
            warnings.append(f"{sloc}: no acceptance_criteria")
        for ti, task in enumerate(story.tasks):
            tloc = f"{sloc}.tasks[{ti}]"
            task_count += 1
            hours += task.estimate_hours
            if not re.fullmatch(rf"{re.escape(story.id)}\.[A-Z]+\d+", task.id):
                errors.append(f"{tloc}.id: expected '{story.id}.T<n>', got '{task.id}'")
            if task.reporter is not None and task.reporter not in people:
                errors.append(f"{tloc}.reporter: unknown people key '{task.reporter}'")
            if task.due_day > story.due_day:
                errors.append(f"{tloc}.due_day: {task.due_day} is after its story's due_day {story.due_day}")
            if task.due_day < first:
                warnings.append(f"{tloc}.due_day: {task.due_day} is before its story's sprint starts (day {first})")
            _check_labels(tloc, task.labels, warnings)
            _check_attachments(tloc, task.attachments, brand_dir, warnings)
        _check_labels(sloc, story.labels, warnings)
        _check_attachments(sloc, story.attachments, brand_dir, warnings)
    lo, hi = PROJECT_HOURS
    if not lo <= hours <= hi:
        warnings.append(f"{file}: task estimate_hours sum to {hours:g} h (expected {lo}-{hi})")
    if not STORIES_PER_PROJECT[0] <= len(doc.stories) <= STORIES_PER_PROJECT[1]:
        warnings.append(f"{file}: {len(doc.stories)} stories (expected about {STORIES_PER_PROJECT[0]}-"
                        f"{STORIES_PER_PROJECT[1]})")
    if not TASKS_PER_PROJECT[0] <= task_count <= TASKS_PER_PROJECT[1]:
        warnings.append(f"{file}: {task_count} tasks (expected about {TASKS_PER_PROJECT[0]}-{TASKS_PER_PROJECT[1]})")


def _check_labels(loc: str, labels: list[str], warnings: list[str]) -> None:
    for label in labels:
        if re.search(r"\s", label):
            warnings.append(f"{loc}.labels: '{label}' contains whitespace (Jira labels cannot; exported with '-')")


def _check_attachments(loc: str, attachments: list[str], brand_dir: Path | None, warnings: list[str]) -> None:
    if brand_dir is None:
        return
    for path in attachments:
        if not (brand_dir / path).is_file():
            warnings.append(f"{loc}.attachments: '{path}' not found under {brand_dir}")


def _check_graph(issues: list[CatalogIssue], errors: list[str], warnings: list[str]) -> None:
    by_id = {i.source_id: i for i in issues}
    for issue in issues:
        for dep in issue.depends_on:
            if dep == issue.source_id:
                errors.append(f"{issue.file}: {issue.source_id}.depends_on: depends on itself")
            elif dep not in by_id:
                errors.append(f"{issue.file}: {issue.source_id}.depends_on: unknown ticket id '{dep}'")
            else:
                other = by_id[dep]
                mine = workdays.program_day(issue.project, issue.due_day)
                theirs = workdays.program_day(other.project, other.due_day)
                if theirs > mine:
                    warnings.append(f"{issue.file}: {issue.source_id} (program day {mine}) depends on {dep} which is "
                                    f"due later (program day {theirs})")
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for dep in by_id[node].depends_on:
            if dep not in by_id or dep == node:
                continue
            if state.get(dep) == 1:
                cycle = stack[stack.index(dep):] + [dep]
                errors.append(f"dependency cycle: {' -> '.join(cycle)}")
            elif dep not in state:
                visit(dep)
        stack.pop()
        state[node] = 2

    for sid in by_id:
        if sid not in state:
            visit(sid)


def read_catalog(tickets_dir: Path, people: set[str], brand_dir: Path | None = None) -> CatalogReport:
    report = CatalogReport()
    files = catalog_files(tickets_dir)
    if not files:
        report.errors.append(f"no ticket files (*.yaml) found in {tickets_dir}")
        return report
    digest = hashlib.sha256()
    projects: dict[int, str] = {}
    for path in files:
        name = path.name
        report.files.append(name)
        raw = path.read_bytes()
        digest.update(name.encode() + b"\0" + raw)
        try:
            data = yaml.safe_load(raw.decode("utf-8"))
        except (yaml.YAMLError, UnicodeDecodeError) as exc:
            report.errors.append(f"{name}: invalid YAML: {exc}".replace("\n", " "))
            continue
        try:
            doc = ProjectFile.model_validate(data)
        except ValidationError as exc:
            for err in exc.errors():
                value = err.get("input")
                shown = f" (got {value!r})" if not isinstance(value, (dict, list)) else ""
                report.errors.append(f"{name}: {_loc(err['loc'])}: {err['msg']}{shown}")
            continue
        m = re.match(r"p(\d+)", name)
        if m and int(m.group(1)) != doc.project:
            report.errors.append(f"{name}: project: {doc.project} does not match the file name prefix p{m.group(1)}")
        if doc.project in projects:
            report.errors.append(f"{name}: project {doc.project} is already defined in {projects[doc.project]}")
            continue
        projects[doc.project] = name
        _check_file(doc, name, people, brand_dir, report.errors, report.warnings)
        report.issues.extend(_flatten(doc, name))
    seen: dict[str, str] = {}
    for issue in report.issues:
        if issue.source_id in seen:
            report.errors.append(f"{issue.file}: duplicate ticket id '{issue.source_id}' (also in "
                                 f"{seen[issue.source_id]})")
        seen.setdefault(issue.source_id, issue.file)
    _check_graph(report.issues, report.errors, report.warnings)
    report.digest = digest.hexdigest()
    return report
