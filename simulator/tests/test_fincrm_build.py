"""Finance-CRM integrity checks over the shared Small build (FakeWms + crm/ax/edi/files)."""

from __future__ import annotations

import csv
import gzip
import io
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import openpyxl
import pandas as pd
import pytest
from test_fincrm_fake_wms import fincrm_build

from harborline.ids import to_id18
from harborline.sim import testing
from harborline.sim.crm import dates_of, obj_prefix, sim_clients
from harborline.sim.edi import iter_documents
from harborline.sim.files import Files, fuel_pct


@pytest.fixture(scope="module")
def build(pg_settings: Any) -> dict[str, Any]:
    return fincrm_build()


def df(ctx: Any, db: str, q: str, params: Any = None) -> pd.DataFrame:
    return testing.fetch_df(ctx, db, q, params)


def anomalies(ctx: Any, category: str) -> pd.DataFrame:
    return df(ctx, "sim_internal", "SELECT * FROM sim.anomaly_log WHERE category = %s", (category,))


def landing(ctx: Any) -> Path:
    return Path(ctx.landing_dir)


# ---------------------------------------------------------------------------------------------- AX: billing -> invoices
def test_invoices_equal_billing_records(build: dict[str, Any]) -> None:
    ctx = build["ctx"]
    lines = df(ctx, "ax2012", """
        SELECT j.invoiceid, j.invoiceamount::float AS header, sum(t.lineamount)::float AS lines,
               coalesce(sum(t.lineamount) FILTER (WHERE t.name NOT LIKE 'Monthly minimum%%'), 0)::float AS billable
        FROM dbo.custinvoicejour j JOIN dbo.custinvoicetrans t ON t.invoiceid = j.invoiceid AND t.dataareaid = j.dataareaid
        WHERE j.invoiceamount > 0 GROUP BY 1, 2""")
    billed = df(ctx, "wms", "SELECT invoice_ref, sum(amount)::float AS amount, count(*) AS n FROM wms.billing_record "
                           "WHERE billing_status = 'INVOICED' GROUP BY 1")
    assert len(lines) > 50 and len(billed) > 20
    merged = lines.merge(billed, left_on="invoiceid", right_on="invoice_ref", how="outer", indicator=True)
    assert set(merged["_merge"]) <= {"both", "left_only"}, "billing records point at invoices that do not exist"
    both = merged[merged["_merge"] == "both"]
    assert (both["billable"] - both["amount"]).abs().max() < 0.005
    minimum_only = merged[merged["_merge"] == "left_only"]
    assert (minimum_only["billable"].abs() < 0.005).all(), "invoice lines without billing records"
    mismatch = set(anomalies(ctx, "header_line_mismatch")["record_key"])
    bad = lines[(lines["header"] - lines["lines"]).abs() > 0.005]
    assert set(bad["invoiceid"]) <= mismatch
    open_old = df(ctx, "wms", "SELECT count(*) AS n FROM wms.billing_record WHERE billing_status = 'ACCRUED' AND activity_date < %s",
                  (build["ctx"].history_start.date().replace(month=7, day=1),))["n"].iloc[0]
    assert open_old == 0


def test_min_adj_only_below_minimum_and_never_for_purefrm(build: dict[str, Any]) -> None:
    ctx = build["ctx"]
    mins = df(ctx, "ax2012", "SELECT j.invoiceaccount FROM dbo.custinvoicetrans t JOIN dbo.custinvoicejour j "
                             "ON j.invoiceid = t.invoiceid WHERE t.name LIKE 'Monthly minimum%%'")
    assert len(mins) > 0
    from harborline.commercial import ax_account_num
    assert ax_account_num(ctx.rules, "PUREFRM") not in set(mins["invoiceaccount"].str.strip())


