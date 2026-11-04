"""Control Center API (prefix /api): health, status, build, batch, stream, DQ, scenarios, runs, events, connections."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..db import get_db
from ..settings import get_settings
from ..sim import scenarios
from ..sim.engine import EngineBusy, get_engine

router = APIRouter(prefix="/api", tags=["control"])

SSE_HEARTBEAT_S = 15


class BuildRequest(BaseModel):
    preset: str
    seed: int | None = None
    confirm: bool = False
    replace: bool = False    # finished data exists and the caller means to wipe it


class BatchRequest(BaseModel):
    hours: float = Field(24, gt=0, le=2208)


class StreamRequest(BaseModel):
    interval_s: float = Field(5, ge=1, le=300)
    speed: float = Field(60, gt=0, le=3600)


class DQRequest(BaseModel):
    denominator: int | None = Field(None, ge=100, le=100_000)
    categories: dict[str, bool] | None = None


class ScenarioRequest(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)


def _call(fn, *args: Any) -> Any:
    """Map engine errors to HTTP: busy / not built -> 409, bad input -> 400."""
    try:
        return fn(*args)
    except EngineBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/health")
def health() -> JSONResponse:
    ok = get_db().ping()
    return JSONResponse({"ok": ok, "db": ok}, status_code=200 if ok else 503)


@router.get("/sim/status")
def status() -> dict[str, Any]:
    return get_engine().status()


@router.post("/sim/build")
def build(req: BuildRequest) -> dict[str, int]:
    if not req.confirm:
        raise HTTPException(400, "A build wipes all simulated source data. Send confirm: true.")
    engine = get_engine()
    if engine.built_at is not None and not req.replace:
        raise HTTPException(409, "Finished data already exists and a build would wipe it. Send replace: true to do that on purpose.")
    return {"run_id": _call(engine.start_build, req.preset, req.seed)}


@router.post("/sim/build/cancel")
def cancel_build() -> dict[str, Any]:
    return _call(get_engine().cancel_build)


@router.post("/sim/batch")
def batch(req: BatchRequest) -> dict[str, Any]:
    return _call(get_engine().batch, req.hours)


@router.post("/sim/stream/start")
def stream_start(req: StreamRequest) -> dict[str, Any]:
    return _call(get_engine().start_stream, req.interval_s, req.speed)


@router.post("/sim/stream/stop")
def stream_stop() -> dict[str, Any]:
    return get_engine().stop_stream()


@router.put("/sim/dq")
def set_dq(req: DQRequest) -> dict[str, Any]:
    return _call(get_engine().set_dq, req.denominator, req.categories)


@router.get("/sim/scenarios")
def list_scenarios() -> list[dict[str, Any]]:
    return scenarios.catalog(get_engine().rules)


@router.post("/sim/scenarios/{scenario_id}")
def run_scenario(scenario_id: str, req: ScenarioRequest | None = None) -> dict[str, Any]:
    if scenario_id not in scenarios.SCENARIO_IDS:
        raise HTTPException(404, f"Unknown scenario {scenario_id!r}")
    return _call(get_engine().run_scenario, scenario_id, (req.params if req else {}))


@router.get("/sim/runs")
def runs(limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
    return get_engine().runs(limit)


@router.get("/sim/connections")
def connections() -> dict[str, Any]:
    s = get_settings()
    base = f"http://localhost:{s.app_public_port}"
    return {
        "postgres": {"host": s.pg_public_host, "port": s.pg_public_port, "user": s.etl_user, "password": s.etl_password,
                     "databases": ["wms", "ax2012", "iot", "tracker"],
                     "publications": {"wms": "hl_wms_cdc", "ax2012": "hl_ax_cdc", "iot": "hl_iot_cdc"}},
        "salesforce": {"instance_url": f"{base}/sfdc", "token_url": f"{base}/sfdc/services/oauth2/token",
                       "client_id": s.sf_client_id, "client_secret": s.sf_client_secret, "api_version": "v67.0"},
        "carrier": {"base_url": f"{base}/carrier/v1", "api_key": s.carrier_api_key},
        "landing_path_host_hint": s.landing_host_hint,
    }


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@router.get("/sim/events")
async def events(request: Request) -> StreamingResponse:
    engine = get_engine()
    queue = engine.hub.subscribe()
    first = await run_in_threadpool(engine.status)

    async def stream() -> AsyncIterator[str]:
        try:
            yield _sse("state", first)
            while not await request.is_disconnected():
                try:
                    event, data = await asyncio.wait_for(queue.get(), timeout=SSE_HEARTBEAT_S)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield _sse(event, data)
        finally:
            engine.hub.unsubscribe(queue)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
