"""Carrier tracking aggregator mock (prefix ``/carrier/v1``).

Serves ``sim_internal.carrier.tracking_event`` the way a multi-carrier tracking aggregator does:
``X-API-Key`` auth, cursor pagination ordered by (received_at, event_id), only events already
received by the simulator clock and inside the 90-day retention window, 120 requests per minute per
key (429 + ``Retry-After``) and occasional 503s. Event times are sent as local ISO-8601 with the
carrier's UTC offset (``event_local_ts``); ``received_at`` is UTC.

Event JSON::

    {"event_id": 81234, "tracking_nbr": "1Z...", "scac": "UPSN", "event_code": "IN_TRANSIT",
     "event_local_ts": "2026-10-30T14:03:22-04:00", "description": "...", "exception_code": null,
     "location": {"city": "Atlanta", "state_prov": "GA", "country_code": "US"},
     "received_at": "2026-10-30T18:10:05Z", "is_correction": false}
"""

from __future__ import annotations

import base64
import binascii
import hmac
import random
import secrets
import threading
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from harborline.tracker.runtime import connection, env, settings, sim_now

DB = "sim_internal"
RATE_LIMIT_PER_MINUTE = 120
RETENTION = timedelta(days=90)
DEFAULT_LIMIT = 100
MAX_LIMIT = 500

router = APIRouter(prefix="/carrier/v1", tags=["carrier"])

_lock = threading.Lock()
_calls: dict[str, deque[float]] = {}
_fault_rate = float(env("CARRIER_FAULT_RATE", "0.01"))
_rng = random.Random()


def api_key() -> str:
    return settings().carrier_api_key


def _monotonic() -> float:
    """Clock for the rate limiter (tests patch it)."""
    return time.monotonic()


def set_fault_rate(rate: float) -> None:
    """Probability of a simulated 503 per request (0 disables, 1 always fails)."""
    global _fault_rate
    _fault_rate = rate


def reset_rate_limits() -> None:
    with _lock:
        _calls.clear()