def test_ar_balance_equals_invoices_minus_settlements(build: dict[str, Any]) -> None:
    ctx = build["ctx"]
    trans = df(ctx, "ax2012", "SELECT recid, dataareaid, trim(accountnum) AS acct, transtype, txt, amountcur::float AS amt, "
                              "settleamountcur::float AS settled, amountmst::float AS mst, settleamountmst::float AS settled_mst, "
                              "closed FROM dbo.custtrans")
    sett = df(ctx, "ax2012", "SELECT transrecid, sum(settleamountcur)::float AS s FROM dbo.custsettlement GROUP BY 1")
    m = trans.merge(sett, left_on="recid", right_on="transrecid", how="left").fillna({"s": 0.0})
    assert (m["settled"] - m["s"]).abs().max() < 0.005, "custtrans.settleamountcur disagrees with custsettlement"
    payments = m[(m["transtype"] == 15) & ~m["txt"].str.startswith("Payment reversal")]
    assert (payments["amt"] < 0).all()
    assert len(m[m["transtype"] == 15]) > 20, "no payments were received"
    invoices = m[(m["transtype"] == 8) & (m["amt"] > 0)]
    ar_from_invoices = (invoices["amt"] - invoices["s"]).groupby(invoices["acct"]).sum()
    ar_all = (m["amt"] - m["settled"]).groupby(m["acct"]).sum()
    diff = (ar_from_invoices.reindex(ar_all.index, fill_value=0) - ar_all).abs()
    assert diff.max() < 0.01
    closed = m[(m["closed"].astype(str) != "1900-01-01") & (m["transtype"] == 8) & (m["amt"] > 0)]
    assert ((closed["amt"] - closed["settled"]).abs() < 0.005).all()
    gl = df(ctx, "ax2012", "SELECT e.subledgervoucherdataareaid AS ent, sum(a.accountingcurrencyamount)::float AS gl "
                           "FROM dbo.generaljournalaccountentry a JOIN dbo.generaljournalentry e ON e.recid = a.generaljournalentry "
                           "WHERE a.ledgeraccount = '1200' GROUP BY 1").set_index("ent")["gl"]
    sub = (m["mst"] - m["settled_mst"]).groupby(m["dataareaid"]).sum()
    tolerance = 0.05 + 30 * len(anomalies(ctx, "header_line_mismatch"))
    assert (gl.reindex(sub.index, fill_value=0) - sub).abs().max() < tolerance


def test_sla_credits_are_exceptions_not_the_rule(build: dict[str, Any]) -> None:
    ctx = build["ctx"]
    n = df(ctx, "ax2012", "SELECT count(*) FILTER (WHERE name LIKE 'SLA credit%%') AS sla, count(*) AS n FROM dbo.custinvoicetrans")
    assert n["sla"].iloc[0] < 0.05 * n["n"].iloc[0]


def test_general_journal_balances(build: dict[str, Any]) -> None:
    ctx = build["ctx"]
    unbalanced = df(ctx, "ax2012", "SELECT generaljournalentry, sum(accountingcurrencyamount) AS s FROM dbo.generaljournalaccountentry "
                                   "GROUP BY 1 HAVING abs(sum(accountingcurrencyamount)) > 0.001")
    assert unbalanced.empty


def test_billing_records_marked_invoiced_with_ax_ids(build: dict[str, Any]) -> None:
    ctx = build["ctx"]
    refs = df(ctx, "wms", "SELECT DISTINCT invoice_ref FROM wms.billing_record WHERE billing_status = 'INVOICED'")
    assert refs["invoice_ref"].str.match(r"^(US|CA|MX)-INV\d{7}$").all()


def test_fx_rates_drift_from_ecb_for_eur_gbp(build: dict[str, Any]) -> None:
    ctx = build["ctx"]
    rows = df(ctx, "ax2012", "SELECT p.fromcurrencycode AS ccy, r.exchangerate::float / 100 AS ax, r.validfrom FROM dbo.exchangerate r "
                             "JOIN dbo.exchangeratecurrencypair p ON p.recid = r.exchangeratecurrencypair WHERE p.tocurrencycode = 'USD'")
    from harborline.sim.crm import first_business_day
    eur = rows[rows["ccy"] == "EUR"]
    gaps = [abs(r.ax - 1 / ctx.signals.fx(first_business_day(r.validfrom.year, r.validfrom.month), "EUR")) /
            (1 / ctx.signals.fx(first_business_day(r.validfrom.year, r.validfrom.month), "EUR")) for r in eur.itertuples()]
    assert min(gaps) >= 0.004 and max(gaps) <= 0.026


