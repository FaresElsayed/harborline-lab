"""Finance-CRM scenarios applied after the shared Small build (runs after test_fincrm_build: it mutates the state)."""

from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from test_fincrm_build import test_ar_balance_equals_invoices_minus_settlements as check_ar
from test_fincrm_fake_wms import MODULES, fincrm_build

from harborline.core import Window
from harborline.sim import scenarios, testing
from harborline.sim.ax import Ax
from harborline.sim.edi import iter_documents
from harborline.sim.engine import merge_dynamic_clients, resolve_module


@pytest.fixture(scope="module")
def build(pg_settings: Any) -> dict[str, Any]:
    return fincrm_build()


class Apply:
    """Wraps a module so one run_window call applies a scenario and then simulates the window (like the engine)."""

    def __init__(self, name: str, scenario_id: str, params: dict[str, Any]) -> None:
        self.mod = resolve_module(name)
        self.name = name
        self.scenario_id, self.params = scenario_id, params
        self.msg: str | None = None

    def step(self, ctx: Any, win: Window) -> None:
        handler = getattr(self.mod, "apply_scenario", None)
        if handler:
            self.msg = handler(ctx, self.scenario_id, self.params, win)
        self.mod.step(ctx, win)


def run_scenario(build: dict[str, Any], scenario_id: str, params: dict[str, Any]) -> dict[str, str | None]:
    ctx = build["ctx"]
    start = ctx.window.end
    win = Window(start, start + timedelta(hours=1))
    params = {**params, "effective_date": start.date().isoformat()}
    if scenario_id == "offboard_client":
        params["end_date"] = (start + timedelta(days=params.get("notice_days", 0))).date().isoformat()
    testing.run_window(ctx, build["fake"], win)
    out = {}
    for name in MODULES:
        a = Apply(name, scenario_id, params)
        testing.run_window(ctx, a, win)
        out[name] = a.msg
    return out


def sf_rows(ctx: Any, where: str, params: Any = None) -> Any:
    return testing.fetch_df(ctx, "sim_internal", f"SELECT id, data, is_deleted, master_record_id FROM sfdc.record WHERE {where}", params)


def test_stream_ticks_are_small_and_cheap(build: dict[str, Any]) -> None:
    ctx = build["ctx"]
    start = ctx.window.end
    for i in range(12):
        win = Window(start + timedelta(minutes=5 * i), start + timedelta(minutes=5 * (i + 1)))
        testing.run_window(ctx, build["fake"], win)
        for name in MODULES:
            t = time.perf_counter()
            testing.run_window(ctx, name, win)
            assert time.perf_counter() - t < 2.0, f"{name} tick too slow"


def test_rate_increase_amends_salesforce(build: dict[str, Any]) -> None:
    ctx = build["ctx"]
    msgs = run_scenario(build, "rate_increase", {"client": "NBOUT", "pct": 6.0, "keyed_in_wms": "never"})
    assert msgs["crm"] and "NBOUT" in msgs["crm"]
    cards = sf_rows(ctx, "object = 'Rate_Card__c' AND data->>'Account__c' = (SELECT id FROM sfdc.record WHERE object = 'Account' "
                         "AND data->>'Name' = 'Northbound Outfitters' AND NOT is_deleted LIMIT 1)")
    status = [d["Status__c"] for d in cards["data"]]
    assert status.count("Active") == 1 and "Superseded" in status
    active = cards[[d["Status__c"] == "Active" for d in cards["data"]]].iloc[0]
    lines = sf_rows(ctx, "object = 'Rate_Card_Line__c' AND data->>'Rate_Card__c' = %s", (active["id"],))
    assert len(lines) > 5


def test_sf_account_merge(build: dict[str, Any]) -> None:
    ctx = build["ctx"]
    before = len(sf_rows(ctx, "object = 'Account' AND is_deleted AND master_record_id IS NOT NULL"))
    msgs = run_scenario(build, "sf_account_merge", {"client": "AUTO", "count": 2})
    assert msgs["crm"]
    merged = sf_rows(ctx, "object = 'Account' AND is_deleted AND master_record_id IS NOT NULL")
    assert len(merged) >= before + 2
    for row in merged.itertuples():
        assert row.data["MasterRecordId"] == row.master_record_id and row.data["IsDeleted"] is True
        kids = sf_rows(ctx, "object = 'Contact' AND data->>'AccountId' = %s AND NOT is_deleted", (row.id,))
        assert kids.empty, "contacts of the merged account must move to the winner"