def _error(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    body = {"error": {"code": code, "message": message, "request_id": f"req_{secrets.token_hex(8)}"}}
    return JSONResponse(body, status_code=status, headers=headers)


def _admit(key: str) -> tuple[int, int] | None:
    """Sliding one-minute window. Returns (remaining, reset_epoch_s) or None when the limit is exceeded."""
    now = _monotonic()
    with _lock:
        window = _calls.setdefault(key, deque())
        while window and now - window[0] >= 60:
            window.popleft()
        if len(window) >= RATE_LIMIT_PER_MINUTE:
            return None
        window.append(now)
        reset_in = 60 - (now - window[0])
        return RATE_LIMIT_PER_MINUTE - len(window), int(time.time() + reset_in)


def _retry_after(key: str) -> int:
    with _lock:
        window = _calls.get(key) or deque([_monotonic()])
        return max(1, int(60 - (_monotonic() - window[0])) + 1)


def _guard(request: Request) -> JSONResponse | dict[str, str]:
    """Auth, rate limit and fault injection. Returns rate-limit headers or an error response."""
    key = request.headers.get("x-api-key", "")
    if not key or not hmac.compare_digest(key, api_key()):
        return _error(401, "UNAUTHORIZED", "Missing or invalid API key (send it in the X-API-Key header)")
    admitted = _admit(key)
    if admitted is None:
        retry = _retry_after(key)
        return _error(429, "RATE_LIMITED", f"Rate limit of {RATE_LIMIT_PER_MINUTE} requests per minute exceeded",
                      {"Retry-After": str(retry), "X-RateLimit-Limit": str(RATE_LIMIT_PER_MINUTE),
                       "X-RateLimit-Remaining": "0"})
    remaining, reset = admitted
    headers = {"X-RateLimit-Limit": str(RATE_LIMIT_PER_MINUTE), "X-RateLimit-Remaining": str(remaining),
               "X-RateLimit-Reset": str(reset)}
    if _fault_rate > 0 and _rng.random() < _fault_rate:
        return _error(503, "SERVICE_UNAVAILABLE", "Upstream carrier feed temporarily unavailable, retry later",
                      {**headers, "Retry-After": "5"})
    return headers


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def encode_cursor(received_at: datetime, event_id: int) -> str:
    raw = f"{received_at.astimezone(UTC).isoformat(timespec='microseconds')}|{event_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        ts, _, event_id = raw.partition("|")
        return datetime.fromisoformat(ts).astimezone(UTC), int(event_id)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise ValueError("invalid cursor") from None


def _parse_since(value: str) -> datetime:
    text = value.strip().replace(" ", "+").replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


_COLUMNS = ("event_id, tracking_nbr, scac, event_code, event_local_ts, description, exception_code, city, "
            "state_prov, country_code, received_at, is_correction")


def _event(row: tuple[Any, ...]) -> dict[str, Any]:
    (event_id, tracking_nbr, scac, event_code, local_ts, description, exception_code, city, state_prov, country,
     received_at, is_correction) = row
    return {
        "event_id": event_id,
        "tracking_nbr": tracking_nbr,
        "scac": scac,
        "event_code": event_code,
        "event_local_ts": local_ts,
        "description": description,
        "exception_code": exception_code,
        "location": {"city": city, "state_prov": state_prov, "country_code": country},
        "received_at": iso_utc(received_at),
        "is_correction": is_correction,
    }


@router.get("/tracking-events")
def tracking_events(request: Request, since: str | None = None, cursor: str | None = None,
                    limit: str | None = None) -> JSONResponse:
    guard = _guard(request)
    if isinstance(guard, JSONResponse):
        return guard
    try:
        size = DEFAULT_LIMIT if limit is None else int(limit)
    except ValueError:
        size = 0
    if not 1 <= size <= MAX_LIMIT:
        return _error(400, "INVALID_PARAMETER", f"limit must be an integer between 1 and {MAX_LIMIT}", guard)
    with connection(DB) as conn:
        now = sim_now(conn)
        retention_start = now - RETENTION
        after: tuple[datetime, int]
        if cursor:
            try:
                after = decode_cursor(cursor)
            except ValueError:
                return _error(400, "INVALID_CURSOR", "cursor is not valid; restart from 'since'", guard)
            if after[0] < retention_start:
                return _error(410, "CURSOR_EXPIRED", "cursor points before the 90-day retention window; backfill from "
                              "the history export and restart with 'since'", guard)
        elif since:
            try:
                after = (max(_parse_since(since), retention_start) - timedelta(microseconds=1), 2**62)
            except ValueError:
                return _error(400, "INVALID_PARAMETER", "since must be an ISO-8601 timestamp", guard)
        else:
            after = (retention_start - timedelta(microseconds=1), 2**62)
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM carrier.tracking_event "
            "WHERE (received_at, event_id) > (%s, %s) AND received_at <= %s AND received_at >= %s "
            "ORDER BY received_at, event_id LIMIT %s",
            [after[0], after[1], now, retention_start, size + 1],
        ).fetchall()
    has_more = len(rows) > size
    rows = rows[:size]
    next_cursor = encode_cursor(rows[-1][10], rows[-1][0]) if rows else cursor
    body = {"events": [_event(r) for r in rows], "next_cursor": next_cursor, "has_more": has_more}
    return JSONResponse(body, headers=guard)


@router.get("/shipments/{tracking_nbr}")
def shipment(request: Request, tracking_nbr: str) -> JSONResponse:
    guard = _guard(request)
    if isinstance(guard, JSONResponse):
        return guard
    with connection(DB) as conn:
        now = sim_now(conn)
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM carrier.tracking_event WHERE tracking_nbr = %s AND received_at <= %s "
            "AND received_at >= %s ORDER BY event_ts, event_id",
            [tracking_nbr.strip(), now, now - RETENTION],
        ).fetchall()
    if not rows:
        return _error(404, "NOT_FOUND", f"No tracking events for {tracking_nbr}", guard)
    events = [_event(r) for r in rows]
    body = {"tracking_nbr": rows[0][1], "scac": rows[0][2], "status": events[-1]["event_code"], "events": events}
    return JSONResponse(body, headers=guard)