def test_freight_bills_have_tracking_text_and_planted_lines(build: dict[str, Any]) -> None:
    ctx = build["ctx"]
    names = df(ctx, "ax2012", "SELECT name FROM dbo.vendinvoicetrans")["name"]
    assert names.str.match(r"^TRK 1Z\w+ WT [\d.]+(LB|KG) ZN [2-8] GND").any()
    assert names.str.startswith("FSC ").any() and names.str.startswith("FUEL SURCHARGE").any()
    assert names.str.match(r"^PRO \d+ BOL").any()


def test_fuel_lines_follow_the_published_diesel_file(build: dict[str, Any]) -> None:
    ctx = build["ctx"]
    with (landing(ctx) / "files" / "reference" / "diesel_weekly.csv").open(newline="", encoding="ascii") as fh:
        price = {r["week_start"]: float(r["diesel_usd_per_gal"]) for r in csv.DictReader(fh)}
    scac_of = {v["acct"]: key.split(":")[1] for key, v in ctx.world["ax"]["vend"].items() if key.startswith("CARRIER:")}
    late = {s["scac"] for s in ctx.planted["stories"] if s["id"] == "fuel_surcharge_misapplied"}
    lines = df(ctx, "ax2012", "SELECT j.invoiceaccount, j.invoicedate, t.name FROM dbo.vendinvoicetrans t "
                              "JOIN dbo.vendinvoicejour j ON j.invoiceid = t.invoiceid AND j.dataareaid = t.dataareaid "
                              "WHERE t.name LIKE 'FUEL SURCHARGE %%' OR t.name LIKE 'FSC %%'")
    assert len(lines) > 100
    seen = set()
    for line in lines.itertuples():
        scac = scac_of[line.invoiceaccount.strip()]
        match = re.match(r"(?:FUEL SURCHARGE|FSC) ([\d.]+)% (?:PRO )?(\S+)", line.name)
        service = "LTL" if line.name.startswith("FSC") else match.group(2)
        week = line.invoicedate - timedelta(days=14 if scac in late else 7)
        assert float(match.group(1)) == fuel_pct(scac, service, price[week.isoformat()]), (scac, line.name)
        seen.add(scac)
    assert seen - late


def test_draft_journals_edited_and_posted(build: dict[str, Any]) -> None:
    ctx = build["ctx"]
    j = df(ctx, "ax2012", "SELECT journalname, posted, recversion FROM dbo.ledgerjournaltable")
    acc = j[j["journalname"] == "ACCRUAL"]
    assert len(acc) >= 2 and (acc["recversion"] > 1).any() and (acc["posted"] == 1).any()


# ---------------------------------------------------------------------------------------------- Salesforce
def sf(ctx: Any, obj: str) -> pd.DataFrame:
    rows = df(ctx, "sim_internal", "SELECT id, data, is_deleted, deleted_date, master_record_id FROM sfdc.record WHERE object = %s", (obj,))
    data = pd.json_normalize(rows["data"].tolist()) if len(rows) else pd.DataFrame()
    return pd.concat([rows.drop(columns=["data"]).reset_index(drop=True), data], axis=1)


def test_sf_ids_have_valid_checksum_and_prefix(build: dict[str, Any]) -> None:
    ctx = build["ctx"]
    rows = df(ctx, "sim_internal", "SELECT id, object, data->>'Id' AS data_id, data->>'SystemModstamp' AS sm FROM sfdc.record")
    assert (rows["id"].str.len() == 18).all()
    assert all(to_id18(i[:15]) == i for i in rows["id"])
    assert (rows["id"] == rows["data_id"]).all()
    prefixes = obj_prefix()
    assert all(i.startswith(prefixes[o]) for i, o in zip(rows["id"], rows["object"], strict=True))
    assert rows["sm"].str.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.000\+0000$").all()


