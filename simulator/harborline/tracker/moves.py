"""Tickets that got a new catalog id in a release, so the work on them follows the ticket and not the id.

A catalog id normally belongs to one ticket for good: an update rewrites the ticket's words in place and the status,
comments and work log stay on it. When a release reorders tickets, the same id starts to mean a different ticket, and
without this list his "In Progress" would land on whatever ticket took the old number.

Only a ticket he has worked on moves. One he hasn't touched has nothing to carry, so it simply becomes whatever the
catalog now says for its id, which also keeps it in the right sprint.

An entry applies only while the ticket still has the summary it had before the move. So it runs once, and does nothing
on a fresh install or on an install from any other version.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from harborline.tracker.catalog import CatalogIssue

MOVING = "moving:"
RETIRED = "retired:"


@dataclass(frozen=True)
class Move:
    was: str
    summary: str
    now: str | None  # None: folded into a ticket that stays put, so work on it stays visible as a removed ticket


# Project 1 was reordered so that it starts easy: first a landed table, then a first number, then the harder tickets.
MOVED = (
    Move("P1.S2.T6", "Infrastructure as code for the platform", "P1.S5.T8"),
    Move("P1.S3.T1", "Ingestion strategy per WMS table (ADR)", "P1.S3.T5"),
    Move("P1.S3.T2", "Config-driven extract to the landing zone", "P1.S3.T3"),
    Move("P1.S3.T3", "Incremental loads for tables with audit timestamps", "P1.S3.T6"),
    Move("P1.S3.T4", "Capture changes to order_line, which has no audit timestamp", "P1.S4.T1"),
    Move("P1.S3.T5", "Detect hard deletes across the WMS", "P1.S4.T2"),
    Move("P1.S3.T6", "Keep task history beyond the 180-day purge", "P1.S4.T2"),  # shares a ticket with hard deletes
    Move("P1.S3.T7", "Backfill the full WMS history", "P1.S3.T4"),
    Move("P1.S3.T8", "Daily snapshots of AX customers and Salesforce accounts for the client crosswalk", "P1.S3.T7"),
    Move("P1.S4.T1", "Layer conventions and modeling tool (ADR)", "P1.S3.T8"),
    Move("P1.S4.T2", "Silver models for the WMS", "P1.S3.T9"),
    Move("P1.S4.T3", "Normalize units of measure", "P1.S3.T10"),
    Move("P1.S4.T4", "Data-quality framework and results table", "P1.S4.T3"),
    Move("P1.S4.T5", "Quarantine and re-processing", "P1.S4.T4"),
    Move("P1.S4.T6", "Data-quality rules for the WMS", "P1.S4.T5"),
    Move("P1.S4.T7", "Cross-system client key check", "P1.S4.T6"),
    Move("P1.S4.T8", "Measure DQ precision and recall against the answer key", "P1.S4.T7"),
    Move("P1.S6.T5", "Lock down access and verify performance", None),
    Move("P1.S6.T6", "UAT sign-off from Iris Novak for P1-D1", "P1.S6.T5"),
    Move("P1.S6.T7", "Readout to Sam and release v0.1.0", "P1.S6.T6"),
)


def has_work(conn: psycopg.Connection, key: str) -> bool:
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM tracker.history WHERE issue_key = %(k)s) "
        "OR EXISTS (SELECT 1 FROM tracker.comment WHERE issue_key = %(k)s) "
        "OR EXISTS (SELECT 1 FROM tracker.worklog WHERE issue_key = %(k)s) "
        "OR EXISTS (SELECT 1 FROM tracker.ac_check WHERE issue_key = %(k)s AND checked) "
        "OR EXISTS (SELECT 1 FROM tracker.issue WHERE key = %(k)s AND (status <> 'To Do' OR pr_url IS NOT NULL))",
        {"k": key},
    ).fetchone()
    return bool(row[0])


def follow_moves(conn: psycopg.Connection, issues: list[CatalogIssue], moves: tuple[Move, ...] = MOVED) -> int:
    """Give each worked-on ticket its new id. A ticket he hasn't touched takes over the id that frees up."""
    rows = {source_id: (key, summary) for key, source_id, summary in conn.execute(
        "SELECT key, source_id, summary FROM tracker.issue WHERE origin = 'catalog' AND NOT removed")}
    sprints = {i.source_id: i.sprint for i in issues}
    assign: dict[str, str] = {}  # issue key -> its new source id
    wanted: set[str] = set()
    moved = 0
    for move in moves:
        row = rows.get(move.was)
        if row is None or row[1] != move.summary or not has_work(conn, row[0]):
            continue
        if move.now is None or move.now in wanted:
            assign[row[0]] = RETIRED + move.was
            continue
        assign[row[0]] = move.now
        wanted.add(move.now)
        moved += 1
    if not assign:
        return 0
    leaving = {source_id for source_id, (key, _) in rows.items() if key in assign}
    freed = sorted((leaving - wanted) & set(sprints))
    settled = dict(assign)
    for source_id, (key, _) in rows.items():
        if source_id not in wanted or key in assign:
            continue
        # Holds an id a worked-on ticket needs, and has nothing of its own to keep.
        settled[key] = freed.pop(0) if freed else RETIRED + source_id
    # source_id is unique and these ids trade places, so park them all first.
    for key in settled:
        conn.execute("UPDATE tracker.issue SET source_id = %s WHERE key = %s", [MOVING + key, key])
    for key, source_id in settled.items():
        if key in assign or source_id not in sprints:
            conn.execute("UPDATE tracker.issue SET source_id = %s WHERE key = %s", [source_id, key])
        else:
            conn.execute("UPDATE tracker.issue SET source_id = %s, sprint_id = %s WHERE key = %s",
                         [source_id, sprints[source_id], key])
    return moved
