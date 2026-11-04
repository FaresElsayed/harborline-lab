"""Microsoft Dynamics AX 2012 (database ax2012, schema dbo).

Reference data at bootstrap (legal entities, ledgers, currencies, finance-keyed exchange rates, chart of accounts,
financial-dimension combinations, payment terms, enum labels); customers, vendors and workers over time; and the
finance calendar: month-end invoicing from WMS billing records (with MIN-ADJ lines and SLA credits), customer
payments and settlements per client payment behaviour, disputes and credit notes, weekly carrier freight bills with
per-package free-text lines, vendor payment runs, payroll / rent / utilities / equipment journals and unposted draft
journals that are edited or deleted before posting.

AX conventions: UTC naive timestamps, integer enums, 1900-01-01 empty dates, RECID per table from 5637144576,
RECVERSION bumped on every update, MODIFIEDBY holds AX user ids, posted documents are immutable.
"""

from __future__ import annotations

import hashlib
import heapq
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from ..commercial import ax_account_num, month_start
from ..core import Window, derive_seed
from ..ids import AX_MAX_DATETIME, AX_RECID_BASE
from .crm import (
    ISO3,
    add_business_days,
    add_months,
    as_utc,
    at_local,
    biz_ts,
    bulk_update,
    churn_of,
    copy_df,
    dates_of,
    diesel,
    dispute_share,
    entity_of_warehouse,
    first_business_day,
    fx_per_usd,
    hist_end,
    hq_address,
    is_business_day,
    last_business_day,
    module_state,
    naive_utc,
    payment_behaviour,
    preset_warehouses,
    rate_on_date,
    rate_versions,
    region_key,
    scenario_client,
    sim_clients,
    sla_terms,
    to_date,
    to_local_naive,
    warehouse_opened,
)
from .files import (
    COUNTRY_CCY,
    UNIT,
    accessorials,
    das_zip,
    equipment_monthly,
    fuel_pct,
    ltl_charge,
    parcel_rate,
    rate_effective,
    rent_monthly,
    tariff_spec,
    utilities_monthly,
)

EMPTY = date(1900, 1, 1)
MAXDT = datetime.fromisoformat(AX_MAX_DATETIME)
EP = {"us01": "US", "ca01": "CA", "mx01": "MX"}
ENT_COUNTRY = {"us01": "US", "ca01": "CA", "mx01": "MX"}
ENT_TZ = {"us01": "America/New_York", "ca01": "America/Toronto", "mx01": "America/Monterrey"}
HAS_BY = {"dirpartytable", "custtable", "vendtable", "custinvoicejour", "custtrans", "vendinvoicejour", "vendtrans",
          "ledgerjournaltable", "ledgerjournaltrans"}
ACCOUNT_TYPE = {"Asset": 5, "Liability": 6, "Revenue": 1, "Expense": 2, "Equity": 7}
FX_CCYS = ("EUR", "GBP", "CAD", "MXN")
TIER_CREDIT = {"S": 25000, "M": 75000, "L": 250000, "XL": 750000}
LANG = {"US": "en-us", "CA": "en-ca", "MX": "es-mx", "EU": "fr", "GB": "en-gb"}
SGA = {"EXEC": 85000, "FIN": 120000, "SLS": 140000, "CS": 90000, "IT": 130000, "QA": 40000, "HR": 35000}
LANDLORD = {"ATL1": "Greenwood Industrial Partners LLC", "DFW1": "Wintergreen Logistics Park LP",
            "RNO1": "Waltham Way Holdings LLC", "CHI1": "Channahon Cold Storage Realty LLC", "NJ1": "Station Road Industrial REIT",
            "LAX1": "Inland Empire Airport Logistics LLC", "TOR1": "Millcreek Industrial Properties Inc.",
            "MTY1": "Parque Industrial Apodaca S.A. de C.V."}
UTILITY = {"ATL1": "Southern Crescent Power", "DFW1": "North Texas Electric Cooperative", "RNO1": "Sierra Valley Energy",
           "CHI1": "Prairie State Power & Light", "NJ1": "Garden State Electric & Gas", "LAX1": "Inland Valley Edison",
           "TOR1": "Peel Region Hydro", "MTY1": "Energía del Noreste S.A. de C.V."}
OTHER_VENDORS = [("SUPPLIES", "us01", "Packline Packaging Supply"), ("SUPPLIES", "ca01", "Northern Carton Co."),
                 ("SUPPLIES", "mx01", "Empaques Regiomontanos S.A. de C.V."), ("IT", "us01", "Cloudbridge Managed IT"),
                 ("IT", "us01", "Fieldgate Software LLC")]
CARRIER_HQ = {"UPSN": ("Atlanta", "GA", "30328", "US"), "FDEG": ("Pittsburgh", "PA", "15108", "US"),
              "FDE": ("Memphis", "TN", "38125", "US"), "USPS": ("Washington", "DC", "20260", "US"),
              "DHLC": ("Weston", "FL", "33326", "US"), "VRPX": ("Dallas", "TX", "75247", "US"),
              "CNPC": ("Ottawa", "ON", "K1A 0B1", "CA"), "PURO": ("Mississauga", "ON", "L4W 5M8", "CA"),
              "ESTF": ("Ciudad de México", "CMX", "01030", "MX"), "ODFL": ("Thomasville", "NC", "27360", "US"),
              "MRLF": ("Macon", "GA", "31201", "US"), "EXLA": ("Richmond", "VA", "23230", "US"),
              "TRMX": ("Monterrey", "NL", "64000", "MX")}
ENUMS = {
    "LedgerTransType": [("None", 0), ("Transfer", 1), ("Sales", 2), ("Purch", 3), ("Invent", 4), ("Production", 5),
                        ("Project", 6), ("Interest", 7), ("Cust", 8), ("ExchAdjustment", 9), ("Vend", 14), ("Payment", 15),
                        ("Tax", 16), ("Bank", 17), ("Settlement", 24), ("CashDiscount", 27), ("WriteOff", 35),
                        ("GeneralJournal", 36)],
    "LedgerPostingType": [("Tax", 4), ("LedgerJournal", 14), ("Bank", 20), ("CustBalance", 31), ("CustRevenue", 32),
                          ("CustPayment", 38), ("CustSettlement", 40), ("VendBalance", 41), ("VendPayment", 46),
                          ("SalesRevenue", 51), ("PurchConsump", 71), ("InventReceipt", 91), ("InventIssue", 92)],
    "CustVendorBlocked": [("No", 0), ("Invoice", 1), ("All", 2), ("Payment", 3), ("Requisition", 4), ("Never", 5)],
    "NoYes": [("No", 0), ("Yes", 1)],
    "DimensionLedgerAccountType": [("ProfitAndLoss", 0), ("Revenue", 1), ("Expense", 2), ("BalanceSheet", 4), ("Asset", 5),
                                   ("Liability", 6), ("Equity", 7)],
    "LedgerJournalACType": [("Ledger", 0), ("Cust", 1), ("Vend", 2), ("Project", 3), ("FixedAssets", 4), ("Bank", 6)],
    "LedgerJournalType": [("Daily", 0), ("VendInvoiceRegister", 3), ("CustPayment", 9), ("Payment", 10)],
    "HcmEmploymentType": [("Employee", 0), ("Contractor", 1)],
}
AX_UNIT = {"PALLET_MONTH": "PLTMO", "BIN_MONTH": "BINMO", "PALLET": "PLT", "CARTON": "CTN", "ORDER": "ORD", "UNIT": "EA",
           "KIT": "KIT", "MONTH": "MO", "SHIPMENT": "SHP"}


def splitname(full: str) -> tuple[str, str]:
    first, _, last = full.replace("Dr. ", "").partition(" ")
    return first, last