def test_every_closed_won_opportunity_maps_to_a_client(build: dict[str, Any]) -> None:
    ctx = build["ctx"]
    opps = sf(ctx, "Opportunity")
    accts = sf(ctx, "Account").set_index("id")
    won = opps[opps["StageName"] == "Closed Won"]
    assert len(won) >= len(sim_clients(ctx)) - 2
    names = {c["name"]: c for c in ctx.rules["clients"]}
    for row in won.itertuples():
        a = accts.loc[row.AccountId]
        assert not a["is_deleted"]
        assert a["Name"] in names or a["Name"].encode("cp1252", "replace").decode("utf-8", "replace") in names, a["Name"]
    nb = won[won["Type"] == "New Business"]
    for row in nb.itertuples():
        c = names.get(accts.loc[row.AccountId, "Name"])
        if c:
            d = dates_of(ctx, c)
            assert row.CloseDate == d.sf_closed_won.isoformat() and d.sf_closed_won < d.go_live
    lost = opps[opps["StageName"] == "Closed Lost"]
    assert len(lost) > 0 and lost["Loss_Reason__c"].notna().all()
    hist = sf(ctx, "OpportunityHistory")
    assert set(won["id"]) <= set(hist["OpportunityId"])


def test_account_types_and_crosswalk_gaps(build: dict[str, Any]) -> None:
    ctx = build["ctx"]
    accts = sf(ctx, "Account")
    live = accts[~accts["is_deleted"]]
    assert live["Health_Score__c"].isna().all() and live["Churn_Risk__c"].isna().all()
    clients = {c["name"]: c["code"] for c in sim_clients(ctx)}
    cust = live[live["Name"].isin(clients) & live["Type"].isin(["Customer - Direct", "Customer - Channel", "Former Customer"])]
    cust = cust.drop_duplicates("Name", keep="first")
    codes = cust["Name"].map(clients)
    blank = cust["WMS_Owner_Code__c"].isna()
    typo = ~blank & (cust["WMS_Owner_Code__c"] != codes)
    assert blank.sum() == 4 and typo.sum() == 3
    assert (live["Type"] == "Prospect").sum() > 40
    churned = {c["code"] for c in sim_clients(ctx) if c.get("churn_month") is not None and c["churn_month"] < -3}
    for name, code in clients.items():
        if code in churned:
            assert (cust.loc[cust["Name"] == name, "Type"] == "Former Customer").all()


def test_rate_cards_versions(build: dict[str, Any]) -> None:
    ctx = build["ctx"]
    cards = sf(ctx, "Rate_Card__c")
    lines = sf(ctx, "Rate_Card_Line__c")
    assert set(cards["Status__c"]) <= {"Draft", "Active", "Superseded", "Expired"}
    active = cards[cards["Status__c"] == "Active"]
    assert active.groupby("Account__c").size().max() == 1
    assert cards[cards["Status__c"] == "Superseded"]["End_Date__c"].notna().all()
    assert set(lines["Rate_Card__c"]) <= set(cards["id"])
    assert cards["Name"].str.match(r"^RC-\d{4}$").all()


def test_cases_reference_wms_keys_in_text(build: dict[str, Any]) -> None:
    ctx = build["ctx"]
    cases = sf(ctx, "Case")
    real = cases[cases["AccountId"].notna() | cases["Reason"].notna()]
    ship = real[real["Reason"].isin(["Late Shipment", "Damaged Goods", "Short Ship", "Mis-ship"])]
    text = ship["Subject"].fillna("") + " " + ship["Description"].fillna("")
    assert len(ship) > 5 and text.str.contains(r"PO \S+|order HL\d+|tracking \S+|HL\d{10}").mean() > 0.9
    filled = real["WMS_Order_Number__c"].notna().mean()
    assert 0 < filled < 0.6
    assert set(real["Status"]) >= {"Closed"} and (real.loc[real["Status"] == "Closed", "ClosedDate"].notna()).all()
    spam = cases[cases["is_deleted"]]
    assert len(spam) > 0 and spam["deleted_date"].notna().all()


