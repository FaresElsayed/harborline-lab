"""Same seed + preset => same WMS / IoT / carrier data (runs before test_wms_simulation, which rebuilds)."""

from __future__ import annotations

import pandas as pd
import pytest

from harborline.sim import testing

MODS = ("wms", "iot", "carrier")
pytestmark = pytest.mark.skipif(not testing.pg_configured(), reason="set HL_TEST_PG_PORT (see harborline/sim/testing.py)")

FINGERPRINT = {
    "wms": [("SELECT count(*), sum(total_units), md5(string_agg(order_nbr || ship_to_postal || client_order_ref, ',' ORDER BY order_id)) "
             "FROM wms.order_header"),
            "SELECT count(*), sum(qty), md5(string_agg(txn_type || qty || txn_ts::text, ',' ORDER BY txn_id)) FROM wms.inventory_txn",
            "SELECT count(*), md5(string_agg(tracking_nbr, ',' ORDER BY shipment_id)) FROM wms.shipment",
            "SELECT count(*), sum(amount) FROM wms.billing_record"],
    "sim_internal": [("SELECT count(*), md5(string_agg(tracking_nbr || event_code || event_local_ts, ',' ORDER BY event_id)) "
                      "FROM carrier.tracking_event")],
}


def _fingerprint(ctx) -> list:
    out = []
    for db, queries in FINGERPRINT.items():
        for sql in queries:
            out.append(tuple(testing.fetch_df(ctx, db, sql).iloc[0].tolist()))
    return out


def _run(days: int, mode_after: str | None = None) -> list:
    ctx = testing.make_ctx("small", "build")
    try:
        testing.bootstrap_modules(ctx, MODS)
        testing.run_days(ctx, days, modules=MODS)
        if mode_after:
            ctx.mode = mode_after
        return _fingerprint(ctx)
    finally:
        testing.close_ctx(ctx)


def test_same_seed_same_data(pg_settings) -> None:
    a = _run(8)
    b = _run(8)
    assert a == b
    assert a[0][0] > 1000 and not pd.isna(a[0][2])