class Ax:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.st = module_state(ctx, "ax")
        for key, empty in (("recid", {}), ("seq", {}), ("q", []), ("cust", {}), ("vend", {}), ("workers", {}), ("davc", {}),
                           ("ledger", {}), ("dataarea", {}), ("pairs", {}), ("axrate", {}), ("open_ar", {}), ("pays", {}),
                           ("open_ap", {}), ("ship_buf", []), ("labor", {}), ("sla", {}), ("rcv", {}), ("drafts", {}),
                           ("shipcnt", {}), ("closed", {}), ("jobs", {}), ("mainaccount", {})):
            self.st.setdefault(key, empty)
        self.st.setdefault("qseq", 0)
        self.seed = ctx.seed
        self.hend = hist_end(ctx)
        self.hstart = as_utc(ctx.history_start).date()
        self.clients = {c["code"]: c for c in sim_clients(ctx)}
        self.whs = preset_warehouses(ctx)
        self.ents = {e["dataareaid"]: e for e in ctx.rules["legal_entities"]}
        self.rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.frames: dict[str, list[pd.DataFrame]] = defaultdict(list)
        self.pending: dict[int, dict[str, Any]] = {}
        self.updates: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.deletes: dict[str, list[int]] = defaultdict(list)
        self.pay_batch: list[tuple[datetime, dict[str, Any]]] = []
        self.floor: datetime | None = None

    # ================================================================== plumbing
    def rng(self, *parts: Any) -> np.random.Generator:
        return np.random.default_rng(derive_seed(self.seed, "ax", *parts))

    def recids(self, table: str, n: int) -> np.ndarray:
        start = self.st["recid"].get(table, AX_RECID_BASE)
        self.st["recid"][table] = start + n
        return np.arange(start, start + n, dtype=np.int64)

    def recid(self, table: str) -> int:
        return int(self.recids(table, 1)[0])

    def seq(self, name: str, start: int = 1) -> int:
        n = self.st["seq"].get(name, start)
        self.st["seq"][name] = n + 1
        return n

    def voucher(self, ent: str) -> str:
        return f"{EP[ent]}-V{self.seq('v:' + ent, 10_000_001):08d}"

    def add(self, table: str, row: dict[str, Any], ts: datetime, by: str | None = None) -> int:
        row.setdefault("recid", self.recid(table))
        stamp = naive_utc(ts)
        row.setdefault("createddatetime", stamp)
        row.setdefault("modifieddatetime", stamp)
        if table in HAS_BY or table == "generaljournalentry":
            row.setdefault("createdby", by or "Admin")
        if table in HAS_BY:
            row.setdefault("modifiedby", by or "Admin")
        self.rows[table].append(row)
        self.pending[row["recid"]] = row
        return row["recid"]

    def upd(self, table: str, recid: int, ts: datetime, by: str | None = None, **cols: Any) -> None:
        row = self.pending.get(recid)
        if row is not None and row in self.rows[table]:
            row.update(cols)
            row["modifieddatetime"] = naive_utc(ts)
            if table in HAS_BY:
                row["modifiedby"] = by or "Admin"
            return
        u = {"recid": recid, **cols, "modifieddatetime": naive_utc(ts)}
        if table in HAS_BY:
            u["modifiedby"] = by or "Admin"
        self.updates[table].append(u)

    def delete(self, table: str, recid: int) -> None:
        self.deletes[table].append(int(recid))

    def schedule(self, ts: datetime, kind: str, payload: dict[str, Any]) -> None:
        self.st["qseq"] += 1
        heapq.heappush(self.st["q"], (as_utc(ts).timestamp(), self.st["qseq"], kind, payload))

    def run_queue(self, until: datetime) -> None:
        q = self.st["q"]
        while q and q[0][0] < until.timestamp():
            ts_f, _, kind, payload = heapq.heappop(q)
            ts = datetime.fromtimestamp(ts_f, as_utc(until).tzinfo)
            if self.floor is not None and ts < self.floor:
                ts = self.floor
            getattr(self, f"ev_{kind}")(ts, payload)
        self.flush_payments()

    def dqlog(self, category: str, table: str, key: Any, column: str | None, before: Any, after: Any, ts: datetime,
              note: str | None = None) -> None:
        self.ctx.dq.log(category, "ax2012", table, None if key is None else str(key), column, before, after, note, ts)

    def flush(self) -> None:
        for table in list(self.rows) + [t for t in self.frames if t not in self.rows]:
            parts = []
            if self.rows.get(table):
                parts.append(pd.DataFrame(self.rows[table]))
            parts += self.frames.get(table, [])
            if not parts:
                continue
            df = parts[0] if len(parts) == 1 else pd.concat(parts, ignore_index=True)
            copy_df(self.ctx, "ax2012", f"dbo.{table}", df)
        for table, ups in self.updates.items():
            groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
            for u in ups:
                groups[tuple(sorted(u))].append(u)
            for cols, items in groups.items():
                df = pd.DataFrame(items, columns=list(cols)).drop_duplicates("recid", keep="last")
                sets = ", ".join(f"{c} = u.{c}" for c in cols if c != "recid") + ", recversion = t.recversion + 1"
                bulk_update(self.ctx, "ax2012", f"dbo.{table}", df, ["recid"], set_sql=sets)
        for table, ids in self.deletes.items():
            self.ctx.conn("ax2012").execute(f"DELETE FROM dbo.{table} WHERE recid = ANY(%s)", (ids,))
        self.rows.clear()
        self.frames.clear()
        self.pending.clear()
        self.updates.clear()
        self.deletes.clear()

    # ================================================================== reference data
    def bootstrap(self) -> None:
        go = min(date(2014, 1, 6), self.hstart - timedelta(days=500))
        ts = at_local(go, 15.0, "America/New_York")
        self.reference(ts)
        for m in range(14, -1, -1):
            d = add_months(self.hstart.replace(day=1), -m)
            self.key_month(d.year, d.month, at_local(first_business_day(d.year, d.month), 8.5, "America/New_York"))
        for i, c in enumerate(sorted(self.clients.values(), key=lambda x: dates_of(self.ctx, x).ax_created)):
            self.schedule_customer(c)
        self.vendors_bootstrap(ts)
        self.workers_bootstrap()
        self.run_queue(as_utc(self.ctx.history_start))
        self.flush()

    def reference(self, ts: datetime) -> None:
        for i, e in enumerate([{"dataareaid": "dat", "name": "Default company"}] + self.ctx.rules["legal_entities"]):
            rid = self.add("dataarea", {"id": e["dataareaid"], "name": e["name"], "isvirtual": 0}, ts)
            for k in ("createddatetime", "modifieddatetime"):
                self.rows["dataarea"][-1].pop(k)
            self.st["dataarea"][e["dataareaid"]] = rid
        for e in self.ctx.rules["legal_entities"]:
            self.st["ledger"][e["dataareaid"]] = self.add("ledger", {
                "name": e["dataareaid"].upper(), "primaryforlegalentity": self.st["dataarea"][e["dataareaid"]],
                "accountingcurrency": e["currency"], "reportingcurrency": "USD"}, ts)
        for code, txt, sym in (("USD", "US Dollar", "$"), ("CAD", "Canadian Dollar", "$"), ("MXN", "Mexican Peso", "$"),
                               ("EUR", "Euro", "€"), ("GBP", "Pound Sterling", "£"), ("JPY", "Japanese Yen", "¥"),
                               ("CNY", "Chinese Yuan", "¥")):
            self.add("currency", {"currencycode": code, "txt": txt, "currencycodeiso": code, "symbol": sym,
                                  "roundingprecision": 1 if code == "JPY" else 0.01}, ts)
        default = self.add("exchangeratetype", {"name": "Default", "description": "Month-end accounting rates"}, ts)
        self.add("exchangeratetype", {"name": "Budget", "description": "FP&A budget rates"}, ts)
        pairs = [(c, "USD") for c in FX_CCYS] + [("USD", "CAD"), ("USD", "MXN")]
        for frm, to in pairs:
            self.st["pairs"][f"{frm}/{to}"] = self.add("exchangeratecurrencypair", {
                "exchangeratetype": default, "fromcurrencycode": frm, "tocurrencycode": to,
                "exchangeratedisplayfactor": 2 if "MXN" in (frm, to) else 0}, ts)
        for gl in self.ctx.rules["gl_accounts"]:
            self.st["mainaccount"][gl["id"]] = self.add("mainaccount", {
                "mainaccountid": gl["id"], "name": gl["name"], "type": ACCOUNT_TYPE[gl["type"]], "ledgerchartofaccounts": 5637144576,
                "currencycode": None, "debitcreditproposal": 0, "monetary": 1, "closetype": 0}, ts)
        for gl in ("1100", "1200", "2000", "2100"):
            self.dim(gl, ts)
        sites = list(self.whs)
        for gl in ("4000", "4010", "4020", "4030", "5000", "5100", "5200", "6000", "6100", "6200"):
            for s in sites:
                self.dim(f"{gl}-{s}-OPS", ts)
        for s in sites:
            self.dim(f"4900-{s}-CS", ts)
        for d in SGA:
            self.dim(f"7000-CORP-{d}", ts)
        for disp in ("6300-CORP-IT", "6400-CORP-FIN", "8000-CORP-FIN", "4900-CORP-FIN"):
            self.dim(disp, ts)
        for ent in self.ents:
            for t in self.ctx.rules["payment_terms"]:
                self.add("paymterm", {"dataareaid": ent, "paymtermid": t["id"], "description": t["description"], "paymmethod": 0,
                                      "numofdays": t["days"], "numofmonths": 0,
                                      "cash": f"{t.get('cash_discount_pct', 0):g}%{t.get('discount_days', 0)}D" if t.get("cash_discount_pct") else None,
                                      "cutoffday": 0}, ts)
        rid = self.recids("srsanalysisenums", sum(len(v) for v in ENUMS.values()))
        i = 0
        for name, items in ENUMS.items():
            for item, value in items:
                self.rows["srsanalysisenums"].append({"enumname": name, "enumitemname": item, "enumitemvalue": value,
                                                      "enumitemlabel": " ".join(_split_camel(item)), "languageid": "en-us",
                                                      "recid": int(rid[i])})
                i += 1

    def dim(self, display: str, ts: datetime | None = None) -> int:
        rid = self.st["davc"].get(display)
        if rid is None:
            main = display.split("-")[0]
            rid = self.add("dimensionattributevaluecombination", {
                "displayvalue": display, "mainaccount": self.st["mainaccount"].get(main, 0),
                "accountstructure": 5637145326 if "-" in display else 0, "ledgerdimensiontype": 0,
                "hash": hashlib.sha1(display.encode()).hexdigest().upper()}, ts or as_utc(self.ctx.history_start))
            self.st["davc"][display] = rid
        return rid

    # ================================================================== exchange rates (finance-keyed, planted drift)
    def key_month(self, y: int, m: int, ts: datetime) -> None:
        key0 = f"{y:04d}-{m:02d}"
        if f"EUR:{key0}" in self.st["axrate"]:
            return
        d = first_business_day(y, m)
        start = date(y, m, 1)
        end = add_months(start, 1) - timedelta(days=1)
        for ccy in FX_CCYS:
            ecb = 1 / fx_per_usd(self.ctx, d, ccy)
            r = self.rng("axfx", ccy, key0)
            if ccy in ("EUR", "GBP"):
                v = ecb * (1 - float(r.uniform(0.005, 0.025)))
            else:
                v = ecb * (1 + float(r.normal(0, 0.0008)))
            v = round(v, 6 if ccy == "MXN" else 4)
            self.st["axrate"][f"{ccy}:{key0}"] = v
            self.add("exchangerate", {"exchangeratecurrencypair": self.st["pairs"][f"{ccy}/USD"], "exchangerate": round(v * 100, 4),
                                      "validfrom": start, "validto": end}, ts, "alanb")
            if ccy in ("CAD", "MXN"):
                self.add("exchangerate", {"exchangeratecurrencypair": self.st["pairs"][f"USD/{ccy}"],
                                          "exchangerate": round(100 / v, 4), "validfrom": start, "validto": end}, ts, "alanb")

    def usd_per(self, ccy: str, d: date) -> float:
        ccy = ccy.strip().upper()
        if ccy == "USD":
            return 1.0
        key = f"{ccy}:{d.year:04d}-{d.month:02d}"
        v = self.st["axrate"].get(key)
        if v is None:
            self.key_month(d.year, d.month, at_local(first_business_day(d.year, d.month), 8.5, "America/New_York"))
            v = self.st["axrate"].get(key, 1 / fx_per_usd(self.ctx, d, ccy) if ccy in FX_CCYS else 1.0)
        return float(v)

    def exch(self, ccy: str, ent: str, d: date) -> float:
        """AX EXCHRATE (x100) from transaction currency to the entity's accounting currency."""
        acct = self.ents[ent]["currency"]
        return round(self.usd_per(ccy, d) / self.usd_per(acct, d) * 100, 6)

    def to_usd(self, amount: float, ent: str, d: date) -> float:
        return round(amount * self.usd_per(self.ents[ent]["currency"], d), 2)

    # ================================================================== general ledger
    def post_gl(self, ts: datetime, ent: str, acctdate: date, docnum: str, category: int, voucher: str,
                lines: list[tuple[str, int, float, str, float, str]], by: str) -> None:
        """lines: (ledger display value, posting type, accounting amount (signed), text, transaction amount, currency)."""
        if not lines:
            return
        gje = self.add("generaljournalentry", {
            "ledger": self.st["ledger"][ent], "accountingdate": acctdate, "documentdate": acctdate, "documentnumber": docnum,
            "journalnumber": f"{EP[ent]}-GJ{self.seq('gj:' + ent, 100001):07d}", "journalcategory": category, "postinglayer": 0,
            "subledgervoucher": voucher, "subledgervoucherdataareaid": ent, "fiscalcalendarperiod": 0, "transferid": 0}, ts, by)
        total = round(sum(x[2] for x in lines[:-1]), 2)
        fixed = list(lines[:-1]) + [(lines[-1][0], lines[-1][1], -total, lines[-1][3], lines[-1][4], lines[-1][5])]
        acct_ccy = self.ents[ent]["currency"]
        for disp, ptype, amount, text, tamount, tccy in fixed:
            amount = round(amount, 2)
            if amount == 0:
                continue
            self.add("generaljournalaccountentry", {
                "generaljournalentry": gje, "ledgerdimension": self.dim(disp, ts), "ledgeraccount": disp, "postingtype": ptype,
                "transactioncurrencyamount": round(tamount if tccy != acct_ccy else amount, 2), "transactioncurrencycode": tccy,
                "accountingcurrencyamount": amount, "reportingcurrencyamount": self.to_usd(amount, ent, acctdate),
                "iscredit": int(amount < 0), "iscorrection": 0, "quantity": 0, "text": text[:1000], "allocationlevel": 0,
                "paymentreference": None}, ts)

    # ================================================================== parties
    def party(self, ts: datetime, name: str, alias: str, lang: str, kind: int, by: str) -> int:
        return self.add("dirpartytable", {
            "partynumber": f"{self.seq('party', 1001):09d}", "name": name[:100], "namealias": alias[:20], "knownas": None,
            "languageid": lang, "instancerelationtype": kind, "primaryaddresslocation": 0, "primarycontactemail": 0,
            "primarycontactphone": 0}, ts, by)

    def postal(self, ts: datetime, party: int, addr: dict[str, Any], desc: str = "Business") -> tuple[int, int]:
        loc = self.add("logisticslocation", {"locationid": f"{self.seq('loc', 100001):09d}", "description": desc,
                                             "ispostaladdress": 1, "parentlocation": 0}, ts)
        self.add("dirpartylocation", {"party": party, "location": loc, "isprimary": 1, "ispostaladdress": 1,
                                      "islocationowner": 1, "isprivate": 0}, ts)
        pid = self.add("logisticspostaladdress", self.address_row(loc, addr, ts), ts)
        self.upd("dirpartytable", party, ts, None, primaryaddresslocation=loc)
        return loc, pid

    @staticmethod
    def address_row(loc: int, addr: dict[str, Any], ts: datetime) -> dict[str, Any]:
        iso3 = ISO3.get(addr["country"], addr["country"])
        city_line = f"{addr['city']}, {addr['state']} {addr['postal']}" if addr.get("state") else f"{addr['postal']} {addr['city']}"
        return {"location": loc, "address": f"{addr['street']}\n{city_line}\n{iso3}", "street": addr["street"],
                "streetnumber": None, "city": addr["city"], "county": None, "state": addr.get("state") or None,
                "zipcode": addr["postal"], "countryregionid": iso3, "districtname": None, "postbox": None,
                "latitude": addr.get("lat"), "longitude": addr.get("lon"), "timezone": 0, "validfrom": naive_utc(ts),
                "validfromtzid": 0, "validto": MAXDT, "validtotzid": 0, "isprivate": 0, "privateforparty": 0}

    # ================================================================== customers
    def schedule_customer(self, c: dict[str, Any]) -> None:
        code = c["code"]
        if code in self.st["cust"] or self.st["jobs"].get(f"cust:{code}"):
            return
        self.st["jobs"][f"cust:{code}"] = True
        d = dates_of(self.ctx, c)
        r = self.rng("cust", code)
        self.schedule(biz_ts(d.ax_created, r), "cust_create", {"code": code})
        if r.random() < 0.12:
            lo, hi = d.ax_created + timedelta(days=200), self.hend - timedelta(days=10)
            if hi > lo:
                self.schedule(biz_ts(lo + timedelta(days=int(r.integers(0, (hi - lo).days))), r), "cust_address", {"code": code})
        churn = churn_of(self.ctx, c)
        if churn:
            self.schedule(biz_ts(churn + timedelta(days=int(r.integers(60, 120))), r), "cust_block", {"code": code, "blocked": 2})
        for i, ann in enumerate(d.anniversaries):
            if churn and ann >= churn:
                break
            self.schedule(biz_ts(ann + timedelta(days=int(r.integers(0, 20))), r), "cust_review", {"code": code, "n": i})

    def ev_cust_create(self, ts: datetime, p: dict[str, Any]) -> None:
        code = p["code"]
        c = self.clients.get(code)
        if c is None or code in self.st["cust"]:
            return
        region = region_key(c)
        r = self.rng("custrow", code)
        by = str(r.choice(["priyar", "alanb"]))
        alias = c["name"].split()[0].upper()
        party = self.party(ts, c["name"], alias, LANG.get(region, "en-us"), 2978, by)
        loc, pid = self.postal(ts, party, hq_address(self.seed, code, region))
        ents = sorted({c["entity"]} | {entity_of_warehouse(self.ctx, w) for w in c["warehouses"]})
        term = self.ctx.rules["default_payment_term_by_tier"][c["tier"]]
        credit = TIER_CREDIT[c["tier"]] * (1.0 if c["currency"] in ("USD", "EUR", "GBP", "CAD") else 18.0)
        acct = ax_account_num(self.ctx.rules, code)
        rows = {}
        for ent in ents:
            vat = {"CA": f"{int(r.integers(100000000, 999999999))}RT0001", "MX": f"{alias[:3]}{int(r.integers(100000, 999999))}AB1",
                   "EU": f"FR{int(r.integers(10**10, 10**11))}", "GB": f"GB{int(r.integers(10**8, 10**9))}"}.get(region)
            rows[ent] = self.add("custtable", {
                "dataareaid": ent, "accountnum": acct, "party": party,
                "custgroup": "3PL" if c["currency"] == self.ents[ent]["currency"] else "3PL-FX", "currency": c["currency"],
                "paymtermid": term, "paymmode": "WIRE" if region in ("EU", "GB") else str(r.choice(["ACH", "ACH", "CHECK"])),
                "creditmax": float(credit), "invoiceaccount": None, "taxgroup": None, "vatnum": vat, "blocked": 0, "dlvmode": None,
                "inventlocation": c["warehouses"][0], "salesgroup": None, "lineofbusinessid": c["vertical"].upper()[:10],
                "segmentid": c["tier"], "defaultdimension": 0, "onetimecustomer": 0}, ts, by)
        self.st["cust"][code] = {"party": party, "location": loc, "postal": pid, "ents": rows, "credit": float(credit),
                                 "term": term, "blocked": 0, "acct": acct}

    def ev_cust_address(self, ts: datetime, p: dict[str, Any]) -> None:
        cu = self.st["cust"].get(p["code"])
        c = self.clients.get(p["code"])
        if cu is None or c is None:
            return
        self.upd("logisticspostaladdress", cu["postal"], ts, None, validto=naive_utc(ts) - timedelta(seconds=1))
        addr = hq_address(self.seed, p["code"], region_key(c), variant=1)
        cu["postal"] = self.add("logisticspostaladdress", self.address_row(cu["location"], addr, ts), ts)

    def ev_cust_review(self, ts: datetime, p: dict[str, Any]) -> None:
        cu = self.st["cust"].get(p["code"])
        if cu is None:
            return
        r = self.rng("review", p["code"], p["n"])
        cu["credit"] = float(max(5000, round(cu["credit"] * float(r.uniform(0.9, 1.45)) / 5000) * 5000))
        cols: dict[str, Any] = {"creditmax": cu["credit"]}
        if r.random() < 0.15 and cu["term"] in ("N30", "N45"):
            cu["term"] = {"N30": "N45", "N45": "N60"}[cu["term"]]
            cols["paymtermid"] = cu["term"]
        for rid in cu["ents"].values():
            self.upd("custtable", rid, ts, "alanb", **cols)

    def ev_cust_block(self, ts: datetime, p: dict[str, Any]) -> None:
        cu = self.st["cust"].get(p["code"])
        if cu is None or cu["blocked"] == p["blocked"]:
            return
        cu["blocked"] = p["blocked"]
        for rid in cu["ents"].values():
            self.upd("custtable", rid, ts, "alanb", blocked=p["blocked"])

    # ================================================================== vendors
    def vendors_bootstrap(self, ts: datetime) -> None:
        countries = {w["address"]["country"] for w in self.whs.values()}
        n = 0
        for car in self.ctx.rules["carriers"]:
            for country in car.get("countries", []):
                if country not in countries:
                    continue
                ent = {"US": "us01", "CA": "ca01", "MX": "mx01"}[country]
                home = car["countries"][0]
                name = car["name"] if country == home else f"{car['name']} {'Canada' if country == 'CA' else 'México'}"
                city, state, postal, iso = CARRIER_HQ.get(car["scac"], ("Atlanta", "GA", "30301", "US"))
                if country != home:
                    city, state, postal, iso = {"CA": ("Mississauga", "ON", "L5T 2N7", "CA"), "MX": ("Ciudad de México", "CMX", "11510", "MX")}[country]
                self.schedule(ts + timedelta(hours=n), "vend_create", {"key": f"CARRIER:{car['scac']}:{ent}", "ent": ent, "name": name,
                                                                       "group": "CARRIER", "addr": [city, state, postal, iso]})
                n += 1
        for code, w in self.whs.items():
            opened = warehouse_opened(self.ctx, w)
            a = w["address"]
            for group, names in (("LANDLORD", LANDLORD), ("UTILITY", UTILITY)):
                when = ts + timedelta(days=30, hours=n) if opened <= self.hstart + timedelta(days=90) else \
                    at_local(opened - timedelta(days=90), 11, "America/New_York")
                self.schedule(when, "vend_create", {
                    "key": f"{group}:{code}", "ent": w["legal_entity"], "name": names.get(code, f"{group.title()} {code}"),
                    "group": group, "addr": [a["city"], a["state"], a["postal"], a["country"]]})
                n += 1
        for group, ent, name in OTHER_VENDORS:
            if ent in {w["legal_entity"] for w in self.whs.values()}:
                self.schedule(ts + timedelta(days=5, hours=n), "vend_create", {"key": f"{group}:{ent}:{name}", "ent": ent, "name": name,
                                                                               "group": group, "addr": ["Atlanta", "GA", "30336", "US"]})
                n += 1

    def ev_vend_create(self, ts: datetime, p: dict[str, Any]) -> None:
        if p["key"] in self.st["vend"]:
            return
        r = self.rng("vend", p["key"])
        party = self.party(ts, p["name"], p["name"].split()[0].upper(), "en-us", 2978, "priyar")
        city, state, postal, iso = p["addr"]
        addr = {"street": f"{int(r.integers(100, 9999))} {r.choice(['Corporate Dr', 'Commerce Pkwy', 'Industrial Ave', 'Main St'])!s}",
                "city": city, "state": state, "postal": postal, "country": iso, "lat": None, "lon": None}
        self.postal(ts, party, addr, "Remit-to")
        acct = f"V{self.seq('vendnum', 20010):05d}"
        ccy = self.ents[p["ent"]]["currency"]
        rid = self.add("vendtable", {
            "dataareaid": p["ent"], "accountnum": acct, "party": party, "vendgroup": p["group"], "currency": ccy,
            "paymtermid": "N30", "paymmode": "ACH", "cashdisc": None, "blocked": 0, "invoiceaccount": None, "taxgroup": None,
            "vatnum": None, "tax1099reports": int(p["group"] in ("LANDLORD", "IT")), "onetimevendor": 0, "defaultdimension": 0},
            ts, "priyar")
        self.st["vend"][p["key"]] = {"recid": rid, "acct": acct, "ent": p["ent"], "name": p["name"], "group": p["group"], "ccy": ccy}

    def vendor(self, key: str) -> dict[str, Any] | None:
        return self.st["vend"].get(key)

    # ================================================================== workers
    def workers_bootstrap(self) -> None:
        start = as_utc(self.ctx.history_start)
        people = [p for p in self.ctx.rules["people"] if p["key"] != "integration.user"]
        for i, p in enumerate(people):
            first, last = splitname(p["name"])
            ent = {"chloe.martin": "ca01", "natalie.gagnon": "ca01", "diego.salinas": "mx01", "rodrigo.trevino": "mx01"}.get(
                p["key"], "us01")
            hire = date(2014 + (i * 7) % 9, 1 + (i * 5) % 12, 1 + (i * 3) % 27)
            self.hire(at_local(min(hire, self.hstart), 9, "America/New_York"), f"{900001 + i}", first, last, ent, hire, None)
        try:
            rows = self.ctx.conn("wms").execute(
                "SELECT u.personnel_nbr, u.first_name, u.last_name, w.warehouse_code, u.hire_date, u.term_date "
                "FROM wms.app_user u LEFT JOIN wms.warehouse w ON w.warehouse_id = u.warehouse_id "
                "WHERE u.personnel_nbr IS NOT NULL ORDER BY u.user_id").fetchall()
        except Exception:  # noqa: BLE001 - the WMS may not have bootstrapped users yet
            rows = []
        for nbr, first, last, wh, hire, term in rows:
            hire_d = to_date(hire) if hire else self.hstart - timedelta(days=365)
            ent = self.whs.get(wh, {}).get("legal_entity", "us01") if wh else "us01"
            when = at_local(min(hire_d, self.hstart), 9, "America/New_York")
            if when > start:
                when = start - timedelta(hours=1)
            self.hire(when, str(nbr), first or "", last or "", ent, hire_d, to_date(term) if term else None)

    def hire(self, ts: datetime, nbr: str, first: str, last: str, ent: str, hire: date, term: date | None) -> None:
        if not nbr or nbr in self.st["workers"]:
            return
        person = self.party(ts, f"{first} {last}".strip(), (last or first).upper(), "en-us", 2975, "priyar")
        self.add("dirpersonname", {"person": person, "firstname": (first or "")[:25], "middlename": None, "lastnameprefix": None,
                                   "lastname": (last or "")[:25], "validfrom": naive_utc(ts), "validto": MAXDT}, ts)
        worker = self.add("hcmworker", {"person": person, "personnelnumber": nbr}, ts)
        validto = datetime.combine(term, datetime.max.time()).replace(microsecond=0) if term else MAXDT
        emp = self.add("hcmemployment", {"worker": worker, "legalentity": self.st["dataarea"].get(ent, 0), "employmenttype": 0,
                                         "validfrom": datetime.combine(hire, datetime.min.time()), "validto": validto,
                                         "defaultdimension": 0}, ts)
        self.st["workers"][nbr] = {"worker": worker, "emp": emp, "person": person, "term": term.isoformat() if term else None}

    def ingest_workers(self, win: Window) -> None:
        hired = self.ctx.bus.get("workers_hired")
        for row in hired.itertuples(index=False) if len(hired) else []:
            ent = getattr(row, "legal_entity", None) or self.whs.get(row.warehouse_code, {}).get("legal_entity", "us01")
            ts = max(win.start, at_local(to_date(row.hire_date), 9, ENT_TZ.get(ent, "America/New_York")))
            ts = min(ts, win.end - timedelta(seconds=1))
            self.hire(ts, str(row.personnel_nbr), row.first_name or "", row.last_name or "", ent, to_date(row.hire_date), None)
        term = self.ctx.bus.get("workers_terminated")
        for row in term.itertuples(index=False) if len(term) else []:
            w = self.st["workers"].get(str(row.personnel_nbr))
            if w is None or w.get("term"):
                continue
            d = to_date(row.term_date)
            w["term"] = d.isoformat()
            ts = min(max(win.start, at_local(d, 17, "America/New_York")), win.end - timedelta(seconds=1))
            self.upd("hcmemployment", w["emp"], ts, None, validto=datetime.combine(d, datetime.max.time()).replace(microsecond=0))

    # ================================================================== bus ingestion
    def ingest(self, win: Window) -> None:
        bus = self.ctx.bus
        orders = bus.get("orders_created")
        if len(orders):
            orders = orders.drop_duplicates("order_id")
            self.st["rcv"].update(zip(orders["order_id"].astype("int64").tolist(), self.ship_by_days(orders).tolist(), strict=True))
        ships = bus.get("shipments_shipped")
        if len(ships):
            ships = ships.drop_duplicates("shipment_id")
            keep = [c for c in ("shipment_nbr", "order_id", "order_nbr", "owner_code", "warehouse_code", "scac", "service_code",
                                "mode", "tracking_nbr", "bol_nbr", "ship_ts", "ship_ts_local", "billed_weight_lb", "total_weight",
                                "weight_uom", "zone", "is_residential", "dest_postal", "dest_country", "carton_count",
                                "freight_charge_est", "currency") if c in ships]
            self.st["ship_buf"].append(ships[keep].copy())
            sts = pd.to_datetime(ships["ship_ts"], utc=True)
            local = pd.to_datetime(ships["ship_ts_local"]) if "ship_ts_local" in ships else sts.dt.tz_localize(None)
            ship_day = (local.dt.normalize() - pd.Timestamp("1970-01-01")) // pd.Timedelta(days=1)
            rcv = self.st["rcv"]
            due = np.array([rcv.pop(i, np.nan) for i in ships["order_id"].astype("int64").tolist()], dtype=float)
            codes = ships["owner_code"].astype(str)
            ok = ~np.isnan(due) & codes.isin(list(self.clients)).to_numpy()
            if ok.any():
                frame = pd.DataFrame({"code": codes.to_numpy()[ok], "ym": sts.dt.strftime("%Y-%m").to_numpy()[ok],
                                      "late": (ship_day.to_numpy(dtype=float) > due)[ok]})
                for (code, ym), grp in frame.groupby(["code", "ym"])["late"]:
                    cnt = self.st["sla"].setdefault(f"{code}|{ym}", [0, 0])
                    cnt[0] += len(grp)
                    cnt[1] += int(grp.sum())
            for wh, n in ships["warehouse_code"].value_counts().items():
                self.st["shipcnt"][wh] = self.st["shipcnt"].get(wh, 0) + int(n)
        labor = bus.get("labor_hours")
        if len(labor):
            labor = labor.assign(ym=pd.to_datetime(labor["work_date"]).dt.strftime("%Y-%m"))
            for (wh, ym), h in labor.groupby(["warehouse_code", "ym"])["hours"].sum().items():
                k = f"{wh}|{ym}"
                self.st["labor"][k] = self.st["labor"].get(k, 0.0) + float(h)
        self.ingest_workers(win)

    def ship_by_days(self, orders: pd.DataFrame) -> np.ndarray:
        """Contract ship-by day (days since epoch, warehouse-local) per order: the ready day (next day when received after the
        site's cutoff, weekends rolled to Monday; B2B not before the requested ship date) plus the SLA allowance of one
        business day (24 h SLA) or two (48 h SLA)."""
        whs = self.ctx.rules["warehouses"]
        cut_p = {w["code"]: _hours(w["cutoffs"]["parcel"]) for w in whs}
        cut_l = {w["code"]: _hours(w["cutoffs"]["ltl"]) for w in whs}
        local = pd.to_datetime(orders["received_ts_local"]) if "received_ts_local" in orders else \
            pd.to_datetime(orders["received_ts"], utc=True).dt.tz_localize(None)
        b2b = (orders.get("order_type", pd.Series("B2C", index=orders.index)).astype(str) == "B2B").to_numpy()
        wh = orders["warehouse_code"].astype(str)
        cut = np.where(b2b, wh.map(cut_l).fillna(13.0), wh.map(cut_p).fillna(15.0))
        hour = (local.dt.hour + local.dt.minute / 60).to_numpy()
        ready = local.dt.normalize().to_numpy().astype("datetime64[D]") + (hour > cut).astype(int)
        if "requested_ship_date" in orders:
            rsd = pd.to_datetime(orders["requested_ship_date"], errors="coerce").to_numpy().astype("datetime64[D]")
            later = b2b & ~np.isnat(rsd) & (rsd > ready)
            ready = np.where(later, rsd, ready)
        ready = np.busday_offset(ready, 0, roll="forward")
        allowance = orders["owner_code"].astype(str).map(
            {c: (1 if self.sla_cached(c)[0] <= 24 else 2) for c in orders["owner_code"].astype(str).unique() if c in self.clients}
        ).fillna(1).astype(int).to_numpy()
        return np.busday_offset(ready, allowance).astype("int64")

    def sla_cached(self, code: str) -> tuple[float, float]:
        key = f"{self.seed}:{code}"
        v = _SLA_CACHE.get(key)
        if v is None:
            v = sla_terms(self.seed, self.ctx.rules, self.clients[code])
            _SLA_CACHE[key] = v
        return v

    # ================================================================== month-end close
    def close_month(self, ts: datetime) -> None:
        cutoff = ts.date().replace(day=1)
        p_start = add_months(cutoff, -1)
        key = p_start.isoformat()
        if self.st["closed"].get(key):
            return
        self.st["closed"][key] = True
        self.key_month(cutoff.year, cutoff.month, ts)
        self.invoice_period(ts + timedelta(minutes=20), p_start, cutoff)
        self.payroll(ts + timedelta(hours=2), p_start, cutoff - timedelta(days=1))
        self.monthly_journals(ts + timedelta(hours=3), cutoff - timedelta(days=1))
        self.rent(ts + timedelta(hours=4), cutoff)
        self.credit_holds(ts + timedelta(hours=5), cutoff)
        old = (ts.date() - date(1970, 1, 1)).days - 60
        rcv = self.st["rcv"]
        for k in [k for k, v in rcv.items() if v < old]:
            del rcv[k]
        for k in [k for k in self.st["sla"] if k.split("|")[1] < add_months(p_start, -3).strftime("%Y-%m")]:
            del self.st["sla"][k]

    def billing_aggregate(self, cutoff: date) -> pd.DataFrame:
        rows = self.ctx.conn("wms").execute(
            "SELECT o.owner_code, br.owner_id, br.warehouse_id, w.warehouse_code, w.time_zone, br.charge_code, br.rate, "
            "sum(br.qty), sum(br.amount), count(*) FROM wms.billing_record br "
            "JOIN wms.owner o ON o.owner_id = br.owner_id JOIN wms.warehouse w ON w.warehouse_id = br.warehouse_id "
            "WHERE br.billing_status = 'ACCRUED' AND br.activity_date < %s GROUP BY 1, 2, 3, 4, 5, 6, 7 ORDER BY 1, 4, 6, 7",
            (cutoff,)).fetchall()
        return pd.DataFrame(rows, columns=["owner_code", "owner_id", "warehouse_id", "warehouse_code", "tz", "charge_code", "rate",
                                           "qty", "amount", "n"])

    def invoice_period(self, ts: datetime, p_start: date, cutoff: date) -> None:
        p_end = cutoff - timedelta(days=1)
        if p_end < self.hstart:
            return
        agg = self.billing_aggregate(cutoff)
        cc = {c["code"]: c for c in self.ctx.rules["charge_codes"]}
        if len(agg):
            agg["code"] = agg["owner_code"].astype(str).str.strip().str.upper()
            agg["ent"] = agg["warehouse_code"].map(lambda w: entity_of_warehouse(self.ctx, w))
        invoiced: list[tuple[int, int, str, str]] = []
        groups = {k: g for k, g in agg.groupby(["code", "ent"])} if len(agg) else {}
        for code, c in sorted(self.clients.items()):
            d = dates_of(self.ctx, c)
            churn = churn_of(self.ctx, c)
            active = d.go_live <= p_end and (churn is None or churn >= p_start)
            for ent in sorted({c["entity"]} | {entity_of_warehouse(self.ctx, w) for w in c["warehouses"]}):
                if (code, ent) not in groups and active and ent == c["entity"]:
                    groups[(code, ent)] = agg.iloc[0:0] if len(agg) else pd.DataFrame(columns=["charge_code"])
        for (code, ent), g in sorted(groups.items(), key=lambda kv: kv[0]):
            c = self.clients.get(code)
            if c is None or code not in self.st["cust"]:
                continue
            inv = self.customer_invoice(ts, code, ent, c, g, p_start, p_end, cc)
            if inv:
                for row in g.itertuples(index=False):
                    invoiced.append((int(row.owner_id), int(row.warehouse_id), inv, row.tz))
                ts += timedelta(seconds=37)
        if invoiced:
            df = pd.DataFrame(sorted(set(invoiced)), columns=["owner_id", "warehouse_id", "invoice_ref", "tz"])
            df["mod_ts"] = [to_local_naive(ts, tz) for tz in df["tz"]]
            self.mark_invoiced(df, cutoff)

    def mark_invoiced(self, df: pd.DataFrame, cutoff: date) -> int:
        conn = self.ctx.conn("wms")
        conn.execute("CREATE TEMP TABLE _fc_inv (owner_id integer, warehouse_id smallint, invoice_ref varchar(20), mod_ts timestamp) "
                     "ON COMMIT DROP")
        copy_df(self.ctx, "wms", "_fc_inv", df[["owner_id", "warehouse_id", "invoice_ref", "mod_ts"]])
        n = conn.execute(
            "UPDATE wms.billing_record t SET billing_status = 'INVOICED', invoice_ref = u.invoice_ref, mod_ts = u.mod_ts "
            "FROM _fc_inv u WHERE t.owner_id = u.owner_id AND t.warehouse_id = u.warehouse_id "
            "AND t.billing_status = 'ACCRUED' AND t.activity_date < %s", (cutoff,)).rowcount
        conn.execute("DROP TABLE _fc_inv")
        return int(n or 0)

    def customer_invoice(self, ts: datetime, code: str, ent: str, c: dict[str, Any], g: pd.DataFrame, p_start: date,
                         p_end: date, cc: dict[str, Any]) -> str | None:
        r = self.rng("inv", code, ent, p_start.isoformat())
        cu = self.st["cust"][code]
        ccy = c["currency"]
        lines: list[dict[str, Any]] = []
        for row in g.itertuples(index=False):
            info = cc.get(row.charge_code, {"name": row.charge_code, "uom": "EA", "gl": "4020"})
            lines.append({"itemid": row.charge_code, "name": f"{info['name']} - {row.warehouse_code} - {p_start:%b %Y}",
                          "qty": float(row.qty), "price": float(row.rate), "amount": round(float(row.amount), 2),
                          "uom": AX_UNIT.get(info["uom"], "EA"), "disp": f"{info['gl']}-{row.warehouse_code}-OPS"})
        fees = round(sum(x["amount"] for x in lines if x["itemid"] != "FRT-RCV"), 2)
        versions = rate_versions(self.ctx, c)
        v = rate_on_date(versions, p_end) or rate_on_date(versions, p_start)
        if v and code != "PUREFRM" and ent == c["entity"]:
            d = dates_of(self.ctx, c)
            churn = churn_of(self.ctx, c)
            first = max(p_start, d.go_live, self.hstart)
            last = min(p_end, churn) if churn else p_end
            days = (last - first).days + 1
            if days > 0:
                minimum = round(v.monthly_minimum * days / ((p_end - p_start).days + 1), 2)
                if fees < minimum:
                    wh = c["warehouses"][0]
                    lines.append({"itemid": "MIN-ADJ", "name": f"Monthly minimum adjustment - {p_start:%b %Y} (minimum {minimum:,.2f})",
                                  "qty": 1.0, "price": round(minimum - fees, 2), "amount": round(minimum - fees, 2), "uom": "MO",
                                  "disp": f"4020-{wh}-OPS"})
        if not lines:
            return None
        inv = f"{EP[ent]}-INV{self.seq('inv:' + ent, 1001):07d}"
        voucher = self.voucher(ent)
        rate = self.exch(ccy, ent, p_end)
        total = round(sum(x["amount"] for x in lines), 2)
        header_amount = total
        if self.ctx.dq.hit("header_line_mismatch", r):
            header_amount = round(total + float(r.choice([-1, 1])) * float(r.uniform(0.01, 25)), 2)
            self.dqlog("header_line_mismatch", "custinvoicejour", inv, "invoiceamount", total, header_amount, ts)
        term_days = next((t["days"] for t in self.ctx.rules["payment_terms"] if t["id"] == cu["term"]), 30)
        due = p_end + timedelta(days=term_days)
        cur_code = ccy
        if self.ctx.dq.hit("invalid_currency", r):
            cur_code = str(r.choice(["US$", "usd", "EURO", "CDN", "XXX"]))
            self.dqlog("invalid_currency", "custinvoicejour", inv, "currencycode", ccy, cur_code, ts)
        due_hdr = due
        if self.ctx.dq.hit("sentinel_date", r):
            due_hdr = EMPTY
            self.dqlog("sentinel_date", "custinvoicejour", inv, "duedate", due, EMPTY, ts)
        mst = round(header_amount * rate / 100, 2)
        self.add("custinvoicejour", {
            "dataareaid": ent, "invoiceid": inv, "invoicedate": p_end, "invoiceaccount": cu["acct"], "orderaccount": cu["acct"],
            "salesid": None, "numbersequencegroup": None, "currencycode": cur_code, "exchrate": rate, "invoiceamount": header_amount,
            "invoiceamountmst": mst, "salesbalance": header_amount, "sumtax": 0, "sumlinedisc": 0, "summarkup": 0, "duedate": due_hdr,
            "ledgervoucher": voucher, "payment": cu["term"], "dlvmode": None, "qty": round(sum(x["qty"] for x in lines), 2),
            "weight": 0, "custgroup": "3PL" if ccy == self.ents[ent]["currency"] else "3PL-FX", "defaultdimension": 0},
            ts, "svc_wmsbill")
        null_mask = self.ctx.dq.mask("null_required", len(lines), r)
        rids = self.recids("custinvoicetrans", len(lines))
        stamp = naive_utc(ts)
        for i, (x, rid) in enumerate(zip(lines, rids, strict=True)):
            item = x["itemid"]
            if null_mask[i]:
                self.dqlog("null_required", "custinvoicetrans", f"{inv}:{i + 1}", "itemid", item, None, ts)
                item = None
            self.rows["custinvoicetrans"].append({
                "recid": int(rid), "dataareaid": ent, "invoiceid": inv, "invoicedate": p_end, "salesid": None,
                "numbersequencegroup": None, "linenum": float(i + 1), "itemid": item, "name": x["name"], "qty": x["qty"],
                "salesunit": x["uom"], "salesprice": x["price"], "priceunit": 1, "lineamount": x["amount"],
                "lineamountmst": round(x["amount"] * rate / 100, 2), "taxamount": 0, "discamount": 0, "currencycode": ccy,
                "inventdimid": None, "inventtransid": None, "dlvdate": p_end, "ledgerdimension": self.dim(x["disp"], ts),
                "defaultdimension": 0, "modifieddatetime": stamp, "createddatetime": stamp})
        acctnum = cu["acct"]
        if self.ctx.dq.hit("key_whitespace_case", r):
            acctnum = acctnum + " "
            self.dqlog("key_whitespace_case", "custtrans", inv, "accountnum", cu["acct"], acctnum, ts)
        trec = self.add("custtrans", self.custtrans_row(ent, acctnum, voucher, p_end, 8, inv, f"Invoice {inv}", header_amount, mst,
                                                         ccy, rate, due, cu["acct"]), ts, "svc_wmsbill")
        gl = [("1200", 31, mst, f"Invoice {inv}", header_amount, ccy)]
        for x in lines:
            gl.append((x["disp"], 32, -round(x["amount"] * rate / 100, 2), x["name"], -x["amount"], ccy))
        gl = gl[1:] + gl[:1]
        self.post_gl(ts, ent, p_end, inv, 8, voucher, gl, "svc_wmsbill")
        self.st["open_ar"][trec] = {"ent": ent, "acct": cu["acct"], "code": code, "inv": inv, "voucher": voucher, "amt": header_amount,
                                    "settled": 0.0, "settled_mst": 0.0, "rate": rate, "ccy": ccy, "due": due.isoformat(),
                                    "date": p_end.isoformat()}
        self.sla_credit(ts + timedelta(minutes=5), code, ent, c, trec, lines, p_start, p_end)
        self.plan_payment(ts, trec)
        return inv

    def custtrans_row(self, ent: str, acct: str, voucher: str, d: date, ttype: int, inv: str | None, txt: str, amount: float,
                      mst: float, ccy: str, rate: float, due: date, orderacct: str, settled: bool = False,
                      paymref: str | None = None, paymmode: str | None = None) -> dict[str, Any]:
        return {"dataareaid": ent, "accountnum": acct, "voucher": voucher, "transdate": d, "transtype": ttype, "invoice": inv,
                "txt": txt[:60], "amountcur": round(amount, 2), "amountmst": round(mst, 2),
                "settleamountcur": round(amount, 2) if settled else 0, "settleamountmst": round(mst, 2) if settled else 0,
                "currencycode": ccy, "exchrate": rate, "duedate": due, "closed": d if settled else EMPTY,
                "lastsettlevoucher": voucher if settled else None, "lastsettledate": d if settled else EMPTY,
                "paymmode": paymmode, "paymreference": paymref, "postingprofile": "GEN", "documentnum": inv, "documentdate": d,
                "orderaccount": orderacct, "approved": 1, "correct": 0, "prepayment": 0, "offsetrecid": 0, "defaultdimension": 0,
                "reportingcurrencyamount": self.to_usd(mst, ent, d)}

    def sla_credit(self, ts: datetime, code: str, ent: str, c: dict[str, Any], inv_rec: int, lines: list[dict[str, Any]],
                   p_start: date, p_end: date) -> None:
        n, late = self.st["sla"].get(f"{code}|{p_start:%Y-%m}", [0, 0])
        if n < 30 or ent != c["entity"]:
            return
        _, target = self.sla_cached(code)
        actual = 100 * (1 - late / n)
        threshold = target - 8.0
        if actual >= threshold:
            return
        base = sum(x["amount"] for x in lines if x["itemid"] and x["itemid"].startswith(("ORD-", "PICK-")))
        pct = min(5.0, max(1.0, threshold - actual))
        credit = round(base * pct / 100, 2)
        if credit < 1:
            return
        text = f"SLA credit - {p_start:%b %Y}: on-time {actual:.1f}% vs {target:.1f}% target"
        self.credit_note(ts, code, ent, credit, text, f"4900-{c['warehouses'][0]}-CS", inv_rec)

    def credit_note(self, ts: datetime, code: str, ent: str, amount: float, text: str, disp: str, settle_with: int | None) -> int:
        cu = self.st["cust"][code]
        c = self.clients[code]
        ccy = c["currency"]
        inv_info = self.st["open_ar"].get(settle_with) if settle_with else None
        d = ts.date()
        rate = inv_info["rate"] if inv_info else self.exch(ccy, ent, d)
        inv = f"{EP[ent]}-INV{self.seq('inv:' + ent, 1001):07d}"
        voucher = self.voucher(ent)
        mst = round(amount * rate / 100, 2)
        self.add("custinvoicejour", {
            "dataareaid": ent, "invoiceid": inv, "invoicedate": d, "invoiceaccount": cu["acct"], "orderaccount": cu["acct"],
            "salesid": None, "numbersequencegroup": None, "currencycode": ccy, "exchrate": rate, "invoiceamount": -amount,
            "invoiceamountmst": -mst, "salesbalance": -amount, "sumtax": 0, "sumlinedisc": 0, "summarkup": 0, "duedate": d,
            "ledgervoucher": voucher, "payment": cu["term"], "dlvmode": None, "qty": -1, "weight": 0,
            "custgroup": "3PL" if ccy == self.ents[ent]["currency"] else "3PL-FX", "defaultdimension": 0}, ts, "alanb")
        self.add("custinvoicetrans", {
            "dataareaid": ent, "invoiceid": inv, "invoicedate": d, "salesid": None, "numbersequencegroup": None, "linenum": 1.0,
            "itemid": None, "name": text, "qty": -1.0, "salesunit": "EA", "salesprice": amount, "priceunit": 1, "lineamount": -amount,
            "lineamountmst": -mst, "taxamount": 0, "discamount": 0, "currencycode": ccy, "inventdimid": None, "inventtransid": None,
            "dlvdate": d, "ledgerdimension": self.dim(disp, ts), "defaultdimension": 0}, ts)
        trec = self.add("custtrans", self.custtrans_row(ent, cu["acct"], voucher, d, 8, inv, f"Credit note {inv}", -amount, -mst, ccy,
                                                         rate, d, cu["acct"], settled=inv_info is not None), ts, "alanb")
        self.post_gl(ts, ent, d, inv, 8, voucher, [(disp, 32, mst, text, amount, ccy), ("1200", 31, -mst, text, -amount, ccy)], "alanb")
        if inv_info:
            self.settle(ts, settle_with, trec, amount, rate, voucher, cn_mst=mst)
        return trec

    def settle(self, ts: datetime, inv_rec: int, off_rec: int, amount: float, off_rate: float, off_voucher: str,
               cn_mst: float | None = None) -> list[int]:
        """Settle ``amount`` of an open invoice against an offset transaction (payment, credit note, write-off)."""
        o = self.st["open_ar"][inv_rec]
        d = ts.date()
        mst_inv = round(amount * o["rate"] / 100, 2)
        mst_off = cn_mst if cn_mst is not None else round(amount * off_rate / 100, 2)
        exadj = round(mst_off - mst_inv, 2)
        group = int(off_rec)
        due = date.fromisoformat(o["due"])
        rows = []
        for trans, offset, cur, m, adj, voucher in ((inv_rec, off_rec, amount, mst_inv, exadj, off_voucher),
                                                    (off_rec, inv_rec, -amount, -mst_off, 0.0, o["voucher"])):
            rows.append(self.add("custsettlement", {
                "dataareaid": o["ent"], "transrecid": int(trans), "transcompany": o["ent"], "transdate": d, "accountnum": o["acct"],
                "offsetrecid": int(offset), "offsettransvoucher": voucher, "offsetaccountnum": o["acct"], "offsetcompany": o["ent"],
                "settleamountcur": round(cur, 2), "settleamountmst": round(m, 2), "exchadjustment": adj, "utilizedcashdisc": 0,
                "duedate": due, "settlementgroup": group, "canbereversed": 1}, ts))
        o["settled"] = round(o["settled"] + amount, 2)
        o["settled_mst"] = round(o["settled_mst"] + mst_inv, 2)
        closed = o["amt"] - o["settled"] <= 0.005
        self.upd("custtrans", inv_rec, ts, "svc_lockbox", settleamountcur=o["settled"], settleamountmst=o["settled_mst"],
                 closed=d if closed else EMPTY, lastsettlevoucher=off_voucher, lastsettledate=d)
        if closed:
            o["closed"] = d.isoformat()
        return rows

    # ================================================================== payments
    def plan_payment(self, ts: datetime, trec: int) -> None:
        o = self.st["open_ar"][trec]
        code = o["code"]
        b = payment_behaviour(self.ctx, code)
        r = self.rng("pay", o["inv"])
        inv_d, due = date.fromisoformat(o["date"]), date.fromisoformat(o["due"])
        base = (due - inv_d).days
        if code == "OAKLINE":
            start = month_start(self.hend, -9)
            f = min(1.0, max(0.0, (inv_d - start).days / max(1, (self.hend - start).days)))
            delay = round(48 + 47 * f + float(r.normal(0, 4)))
        elif b == "early":
            delay = max(4, base - int(r.integers(5, 16)))
        elif b == "slow":
            delay = base + int(r.integers(10, 26))
        elif b == "very_slow":
            delay = base + int(r.integers(30, 76))
        else:
            delay = max(5, base + round(float(r.normal(1, 3))))
        when = max(biz_ts(inv_d + timedelta(days=delay), r, ENT_TZ[o["ent"]], 9, 16), ts + timedelta(days=1))
        while not is_business_day(when.date()):
            when += timedelta(days=1)
        c = self.clients.get(code)
        churn = churn_of(self.ctx, c) if c else None
        if churn and b == "very_slow" and inv_d >= churn - timedelta(days=45) and r.random() < 0.35:
            self.schedule(biz_ts(due + timedelta(days=120), r), "writeoff", {"inv": int(trec)})
            return
        share = dispute_share(self.seed, o["inv"], b)
        self.schedule(when, "pay", {"inv": int(trec), "share": share})
        if share > 0:
            self.schedule(when + timedelta(days=float(r.uniform(20, 50))), "dispute_resolve", {"inv": int(trec)})

    def ev_pay(self, ts: datetime, p: dict[str, Any]) -> None:
        self.pay_batch.append((ts, p))

    def flush_payments(self) -> None:
        if not self.pay_batch:
            return
        groups: dict[tuple[str, str, str], list[tuple[datetime, dict[str, Any]]]] = defaultdict(list)
        for ts, p in self.pay_batch:
            o = self.st["open_ar"].get(p["inv"])
            if o is None or o.get("closed"):
                continue
            groups[(o["ent"], o["code"], ts.date().isoformat())].append((ts, p))
        self.pay_batch = []
        for (ent, code, _), items in sorted(groups.items()):
            ts = min(t for t, _ in items)
            alloc = []
            for _, p in items:
                o = self.st["open_ar"][p["inv"]]
                open_amt = round(o["amt"] - o["settled"], 2)
                amt = round(open_amt - (o["amt"] * p.get("share", 0.0) if p.get("share") else 0.0), 2)
                if amt > 0.004:
                    alloc.append((p["inv"], amt))
            if alloc:
                self.receive_payment(ts, ent, code, alloc)

    def receive_payment(self, ts: datetime, ent: str, code: str, alloc: list[tuple[int, float]], txt: str | None = None) -> int:
        cu = self.st["cust"][code]
        ccy = self.clients[code]["currency"]
        d = ts.date()
        r = self.rng("payrow", code, ts.isoformat())
        rate = self.exch(ccy, ent, d)
        total = round(sum(a for _, a in alloc), 2)
        voucher = self.voucher(ent)
        mode = "WIRE" if ccy in ("EUR", "GBP") else str(r.choice(["ACH", "ACH", "CHECK"]))
        ref = f"{int(r.integers(10**7, 10**8))}" if mode == "CHECK" else f"{mode}{d:%y%m%d}{int(r.integers(1000, 9999))}"
        mst = round(total * rate / 100, 2)
        prec = self.add("custtrans", self.custtrans_row(ent, cu["acct"], voucher, d, 15, None, txt or f"Customer payment - {mode} {ref}",
                                                         -total, -mst, ccy, rate, d, cu["acct"], settled=True, paymref=ref,
                                                         paymmode=mode), ts, "svc_lockbox")
        gl: list[tuple[str, int, float, str, float, str]] = [("1100", 20, mst, f"Payment {ref}", total, ccy)]
        ar_mst, settle_rows = 0.0, []
        for inv_rec, amt in alloc:
            o = self.st["open_ar"][inv_rec]
            ar_mst += round(amt * o["rate"] / 100, 2)
            settle_rows += self.settle(ts, inv_rec, prec, amt, rate, voucher)
        fx = round(mst - ar_mst, 2)
        if fx:
            gl.append(("8000-CORP-FIN", 40, -fx, "Realized exchange rate difference", 0.0, self.ents[ent]["currency"]))
        gl.append(("1200", 38, -ar_mst, f"Payment {ref}", -total, ccy))
        self.post_gl(ts, ent, d, ref, 15, voucher, gl, "svc_lockbox")
        self.st["pays"][str(prec)] = {"ent": ent, "code": code, "voucher": voucher, "total": total, "mst": mst, "rate": rate,
                                      "alloc": [[int(i), a] for i, a in alloc], "settle": [int(x) for x in settle_rows],
                                      "date": d.isoformat()}
        for i in alloc:
            o = self.st["open_ar"][i[0]]
            if o.get("closed"):
                self.st["open_ar"].pop(i[0])
        if r.random() < 0.004:
            self.schedule(ts + timedelta(days=float(r.uniform(3, 7))), "reverse", {"pay": int(prec)})
        cutoff = (d - timedelta(days=12)).isoformat()
        for k in [k for k, v in self.st["pays"].items() if v["date"] < cutoff]:
            del self.st["pays"][k]
        return prec

    def ev_dispute_resolve(self, ts: datetime, p: dict[str, Any]) -> None:
        o = self.st["open_ar"].get(p["inv"])
        if o is None or o.get("closed"):
            return
        remaining = round(o["amt"] - o["settled"], 2)
        if remaining <= 0.004:
            return
        r = self.rng("dispute", o["inv"])
        if r.random() < 0.6:
            c = self.clients[o["code"]]
            self.credit_note(ts, o["code"], o["ent"], remaining, f"Billing adjustment - disputed charges on {o['inv']}",
                             f"4900-{c['warehouses'][0]}-CS", p["inv"])
            if o.get("closed"):
                self.st["open_ar"].pop(p["inv"], None)
        else:
            self.receive_payment(ts, o["ent"], o["code"], [(p["inv"], remaining)], txt=f"Payment - dispute resolved {o['inv']}")

    def ev_writeoff(self, ts: datetime, p: dict[str, Any]) -> None:
        o = self.st["open_ar"].get(p["inv"])
        if o is None or o.get("closed"):
            return
        remaining = round(o["amt"] - o["settled"], 2)
        if remaining <= 0.004:
            return
        voucher = self.voucher(o["ent"])
        d = ts.date()
        mst = round(remaining * o["rate"] / 100, 2)
        rec = self.add("custtrans", self.custtrans_row(o["ent"], o["acct"], voucher, d, 35, None, f"Write-off {o['inv']}", -remaining,
                                                        -mst, o["ccy"], o["rate"], d, o["acct"], settled=True), ts, "alanb")
        self.settle(ts, p["inv"], rec, remaining, o["rate"], voucher, cn_mst=mst)
        self.post_gl(ts, o["ent"], d, o["inv"], 35, voucher, [("4900-CORP-FIN", 14, mst, f"Bad debt write-off {o['inv']}", remaining,
                                                                o["ccy"]), ("1200", 31, -mst, "Write-off", -remaining, o["ccy"])], "alanb")
        self.st["open_ar"].pop(p["inv"], None)

    def ev_reverse(self, ts: datetime, p: dict[str, Any]) -> None:
        """Returned payment (NSF / ACH return): settlements are DELETED, the invoices reopen, a reversal is booked."""
        pay = self.st["pays"].pop(str(p["pay"]), None)
        if pay is None:
            return
        for rid in pay["settle"]:
            self.delete("custsettlement", rid)
        d = ts.date()
        ent, code = pay["ent"], pay["code"]
        cu = self.st["cust"][code]
        ccy = self.clients[code]["currency"]
        voucher = self.voucher(ent)
        rec = self.add("custtrans", self.custtrans_row(ent, cu["acct"], voucher, d, 15, None, "Payment reversal - returned item",
                                                        pay["total"], pay["mst"], ccy, pay["rate"], d, cu["acct"], settled=True),
                       ts, "alanb")
        for trans, offset, cur, m in ((int(p["pay"]), rec, -pay["total"], -pay["mst"]), (rec, int(p["pay"]), pay["total"], pay["mst"])):
            self.add("custsettlement", {
                "dataareaid": ent, "transrecid": trans, "transcompany": ent, "transdate": d, "accountnum": cu["acct"],
                "offsetrecid": offset, "offsettransvoucher": voucher, "offsetaccountnum": cu["acct"], "offsetcompany": ent,
                "settleamountcur": cur, "settleamountmst": m, "exchadjustment": 0, "utilizedcashdisc": 0, "duedate": d,
                "settlementgroup": rec, "canbereversed": 0}, ts)
        self.upd("custtrans", int(p["pay"]), ts, "alanb", lastsettlevoucher=voucher, lastsettledate=d)
        ar_mst = 0.0
        for inv_rec, amt in pay["alloc"]:
            o = self.st["open_ar"].get(inv_rec)
            if o is None:
                o = self.reopen(inv_rec)
                if o is None:
                    continue
            mst_inv = round(amt * o["rate"] / 100, 2)
            ar_mst += mst_inv
            o["settled"] = round(o["settled"] - amt, 2)
            o["settled_mst"] = round(o["settled_mst"] - mst_inv, 2)
            o.pop("closed", None)
            self.st["open_ar"][inv_rec] = o
            self.upd("custtrans", inv_rec, ts, "alanb", settleamountcur=o["settled"], settleamountmst=o["settled_mst"], closed=EMPTY)
            r = self.rng("repay", inv_rec, d.isoformat())
            self.schedule(ts + timedelta(days=float(r.uniform(7, 20))), "pay", {"inv": int(inv_rec), "share": 0.0})
        gl = [("1200", 31, ar_mst, "Returned payment", pay["total"], ccy)]
        fx = round(pay["mst"] - ar_mst, 2)
        if fx:
            gl.append(("8000-CORP-FIN", 40, fx, "Exchange difference on returned payment", 0.0, self.ents[ent]["currency"]))
        gl.append(("1100", 20, -pay["mst"], "Returned payment", -pay["total"], ccy))
        self.post_gl(ts, ent, d, voucher, 15, voucher, gl, "alanb")

    def reopen(self, inv_rec: int) -> dict[str, Any] | None:
        row = self.ctx.conn("ax2012").execute(
            "SELECT dataareaid, accountnum, invoice, voucher, amountcur, settleamountcur, settleamountmst, exchrate, currencycode, "
            "duedate, transdate FROM dbo.custtrans WHERE recid = %s", (inv_rec,)).fetchone()
        if row is None:
            return None
        ent, acct, inv, voucher, amt, sc, sm, rate, ccy, due, d = row
        code = next((k for k, v in self.st["cust"].items() if v["acct"] == str(acct).strip()), None)
        if code is None:
            return None
        return {"ent": ent, "acct": str(acct).strip(), "code": code, "inv": inv, "voucher": voucher, "amt": float(amt),
                "settled": float(sc), "settled_mst": float(sm), "rate": float(rate), "ccy": str(ccy).strip(), "due": due.isoformat(),
                "date": d.isoformat()}

    def credit_holds(self, ts: datetime, today: date) -> None:
        overdue: dict[str, int] = defaultdict(int)
        for o in self.st["open_ar"].values():
            if not o.get("closed") and (today - date.fromisoformat(o["due"])).days > 45:
                overdue[o["code"]] += 1
        for code, cu in self.st["cust"].items():
            if cu["blocked"] == 2:
                continue
            want = 1 if overdue.get(code, 0) >= 2 and payment_behaviour(self.ctx, code) in ("very_slow", "slow") else 0
            if want != cu["blocked"]:
                self.ev_cust_block(ts, {"code": code, "blocked": want})

    # ================================================================== vendors: invoices & payments
    def vendor_invoice(self, ts: datetime, vkey: str, invoiceid: str, invdate: date, lines: pd.DataFrame,
                       by: str = "svc_apimport", txt: str | None = None) -> None:
        """lines: columns name, amount, disp (ledger display value)."""
        v = self.st["vend"].get(vkey)
        if v is None or lines.empty:
            return
        ent, ccy = v["ent"], v["ccy"]
        internal = f"{EP[ent]}-VI{self.seq('vi:' + ent, 100001):07d}"
        voucher = self.voucher(ent)
        total = round(float(lines["amount"].sum()), 2)
        rate = 100.0
        due = invdate + timedelta(days=30)
        self.add("vendinvoicejour", {
            "dataareaid": ent, "invoiceid": invoiceid, "internalinvoiceid": internal, "invoicedate": invdate, "documentdate": invdate,
            "invoiceaccount": v["acct"], "orderaccount": v["acct"], "purchid": None, "currencycode": ccy, "exchrate": rate,
            "invoiceamount": total, "invoiceamountmst": total, "sumtax": 0, "duedate": due, "ledgervoucher": voucher, "payment": "N30",
            "vendgroup": v["group"], "numbersequencegroup": None}, ts, by)
        n = len(lines)
        stamp = naive_utc(ts)
        dims = {d: self.dim(d, ts) for d in lines["disp"].unique()}
        frame = pd.DataFrame({
            "recid": self.recids("vendinvoicetrans", n), "dataareaid": ent, "invoiceid": invoiceid, "internalinvoiceid": internal,
            "invoicedate": invdate, "linenum": np.arange(1, n + 1, dtype=float), "itemid": None, "name": lines["name"].to_numpy(),
            "qty": 1.0, "purchprice": lines["amount"].to_numpy(), "lineamount": lines["amount"].to_numpy(),
            "lineamountmst": lines["amount"].to_numpy(), "currencycode": ccy, "ledgerdimension": lines["disp"].map(dims).to_numpy(),
            "modifieddatetime": stamp, "createddatetime": stamp})
        self.frames["vendinvoicetrans"].append(frame)
        trec = self.add("vendtrans", {
            "dataareaid": ent, "accountnum": v["acct"], "voucher": voucher, "transdate": invdate, "transtype": 3, "invoice": invoiceid,
            "txt": (txt or f"{v['name']} invoice {invoiceid}")[:60], "amountcur": -total, "amountmst": -total, "settleamountcur": 0,
            "settleamountmst": 0, "currencycode": ccy, "exchrate": rate, "duedate": due, "closed": EMPTY, "lastsettledate": EMPTY,
            "approved": 1, "approveddate": ts.date(), "journalnum": None, "paymtermid": "N30"}, ts, by)
        by_disp = lines.groupby("disp")["amount"].sum()
        gl = [(d, 71, round(float(a), 2), f"{v['name']} {invoiceid}", round(float(a), 2), ccy) for d, a in by_disp.items()]
        gl.append(("2000", 41, -total, f"{v['name']} {invoiceid}", -total, ccy))
        self.post_gl(ts, ent, invdate, invoiceid, 3, voucher, gl, by)
        self.st["open_ap"][trec] = {"vkey": vkey, "amt": total, "due": due.isoformat(), "inv": invoiceid}

    def payment_run(self, ts: datetime) -> None:
        horizon = (ts.date() + timedelta(days=7)).isoformat()
        groups: dict[str, list[int]] = defaultdict(list)
        for rec, o in self.st["open_ap"].items():
            if o["due"] <= horizon:
                groups[o["vkey"]].append(rec)
        d = ts.date()
        for vkey, recs in sorted(groups.items()):
            v = self.st["vend"][vkey]
            total = round(sum(self.st["open_ap"][r]["amt"] for r in recs), 2)
            voucher = self.voucher(v["ent"])
            self.add("vendtrans", {
                "dataareaid": v["ent"], "accountnum": v["acct"], "voucher": voucher, "transdate": d, "transtype": 15, "invoice": None,
                "txt": f"Vendor payment - ACH run {d:%Y%m%d}", "amountcur": total, "amountmst": total, "settleamountcur": total,
                "settleamountmst": total, "currencycode": v["ccy"], "exchrate": 100.0, "duedate": d, "closed": d, "lastsettledate": d,
                "approved": 1, "approveddate": d, "journalnum": f"{EP[v['ent']]}-PAY{self.seq('payjrn', 1001):06d}", "paymtermid": None},
                ts, "rebeccal")
            for rec in sorted(recs):
                o = self.st["open_ap"].pop(rec)
                self.upd("vendtrans", rec, ts, "rebeccal", settleamountcur=-o["amt"], settleamountmst=-o["amt"], closed=d, lastsettledate=d)
            self.post_gl(ts, v["ent"], d, voucher, 15, voucher, [("2000", 46, total, f"Payment {v['name']}", total, v["ccy"]),
                                                                  ("1100", 20, -total, f"Payment {v['name']}", -total, v["ccy"])],
                         "rebeccal")

    def ev_vinv_post(self, ts: datetime, p: dict[str, Any]) -> None:
        self.vendor_invoice(ts, p["vkey"], p["invoiceid"], date.fromisoformat(p["date"]), pd.DataFrame(p["lines"]), txt=p.get("txt"))

    # ================================================================== carrier freight bills
    def carrier_bills(self, ts: datetime, win: Window) -> None:
        if not self.st["ship_buf"]:
            return
        buf = pd.concat(self.st["ship_buf"], ignore_index=True)
        local = buf["ship_ts_local"] if "ship_ts_local" in buf else buf["ship_ts"]
        buf["ship_day"] = pd.to_datetime(local).dt.date
        monday = ts.date()
        due = buf[buf["ship_day"] < monday]
        rest = buf[buf["ship_day"] >= monday]
        self.st["ship_buf"] = [rest.drop(columns=["ship_day"])] if len(rest) else []
        if due.empty:
            return
        due = due.assign(ent=due["warehouse_code"].map(lambda w: entity_of_warehouse(self.ctx, w)))
        t = ts
        for (scac, ent), g in sorted(due.groupby(["scac", "ent"]), key=lambda kv: kv[0]):
            vkey = f"CARRIER:{scac}:{ent}"
            if vkey not in self.st["vend"]:
                name = next((c["name"] for c in self.ctx.rules["carriers"] if c["scac"] == scac), scac)
                self.ev_vend_create(ts, {"key": vkey, "ent": ent, "name": name, "group": "CARRIER",
                                         "addr": list(CARRIER_HQ.get(scac, ("Atlanta", "GA", "30301", "US")))})
            lines = self.freight_lines(g, scac, ent, monday)
            r = self.rng("bill", scac, ent, monday.isoformat())
            inv = self.carrier_invoice_no(scac, ent, monday)
            carrier = self.st["vend"][vkey]["name"]
            txt = f"{carrier} freight {monday - timedelta(days=7):%m/%d}-{monday - timedelta(days=1):%m/%d}"
            if self.ctx.dq.hit("late_arriving", r):
                when = t + timedelta(days=float(r.uniform(14, 28)))
                self.dqlog("late_arriving", "vendinvoicejour", inv, "createddatetime", monday, when.date(), ts,
                           "carrier invoice keyed weeks after its invoice date")
                self.schedule(when, "vinv_post", {"vkey": vkey, "invoiceid": inv, "date": monday.isoformat(), "txt": txt,
                                                  "lines": lines.to_dict("list")})
                continue
            post_ts = min(t + timedelta(minutes=float(r.uniform(5, 240))), win.end - timedelta(seconds=1))
            self.vendor_invoice(post_ts, vkey, inv, monday, lines, txt=txt)
            t += timedelta(seconds=11)

    def carrier_invoice_no(self, scac: str, ent: str, d: date) -> str:
        n = self.seq(f"cinv:{scac}:{ent}", 101)
        yy, ww, _ = d.isocalendar()
        if scac == "UPSN":
            acct = "".join("0123456789ABCDEFGHJKLMNPRTVWXY"[(derive_seed("ups", ent) >> (5 * i)) % 30] for i in range(6))
            return f"0000{acct}{ww:02d}{yy % 10}"
        if scac in ("FDEG", "FDE"):
            return f"{7 + n % 2}-{(n * 37) % 900 + 100:03d}-{(d.toordinal() * 13 + n) % 90000 + 10000:05d}"
        if scac == "USPS":
            return f"EPS{d:%y%m%d}{n:04d}"
        return f"{scac[:2]}{d:%y%m%d}{n:03d}"

    def freight_lines(self, g: pd.DataFrame, scac: str, ent: str, bill_date: date) -> pd.DataFrame:
        country = ENT_COUNTRY[ent]
        unit = UNIT.get(country, "LB")
        t = tariff_spec(scac)
        r = self.rng("freight", scac, ent, bill_date.isoformat())
        g = g.reset_index(drop=True)
        n = len(g)
        days = pd.to_datetime(pd.Series(g["ship_day"]))
        cal = days.dt.year.to_numpy()
        eff = pd.to_datetime(pd.Series(cal).map(lambda y: rate_effective(int(y)))).to_numpy()
        year = np.where(days.to_numpy() >= eff, cal, cal - 1)
        w_lb = pd.to_numeric(g.get("billed_weight_lb"), errors="coerce").to_numpy(dtype=float)
        tw = pd.to_numeric(g.get("total_weight"), errors="coerce").to_numpy(dtype=float)
        wu = g.get("weight_uom", pd.Series(["LB"] * n)).astype(str).str.upper().to_numpy()
        fallback = np.where(wu == "KG", tw * 2.20462, tw)
        w_lb = np.where(np.isnan(w_lb), fallback, w_lb)
        w_lb = np.where(np.isnan(w_lb) | (w_lb <= 0), 1.0, w_lb)
        zone = pd.to_numeric(g.get("zone"), errors="coerce").fillna(5).astype(int).to_numpy()
        svc = g["service_code"].astype(str).to_numpy()
        resi = g.get("is_residential", pd.Series([False] * n)).fillna(False).astype(bool).to_numpy()
        trk = g["tracking_nbr"].fillna(g["shipment_nbr"]).astype(str).to_numpy()
        wh = g["warehouse_code"].astype(str).to_numpy()
        postal = g.get("dest_postal", pd.Series([""] * n)).astype(str).to_numpy()
        planted = np.zeros(n, dtype=int)
        if scac == "VRPX":
            hit = r.random(n) < 0.03
            planted = np.where(hit, r.choice([1, 2, 3], size=n, p=[0.4, 0.3, 0.3]), 0)
        billed_lb = np.where(planted == 1, w_lb * r.uniform(1.15, 1.4, n), w_lb)
        bill_w = billed_lb if unit == "LB" else billed_lb / 2.20462
        monday = bill_date - timedelta(days=7)
        lag = 14 if scac == "MRLF" else 7
        fuel = {s: fuel_pct(scac, s, diesel(self.ctx, monday - timedelta(days=lag - 7))) for s in set(svc)}
        names: list[np.ndarray] = []
        amounts: list[np.ndarray] = []
        disps: list[np.ndarray] = []
        base = np.zeros(n)
        if t["kind"] == "parcel":
            for (s, y) in {(a, b) for a, b in zip(svc, year, strict=True)}:
                m = (svc == s) & (year == y)
                base[m] = parcel_rate(scac, s, country, int(y), zone[m], bill_w[m])
            wtxt = pd.Series(np.round(bill_w, 1)).map(lambda v: f"{v:.1f}").to_numpy(dtype=object)
            unit_lbl = np.full(n, unit, dtype=object)
            mix = self.ctx.dq.mask("unit_mixup", n, r)
            for i in np.flatnonzero(mix):
                unit_lbl[i] = "KG" if unit == "LB" else "LB"
                self.dqlog("unit_mixup", "vendinvoicetrans", trk[i], "name", f"WT {wtxt[i]}{unit}", f"WT {wtxt[i]}{unit_lbl[i]}",
                           at_local(bill_date, 10, "America/New_York"))
            cartons = pd.to_numeric(g.get("carton_count"), errors="coerce").fillna(1).astype(int).to_numpy()
            pkg = np.where(cartons > 1, np.char.add(" PKGS ", cartons.astype(str)), "")
            names.append(np.char.add(np.char.add(np.char.add(np.char.add(np.char.add(np.char.add(np.char.add(
                np.char.add("TRK ", trk.astype(str)), " WT "), wtxt.astype(str)), unit_lbl.astype(str)), " ZN "),
                zone.astype(str)), np.char.add(" ", svc.astype(str))), pkg.astype(str)))
            amounts.append(base)
            disps.append(np.char.add(np.char.add("5100-", wh.astype(str)), "-OPS"))
            acc_by_year = {int(y): accessorials(scac, country, int(y)) for y in set(year)}
            resi_amt = np.array([acc_by_year[int(y)].get("resi", 0.0) for y in year])
            ahs_amt = np.array([acc_by_year[int(y)].get("ahs", 0.0) for y in year])
            das_amt = np.array([acc_by_year[int(y)].get("das", 0.0) for y in year])
            resi_m = ((resi | (planted == 3)) & (resi_amt > 0))
            ahs_m = (w_lb > 50) & (ahs_amt > 0)
            das_m = pd.Series(postal).map({p: _das(p) for p in set(postal.tolist())}).to_numpy(dtype=bool) & (das_amt > 0)
            for mask, label, amt in ((resi_m, "RESIDENTIAL SURCHARGE TRK ", resi_amt), (ahs_m, "ADDITIONAL HANDLING TRK ", ahs_amt),
                                     (das_m, "DELIVERY AREA SURCHARGE TRK ", das_amt)):
                if mask.any():
                    names.append(np.char.add(label, trk[mask].astype(str)))
                    amounts.append(amt[mask])
                    disps.append(np.char.add(np.char.add("5100-", wh[mask].astype(str)), "-OPS"))
            dup = planted == 2
            if dup.any():
                names.append(names[0][dup])
                amounts.append(base[dup])
                disps.append(disps[0][dup])
            svc_of = dict(zip(trk.tolist(), svc.tolist(), strict=True))
            line_svc = np.concatenate([svc] + [pd.Series(n).str.rsplit(" ", n=1).str[-1].map(svc_of).fillna(svc[0]).to_numpy()
                                               for n in names[1:]])
            charge = pd.DataFrame({"amt": np.concatenate(amounts), "disp": np.concatenate(disps), "svc": line_svc})
            for (disp, s_code), amt in charge.groupby(["disp", "svc"])["amt"].sum().items():
                pct = fuel.get(s_code, 0.0)
                names.append(np.array([f"FUEL SURCHARGE {pct:.2f}% {s_code} {disp.split('-')[1]} WK {monday:%Y-%m-%d}"]))
                amounts.append(np.array([round(float(amt) * pct / 100, 2)]))
                disps.append(np.array([disp]))
        else:
            for y in set(year):
                m = year == y
                base[m] = ltl_charge(scac, country, int(y), zone[m], billed_lb[m])
            pro = trk.astype(str)
            bol = g.get("bol_nbr", pd.Series([""] * n)).fillna("").astype(str).to_numpy()
            wtxt = np.round(billed_lb).astype(int).astype(str)
            names.append(np.char.add(np.char.add(np.char.add(np.char.add(np.char.add(np.char.add("PRO ", pro), " BOL "), bol), " WT "),
                                                 wtxt), np.char.add("LB CL 70 ZN ", zone.astype(str))))
            amounts.append(base)
            disps.append(np.char.add(np.char.add("5100-", wh.astype(str)), "-OPS"))
            pct = np.array([fuel.get(s, 0.0) for s in svc])
            names.append(np.char.add(np.char.add(np.char.add("FSC ", np.char.mod("%.2f", pct)), "% PRO "), pro))
            amounts.append(np.round(base * pct / 100, 2))
            disps.append(disps[0])
            acc_by_year = {int(y): accessorials(scac, country, int(y)) for y in set(year)}
            if resi.any():
                lg = np.array([acc_by_year[int(y)]["liftgate"] for y in year])[resi]
                rd = np.array([acc_by_year[int(y)]["resi"] for y in year])[resi]
                names += [np.char.add("LIFTGATE PRO ", pro[resi]), np.char.add("RESIDENTIAL DELIVERY PRO ", pro[resi])]
                amounts += [lg, rd]
                disps += [disps[0][resi], disps[0][resi]]
        out = pd.DataFrame({"name": np.concatenate(names).astype(object), "amount": np.round(np.concatenate(amounts).astype(float), 2),
                            "disp": np.concatenate(disps).astype(object)})
        dupmask = self.ctx.dq.mask("duplicate_row", len(out), r)
        if dupmask.any():
            for i in np.flatnonzero(dupmask):
                self.dqlog("duplicate_row", "vendinvoicetrans", out["name"].iat[i], None, None, None,
                           at_local(bill_date, 10, "America/New_York"), "freight bill line billed twice")
            out = pd.concat([out, out[dupmask]], ignore_index=True)
        return out

    # ================================================================== other vendor invoices
    def rent(self, ts: datetime, month: date) -> None:
        for code, w in self.whs.items():
            if warehouse_opened(self.ctx, w) > month:
                continue
            vkey = f"LANDLORD:{code}"
            amount = rent_monthly(self.ctx, w, month)
            lines = pd.DataFrame({"name": [f"Base rent {month:%b %Y} - {w['name']}", f"CAM / property tax estimate {month:%b %Y}"],
                                  "amount": [round(amount * 0.86, 2), round(amount * 0.14, 2)], "disp": [f"6000-{code}-OPS"] * 2})
            self.vendor_invoice(ts + timedelta(minutes=3 * len(self.rows["vendinvoicejour"])), vkey, f"RENT-{code}-{month:%Y%m}", month,
                                lines, by="alanb")

    def utilities(self, ts: datetime) -> None:
        month = add_months(ts.date().replace(day=1), -1)
        for code, w in self.whs.items():
            if warehouse_opened(self.ctx, w) > month:
                continue
            r = self.rng("util", code, month.isoformat())
            total = utilities_monthly(w, month) * float(r.uniform(0.92, 1.08))
            parts = [("Electricity service", 0.78), ("Water / sewer", 0.06)]
            parts.insert(1, ("Natural gas", 0.16) if code in ("CHI1", "NJ1", "TOR1", "RNO1") else ("Demand charge", 0.16))
            lines = pd.DataFrame({"name": [f"{n} {month:%b %Y} - acct {derive_seed('ua', code) % 10**9:09d}" for n, _ in parts],
                                  "amount": [round(total * s, 2) for _, s in parts], "disp": [f"6100-{code}-OPS"] * len(parts)})
            self.vendor_invoice(ts, f"UTILITY:{code}", f"{code[:2]}{derive_seed('ua', code) % 10**6:06d}-{month:%y%m}",
                                ts.date() - timedelta(days=int(r.integers(1, 5))), lines, by="alanb")

    def supplies(self, ts: datetime) -> None:
        counts = self.st["shipcnt"]
        self.st["shipcnt"] = {}
        by_ent: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for code, n in counts.items():
            w = self.whs.get(code)
            if w is None or n <= 0:
                continue
            local = {"TOR1": 1.36, "MTY1": 18.6}.get(code, 1.0)
            by_ent[w["legal_entity"]].append((code, round(n * 0.62 * local, 2)))
        names = {e: n for g, e, n in OTHER_VENDORS if g == "SUPPLIES"}
        for ent, items in sorted(by_ent.items()):
            vkey = f"SUPPLIES:{ent}:{names.get(ent)}"
            lines = pd.DataFrame({"name": [f"Corrugated cartons, void fill, tape - {c}" for c, _ in items],
                                  "amount": [a for _, a in items], "disp": [f"5200-{c}-OPS" for c, _ in items]})
            self.vendor_invoice(ts, vkey, f"PKL{ts:%y%m%d}{self.seq('pkl', 11):03d}", ts.date(), lines, by="alanb")

    def it_invoices(self, ts: datetime) -> None:
        month = ts.date().replace(day=1)
        self.vendor_invoice(ts, "IT:us01:Cloudbridge Managed IT", f"CB-{month:%Y%m}-{self.seq('cb', 501)}", ts.date(),
                            pd.DataFrame({"name": [f"Managed IT services {month:%b %Y}", "Network monitoring", "Help desk seats"],
                                          "amount": [42000.0, 6500.0, 9800.0], "disp": ["6300-CORP-IT"] * 3}), by="alanb")
        if month.month in (1, 4, 7, 10):
            self.vendor_invoice(ts, "IT:us01:Fieldgate Software LLC", f"FG{month:%y}{month.month:02d}-{self.seq('fg', 101)}", ts.date(),
                                pd.DataFrame({"name": [f"WMS subscription Q{(month.month - 1) // 3 + 1} {month.year}"],
                                              "amount": [58500.0], "disp": ["6300-CORP-IT"]}), by="alanb")

    # ================================================================== journals
    def journal(self, ts: datetime, ent: str, name: str, desc: str, lines: list[tuple[str, float, float, str]], transdate: date,
                by: str, posted: bool) -> str:
        num = f"{EP[ent]}-JRN{self.seq('jrn:' + ent, 100001):06d}"
        jrec = self.add("ledgerjournaltable", {"dataareaid": ent, "journalnum": num, "journalname": name, "name": desc,
                                               "journaltype": 0, "posted": int(posted), "posteddatetime": naive_utc(ts) if posted else None},
                        ts, by)
        voucher = self.voucher(ent)
        ccy = self.ents[ent]["currency"]
        recs = []
        for i, (disp, debit, credit, txt) in enumerate(lines, start=1):
            recs.append(self.add("ledgerjournaltrans", {
                "dataareaid": ent, "journalnum": num, "linenum": float(i), "transdate": transdate, "accounttype": 0,
                "ledgerdimension": self.dim(disp, ts), "txt": txt[:60], "amountcurdebit": round(debit, 2),
                "amountcurcredit": round(credit, 2), "currencycode": ccy, "exchrate": 100.0, "voucher": voucher}, ts, by))
        if posted:
            self.post_journal_gl(ts, ent, num, voucher, lines, transdate, by)
        else:
            self.st["drafts"][num] = {"ent": ent, "rec": jrec, "voucher": voucher, "date": transdate.isoformat(), "by": by,
                                      "lines": [[int(r), d, de, cr, t] for r, (d, de, cr, t) in zip(recs, lines, strict=True)]}
        return num

    def post_journal_gl(self, ts: datetime, ent: str, num: str, voucher: str, lines: list[tuple[str, float, float, str]],
                        transdate: date, by: str) -> None:
        ccy = self.ents[ent]["currency"]
        gl = [(d, 14, round(de - cr, 2), t, round(de - cr, 2), ccy) for d, de, cr, t in lines if round(de - cr, 2)]
        total = round(sum(x[2] for x in gl), 2)
        if total and gl:
            gl.append(("1100", 14, -total, "Rounding / balancing", -total, ccy))
        self.post_gl(ts, ent, transdate, num, 36, voucher, gl, by)

    def payroll(self, ts: datetime, p_start: date, p_end: date) -> None:
        ym = p_start.strftime("%Y-%m")
        wages = self.ctx.rules["labor"]["hourly_wage_usd"]
        by_ent: dict[str, list[tuple[str, float, float, str]]] = defaultdict(list)
        for code, w in self.whs.items():
            hours = self.st["labor"].pop(f"{code}|{ym}", 0.0)
            if hours <= 0:
                continue
            country = w["address"]["country"]
            local = 1.0 if country == "US" else fx_per_usd(self.ctx, p_end, COUNTRY_CCY[country])
            amount = round(hours * wages.get(country, 19.5) * local * 1.28, 2)
            by_ent[w["legal_entity"]].append((f"5000-{code}-OPS", amount, 0.0, f"Hourly payroll {p_start:%b %Y} {code} ({hours:,.0f} h)"))
        g = 1.035 ** max(0, p_end.year - 2023)
        for dept, amount in SGA.items():
            by_ent["us01"].append((f"7000-CORP-{dept}", round(amount * g, 2), 0.0, f"Salaried payroll {p_start:%b %Y} {dept}"))
        for ent, lines in sorted(by_ent.items()):
            if ent not in self.ents:
                continue
            total = round(sum(x[1] for x in lines), 2)
            lines.append(("1100", 0.0, total, f"Payroll funding {p_start:%b %Y}"))
            self.journal(ts, ent, "PAYROLL", f"Payroll {p_start:%B %Y}", lines, p_end, "svc_payroll", posted=True)

    def monthly_journals(self, ts: datetime, p_end: date) -> None:
        by_ent: dict[str, list[tuple[str, float, float, str]]] = defaultdict(list)
        for code, w in self.whs.items():
            if warehouse_opened(self.ctx, w) > p_end:
                continue
            by_ent[w["legal_entity"]].append((f"6200-{code}-OPS", equipment_monthly(w), 0.0, f"MHE fleet lease {p_end:%b %Y} {code}"))
        g = 1.035 ** max(0, p_end.year - 2023)
        by_ent["us01"].append(("6400-CORP-FIN", round(38000 * g, 2), 0.0, f"Insurance premium {p_end:%b %Y}"))
        for ent, lines in sorted(by_ent.items()):
            if ent not in self.ents:
                continue
            total = round(sum(x[1] for x in lines), 2)
            lines.append(("1100", 0.0, total, "Lease / insurance payments"))
            self.journal(ts, ent, "GENJRNL", f"Leases and insurance {p_end:%b %Y}", lines, p_end, "alanb", posted=True)

    def accrual_draft(self, ts: datetime) -> None:
        if not self.st["ship_buf"]:
            return
        buf = pd.concat(self.st["ship_buf"], ignore_index=True)
        if "freight_charge_est" not in buf:
            return
        est = buf.groupby("warehouse_code")["freight_charge_est"].sum()
        by_ent: dict[str, list[tuple[str, float, float, str]]] = defaultdict(list)
        for code, amt in est.items():
            if code in self.whs and float(amt) > 0:
                by_ent[self.whs[code]["legal_entity"]].append((f"5100-{code}-OPS", round(float(amt), 2), 0.0, f"Freight accrual {code}"))
        r = self.rng("accrual", ts.date().isoformat())
        for ent, lines in sorted(by_ent.items()):
            total = round(sum(x[1] for x in lines), 2)
            lines.append(("2100", 0.0, total, "Accrued freight - unbilled shipments"))
            num = self.journal(ts, ent, "ACCRUAL", f"Month-end freight accrual {ts:%b %Y}", lines, ts.date(), "alanb", posted=False)
            nxt = add_business_days(ts.date(), 1)
            self.schedule(at_local(nxt, float(r.uniform(9, 12)), "America/New_York"), "jrn_edit", {"num": num})
            post_day = add_business_days(first_business_day(*(add_months(ts.date().replace(day=1), 1).timetuple()[:2])), 1)
            self.schedule(at_local(post_day, 15.0, "America/New_York"), "jrn_post", {"num": num, "reverse": True})

    def reclass_draft(self, ts: datetime) -> None:
        r = self.rng("reclass", ts.date().isoformat())
        sites = [c for c, w in self.whs.items() if w["legal_entity"] == "us01"]
        if len(sites) < 2:
            return
        a, b = (str(x) for x in r.choice(sites, size=2, replace=False))
        amt = round(float(r.uniform(800, 9000)), 2)
        lines = [(f"5200-{a}-OPS", amt, 0.0, f"Reclass packaging {b}->{a}"), (f"5200-{b}-OPS", 0.0, amt, f"Reclass packaging {b}->{a}")]
        num = self.journal(ts, "us01", "RECLASS", "Packaging cost reclass (draft)", lines, ts.date(), "priyar", posted=False)
        self.schedule(ts + timedelta(days=float(r.uniform(0.5, 1.5))), "jrn_edit", {"num": num})
        if r.random() < 0.6:
            self.schedule(ts + timedelta(days=float(r.uniform(2, 5))), "jrn_delete", {"num": num})
        else:
            self.schedule(ts + timedelta(days=float(r.uniform(2, 5))), "jrn_post", {"num": num})

    def ev_jrn_edit(self, ts: datetime, p: dict[str, Any]) -> None:
        j = self.st["drafts"].get(p["num"])
        if j is None:
            return
        r = self.rng("edit", p["num"])
        lines = j["lines"]
        debit_lines = [ln for ln in lines if ln[2] > 0]
        credit_line = next((ln for ln in lines if ln[3] > 0), None)
        for ln in debit_lines:
            if r.random() < 0.35:
                ln[2] = round(ln[2] * float(r.uniform(0.9, 1.12)), 2)
                ln[4] = (ln[4] + " (rev)")[:60]
                self.upd("ledgerjournaltrans", ln[0], ts, j["by"], amountcurdebit=ln[2], txt=ln[4])
        if len(debit_lines) > 2 and r.random() < 0.25:
            gone = debit_lines[int(r.integers(0, len(debit_lines)))]
            lines.remove(gone)
            self.delete("ledgerjournaltrans", gone[0])
        if credit_line:
            new_total = round(sum(ln[2] for ln in lines if ln is not credit_line), 2)
            if new_total != credit_line[3]:
                credit_line[3] = new_total
                self.upd("ledgerjournaltrans", credit_line[0], ts, j["by"], amountcurcredit=new_total)
        self.upd("ledgerjournaltable", j["rec"], ts, j["by"], name=f"{p['num']} (edited)"[:60])

    def ev_jrn_post(self, ts: datetime, p: dict[str, Any]) -> None:
        j = self.st["drafts"].pop(p["num"], None)
        if j is None:
            return
        self.upd("ledgerjournaltable", j["rec"], ts, j["by"], posted=1, posteddatetime=naive_utc(ts))
        lines = [(d, de, cr, t) for _, d, de, cr, t in j["lines"]]
        tdate = date.fromisoformat(j["date"])
        self.post_journal_gl(ts, j["ent"], p["num"], j["voucher"], lines, tdate, j["by"])
        if p.get("reverse"):
            rev = [(d, cr, de, f"Reversal: {t}"[:60]) for d, de, cr, t in lines]
            self.journal(ts + timedelta(minutes=2), j["ent"], "ACCRREV", f"Reversal of {p['num']}", rev, tdate + timedelta(days=1),
                         j["by"], posted=True)

    def ev_jrn_delete(self, ts: datetime, p: dict[str, Any]) -> None:
        j = self.st["drafts"].pop(p["num"], None)
        if j is None:
            return
        for ln in j["lines"]:
            self.delete("ledgerjournaltrans", ln[0])
        self.delete("ledgerjournaltable", j["rec"])

    # ================================================================== run
    def discover(self) -> None:
        for c in self.clients.values():
            self.schedule_customer(c)

    def recurring(self, win: Window) -> None:
        for ts in win.crosses_local_time("America/New_York", 9, 0):
            d = ts.date()
            if d == first_business_day(d.year, d.month):
                self.close_month(ts)
        for ts in win.crosses_local_time("America/New_York", 10, 0):
            if ts.weekday() == 0:
                self.carrier_bills(ts, win)
        for ts in win.crosses_local_time("America/New_York", 11, 0):
            d = ts.date()
            if d.day == 5:
                self.it_invoices(ts)
            if d.day == 10:
                self.utilities(ts)
            if d.day in (2, 16):
                self.supplies(ts)
            if d.day == 12 and self.rng("reclass?", d.isoformat()).random() < 0.3:
                self.reclass_draft(ts)
        for ts in win.crosses_local_time("America/New_York", 14, 0):
            if ts.weekday() == 3:
                self.payment_run(ts)
        for ts in win.crosses_local_time("America/New_York", 16, 0):
            d = ts.date()
            if d == last_business_day(d.year, d.month):
                self.accrual_draft(ts)

    def step(self, win: Window) -> None:
        self.floor = win.start
        self.discover()
        self.ingest(win)
        self.run_queue(win.start)
        self.recurring(win)
        self.run_queue(win.end)
        self.flush()