def test_salesforce_changes_cover_insert_update_delete(build: dict[str, Any]) -> None:
    counts = build["counts"]
    sf_rows = {k[1]: v for k, v in counts.items() if k[0] == "salesforce"}
    assert sum(v[0] for v in sf_rows.values()) > 0 and sum(v[1] for v in sf_rows.values()) > 0
    assert sum(v[2] for v in sf_rows.values()) > 0
    assert counts[("edi", "940")][0] > 0 and counts[("files", "item_master")][0] > 0
    assert not any(k[0] == "ax2012" and v[0] < 0 for k, v in counts.items())


# ---------------------------------------------------------------------------------------------- EDI
def segments(data: bytes) -> list[str]:
    return [s.strip("\r\n") for s in data.decode("ascii").split("~") if s.strip("\r\n")]


def docs(ctx: Any, sub: str = "") -> list[tuple[str, bytes]]:
    """Every EDI document (loose files and archived history) under landing/edi/<sub>."""
    return list(iter_documents(landing(ctx) / "edi", sub))


def envelope_ok(segs: list[str]) -> bool:
    if len(segs[0]) != 105 or not segs[0].startswith("ISA*"):
        return False
    isa, gs, ge, iea = (s.split("*") for s in (segs[0], segs[1], segs[-2], segs[-1]))
    if iea[0] != "IEA" or iea[1] != "1" or iea[2] != isa[13] or ge[0] != "GE" or ge[2] != gs[6]:
        return False
    n_sets, i = 0, 2
    while i < len(segs) - 2:
        st = segs[i].split("*")
        if st[0] != "ST":
            return False
        j = i
        while not segs[j].startswith("SE*"):
            j += 1
        se = segs[j].split("*")
        if int(se[1]) != j - i + 1 or se[2] != st[2]:
            return False
        n_sets += 1
        i = j + 1
    return int(ge[1]) == n_sets


def test_edi_history_is_archived_by_month_to_keep_the_file_count_down(build: dict[str, Any]) -> None:
    ctx = build["ctx"]
    zips = list((landing(ctx) / "edi" / "archive").rglob("*.zip"))
    assert zips and all(re.fullmatch(r"\d{3}_[A-Z0-9]+_\d{6}\.zip", z.name) for z in zips)


def test_edi_envelopes_valid(build: dict[str, Any]) -> None:
    ctx = build["ctx"]
    malformed = {Path(p).name.rsplit("!", 1)[-1] for p in anomalies(ctx, "malformed_edi")["object"]}
    files = docs(ctx)
    assert {"940", "945", "997", "856"} <= {name.split("_", 1)[0] for name, _ in files}
    for name, data in files:
        if name in malformed:
            continue
        assert envelope_ok(segments(data)), name
        assert re.match(r"^(940|945|997|856)_[A-Z0-9]+_\d{14}_\d{9}\.edi$", name)


def test_every_non_dropped_940_maps_to_a_wms_order(build: dict[str, Any]) -> None:
    ctx = build["ctx"]
    refs = set()
    for name, data in docs(ctx, "inbound/940"):
        code = name.split("_")[1]
        for s in segments(data):
            if s.startswith("W05*"):
                refs.add((code, s.split("*")[3].strip().upper()))
    wms = df(ctx, "wms", "SELECT o.owner_code, h.client_order_ref, h.received_ts FROM wms.order_header h "
                         "JOIN wms.owner o ON o.owner_id = h.owner_id WHERE h.channel = 'EDI'")
    wms["client_order_ref"] = wms["client_order_ref"].fillna("").str.strip().str.upper()
    known = set(zip(wms["owner_code"], wms["client_order_ref"], strict=True))
    since = ctx.history_start.replace(tzinfo=None) + timedelta(days=1)
    recent = wms[pd.to_datetime(wms["received_ts"]) > since]
    dropped = {(c, po.strip().upper()) for c, po, _ in ctx.world["edi"]["dropped"]}
    missing = refs - known
    # the WMS hard-deletes a few orders later (abandoned / cancelled); everything else missing must be a planted drop
    assert len(missing - dropped) <= 0.01 * len(refs), f"940 orders missing in the WMS: {sorted(missing - dropped)[:5]}"
    assert {c for c, _ in missing} <= {"APEXAUT", "BEACNHW"}
    assert set(zip(recent["owner_code"], recent["client_order_ref"], strict=True)) <= refs, "an EDI order has no 940"


