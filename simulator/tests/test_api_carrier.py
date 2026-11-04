"""Carrier tracking aggregator mock: auth, cursor paging, visibility, retention, 429 + Retry-After, 503 toggle.

Needs the test Postgres (HL_TEST_PG_PORT, see harborline/sim/testing.py).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from harborline.api import carrier_api
from harborline.db import get_db
from harborline.settings import Settings

SIM_NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
BASE = "/carrier/v1"


def _event(event_id: int, trk: str, code: str, event_ts: datetime, received_at: datetime,
           offset_h: int = -4) -> list[Any]:
    local = event_ts.astimezone(UTC) + timedelta(hours=offset_h)
    local_ts = local.strftime("%Y-%m-%dT%H:%M:%S") + f"{'+' if offset_h >= 0 else '-'}{abs(offset_h):02d}:00"
    return [event_id, trk, "UPSN", code, event_ts, local_ts, "Atlanta", "GA", "US", f"{code} scan", None,
            received_at, False]


@pytest.fixture
def setup(pg_settings: Settings) -> Iterator[tuple[TestClient, dict[str, str], psycopg.Connection]]:
    carrier_api.set_fault_rate(0.0)
    carrier_api.reset_rate_limits()
    with get_db().connect("sim_internal", autocommit=True) as conn:
        conn.execute("TRUNCATE carrier.tracking_event")
        conn.execute("DELETE FROM sim.state")
        conn.execute("INSERT INTO sim.state (id, sim_clock) VALUES (1, %s)", [SIM_NOW])
        t = SIM_NOW - timedelta(days=1)
        rows = [
            _event(1, "1ZA", "LABEL_CREATED", t, t + timedelta(minutes=5)),
            _event(2, "1ZA", "PICKED_UP", t + timedelta(hours=2), t + timedelta(hours=2, minutes=1)),
            _event(3, "1ZB", "LABEL_CREATED", t + timedelta(hours=1), t + timedelta(hours=2, minutes=1)),  # tie
            _event(4, "1ZA", "IN_TRANSIT", t + timedelta(hours=6), t + timedelta(hours=7)),
            _event(5, "1ZA", "DELIVERED", t + timedelta(hours=20), t + timedelta(hours=20, minutes=3)),
            _event(6, "1ZB", "PICKED_UP", t + timedelta(hours=3), t + timedelta(hours=21)),  # late arrival
            _event(7, "1ZB", "IN_TRANSIT", t + timedelta(hours=22), SIM_NOW),  # exactly at the clock
            _event(8, "1ZB", "OUT_FOR_DELIVERY", t + timedelta(hours=23), SIM_NOW + timedelta(minutes=1)),  # future
            _event(9, "1ZOLD", "DELIVERED", SIM_NOW - timedelta(days=95), SIM_NOW - timedelta(days=91)),  # expired
        ]
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO carrier.tracking_event (event_id, tracking_nbr, scac, event_code, event_ts, event_local_ts, "
                "city, state_prov, country_code, description, exception_code, received_at, is_correction) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", rows)
        app = FastAPI()
        app.include_router(carrier_api.router)
        yield TestClient(app), {"X-API-Key": pg_settings.carrier_api_key}, conn
        conn.execute("DELETE FROM sim.state")
    carrier_api.reset_rate_limits()


def test_api_key_required(setup: tuple[TestClient, dict[str, str], psycopg.Connection]) -> None:
    client, _, _ = setup
    r = client.get(f"{BASE}/tracking-events")
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHORIZED"
    assert client.get(f"{BASE}/tracking-events", headers={"X-API-Key": "wrong"}).status_code == 401


def test_cursor_paging_returns_visible_events_in_cursor_order(
        setup: tuple[TestClient, dict[str, str], psycopg.Connection]) -> None:
    client, headers, _ = setup
    seen, cursor, pages = [], None, 0
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        body = client.get(f"{BASE}/tracking-events", params=params, headers=headers).json()
        seen += body["events"]
        cursor = body["next_cursor"]
        pages += 1
        if not body["has_more"]:
            break
    assert [e["event_id"] for e in seen] == [1, 2, 3, 4, 5, 6, 7]  # (received_at, event_id); no future, no expired
    assert pages == 4
    first = seen[0]
    assert first["event_local_ts"] == "2026-10-29T08:00:00-04:00"
    assert first["received_at"] == "2026-10-29T12:05:00Z"
    assert first["location"] == {"city": "Atlanta", "state_prov": "GA", "country_code": "US"}
    assert set(first) == {"event_id", "tracking_nbr", "scac", "event_code", "event_local_ts", "description",
                          "exception_code", "location", "received_at", "is_correction"}
    # Polling with the last cursor returns nothing new until the clock moves past the next arrival.
    again = client.get(f"{BASE}/tracking-events", params={"cursor": cursor}, headers=headers).json()
    assert again == {"events": [], "next_cursor": cursor, "has_more": False}


def test_new_arrivals_show_up_after_the_last_cursor(
        setup: tuple[TestClient, dict[str, str], psycopg.Connection]) -> None:
    client, headers, conn = setup
    body = client.get(f"{BASE}/tracking-events", params={"limit": 500}, headers=headers).json()
    conn.execute("UPDATE sim.state SET sim_clock = %s", [SIM_NOW + timedelta(minutes=5)])
    more = client.get(f"{BASE}/tracking-events", params={"cursor": body["next_cursor"]}, headers=headers).json()
    assert [e["event_id"] for e in more["events"]] == [8]


def test_since_filter_and_parameter_errors(setup: tuple[TestClient, dict[str, str], psycopg.Connection]) -> None:
    client, headers, _ = setup
    body = client.get(f"{BASE}/tracking-events", params={"since": "2026-10-29T14:01:00Z"}, headers=headers).json()
    assert [e["event_id"] for e in body["events"]] == [2, 3, 4, 5, 6, 7]
    assert client.get(f"{BASE}/tracking-events", params={"limit": 501}, headers=headers).status_code == 400
    assert client.get(f"{BASE}/tracking-events", params={"cursor": "%%%"}, headers=headers).json()["error"]["code"] \
        == "INVALID_CURSOR"
    old = carrier_api.encode_cursor(SIM_NOW - timedelta(days=100), 1)
    expired = client.get(f"{BASE}/tracking-events", params={"cursor": old}, headers=headers)
    assert expired.status_code == 410 and expired.json()["error"]["code"] == "CURSOR_EXPIRED"


def test_shipment_history(setup: tuple[TestClient, dict[str, str], psycopg.Connection]) -> None:
    client, headers, _ = setup
    body = client.get(f"{BASE}/shipments/1ZB", headers=headers).json()
    assert [e["event_code"] for e in body["events"]] == ["LABEL_CREATED", "PICKED_UP", "IN_TRANSIT"]
    assert body["status"] == "IN_TRANSIT" and body["scac"] == "UPSN"
    assert client.get(f"{BASE}/shipments/1ZOLD", headers=headers).status_code == 404


def test_rate_limit_429_with_retry_after(setup: tuple[TestClient, dict[str, str], psycopg.Connection],
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    client, headers, _ = setup
    clock = {"t": 1000.0}
    monkeypatch.setattr(carrier_api, "_monotonic", lambda: clock["t"])
    for i in range(carrier_api.RATE_LIMIT_PER_MINUTE):
        clock["t"] += 0.1
        r = client.get(f"{BASE}/shipments/1ZA", headers=headers)
        assert r.status_code == 200, i
    assert r.headers["X-RateLimit-Remaining"] == "0"
    limited = client.get(f"{BASE}/shipments/1ZA", headers=headers)
    assert limited.status_code == 429 and limited.json()["error"]["code"] == "RATE_LIMITED"
    assert 1 <= int(limited.headers["Retry-After"]) <= 60
    clock["t"] += int(limited.headers["Retry-After"])
    assert client.get(f"{BASE}/shipments/1ZA", headers=headers).status_code == 200


def test_fault_injection_toggle(setup: tuple[TestClient, dict[str, str], psycopg.Connection]) -> None:
    client, headers, _ = setup
    carrier_api.set_fault_rate(1.0)
    r = client.get(f"{BASE}/tracking-events", headers=headers)
    assert r.status_code == 503 and r.headers["Retry-After"] == "5"
    assert r.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    carrier_api.set_fault_rate(0.0)
    assert client.get(f"{BASE}/tracking-events", headers=headers).status_code == 200
