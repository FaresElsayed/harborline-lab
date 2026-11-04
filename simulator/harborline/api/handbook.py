"""Handbook progress (checkbox state of the HTML handbook pages), kept in sim_internal.sim.handbook_progress."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Path
from fastapi.responses import FileResponse

from ..db import get_db
from ..settings import get_settings

router = APIRouter(prefix="/api/handbook", tags=["handbook"])

PAGE = Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,59}$")
PERSON = Path(pattern=r"^[a-z0-9][a-z0-9\-]{0,59}$")
MAX_STATE_BYTES = 256 * 1024


def _row(page: str, state: Any, updated_at: Any) -> dict[str, Any]:
    return {"page": page, "state": state, "updated_at": updated_at.isoformat() if updated_at else None}


@router.get("/progress")
def all_progress() -> list[dict[str, Any]]:
    rows = get_db().fetchall("sim_internal", "SELECT page, state, updated_at FROM sim.handbook_progress ORDER BY page")
    return [_row(*r) for r in rows]


@router.get("/progress/{page}")
def get_progress(page: str = PAGE) -> dict[str, Any]:
    rows = get_db().fetchall(
        "sim_internal", "SELECT page, state, updated_at FROM sim.handbook_progress WHERE page = %s", (page,)
    )
    return _row(*rows[0]) if rows else _row(page, {}, None)


@router.put("/progress/{page}")
def put_progress(body: Annotated[dict[str, Any], Body()], page: str = PAGE) -> dict[str, Any]:
    """Body: ``{"state": {...}}`` (a bare object is accepted too)."""
    state = body["state"] if isinstance(body.get("state"), dict) else body
    payload = json.dumps(state)
    if len(payload) > MAX_STATE_BYTES:
        raise HTTPException(413, "progress state too large")
    rows = get_db().fetchall(
        "sim_internal",
        "INSERT INTO sim.handbook_progress (page, state, updated_at) VALUES (%s, %s, now()) "
        "ON CONFLICT (page) DO UPDATE SET state = EXCLUDED.state, updated_at = now() RETURNING page, state, updated_at",
        (page, payload),
    )
    return _row(*rows[0])


def _team_state() -> dict[str, Any]:
    rows = get_db().fetchall("sim_internal", "SELECT state FROM sim.handbook_progress WHERE page = 'team'")
    return dict(rows[0][0]) if rows else {}


@router.get("/chat-zip")
def chat_zip() -> FileResponse:
    """The zip that goes with every colleague's prompt."""
    path = get_settings().handbook_dir.parent / "setup" / "harborline-chat.zip"
    if not path.is_file():
        raise HTTPException(404, "harborline-chat.zip is missing. Run the setup file again.")
    return FileResponse(path, media_type="application/zip", filename="harborline-chat.zip")


@router.get("/team-links")
def team_links() -> dict[str, Any]:
    """The saved Team chat links and site, for the setup files."""
    data = _team_state().get("data", {})
    return {"links": data.get("links", {}), "site": data.get("site")}


@router.put("/team-site")
def put_team_site(body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
    """Save which AI website Team chat opens. Body: ``{"name": "Claude", "url": "https://claude.ai/new"}``."""
    url = str(body.get("url", "")).strip()
    if not re.fullmatch(r"https://\S{1,500}", url):
        raise HTTPException(400, "An address starts with https://")
    state = _team_state()
    state.setdefault("data", {})["site"] = {"name": str(body.get("name", "AI site"))[:60], "url": url}
    state.setdefault("items", {})
    state["updated_at"] = datetime.now(UTC).isoformat()
    put_progress({"state": state}, "team")
    return state["data"]["site"]


@router.put("/team-links/{person}")
def put_team_link(body: Annotated[dict[str, Any], Body()], person: str = PERSON) -> dict[str, Any]:
    """Save one Team chat link and tick that person's box. Body: ``{"url": "https://...", "item": "it.kevin-ohara"}``."""
    url = str(body.get("url", "")).strip()
    if not re.fullmatch(r"https://\S{1,500}", url):
        raise HTTPException(400, "A chat address starts with https://")
    state = _team_state()
    state.setdefault("data", {}).setdefault("links", {})[person] = url
    now = datetime.now(UTC).isoformat()
    item = body.get("item")
    state.setdefault("items", {})
    if isinstance(item, str) and item:
        state["items"].setdefault(item, now)
    state["updated_at"] = now
    put_progress({"state": state}, "team")
    return {"links": state["data"]["links"]}