def test_945_matches_shipments_and_997_acks_every_inbound(build: dict[str, Any]) -> None:
    ctx = build["ctx"]
    inbound = len(docs(ctx, "inbound"))
    acks = len(docs(ctx, "outbound/997"))
    resends = len(anomalies(ctx, "duplicate_row").query("system == 'edi'"))
    assert acks == inbound - resends
    orders = set(df(ctx, "wms", "SELECT order_nbr FROM wms.order_header")["order_nbr"])
    for _, data in docs(ctx, "outbound/945")[:50]:
        for s in segments(data):
            if s.startswith("W06*"):
                assert s.split("*")[5] in orders


# ---------------------------------------------------------------------------------------------- business files
def test_xlsx_files_open_with_openpyxl(build: dict[str, Any]) -> None:
    ctx = build["ctx"]
    files = sorted((landing(ctx) / "files").rglob("*.xlsx"))
    names = {p.name for p in files}
    assert any(n.startswith("UPSN_rates_") for n in names)
    assert any(re.match(r"budget_FY\d{4}\.xlsx", n) for n in names) and any("reforecast" in n for n in names)
    assert any(n.startswith("warehouse_master_") for n in names)
    for p in files:
        wb = openpyxl.load_workbook(p)
        assert wb.sheetnames
        if p.name.startswith("budget_"):
            ws = wb.worksheets[0]
            assert ws.merged_cells.ranges
            labels = [ws.cell(r, 3).value for r in range(1, ws.max_row + 1)]
            assert "Total Revenue" in labels and "Operating Income (EBITDA)" in labels
        if "_rates_" in p.name:
            assert "Fuel Surcharge" in wb.sheetnames and "Accessorials" in wb.sheetnames


def test_item_master_csv_encodings_as_declared(build: dict[str, Any]) -> None:
    ctx = build["ctx"]
    root = landing(ctx) / "files" / "clients"
    specs = sorted(root.glob("*/file_spec.txt"))
    assert len(specs) >= 10
    enc_map = {"UTF-8": "utf-8", "UTF-8 with BOM": "utf-8-sig", "Windows-1252 (ANSI)": "cp1252"}
    delim_map = {"comma (,)": ",", "semicolon (;)": ";", "pipe (|)": "|", "tab": "\t"}
    seen = set()
    for spec in specs:
        text = spec.read_text(encoding="utf-8")
        enc = enc_map[re.search(r"Encoding: (.+)", text).group(1)]
        delim = delim_map[re.search(r"Delimiter: (.+)", text).group(1)]
        seen.add(enc)
        for f in spec.parent.glob("item_master_*.csv"):
            raw = f.read_bytes()
            assert raw.startswith(b"\xef\xbb\xbf") == (enc == "utf-8-sig"), f
            body = raw.decode(enc)
            rows = list(csv.reader(io.StringIO(body), delimiter=delim))
            assert len(rows[0]) >= 22 and all(len(r) == len(rows[0]) for r in rows if r), f
    assert len(seen) >= 2


def test_carrier_history_export(build: dict[str, Any]) -> None:
    ctx = build["ctx"]
    now = ctx.history_start + timedelta(days=125)
    Files(ctx).export_history(now)
    ctx.commit()
    out = sorted((landing(ctx) / "files" / "carrier" / "history_export").glob("tracking_events_*.csv.gz"))
    assert out and out[0].name == f"tracking_events_{ctx.history_start:%Y%m}.csv.gz"
    with gzip.open(out[0], "rt", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    cutoff = now - timedelta(days=90)
    expected = df(ctx, "sim_internal", "SELECT count(*) AS n FROM carrier.tracking_event WHERE event_ts >= %s AND event_ts < %s",
                  (ctx.history_start, min(cutoff, ctx.history_start + timedelta(days=30))))["n"].iloc[0]
    assert len(rows) == expected > 0
    assert rows[0]["event_ts_utc"].endswith("Z")
