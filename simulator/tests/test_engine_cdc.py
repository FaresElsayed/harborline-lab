"""CDC proof: etl_reader (REPLICATION) creates logical slots and sees INSERT / UPDATE / DELETE from a batch.

The fake-module test always runs. The real-module test runs a Small build with the domain modules and is skipped
until ``harborline.sim.wms`` exists.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator

import pytest

from harborline.db import get_db
from harborline.settings import Settings
from harborline.sim.engine import Engine, get_engine, reset_engine


@pytest.fixture
def slots() -> Iterator[list[str]]:
    created: list[str] = []
    yield created
    with get_db().connect("wms", "super", autocommit=True) as conn:
        for name in created:
            conn.execute("SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots WHERE slot_name = %s", (name,))


def _create_slot(name: str, plugin: str, slots: list[str]) -> None:
    with get_db().connect("wms", "super", autocommit=True) as conn:
        conn.execute("SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots WHERE slot_name = %s", (name,))
    with get_db().connect("wms", "etl", autocommit=True) as conn:       # as the user's own login
        conn.execute("SELECT pg_create_logical_replication_slot(%s, %s)", (name, plugin))
    slots.append(name)


def _decoded(name: str) -> list[str]:
    with get_db().connect("wms", "etl", autocommit=True) as conn:
        return [r[0] for r in conn.execute("SELECT data FROM pg_logical_slot_get_changes(%s, NULL, NULL)", (name,))]


def _pgoutput_kinds(name: str, publication: str) -> set[str]:
    with get_db().connect("wms", "etl", autocommit=True) as conn:
        rows = conn.execute(
            "SELECT data FROM pg_logical_slot_get_binary_changes(%s, NULL, NULL, 'proto_version', '1', "
            "'publication_names', %s)", (name, publication)).fetchall()
    return {bytes(r[0])[:1].decode() for r in rows}


def _kinds(lines: list[str], schema: str = "wms") -> set[str]:
    out = set()
    for line in lines:
        if line.startswith(f"table {schema}."):
            out.add(line.split(": ", 2)[1])
    return out


def test_etl_reader_sees_inserts_updates_deletes_from_a_batch(built_engine: Engine, slots: list[str]) -> None:
    _create_slot("hl_test_decoding", "test_decoding", slots)
    _create_slot("hl_test_pgoutput", "pgoutput", slots)
    assert built_engine.batch(3)["status"] == "ok"
    lines = _decoded("hl_test_decoding")
    assert {"INSERT", "UPDATE", "DELETE"} <= _kinds(lines)
    assert any(line.startswith("table wms.warehouse: UPDATE") for line in lines)
    assert any(line.startswith("table wms.inventory_txn: DELETE: txn_id[bigint]") for line in lines)
    assert {"I", "U", "D"} <= _pgoutput_kinds("hl_test_pgoutput", "hl_wms_cdc")


@pytest.mark.slow
@pytest.mark.skipif(importlib.util.find_spec("harborline.sim.wms") is None, reason="domain module harborline.sim.wms not built yet")
def test_cdc_with_domain_modules(pg_settings: Settings, slots: list[str]) -> None:
    with get_db().connect("sim_internal") as conn:
        conn.execute("DELETE FROM sim.state")
        conn.commit()
    reset_engine()
    eng = get_engine()
    eng.load()
    try:
        eng.start_build("small")
        eng._build_thread.join(1800)
        assert eng.error is None, eng.error
        _create_slot("hl_test_domain", "test_decoding", slots)
        summary = eng.batch(24)
        assert summary["status"] == "ok", summary["error"]
        kinds = _kinds(_decoded("hl_test_domain"))
        assert {"INSERT", "UPDATE", "DELETE"} <= kinds, kinds
        totals = {k: sum(c[k] for c in summary["changes"] if c["system"] == "wms") for k in ("ins", "upd", "del")}
        assert all(v > 0 for v in totals.values()), totals
    finally:
        eng.shutdown()
        reset_engine()