_DAS: dict[str, bool] = {}
_SLA_CACHE: dict[str, tuple[float, float]] = {}


def _das(postal: str) -> bool:
    hit = _DAS.get(postal)
    if hit is None:
        hit = _DAS[postal] = das_zip(postal)
    return hit


def _hours(hhmm: str) -> float:
    h, _, m = str(hhmm).partition(":")
    return int(h) + int(m or 0) / 60


def _split_camel(name: str) -> list[str]:
    out, cur = [], ""
    for ch in name:
        if ch.isupper() and cur and not cur[-1].isupper():
            out.append(cur)
            cur = ch
        else:
            cur += ch
    return out + ([cur] if cur else [])


class AxModule:
    name = "ax"

    def bootstrap(self, ctx: Any) -> None:
        Ax(ctx).bootstrap()

    def step(self, ctx: Any, win: Window) -> None:
        Ax(ctx).step(win)

    def apply_scenario(self, ctx: Any, scenario_id: str, params: dict[str, Any], win: Window) -> str | None:
        ax = Ax(ctx)
        ax.floor = win.start
        msg = None
        if scenario_id == "onboard_client":
            before = set(ax.st["cust"])
            ax.discover()
            ax.run_queue(win.start + timedelta(seconds=1))
            new = sorted(set(ax.st["cust"]) - before)
            msg = f"customer account(s) {', '.join(ax.st['cust'][c]['acct'] for c in new)} created" if new else \
                "customer account will be created before go-live"
        elif scenario_id == "offboard_client":
            code = scenario_client(params)
            if code in ax.st["cust"]:
                r = ax.rng("offboard", code)
                end = to_date(params.get("end_date") or win.start.date())
                ax.schedule(at_local(end + timedelta(days=int(r.integers(60, 120))), 11, "America/New_York"), "cust_block",
                            {"code": code, "blocked": 2})
                msg = f"customer {ax.st['cust'][code]['acct']} gets a final invoice; blocked after collection"
        ax.flush()
        return msg


MODULE = AxModule()
