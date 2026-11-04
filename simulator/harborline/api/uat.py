"""UAT source extract API (prefix /api/uat): the independent copy of the sources Iris Novak reviews a dashboard with.

GET  /api/uat/extract/options   projects, closed months, defaults, whether an extract is possible now and why not
POST /api/uat/extract           {project, periods?} -> builds the zip (409 when the data is not still)
GET  /api/uat/extract/{id}      downloads a built zip (the last few are kept)
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..sim.engine import get_engine
from ..uat_extract import ExtractRefused, get_service

router = APIRouter(prefix="/api/uat", tags=["uat"])


class ExtractRequest(BaseModel):
    project: str = Field(..., min_length=2, max_length=4, examples=["P2"])
    periods: list[str] | None = Field(None, max_length=12, examples=[["2026-06", "2026-09"]])


@router.get("/extract/options")
def extract_options() -> dict[str, Any]:
    return get_service().options(get_engine())


@router.post("/extract")
def build_extract(req: ExtractRequest) -> dict[str, Any]:
    """Runs in the threadpool (sync endpoint). Holds the engine's run lock while it reads, so nothing moves."""
    try:
        info = get_service().build(req.project, req.periods, get_engine())
    except ExtractRefused as exc:
        raise HTTPException(409, str(exc), headers={"X-Reason-Code": exc.code}) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return info.as_json()


@router.get("/extract/{extract_id}")
def download_extract(extract_id: str) -> FileResponse:
    info = get_service().get(extract_id)
    if info is None:
        raise HTTPException(404, "That extract is no longer available. Build a new one.")
    return FileResponse(info.path, media_type="application/zip", filename=info.filename)
