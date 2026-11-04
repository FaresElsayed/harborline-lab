"""Program calendar: working days skip Saturday and Sunday only.

Program day 1 is the first working day on or after the configured start date. Each project spans 20
working days; sprint n spans program days (n-1)*10+1 .. n*10; project p owns sprints 2p-1 and 2p.
"""

from __future__ import annotations

from datetime import date, timedelta

DAYS_PER_SPRINT = 10
DAYS_PER_PROJECT = 20
PROJECTS = 8
SPRINTS = PROJECTS * 2


def first_workday(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def workday(start: date, n: int) -> date:
    """Date of program working day ``n`` (1-based)."""
    if n < 1:
        raise ValueError("working day numbers start at 1")
    d = first_workday(start)
    weeks, rest = divmod(n - 1, 5)
    d += timedelta(weeks=weeks)
    while rest:
        d += timedelta(days=1)
        if d.weekday() < 5:
            rest -= 1
    return d


def program_day(project: int, day: int) -> int:
    return (project - 1) * DAYS_PER_PROJECT + day


def sprint_id(project: int, sprint_in_project: int) -> int:
    return (project - 1) * 2 + sprint_in_project


def sprint_project(sprint: int) -> int:
    return (sprint + 1) // 2


def sprint_program_days(sprint: int) -> tuple[int, int]:
    return (sprint - 1) * DAYS_PER_SPRINT + 1, sprint * DAYS_PER_SPRINT


def sprint_dates(start: date, sprint: int) -> tuple[date, date]:
    first, last = sprint_program_days(sprint)
    return workday(start, first), workday(start, last)


def is_workday(d: date) -> bool:
    return d.weekday() < 5


def days_between(first: date, last: date) -> list[date]:
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]