def test_duplicate_file_resend_reuses_isa_control_number(build: dict[str, Any]) -> None:
    ctx = build["ctx"]
    msgs = run_scenario(build, "duplicate_file_resend", {"kind": "all", "count": 2})
    assert msgs["edi"] and msgs["files"]
    ctrls: dict[tuple[str, str], int] = {}
    for name, _ in iter_documents(Path(ctx.landing_dir) / "edi", "inbound/940"):
        key = (name.split("_")[1], name.removesuffix(".edi").rsplit("_", 1)[1])
        ctrls[key] = ctrls.get(key, 0) + 1
    assert max(ctrls.values()) >= 2


def test_schema_drift_release_adds_sf_field_and_csv_column(build: dict[str, Any]) -> None:
    ctx = build["ctx"]
    msgs = run_scenario(build, "schema_drift_release", {"systems": "all"})
    assert msgs["crm"] and msgs["files"]
    fields = testing.fetch_df(ctx, "sim_internal", "SELECT api_name FROM sfdc.custom_field WHERE object = 'Case'")
    assert "Carrier_Claim_Number__c" in set(fields["api_name"])
    testing.run_days(ctx, 8, modules=[build["fake"], *MODULES])
    newest = max((Path(ctx.landing_dir) / "files" / "clients").rglob("item_master_*.csv"), key=lambda p: p.stat().st_mtime)
    head = newest.read_bytes()[:400].decode("cp1252", errors="replace").lower()
    assert "gtin" in head


def test_offboard_and_onboard_client(build: dict[str, Any]) -> None:
    ctx = build["ctx"]
    msgs = run_scenario(build, "offboard_client", {"client": "PETALPN", "notice_days": 0})
    assert "Former Customer" in (msgs["crm"] or "")
    testing.run_days(ctx, 1, modules=[build["fake"], *MODULES])
    acct = sf_rows(ctx, "object = 'Account' AND data->>'Name' = 'Petal & Pine' AND NOT is_deleted")
    assert any(d["Type"] == "Former Customer" for d in acct["data"])
    params = {"vertical": "pet", "tier": "M", "warehouse": "ATL1", "currency": "USD", "go_live_days": 14, "name": ""}
    client = scenarios.make_client(ctx.rules, params, ctx.window.end, ctx.history_end, np.random.default_rng(1))
    ctx.world.setdefault("dynamic_clients", []).append(client)
    merge_dynamic_clients(ctx.rules, ctx.world)
    msgs = run_scenario(build, "onboard_client", {**params, "client": client})
    assert client["code"] in (msgs["crm"] or "")
    won = sf_rows(ctx, "object = 'Opportunity' AND data->>'StageName' = 'Closed Won' AND data->>'AccountId' IN "
                       "(SELECT id FROM sfdc.record WHERE object = 'Account' AND data->>'Name' = %s)", (client["name"],))
    assert len(won) == 1
    cust = testing.fetch_df(ctx, "ax2012", "SELECT count(*) AS n FROM dbo.custtable t JOIN dbo.dirpartytable p ON p.recid = t.party "
                                           "WHERE p.name = %s", (client["name"],))
    assert cust["n"].iloc[0] == 1


class _ReversePayment:
    """Returns the most recent customer payment (NSF / ACH return) inside a window."""

    name = "ax"

    def __init__(self) -> None:
        self.pay: int | None = None

    def step(self, ctx: Any, win: Window) -> None:
        ax = Ax(ctx)
        ax.floor = win.start
        if ax.st["pays"]:
            self.pay = int(max(ax.st["pays"], key=lambda k: ax.st["pays"][k]["date"]))
            ax.ev_reverse(win.start, {"pay": self.pay})
        ax.flush()


def test_returned_payment_deletes_settlements_and_reopens_invoices(build: dict[str, Any]) -> None:
    ctx = build["ctx"]
    before = testing.fetch_df(ctx, "ax2012", "SELECT count(*) AS n FROM dbo.custsettlement")["n"].iloc[0]
    rev = _ReversePayment()
    start = ctx.window.end
    changes = testing.run_window(ctx, rev, Window(start, start + timedelta(hours=1)))
    assert rev.pay is not None
    assert changes.rows[("ax2012", "custsettlement")][2] >= 2
    after = testing.fetch_df(ctx, "ax2012", "SELECT count(*) AS n FROM dbo.custsettlement")["n"].iloc[0]
    assert after == before   # the original pair is deleted, the payment/reversal pair is added
    check_ar(build)
