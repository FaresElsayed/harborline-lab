"""Control API (spec 5.1) and handbook progress through FastAPI's TestClient, with the fake module engine."""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harborline.api import control
from harborline.api.main import create_app
from harborline.core import MODULE_ORDER
from harborline.db import get_db
from harborline.sim.engine import BUILD_PROOF, Engine
from harborline.sim.scenarios import SCENARIO_IDS

RUN_KEYS = {"run_id", "kind", "label", "started_at", "finished_at", "sim_from", "sim_to", "status", "error", "changes"}


@pytest.fixture
def client(fake_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(control, "SSE_HEARTBEAT_S", 0.2)
    with TestClient(create_app()) as c:
        yield c


def _wait_idle(client: TestClient, timeout_s: float = 120) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status = client.get("/api/sim/status").json()
        if status["state"] != "building":
            return status
        time.sleep(0.2)
    raise AssertionError("build did not finish")


def test_health_and_connections(client: TestClient) -> None:
    assert client.get("/api/health").json() == {"ok": True, "db": True}
    conn = client.get("/api/sim/connections").json()
    assert conn["postgres"]["user"] == "etl_reader" and set(conn["postgres"]["databases"]) == {"wms", "ax2012", "iot", "tracker"}
    assert conn["salesforce"]["instance_url"].endswith("/sfdc") and conn["salesforce"]["api_version"] == "v67.0"
    assert conn["carrier"]["base_url"].endswith("/carrier/v1") and conn["carrier"]["api_key"]
    assert "landing_path_host_hint" in conn


def test_build_batch_runs_and_status(client: TestClient) -> None:
    assert client.post("/api/sim/batch", json={"hours": 1}).status_code == 409
    assert client.post("/api/sim/build", json={"preset": "small"}).status_code == 400          # no confirm
    assert client.post("/api/sim/build", json={"preset": "huge", "confirm": True}).status_code == 400
    run_id = client.post("/api/sim/build", json={"preset": "small", "seed": 3, "confirm": True}).json()["run_id"]
    status = _wait_idle(client)
    assert status["state"] == "idle" and status["preset"] == "small" and status["seed"] == 3
    assert status["last_run"]["run_id"] == run_id and status["last_run"]["status"] == "ok"
    assert status["build"] is None and status["sim_clock"] == status["history_end"]
    assert {"denominator", "categories"} <= status["dq"].keys()
    assert {"running", "interval_s", "speed", "ticks"} <= status["stream"].keys()

    summary = client.post("/api/sim/batch", json={"hours": 6}).json()
    assert RUN_KEYS <= summary.keys() and summary["kind"] == "batch" and summary["status"] == "ok"
    assert {"system": "wms", "table": "inventory_txn", "ins": 24, "upd": 0, "del": 1} in summary["changes"]
    assert client.post("/api/sim/batch", json={"hours": 0}).status_code == 422

    runs = client.get("/api/sim/runs?limit=5").json()
    assert [r["kind"] for r in runs[:2]] == ["batch", "build"] and RUN_KEYS <= runs[0].keys()

    started = client.post("/api/sim/stream/start", json={"interval_s": 1, "speed": 60}).json()
    assert started["stream"]["running"] is True and started["state"] == "streaming"
    time.sleep(1.5)
    stopped = client.post("/api/sim/stream/stop").json()
    assert stopped["stream"]["running"] is False and stopped["stream"]["ticks"] >= 1


def test_dq_endpoint(client: TestClient) -> None:
    dq = client.put("/api/sim/dq", json={"denominator": 100000, "categories": {"sensor_spike": False}}).json()
    assert dq["denominator"] == 100000 and dq["categories"]["sensor_spike"] is False
    assert client.put("/api/sim/dq", json={"denominator": 10}).status_code == 422


def test_scenarios_endpoints(client: TestClient) -> None:
    catalog = client.get("/api/sim/scenarios").json()
    assert tuple(s["id"] for s in catalog) == SCENARIO_IDS and len(catalog) == 10
    for spec in catalog:
        assert {"id", "name", "description", "params"} <= spec.keys()
        for p in spec["params"]:
            assert {"name", "type", "default"} <= p.keys()
            if p["type"] == "select":
                assert p["options"] and p["default"] in p["options"]
    assert client.post("/api/sim/scenarios/nope", json={"params": {}}).status_code == 404
    assert client.post("/api/sim/scenarios/storm", json={"params": {}}).status_code == 409         # not built yet


def test_handbook_progress(client: TestClient) -> None:
    page = f"test-page-{uuid.uuid4().hex[:8]}"
    assert client.get(f"/api/handbook/progress/{page}").json() == {"page": page, "state": {}, "updated_at": None}
    saved = client.put(f"/api/handbook/progress/{page}", json={"state": {"brief:read": True}}).json()
    assert saved["state"] == {"brief:read": True} and saved["updated_at"]
    assert client.get(f"/api/handbook/progress/{page}").json()["state"] == {"brief:read": True}
    assert client.put(f"/api/handbook/progress/{page}", json={"bare": 1}).json()["state"] == {"bare": 1}
    assert any(p["page"] == page for p in client.get("/api/handbook/progress").json())
    assert client.put("/api/handbook/progress/bad%20name!", json={"state": {}}).status_code == 422


def test_team_links_and_site_survive_a_browser_sync(client: TestClient) -> None:
    client.put("/api/handbook/progress/team", json={"state": {"items": {"setup.account": "2026-01-01"}, "total": 26}})
    assert client.put("/api/handbook/team-links/kevin-ohara", json={"url": "http://x"}).status_code == 400
    assert client.put("/api/handbook/team-links/BAD%20ID", json={"url": "https://x"}).status_code == 422
    url = "https://claude.ai/chat/abc"
    saved = client.put("/api/handbook/team-links/kevin-ohara", json={"url": url, "item": "it.kevin-ohara"}).json()
    assert saved["links"]["kevin-ohara"] == url
    assert client.put("/api/handbook/team-site", json={"name": "Claude", "url": "https://claude.ai/new"}).status_code == 200
    got = client.get("/api/handbook/team-links").json()
    assert got["links"]["kevin-ohara"] == url and got["site"]["name"] == "Claude"
    state = client.get("/api/handbook/progress/team").json()["state"]
    # What the handbook page reads back: the earlier tick stays, the new one is added, the links sit under data.
    assert state["items"]["setup.account"] == "2026-01-01" and "it.kevin-ohara" in state["items"]
    assert state["data"]["links"]["kevin-ohara"] == url and state["updated_at"] and state["total"] == 26
    client.put("/api/handbook/progress/team", json={"state": {}})


def test_events_stream_starts_with_state(client: TestClient) -> None:
    with client.stream("GET", "/api/sim/events") as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        lines = []
        for line in resp.iter_lines():
            lines.append(line)
            if line.startswith("data:"):
                break
    assert lines[0] == "event: state"
    assert json.loads(lines[1][5:])["state"] in ("idle", "error")


def test_spa_fallback_and_unknown_api(client: TestClient) -> None:
    page = client.get("/tracker/board")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert client.get("/api/does-not-exist").status_code == 404
    assert client.get("/handbook/", follow_redirects=False).headers["location"] == "/START_HERE.html"


def _slow(seconds: float, calls: list[int], result):
    def read(*_args, **_kwargs):
        calls.append(1)
        time.sleep(seconds)
        return result
    return read


def test_status_answers_at_once_while_the_landing_folder_is_slow(client: TestClient, fake_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows the landing folder is a shared folder: reading every file can take minutes."""
    walks: list[int] = []
    monkeypatch.setattr("harborline.sim.engine.os.walk", _slow(4, walks, []))
    fake_engine.expire_status()
    results: list[float] = []

    def timed() -> None:
        started = time.monotonic()
        assert client.get("/api/sim/status").status_code == 200
        results.append(time.monotonic() - started)

    threads = [threading.Thread(target=timed) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 8 and max(results) < 3
    assert len(walks) == 1, "many callers must share one read of the folder"


def test_status_answers_at_once_while_the_database_is_slow(client: TestClient, fake_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fake_engine.db, "table_rows", _slow(4, [], []))
    fake_engine.expire_status()
    started = time.monotonic()
    assert client.get("/api/sim/status").status_code == 200
    assert time.monotonic() - started < 3


def test_a_slow_status_does_not_hold_up_the_rest_of_the_api(client: TestClient, fake_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("harborline.sim.engine.os.walk", _slow(4, [], []))
    fake_engine.expire_status()
    started = time.monotonic()
    run_id = client.post("/api/sim/build", json={"preset": "small", "seed": 3, "confirm": True}).json()["run_id"]
    assert run_id and time.monotonic() - started < 3, "starting a build must not wait for the folder"
    _wait_idle(client)


def test_a_build_never_wipes_finished_data_unless_asked_to(client: TestClient) -> None:
    client.post("/api/sim/build", json={"preset": "small", "seed": 3, "confirm": True})
    built_at = _wait_idle(client)["built_at"]
    assert built_at

    refused = client.post("/api/sim/build", json={"preset": "small", "seed": 3, "confirm": True})
    assert refused.status_code == 409 and "wipe" in refused.json()["detail"]
    assert client.get("/api/sim/status").json()["built_at"] == built_at
    assert len(client.get("/api/sim/runs").json()) == 1

    assert client.post("/api/sim/build", json={"preset": "small", "seed": 4, "confirm": True, "replace": True}).status_code == 200
    assert _wait_idle(client)["built_at"] != built_at


def _stamp() -> object:
    with get_db().connect("sim_internal") as conn:
        return conn.execute("SELECT built_at FROM sim.state WHERE id = 1").fetchone()[0]


def test_the_finished_stamp_goes_before_any_data_does(client: TestClient, fake_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Setup trusts built_at, so a crash mid-wipe must not leave it on wiped data."""
    client.post("/api/sim/build", json={"preset": "small", "seed": 3, "confirm": True})
    _wait_idle(client)
    assert _stamp() is not None

    seen: list[object] = []
    real = fake_engine.db.wipe_source_data
    monkeypatch.setattr(fake_engine.db, "wipe_source_data", lambda: (seen.append(_stamp()), real()))
    client.post("/api/sim/build", json={"preset": "small", "seed": 3, "confirm": True, "replace": True})
    assert _wait_idle(client)["built_at"]
    assert seen == [None] and _stamp() is not None


def test_a_build_that_fails_at_the_last_moment_is_not_stamped_finished(client: TestClient, fake_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(_modules: object) -> None:
        raise RuntimeError("The build finished with empty tables: wms.order_header")

    monkeypatch.setattr(fake_engine, "_require_every_system_has_data", fail)
    client.post("/api/sim/build", json={"preset": "small", "seed": 3, "confirm": True})
    status = _wait_idle(client)
    assert status["built_at"] is None and "empty tables" in status["error"] and _stamp() is None
    assert status["last_run"]["status"] == "error"


def test_every_module_in_the_package_has_a_table_that_proves_it_ran() -> None:
    assert {"wms", "ax", "crm", "iot", "carrier"} <= set(MODULE_ORDER) and set(BUILD_PROOF) <= set(MODULE_ORDER)


def test_a_running_build_can_be_cancelled(client: TestClient, fake_engine: Engine, fake_module, monkeypatch: pytest.MonkeyPatch) -> None:
    assert client.post("/api/sim/build/cancel").status_code == 409    # nothing is running
    real = fake_module.step

    def slow_step(ctx, win) -> None:
        time.sleep(0.05)
        real(ctx, win)

    monkeypatch.setattr(fake_module, "step", slow_step)
    client.post("/api/sim/build", json={"preset": "small", "seed": 3, "confirm": True})
    cancelled = client.post("/api/sim/build/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["build"]["phase"] == "cancelling"

    status = _wait_idle(client, 30)
    assert status["built_at"] is None and status["error"] is None and status["state"] == "idle" and _stamp() is None
    assert status["last_run"]["status"] == "cancelled"

    # A restart must not turn the person's own cancel into a fault.
    fake_engine.load()
    assert fake_engine.error is None and fake_engine.built_at is None

    # Nothing finished is left to protect, so no replace is needed to start again.
    monkeypatch.setattr(fake_module, "step", real)
    assert client.post("/api/sim/build", json={"preset": "small", "seed": 3, "confirm": True}).status_code == 200
    assert _wait_idle(client)["built_at"] and _stamp() is not None


def test_a_landing_folder_that_cannot_be_written_stops_the_build_before_any_wipe(client: TestClient, fake_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    client.post("/api/sim/build", json={"preset": "small", "seed": 3, "confirm": True})
    built_at = _wait_idle(client)["built_at"]
    wiped: list[int] = []
    monkeypatch.setattr(fake_engine.db, "wipe_source_data", lambda: wiped.append(1))
    real_mkdir = Path.mkdir

    def refuse(self: Path, *args, **kwargs) -> None:
        if ".hl-write-check" in self.name:
            raise PermissionError(13, "Permission denied", str(self))
        real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", refuse)
    client.post("/api/sim/build", json={"preset": "small", "seed": 3, "confirm": True, "replace": True})
    status = _wait_idle(client, 30)
    assert not wiped and "Nothing was wiped" in status["error"] and "docker compose restart app" in status["error"]
    assert _stamp() is not None, "the finished data is still there, so its stamp must be too"
    assert status["built_at"] == built_at


def test_data_built_before_a_carrier_rename_says_so_instead_of_crashing(client: TestClient, fake_engine: Engine) -> None:
    assert client.post("/api/sim/build", json={"preset": "small", "seed": 3, "confirm": True}).status_code == 200
    assert _wait_idle(client)["built_at"]
    fake_engine.load()
    assert fake_engine.error is None and not fake_engine.outdated_data

    with fake_engine.db.connect("wms") as conn:
        conn.execute("DELETE FROM wms.carrier")
        conn.execute("INSERT INTO wms.carrier (carrier_id, scac, carrier_name, mode, mod_ts) VALUES (1, 'OLDC', 'Old', 'LTL', now())")
        conn.commit()
    fake_engine.load()
    assert fake_engine.outdated_data and fake_engine.built_at  # the finished data is kept
    answer = client.post("/api/sim/batch", json={"hours": 1})
    assert answer.status_code == 409 and "Build dataset" in answer.json()["detail"]
    assert "Build dataset" in client.get("/api/sim/status").json()["error"]

    # One new build clears it.
    assert client.post("/api/sim/build", json={"preset": "small", "seed": 3, "confirm": True, "replace": True}).status_code == 200
    status = _wait_idle(client)
    assert status["built_at"] and status["error"] is None and not fake_engine.outdated_data


def test_the_team_page_can_download_the_chat_zip(client: TestClient) -> None:
    answer = client.get("/api/handbook/chat-zip")
    assert answer.status_code == 200 and answer.headers["content-type"] == "application/zip"
    assert answer.content[:2] == b"PK"
