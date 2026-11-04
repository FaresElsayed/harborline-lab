"""FastAPI app on :8080: control API, handbook progress, mock SaaS APIs, tracker, handbook/brand files and the SPA.

Run: ``uvicorn harborline.api.main:app --host 0.0.0.0 --port 8080`` (one worker: the engine lives in-process).
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from ..db import get_db
from ..settings import get_settings
from ..sim.engine import get_engine
from . import control, handbook, uat

log = logging.getLogger("harborline.api")

# Routers owned by other workstreams: module -> default prefix (used only if the router does not carry one).
OPTIONAL_ROUTERS = {"salesforce": "/sfdc", "carrier_api": "/carrier/v1", "tracker": "/api/tracker"}
API_PREFIXES = ("api/", "sfdc/", "carrier/", "docs", "openapi.json", "redoc")
DEV_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:4173", "http://127.0.0.1:4173"]

_FALLBACK_PAGE = """<!doctype html><html lang="en"><meta charset="utf-8"><title>Harborline Simulator</title>
<body style="font-family:system-ui;margin:3rem;max-width:40rem;line-height:1.5">
<h1>Harborline 3PL Simulator</h1><p>The API is running, but the web UI has not been built into this image.</p>
<ul><li><a href="/START_HERE.html">Start here (handbook)</a></li><li><a href="/docs">API docs</a></li>
<li><a href="/api/sim/status">Simulator status (JSON)</a></li></ul></body></html>"""


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    await run_in_threadpool(get_db().bootstrap)
    engine = get_engine()
    await run_in_threadpool(engine.load)
    await run_in_threadpool(_load_tracker_catalog)
    log.info("Harborline simulator ready (state=%s, sim_clock=%s)", engine.state, engine.sim_clock)
    try:
        yield
    finally:
        await run_in_threadpool(engine.shutdown)
        get_db().close()


def _load_tracker_catalog() -> None:
    try:
        importlib.import_module("harborline.api.tracker").startup()
    except Exception:  # the tracker loads lazily on first request if this fails
        log.exception("tracker catalog did not load at startup")


def _include_optional(app: FastAPI) -> None:
    for name, prefix in OPTIONAL_ROUTERS.items():
        qualified = f"harborline.api.{name}"
        try:
            module = importlib.import_module(qualified)
        except ModuleNotFoundError as exc:
            if exc.name != qualified:
                log.exception("%s failed to import; its endpoints are disabled", qualified)
            else:
                log.warning("%s is not available; its endpoints are disabled", qualified)
            continue
        except Exception:  # one broken router must not take the Control Center down
            log.exception("%s failed to import; its endpoints are disabled", qualified)
            continue
        router: APIRouter | None = getattr(module, "router", None)
        if router is None:
            log.warning("%s has no router", qualified)
            continue
        carries_prefix = bool(router.prefix) or any(getattr(r, "path", "").startswith(prefix) for r in router.routes)
        app.include_router(router, prefix="" if carries_prefix else prefix)


def _mount_static(app: FastAPI) -> None:
    s = get_settings()

    @app.get("/START_HERE.html", include_in_schema=False)
    def start_here() -> FileResponse:
        if not s.start_here.is_file():
            raise HTTPException(404, "START_HERE.html not found")
        return FileResponse(s.start_here)

    @app.get("/handbook", include_in_schema=False)
    @app.get("/handbook/", include_in_schema=False)
    def handbook_index() -> RedirectResponse:
        return RedirectResponse("/START_HERE.html")

    for url, folder in (("/handbook", s.handbook_dir), ("/brand", s.brand_dir)):
        if folder.is_dir():
            app.mount(url, StaticFiles(directory=folder, html=True), name=url.strip("/"))
        else:
            log.warning("%s not found; %s/* is not served", folder, url)

    dist = s.web_dist
    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="spa-assets")

    @app.get("/{path:path}", include_in_schema=False, response_model=None)
    def spa(path: str) -> FileResponse | HTMLResponse:
        if path.startswith(API_PREFIXES):
            raise HTTPException(404, "Not found")
        index = dist / "index.html"
        if path:
            candidate = (dist / path).resolve()
            if candidate.is_file() and candidate.is_relative_to(dist.resolve()):
                return FileResponse(candidate)
        if index.is_file():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return HTMLResponse(_FALLBACK_PAGE)


def create_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = FastAPI(title="Harborline 3PL Simulator", version="1.0.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware, allow_origins=DEV_ORIGINS, allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
    )
    app.include_router(control.router)
    app.include_router(handbook.router)
    app.include_router(uat.router)
    _include_optional(app)
    _mount_static(app)
    return app


app = create_app()
