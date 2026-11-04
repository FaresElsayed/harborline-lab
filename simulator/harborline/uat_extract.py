"""UAT source extract: the source rows Iris Novak re-performs a project's dashboard KPIs from, as one zip.

The engineer reviews with Iris in a chat that cannot reach the databases, so the simulator hands over an independent
copy of the sources for one project and up to three closed months:

    manifest.json              what is in the zip, the scope rule and time basis of every file, notes, gaps
    wms/<table>.csv            \
    ax2012/<table>.csv          > read through Iris's own read-only login (uat_reviewer): exactly what she may see
    iot/<table>.csv            /
    salesforce/<Object>.csv    every field of the object, formatted as the REST / Bulk API returns it
    carrier/tracking_event.csv the carrier API's fields (location flattened), events received by the sim clock
    edi/inbound_940_orders.csv one row per 940 order parsed from landing/edi (no consignee data)
    files/<name>               copies of the landing business files the KPIs need (budget, rate cards, site master)
    platform/*.csv             Project 1 only: rows and newest change per source table, daily created / changed rows

Scope per project lives in ``PROJECTS`` (derived from handbook/dashboards.html: every KPI tile, breakdown and trend).
Child rows follow their parent's scope. Database files are streamed with ``COPY (SELECT ...) TO STDOUT`` into a
deflated zip member, so a Large build never sits in memory. Values are written as stored: no time-zone conversion,
no rounding, NULL as an empty field. Consumer contact data and worker names are left out (see ``columns_omitted``).
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from .api import soql
from .db import Database, get_db
from .settings import Settings, get_settings

log = logging.getLogger("harborline.uat_extract")

EXTRACT_VERSION = 1
MAX_PERIODS = 3
KEEP_EXTRACTS = 5
STATEMENT_TIMEOUT = "15min"  # one COPY of a Large table, including the time the zip writer takes
RECENT_DAYS = 8  # P6: today plus the same weekday last week
TREND_DAYS = 26 * 7  # P5: order trend compares the last 13 weeks with the 13 before
ACTIVITY_DAYS = 35  # P1: last run plus a four-week same-weekday baseline
EDI_RECENT_DAYS = 7  # P5 / P8: 940s still waiting to be matched
RATE_CARD_LOOKBACK_DAYS = 60  # P4: packages billed in a period shipped up to weeks before it
ID_RE = re.compile(r"^[0-9a-f]{12}$")
MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
SCHEMA_OF = {"wms": "wms", "ax2012": "dbo", "iot": "telemetry"}
SOURCE_DBS = ("wms", "ax2012", "iot")
TIME_BASIS = {
    "wms": "warehouse local time, no zone (warehouse.time_zone names the zone of each site)",
    "ax2012": "dates as posted; createddatetime / modifieddatetime are UTC without a zone",
    "iot": "device_ts_ms is epoch milliseconds UTC (device clock); ingested_at is UTC",
    "salesforce": "UTC (ISO-8601 ending in Z); date fields are YYYY-MM-DD",
    "carrier": "event_local_ts is carrier-local with its UTC offset; received_at is UTC",
}
CONSUMER_PII = (
    "ship_to_name",
    "ship_to_company",
    "ship_to_address1",
    "ship_to_address2",
    "ship_to_phone",
    "ship_to_email",
)
CONSUMER_CONTACT = ("ship_to_name", "ship_to_company", "ship_to_phone", "ship_to_email")
WORKER_NAMES = ("first_name", "last_name")


class ExtractRefused(RuntimeError):
    """The extract cannot be built right now (data not still, another extract running). Maps to HTTP 409."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ===================================================================================================== scope mapping
@dataclass(frozen=True)
class Src:
    """One file. ``where`` is a SQL predicate on alias ``t`` (tokens like ``{P:t.ship_ts}``: see ``Scope.expand``)."""

    path: str
    scope: str
    where: str = "TRUE"
    omit: tuple[str, ...] = ()


@dataclass(frozen=True)
class Key:
    """A set of business keys read from one system to scope another (tracking numbers, order numbers)."""

    name: str
    db: str
    sql: str


@dataclass(frozen=True)
class Gap:
    dashboard: str
    kpi: str
    why: str


@dataclass(frozen=True)
class Project:
    key: str
    name: str
    dashboards: tuple[tuple[str, str], ...]
    files: tuple[Src, ...]
    keys: tuple[Key, ...] = ()
    business_files: tuple[str, ...] = ()  # budget | rate_cards | warehouse_master
    edi_940: bool = False
    platform: bool = False
    periods: str = "two"  # two | latest | iot | none (see default_periods)
    not_in_extract: tuple[Gap, ...] = ()
    notes: tuple[str, ...] = ()


def full(path: str, scope: str = "full table", omit: tuple[str, ...] = ()) -> Src:
    return Src(path, scope, "TRUE", omit)


def sub(table: str, key: str, where: str) -> str:
    return f"SELECT t.{key} FROM {table} t WHERE ({where})"


def child(fk: str, table: str, key: str, where: str) -> str:
    return f"t.{fk} IN ({sub(table, key, where)})"


# Shared predicates. Each is written against alias t of its own table; children nest them as sub-selects.
OPEN_ORDER = "t.order_status NOT IN ('SHIPPED', 'CANCELLED')"
OPEN_RECEIPT = "t.receipt_status <> 'COMPLETE'"
OPEN_TASK = "t.task_status NOT IN ('COMPLETE', 'CANCELLED')"
SHIPPED_IN_PERIOD = "t.order_id IN (SELECT s.order_id FROM wms.shipment s WHERE {P:s.ship_ts})"
CUSTOMER_PARTIES = "t.recid IN (SELECT c.party FROM dbo.custtable c)"
ORG_PARTIES = "t.recid IN (SELECT c.party FROM dbo.custtable c UNION SELECT v.party FROM dbo.vendtable v)"
REGULATED_SENSOR = "split_part(s.zone_code, '-', 2) IN ('COOLER', 'FREEZER')"
COLD_LOCATIONS = (
    "SELECT l.location_id FROM wms.location l JOIN wms.zone z ON z.zone_id = l.zone_id "
    "WHERE z.zone_type IN ('COOLER', 'FREEZER')"
)

WMS_DIMENSIONS = (
    full("wms/warehouse.csv"),
    full("wms/zone.csv"),
    full("wms/location.csv"),
    full("wms/owner.csv"),
    full("wms/owner_warehouse.csv"),
    full("wms/material.csv"),
    full("wms/carrier.csv"),
    full("wms/carrier_service.csv"),
    full("wms/app_user.csv", "full table (worker names left out)", WORKER_NAMES),
)
AX_REFERENCE = (
    full("ax2012/dataarea.csv"),
    full("ax2012/currency.csv"),
    full("ax2012/exchangeratetype.csv"),
    full("ax2012/exchangeratecurrencypair.csv"),
    full("ax2012/exchangerate.csv"),
    full("ax2012/srsanalysisenums.csv"),
)
AX_CUSTOMERS = (
    full("ax2012/custtable.csv"),
    full("ax2012/paymterm.csv"),
    Src("ax2012/dirpartytable.csv", "parties of the customers in custtable", CUSTOMER_PARTIES),
)
AX_AR = (
    full(
        "ax2012/custinvoicejour.csv",
        "full table: every posted invoice and credit note (DSO, aging and trends look back)",
    ),
    full("ax2012/custtrans.csv", "full table: open AR as of any date is rebuilt from it"),
    full("ax2012/custsettlement.csv", "full table: settlements that close invoices, as of any date"),
)
AX_GL = (
    full("ax2012/ledger.csv"),
    full("ax2012/mainaccount.csv"),
    full("ax2012/dimensionattributevaluecombination.csv"),
    Src("ax2012/generaljournalentry.csv", "journal entries with accountingdate in a period", "{P:t.accountingdate}"),
    Src(
        "ax2012/generaljournalaccountentry.csv",
        "lines of the included journal entries",
        child("generaljournalentry", "dbo.generaljournalentry", "recid", "{P:t.accountingdate}"),
    ),
)


# ---------------------------------------------------------------------------------------------------- P2
P2_ORDERS = f"{{P:t.received_ts}} OR {{P:t.requested_ship_date}} OR {OPEN_ORDER} OR {SHIPPED_IN_PERIOD}"
P2_RECEIPTS = f"{{P:t.arrived_ts}} OR {{P:t.completed_ts}} OR {OPEN_RECEIPT}"
P2_SHIPMENTS = child("order_id", "wms.order_header", "order_id", P2_ORDERS)
P2 = Project(
    "P2",
    "Warehouse Performance Scorecard",
    (("P2-D1", "Warehouse Ops Scorecard"), ("P2-D2", "Site Operations Detail")),
    (
        *WMS_DIMENSIONS,
        Src(
            "wms/order_header.csv",
            "orders received, due to ship (requested_ship_date) or shipped in a period, plus every "
            "order still open at the sim clock",
            P2_ORDERS,
            CONSUMER_PII,
        ),
        Src(
            "wms/order_line.csv",
            "lines of the included orders (cancelled lines are deleted in the WMS)",
            child("order_id", "wms.order_header", "order_id", P2_ORDERS),
        ),
        Src("wms/shipment.csv", "shipments of the included orders", P2_SHIPMENTS),
        Src(
            "wms/shipment_carton.csv",
            "cartons of the included shipments",
            child("shipment_id", "wms.shipment", "shipment_id", P2_SHIPMENTS),
        ),
        Src(
            "wms/receipt_header.csv",
            "receipts that arrived or completed in a period, plus receipts still open at the sim clock",
            P2_RECEIPTS,
        ),
        Src(
            "wms/receipt_line.csv",
            "lines of the included receipts",
            child("receipt_id", "wms.receipt_header", "receipt_id", P2_RECEIPTS),
        ),
        Src(
            "wms/asn_header.csv",
            "ASNs of the included receipts",
            child("asn_id", "wms.receipt_header", "asn_id", P2_RECEIPTS),
        ),
        Src(
            "wms/asn_line.csv",
            "lines of the included ASNs",
            child("asn_id", "wms.asn_header", "asn_id", child("asn_id", "wms.receipt_header", "asn_id", P2_RECEIPTS)),
        ),
        Src(
            "wms/task.csv",
            "tasks created or completed in a period, still open at the sim clock, or linked to an included "
            "order or receipt (the WMS deletes tasks older than 180 days)",
            f"{{P:t.created_ts}} OR {{P:t.completed_ts}} OR {OPEN_TASK} "
            f"OR {child('order_id', 'wms.order_header', 'order_id', P2_ORDERS)} "
            f"OR {child('receipt_id', 'wms.receipt_header', 'receipt_id', P2_RECEIPTS)}",
        ),
        Src(
            "wms/inventory_txn.csv",
            "PICK and PUTAWAY ledger rows with txn_ts in a period, plus the putaways of the "
            "included receipts (scan times for productivity and the dock-to-stock stop event)",
            f"t.txn_type IN ('PICK', 'PUTAWAY') AND ({{P:t.txn_ts}} OR (t.ref_type = 'RECEIPT' AND "
            f"{child('ref_id', 'wms.receipt_header', 'receipt_id', P2_RECEIPTS)}))",
        ),
        Src(
            "wms/cycle_count.csv",
            "cycle counts counted or last changed (approved) in a period",
            "{P:t.count_ts} OR {P:t.mod_ts}",
        ),
        full("wms/inventory.csv", "full table: on-hand at the sim clock (the WMS keeps no inventory history)"),
    ),
    business_files=("warehouse_master",),
    not_in_extract=(
        Gap(
            "P2-D1",
            "Space utilization (past days)",
            "The WMS keeps current inventory only, so end-of-day occupancy for "
            "a past date cannot be rebuilt from the sources. Today's utilization can: inventory.csv, location.csv and the "
            "warehouse master workbook.",
        ),
        Gap("P2-D1", "Weather and holiday annotations", "Public reference data, not a Harborline source."),
        Gap(
            "P2-D2",
            "Productivity vs standard",
            "The engineered standards are agreed values in the metric catalog; "
            "the actual picks per productive hour can be re-performed.",
        ),
    ),
)

# ---------------------------------------------------------------------------------------------------- P3
P3_ORDERS = SHIPPED_IN_PERIOD
P3_RECEIPTS = "{P:t.completed_ts}"
P3_RETURNS = "{P:t.received_ts}"
P3 = Project(
    "P3",
    "Billing Assurance & AR",
    (("P3-D1", "Revenue Assurance"), ("P3-D2", "AR & Cash"), ("P3-D3", "Warehouse P&L vs Budget")),
    (
        full("wms/warehouse.csv"),
        full("wms/owner.csv"),
        full("wms/owner_warehouse.csv"),
        full("wms/billing_rate.csv", "full table (rates are updated in place; no history is kept)"),
        Src("wms/billing_record.csv", "billing records with activity_date in a period", "{P:t.activity_date}"),
        Src("wms/order_header.csv", "orders shipped in a period (billable order activity)", P3_ORDERS, CONSUMER_PII),
        Src(
            "wms/order_line.csv",
            "lines of the included orders",
            child("order_id", "wms.order_header", "order_id", P3_ORDERS),
        ),
        Src(
            "wms/shipment.csv",
            "shipments of the included orders",
            child("order_id", "wms.order_header", "order_id", P3_ORDERS),
        ),
        Src("wms/receipt_header.csv", "receipts completed in a period (billable receiving activity)", P3_RECEIPTS),
        Src(
            "wms/receipt_line.csv",
            "lines of the included receipts",
            child("receipt_id", "wms.receipt_header", "receipt_id", P3_RECEIPTS),
        ),
        Src("wms/return_header.csv", "returns received in a period (billable return processing)", P3_RETURNS),
        Src(
            "wms/return_line.csv",
            "lines of the included returns",
            child("return_id", "wms.return_header", "return_id", P3_RETURNS),
        ),
        *AX_REFERENCE,
        *AX_CUSTOMERS,
        *AX_AR,
        Src(
            "ax2012/custinvoicetrans.csv",
            "lines of invoices and credit notes dated in a period",
            "(t.dataareaid, t.invoiceid) IN (SELECT j.dataareaid, j.invoiceid FROM dbo.custinvoicejour j "
            "WHERE {P:j.invoicedate})",
        ),
        *AX_GL,
        full("salesforce/Account.csv"),
        full("salesforce/Contract.csv"),
        full("salesforce/Rate_Card__c.csv"),
        full("salesforce/Rate_Card_Line__c.csv"),
    ),
    business_files=("budget",),
    not_in_extract=(
        Gap(
            "P3-D1",
            "FX rate gap (market side)",
            "ECB reference rates are public market data, not a Harborline "
            "source. The AX accounting rates are in exchangerate.csv (stored x100).",
        ),
        Gap(
            "P3-D1",
            "Storage charges for past months (STOR-PLT, STOR-BIN, COLD-SUR)",
            "The WMS keeps current "
            "inventory only, so past storage activity cannot be re-derived; tie storage to billing_record instead.",
        ),
        Gap(
            "P3-D3",
            "Year to date and 24-month trends",
            "Only the chosen months of GL entries are included. Add the "
            "months you need as periods (up to three per extract).",
        ),
    ),
)

# ---------------------------------------------------------------------------------------------------- P4
P4_INVOICES = (
    "{P:t.invoicedate} AND (t.vendgroup = 'CARRIER' OR t.invoiceaccount IN "
    "(SELECT v.accountnum FROM dbo.vendtable v WHERE v.vendgroup = 'CARRIER'))"
)
P4_SHIPMENTS = (
    "{P:t.ship_ts} OR t.tracking_nbr = ANY(%(bill_trk)s) OR t.shipment_nbr = ANY(%(bill_trk)s) OR "
    "t.shipment_id IN (SELECT c.shipment_id FROM wms.shipment_carton c WHERE c.tracking_nbr = ANY(%(bill_trk)s))"
)
P4 = Project(
    "P4",
    "Freight Audit & Carrier Performance",
    (("P4-D1", "Freight Audit"), ("P4-D2", "Carrier Scorecard")),
    (
        full("wms/warehouse.csv"),
        full("wms/owner.csv"),
        full("wms/carrier.csv"),
        full("wms/carrier_service.csv"),
        Src(
            "wms/shipment.csv",
            "shipments shipped in a period, plus every shipment whose tracking or PRO number appears "
            "on an included carrier bill line",
            P4_SHIPMENTS,
        ),
        Src(
            "wms/shipment_carton.csv",
            "cartons of the included shipments",
            child("shipment_id", "wms.shipment", "shipment_id", P4_SHIPMENTS),
        ),
        Src(
            "wms/order_header.csv",
            "orders of the included shipments (ship-to city, state, postal code and residential "
            "flag for zones and lanes)",
            child("order_id", "wms.shipment", "order_id", P4_SHIPMENTS),
            CONSUMER_PII,
        ),
        *AX_REFERENCE,
        full("ax2012/vendtable.csv"),
        Src("ax2012/dirpartytable.csv", "parties of the customers and vendors", ORG_PARTIES),
        Src(
            "ax2012/vendinvoicejour.csv",
            "carrier bills (vendor group CARRIER) with invoicedate in a period",
            P4_INVOICES,
        ),
        Src(
            "ax2012/vendinvoicetrans.csv",
            "lines of the included carrier bills",
            "(t.dataareaid, t.internalinvoiceid) IN (SELECT t.dataareaid, t.internalinvoiceid FROM dbo.vendinvoicejour t "
            f"WHERE ({P4_INVOICES}))",
        ),
        Src(
            "carrier/tracking_event.csv",
            "events for the tracking numbers of the included shipments, cartons and bill "
            "lines, received by the sim clock (no 90-day API retention limit)",
            "t.tracking_nbr = ANY(%(tracking)s)",
        ),
    ),
    keys=(
        Key(
            "bill_trk",
            "ax2012",
            "SELECT DISTINCT (regexp_match(t.name, '(?:TRK|PRO) ([^ ]+)'))[1] FROM dbo.vendinvoicetrans t "
            "WHERE (t.dataareaid, t.internalinvoiceid) IN (SELECT t.dataareaid, t.internalinvoiceid FROM dbo.vendinvoicejour t "
            f"WHERE ({P4_INVOICES})) AND t.name ~ '(TRK|PRO) '",
        ),
        Key(
            "tracking",
            "wms",
            f"SELECT t.tracking_nbr FROM wms.shipment t WHERE ({P4_SHIPMENTS}) AND t.tracking_nbr IS NOT NULL "
            "UNION SELECT c.tracking_nbr FROM wms.shipment_carton c WHERE c.tracking_nbr IS NOT NULL AND c.shipment_id IN "
            f"({sub('wms.shipment', 'shipment_id', P4_SHIPMENTS)}) UNION SELECT unnest(%(bill_trk)s::text[])",
        ),
    ),
    business_files=("rate_cards",),
    not_in_extract=(
        Gap(
            "P4-D1",
            "Fuel surcharge variance (expected %)",
            "Needs the weekly EIA diesel price, which is public reference "
            "data. The carriers' fuel tables are in the rate card workbooks; the applied % is on the bill lines.",
        ),
        Gap(
            "P4-D1",
            "Disputable now",
            "Each carrier's dispute window is an agreed value in the metric catalog; the "
            "overbilled lines themselves can be re-performed.",
        ),
        Gap("P4-D2", "Composite score", "Weights and the minimum-volume rule are agreed values in the metric catalog."),
    ),
)

# ---------------------------------------------------------------------------------------------------- P5
P5_ORDERS = (
    "{PX:t.received_ts} OR {RECENT:t.received_ts} OR "
    + OPEN_ORDER
    + " OR t.order_id IN (SELECT s.order_id FROM wms.shipment s WHERE {P:s.ship_ts} OR {TREND:s.ship_ts})"
    " OR t.order_nbr = ANY(%(case_orders)s)"
)
P5 = Project(
    "P5",
    "Client 360",
    (("P5-D1", "Sales Pipeline"), ("P5-D2", "Client Health 360")),
    (
        full("salesforce/Account.csv"),
        full("salesforce/Contract.csv"),
        full("salesforce/Opportunity.csv"),
        full("salesforce/OpportunityHistory.csv"),
        full("salesforce/OpportunityLineItem.csv"),
        full("salesforce/Pricebook2.csv"),
        full("salesforce/PricebookEntry.csv"),
        full("salesforce/Product2.csv"),
        full(
            "salesforce/Case.csv",
            "full object (SuppliedName and SuppliedEmail left out)",
            ("SuppliedName", "SuppliedEmail"),
        ),
        full("salesforce/Task.csv"),
        full("salesforce/User.csv"),
        full("wms/warehouse.csv"),
        full("wms/owner.csv"),
        full("wms/owner_warehouse.csv"),
        full("wms/carrier.csv"),
        full("wms/carrier_service.csv"),
        Src(
            "wms/order_header.csv",
            "orders received in a period (two days either side, for EDI matching) or in the last "
            f"{RECENT_DAYS} days, shipped in a period or in the {TREND_DAYS // 7} weeks before the sim clock, still open, or "
            "named on a Salesforce case",
            P5_ORDERS,
            CONSUMER_PII,
        ),
        Src(
            "wms/shipment.csv",
            "shipments of the included orders",
            child("order_id", "wms.order_header", "order_id", P5_ORDERS),
        ),
        *AX_REFERENCE,
        *AX_CUSTOMERS,
        *AX_AR,
    ),
    keys=(
        Key(
            "case_orders",
            "salesforce",
            "SELECT DISTINCT trim(r.data->>'WMS_Order_Number__c') FROM sfdc.record r "
            "WHERE r.object = 'Case' AND NOT r.is_deleted AND coalesce(trim(r.data->>'WMS_Order_Number__c'), '') <> ''",
        ),
    ),
    edi_940=True,
    not_in_extract=(
        Gap(
            "P5-D1",
            "Pipeline coverage (target)",
            "The new-business target is versioned seed data agreed with Hannah "
            "Price, not a source system; open pipeline itself can be re-performed.",
        ),
        Gap(
            "P5-D2",
            "Health score history and drivers",
            "Only the current Health_Score__c and Churn_Risk__c are in "
            "Salesforce; weekly history and weights come from the engineer's model and metric catalog.",
        ),
    ),
)

# ---------------------------------------------------------------------------------------------------- P6
P6_ORDERS = f"{OPEN_ORDER} OR {{RECENT:t.received_ts}} OR {{RECENT:t.mod_ts}}"
P6_SHIPMENTS = (
    f"{{RECENT:t.ship_ts}} OR {{RECENT:t.mod_ts}} OR {child('order_id', 'wms.order_header', 'order_id', P6_ORDERS)}"
)
P6_RECEIPTS = f"{OPEN_RECEIPT} OR {{RECENT:t.arrived_ts}} OR {{RECENT:t.mod_ts}}"
P6 = Project(
    "P6",
    "Real-Time Operations",
    (("P6-D1", "Live Operations Board"),),
    (
        *WMS_DIMENSIONS,
        Src(
            "wms/order_header.csv",
            f"orders still open at the sim clock, plus orders received or changed in the last {RECENT_DAYS} days",
            P6_ORDERS,
            CONSUMER_PII,
        ),
        Src(
            "wms/order_line.csv",
            "lines of the included orders",
            child("order_id", "wms.order_header", "order_id", P6_ORDERS),
        ),
        Src(
            "wms/shipment.csv",
            f"shipments shipped or changed in the last {RECENT_DAYS} days, plus shipments of the included orders",
            P6_SHIPMENTS,
        ),
        Src(
            "wms/shipment_carton.csv",
            "cartons of the included shipments",
            child("shipment_id", "wms.shipment", "shipment_id", P6_SHIPMENTS),
        ),
        Src(
            "wms/task.csv",
            f"tasks still open at the sim clock, plus tasks created, completed or changed in the last "
            f"{RECENT_DAYS} days",
            f"{OPEN_TASK} OR {{RECENT:t.created_ts}} OR {{RECENT:t.completed_ts}} OR {{RECENT:t.mod_ts}}",
        ),
        Src(
            "wms/receipt_header.csv",
            f"receipts still open (trailers arrived or receiving), plus receipts that arrived or "
            f"changed in the last {RECENT_DAYS} days",
            P6_RECEIPTS,
        ),
        Src(
            "wms/receipt_line.csv",
            "lines of the included receipts",
            child("receipt_id", "wms.receipt_header", "receipt_id", P6_RECEIPTS),
        ),
        Src("wms/inventory.csv", "inventory on HOLD at the sim clock", "t.inv_status = 'HOLD'"),
        Src(
            "iot/dock_door_event.csv",
            f"dock door events with device time in the last {RECENT_DAYS} days",
            "{RECENTMS:t.device_ts_ms}",
        ),
    ),
    periods="none",
    not_in_extract=(
        Gap(
            "P6-D1",
            "Board freshness",
            "Measured by the engineer's CDC monitor (commit time of the newest applied change), not by the sources.",
        ),
    ),
    notes=(
        (
            f"Project 6 is a live board: the extract holds the state at the sim clock and the last {RECENT_DAYS} days "
            "(today and the same weekday last week). Months are not used."
        ),
    ),
)

# ---------------------------------------------------------------------------------------------------- P7
TRACED_LOTS = (
    "SELECT rl.material_id, rl.lot_nbr FROM wms.receipt_line rl JOIN wms.material m ON m.material_id = rl.material_id "
    "WHERE rl.lot_nbr IS NOT NULL AND m.temp_class IN ('COOLER', 'FREEZER') AND {P:rl.received_ts}"
)
P7_RECEIPTS = f"t.receipt_id IN (SELECT rl.receipt_id FROM wms.receipt_line rl WHERE (rl.material_id, rl.lot_nbr) IN ({TRACED_LOTS}))"
P7_ORDERS = (
    f"t.order_id IN (SELECT x.ref_id FROM wms.inventory_txn x WHERE x.ref_type = 'ORDER' "
    f"AND (x.material_id, x.lot_nbr) IN ({TRACED_LOTS}))"
)
P7_SHIPMENTS = child("order_id", "wms.order_header", "order_id", P7_ORDERS)
P7_INVENTORY = (
    f"(t.material_id, t.lot_nbr) IN ({TRACED_LOTS}) OR t.location_id IN ({COLD_LOCATIONS}) OR t.inv_status = 'HOLD'"
)
P7 = Project(
    "P7",
    "Cold Chain & Lot Traceability",
    (("P7-D1", "Cold Chain Monitor"), ("P7-D2", "Recall Trace")),
    (
        full("iot/sensor.csv"),
        Src(
            "iot/sensor_reading.csv",
            "readings of sensors in COOLER and FREEZER zones with device time in a period (a day "
            "either side, so every site's local month is complete)",
            f"t.sensor_id IN (SELECT s.sensor_id FROM telemetry.sensor s WHERE {REGULATED_SENSOR}) AND {{PMS:t.device_ts_ms}}",
        ),
        Src(
            "iot/dock_door_event.csv",
            "door events at sites with COOLER or FREEZER zones, device time in a period (a day either side)",
            f"t.warehouse_code IN (SELECT split_part(s.zone_code, '-', 1) FROM telemetry.sensor s WHERE {REGULATED_SENSOR}) "
            "AND {PMS:t.device_ts_ms}",
        ),
        full("wms/warehouse.csv"),
        full("wms/zone.csv"),
        full("wms/location.csv"),
        full("wms/owner.csv"),
        full("wms/material.csv"),
        full("wms/carrier.csv"),
        full("wms/carrier_service.csv"),
        Src(
            "wms/inventory.csv",
            "on-hand at the sim clock for the traced lots, in COOLER and FREEZER locations, and everything on HOLD",
            P7_INVENTORY,
        ),
        Src(
            "wms/inventory_txn.csv",
            "the full ledger of the traced lots (COOLER and FREEZER lots received in a period), "
            "plus every movement into or out of a COOLER or FREEZER location and every HOLD, RELEASE and DESTROY from the "
            "first period's start to the sim clock",
            f"(t.material_id, t.lot_nbr) IN ({TRACED_LOTS}) OR ({{FROM_FIRST:t.txn_ts}} AND (t.from_location_id IN "
            f"({COLD_LOCATIONS}) OR t.to_location_id IN ({COLD_LOCATIONS}) OR t.txn_type IN ('HOLD', 'RELEASE', 'DESTROY')))",
        ),
        Src("wms/receipt_header.csv", "every receipt that brought in a traced lot", P7_RECEIPTS),
        Src(
            "wms/receipt_line.csv",
            "all lines of the included receipts (sibling lots)",
            child("receipt_id", "wms.receipt_header", "receipt_id", P7_RECEIPTS),
        ),
        Src(
            "wms/asn_header.csv",
            "ASNs of the included receipts (supplier)",
            child("asn_id", "wms.receipt_header", "asn_id", P7_RECEIPTS),
        ),
        Src(
            "wms/asn_line.csv",
            "lines of the included ASNs",
            child("asn_id", "wms.asn_header", "asn_id", child("asn_id", "wms.receipt_header", "asn_id", P7_RECEIPTS)),
        ),
        Src(
            "wms/license_plate.csv",
            "license plates on the included receipt lines or on-hand rows",
            f"t.lpn_id IN (SELECT t.lpn_id FROM wms.receipt_line t WHERE {child('receipt_id', 'wms.receipt_header', 'receipt_id', P7_RECEIPTS)}) "
            f"OR t.lpn_id IN ({sub('wms.inventory', 'lpn_id', P7_INVENTORY)})",
        ),
        Src(
            "wms/order_header.csv",
            "orders that picked a traced lot (ship-to address kept for the recall; consignee name, "
            "company, phone and email left out)",
            P7_ORDERS,
            CONSUMER_CONTACT,
        ),
        Src(
            "wms/order_line.csv",
            "lines of the included orders",
            child("order_id", "wms.order_header", "order_id", P7_ORDERS),
        ),
        Src("wms/shipment.csv", "shipments of the included orders", P7_SHIPMENTS),
        Src(
            "wms/shipment_carton.csv",
            "cartons of the included shipments",
            child("shipment_id", "wms.shipment", "shipment_id", P7_SHIPMENTS),
        ),
        Src(
            "wms/return_header.csv",
            "returns against the included orders",
            child("original_order_id", "wms.order_header", "order_id", P7_ORDERS),
        ),
        Src(
            "wms/return_line.csv",
            "lines of the included returns",
            child(
                "return_id",
                "wms.return_header",
                "return_id",
                child("original_order_id", "wms.order_header", "order_id", P7_ORDERS),
            ),
        ),
        Src(
            "carrier/tracking_event.csv",
            "events for the tracking numbers of the included shipments and cartons, received by the sim clock",
            "t.tracking_nbr = ANY(%(tracking)s)",
        ),
    ),
    keys=(
        Key(
            "tracking",
            "wms",
            f"SELECT t.tracking_nbr FROM wms.shipment t WHERE ({P7_SHIPMENTS}) AND t.tracking_nbr IS NOT NULL "
            "UNION SELECT c.tracking_nbr FROM wms.shipment_carton c WHERE c.tracking_nbr IS NOT NULL AND c.shipment_id IN "
            f"({sub('wms.shipment', 'shipment_id', P7_SHIPMENTS)})",
        ),
    ),
    not_in_extract=(
        Gap(
            "P7-D1",
            "Effective-dated thresholds and the SOP duration rule",
            "zone.csv holds today's temperature range "
            "only; earlier threshold versions and the excursion duration rule are Quality's SOP, recorded in the metric "
            "catalog.",
        ),
        Gap(
            "P7-D1",
            "Mean kinetic temperature",
            "Uses the activation energy documented in the metric catalog; the readings are included.",
        ),
        Gap("P7-D2", "Time to trace", "Measured from the engineer's own recall request log."),
    ),
    periods="iot",
    notes=(
        (
            "Traced lots are lots of COOLER and FREEZER materials received in a period. For any other lot, ask for an "
            "extract of the period it was received in."
        ),
    ),
)

# ---------------------------------------------------------------------------------------------------- P8
P8_ORDERS = f"{{P:t.received_ts}} OR {{MTD:t.received_ts}} OR {OPEN_ORDER} OR {SHIPPED_IN_PERIOD} OR {{MTD_SHIPPED}}"
P8_RECEIPTS = f"{{P:t.arrived_ts}} OR {{P:t.completed_ts}} OR {{MTD:t.arrived_ts}} OR {OPEN_RECEIPT}"
P8_RETURNS = "{P:t.received_ts} OR {MTD:t.received_ts}"
P8 = Project(
    "P8",
    "Client Portal & Executive Scorecard",
    (("P8-D1", "Client Portal"), ("P8-D2", "Executive Scorecard")),
    (
        full("wms/warehouse.csv"),
        full("wms/zone.csv"),
        full("wms/owner.csv"),
        full("wms/owner_warehouse.csv"),
        full("wms/material.csv"),
        full("wms/carrier.csv"),
        full("wms/carrier_service.csv"),
        Src(
            "wms/order_header.csv",
            "orders received or shipped in a period or in the month to date, plus orders still open at the sim clock",
            P8_ORDERS,
            CONSUMER_PII,
        ),
        Src(
            "wms/order_line.csv",
            "lines of the included orders",
            child("order_id", "wms.order_header", "order_id", P8_ORDERS),
        ),
        Src(
            "wms/shipment.csv",
            "shipments of the included orders",
            child("order_id", "wms.order_header", "order_id", P8_ORDERS),
        ),
        Src(
            "wms/receipt_header.csv",
            "receipts that arrived or completed in a period or in the month to date, plus receipts still open",
            P8_RECEIPTS,
        ),
        Src(
            "wms/receipt_line.csv",
            "lines of the included receipts",
            child("receipt_id", "wms.receipt_header", "receipt_id", P8_RECEIPTS),
        ),
        Src(
            "wms/asn_header.csv",
            "ASNs of the included receipts",
            child("asn_id", "wms.receipt_header", "asn_id", P8_RECEIPTS),
        ),
        Src(
            "wms/inventory_txn.csv",
            "PUTAWAY ledger rows of the included receipts (dock-to-stock stop event)",
            f"t.txn_type = 'PUTAWAY' AND t.ref_type = 'RECEIPT' AND {child('ref_id', 'wms.receipt_header', 'receipt_id', P8_RECEIPTS)}",
        ),
        Src("wms/return_header.csv", "returns received in a period or in the month to date", P8_RETURNS),
        Src(
            "wms/return_line.csv",
            "lines of the included returns",
            child("return_id", "wms.return_header", "return_id", P8_RETURNS),
        ),
        full("wms/inventory.csv", "full table: on-hand, allocated and held at the sim clock"),
        full("wms/billing_rate.csv"),
        Src(
            "wms/billing_record.csv",
            "billing records with activity_date in a period or in the month to date",
            "{P:t.activity_date} OR {MTD:t.activity_date}",
        ),
        *AX_REFERENCE,
        *AX_CUSTOMERS,
        *AX_AR,
        *AX_GL,
        full("salesforce/Account.csv"),
        full("salesforce/Contract.csv"),
        full(
            "salesforce/Case.csv",
            "full object (SuppliedName and SuppliedEmail left out)",
            ("SuppliedName", "SuppliedEmail"),
        ),
        full("salesforce/Opportunity.csv"),
        full("salesforce/User.csv"),
    ),
    edi_940=False,
    periods="latest",
    not_in_extract=(
        Gap(
            "P8-D2", "Data platform cost per month", "Comes from the engineer's cloud cost report, not a source system."
        ),
        Gap(
            "P8-D2",
            "Cold chain time in range",
            "Re-perform it from a Project 7 extract of the same month (sensor readings are large).",
        ),
        Gap("P8-D2", "Pipeline coverage (target)", "The new-business target is seed data agreed with Hannah Price."),
    ),
    notes=(
        (
            "Project 8 combines the core of Projects 2, 3 and 5. By default it covers the latest closed month only; add "
            "months as periods when a trend point needs them."
        ),
    ),
)

# ---------------------------------------------------------------------------------------------------- P1
P1 = Project(
    "P1",
    "Data Platform Foundation",
    (("P1-D1", "Data Platform Health"),),
    (
        full("wms/warehouse.csv"),
        full("wms/owner.csv"),
        full("wms/owner_warehouse.csv"),
        full("ax2012/dataarea.csv"),
        full("ax2012/custtable.csv"),
        Src("ax2012/dirpartytable.csv", "parties of the customers in custtable", CUSTOMER_PARTIES),
        full("ax2012/custinvoicejour.csv", "full table: the legal entities each client is invoiced from"),
        full("salesforce/Account.csv"),
    ),
    platform=True,
    not_in_extract=(
        Gap("P1-D1", "Run success rate and run duration p95", "Come from the engineer's orchestrator logs."),
        Gap("P1-D1", "DQ pass rate and rows in quarantine", "Come from the engineer's own data quality checks."),
        Gap("P1-D1", "Quota used", "Comes from the platform's usage or billing report."),
        Gap(
            "P1-D1",
            "Deletes in volume anomalies",
            "The sources delete rows physically and keep no trace; "
            "daily_activity.csv shows created and changed rows only.",
        ),
    ),
    notes=(
        (
            "platform/table_stats.csv counts every source table and Salesforce object at the sim clock; "
            "platform/daily_activity.csv counts rows created and changed per day (a row changed several times counts once, "
            f"on its latest change) in the periods and in the {ACTIVITY_DAYS} days before the sim clock."
        ),
    ),
)

PROJECTS: dict[str, Project] = {p.key: p for p in (P1, P2, P3, P4, P5, P6, P7, P8)}

GENERAL_NOTES = (
    (
        "CSV files are UTF-8 with a header row, comma separated, RFC 4180 quoting. NULL is an empty field; an empty string "
        'is "". Postgres booleans are t / f; Salesforce and carrier booleans are true / false.'
    ),
    "WMS timestamps are warehouse local time without a zone; warehouse.time_zone names each site's zone.",
    (
        "AX 2012: company tables carry dataareaid (us01 USD, ca01 CAD, mx01 MXN). createddatetime and modifieddatetime are "
        "UTC. Enums are integers (labels in srsanalysisenums.csv where included). An empty date is 1900-01-01; an open-ended "
        "validto is 2154-12-31. numeric(32,16) amounts print with 16 decimals. exchangerate is stored x100 (135.2 means 1.352)."
    ),
    "AX posted documents never change; corrections are new documents and credit notes are invoices with negative amounts.",
    (
        "Units: TOR1 and MTY1 record weights in KG and dimensions in CM; the US sites use LB and IN (weight_uom / dim_uom on "
        "each row)."
    ),
    (
        "Hard deletes: the WMS deletes cancelled order lines, voided cartons, tasks older than 180 days and inventory rows at "
        "zero; AX deletes reversed settlements. If the sim clock moved after the dashboard refreshed, rows may differ."
    ),
    (
        "Salesforce: deleted and merged records are left out (queryAll would still show them for 15 days). Field names and "
        "values are those of the REST and Bulk APIs."
    ),
    (
        "Carrier events: only events received by the sim clock. location.city / state_prov / country_code of the API are "
        "flattened into columns."
    ),
    (
        "Consumer contact data (order ship-to name, company, address, phone, email; Case SuppliedName / SuppliedEmail) and "
        "worker names are left out; each file lists them under columns_omitted."
    ),
)


# ===================================================================================================== periods
@dataclass(frozen=True)
class Period:
    month: str
    start: date
    end: date  # exclusive

    def as_json(self) -> dict[str, str]:
        return {
            "month": self.month,
            "start": self.start.isoformat(),
            "end": (self.end - timedelta(days=1)).isoformat(),
            "end_exclusive": self.end.isoformat(),
        }


def month_start(d: date, add: int = 0) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 + add, 12)
    return date(y, m + 1, 1)


def period_of(month: str) -> Period:
    m = MONTH_RE.match(month or "")
    if not m or not 1 <= int(m.group(2)) <= 12:
        raise ValueError(f"{month!r} is not a month. Use YYYY-MM, for example 2026-09.")
    start = date(int(m.group(1)), int(m.group(2)), 1)
    return Period(month, start, month_start(start, 1))


def _local_instant(d: date, tz: str) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=ZoneInfo(tz)).astimezone(UTC)


def closed_months(history_start: datetime, sim_clock: datetime, zones: list[str]) -> list[str]:
    """Months that lie inside the history and had closed by the sim clock in every site's local time."""
    zones = zones or ["UTC"]
    out: list[str] = []
    cur = month_start(history_start.astimezone(UTC).date())
    while True:
        nxt = month_start(cur, 1)
        if max(_local_instant(nxt, z) for z in zones) > sim_clock:
            break
        if min(_local_instant(cur, z) for z in zones) >= history_start:
            out.append(f"{cur:%Y-%m}")
        cur = nxt
    return out


def default_periods(available: list[str], mode: str, iot_from: datetime | None = None) -> list[str]:
    """latest: the latest closed month. two: that month and the month three months before it (the oldest month when the
    history is shorter), the two periods Iris samples. iot: like two, among the months that have sensor readings."""
    if mode == "iot":
        with_readings = [m for m in available if iot_from is not None and _month_end_utc(m) > iot_from]
        return default_periods(with_readings or available, "two")
    if not available or mode == "none":
        return []
    latest = available[-1]
    if mode == "latest":
        return [latest]
    earlier = f"{month_start(period_of(latest).start, -3):%Y-%m}"
    if earlier not in available:
        earlier = available[0]
    return sorted({earlier, latest})


def _month_end_utc(month: str) -> datetime:
    end = period_of(month).end
    return datetime(end.year, end.month, end.day, tzinfo=UTC)


def validate_periods(
    requested: list[str] | None, available: list[str], mode: str, iot_from: datetime | None = None
) -> list[Period]:
    if mode == "none":
        return []
    if requested is None:
        requested = default_periods(available, mode, iot_from)
    months = sorted({str(m).strip() for m in requested})
    if not months:
        raise ValueError("Choose at least one month.")
    if len(months) > MAX_PERIODS:
        raise ValueError(f"Choose at most {MAX_PERIODS} months per extract.")
    periods = [period_of(m) for m in months]
    for p in periods:
        if p.month not in available:
            if available and p.month > available[-1]:
                raise ValueError(
                    f"{p.month} has not closed yet at the sim clock in every site's local time. "
                    f"The latest closed month is {available[-1]}."
                )
            if available and p.month < available[0]:
                raise ValueError(f"{p.month} is before the simulated history, which starts with {available[0]}.")
            raise ValueError(f"{p.month} is not a whole month of the simulated history.")
    return periods


# ===================================================================================================== SQL tokens
def _lit(d: date | datetime) -> str:
    if isinstance(d, datetime):
        return f"'{d:%Y-%m-%d %H:%M:%S}'"
    return f"'{d:%Y-%m-%d}'"


@dataclass
class Scope:
    """The extract's time frame. ``expand`` turns predicate tokens into SQL with literal bounds:

    {P:col}        col (local timestamp or date) inside a period
    {PX:col}       same, with two days either side
    {PMS:col}      epoch-millisecond col inside a period, a day either side
    {PZ:col}       timestamptz col inside a period (UTC calendar)
    {RECENT:col}   local timestamp in the last RECENT_DAYS before the sim clock (plus a day for time zones)
    {RECENTMS:col} epoch-millisecond col in the same window
    {TREND:col}    local timestamp in the TREND_DAYS before the sim clock
    {MTD:col}      local timestamp or date in the sim clock's month (to date)
    {MTD_SHIPPED}  order shipped in the month to date
    {FROM_FIRST:col} local timestamp on or after the first period's start
    {DAYS<n>:col}  local timestamp or date in the n days before the sim clock
    """

    periods: list[Period]
    sim_clock: datetime  # aware UTC
    keys: dict[str, list[str]] = field(default_factory=dict)

    @property
    def clock_naive(self) -> datetime:
        return self.sim_clock.astimezone(UTC).replace(tzinfo=None)

    def _ranges(self, col: str, pad: timedelta = timedelta(0)) -> str:
        if not self.periods:
            return "FALSE"
        parts = [f"({col} >= {_lit(p.start - pad)} AND {col} < {_lit(p.end + pad)})" for p in self.periods]
        return "(" + " OR ".join(parts) + ")"

    def _ms(self, start: datetime, end: datetime) -> tuple[int, int]:
        return int(start.replace(tzinfo=UTC).timestamp() * 1000), int(end.replace(tzinfo=UTC).timestamp() * 1000)

    def token(self, name: str, col: str | None) -> str:
        clock = self.clock_naive
        if name == "P":
            return self._ranges(col)
        if name == "PX":
            return self._ranges(col, timedelta(days=2))
        if name == "PZ":
            if not self.periods:
                return "FALSE"
            return (
                "("
                + " OR ".join(
                    f"({col} >= TIMESTAMPTZ '{p.start:%Y-%m-%d} 00:00+00' AND {col} < TIMESTAMPTZ "
                    f"'{p.end:%Y-%m-%d} 00:00+00')"
                    for p in self.periods
                )
                + ")"
            )
        if name == "PMS":
            if not self.periods:
                return "FALSE"
            parts = []
            for p in self.periods:
                lo, hi = self._ms(
                    datetime.combine(p.start, datetime.min.time()) - timedelta(days=1),
                    datetime.combine(p.end, datetime.min.time()) + timedelta(days=1),
                )
                parts.append(f"({col} >= {lo} AND {col} < {hi})")
            return "(" + " OR ".join(parts) + ")"
        if name == "RECENT":
            return f"({col} >= {_lit(clock - timedelta(days=RECENT_DAYS + 1))})"
        if name == "RECENTMS":
            lo, hi = self._ms(clock - timedelta(days=RECENT_DAYS + 1), clock + timedelta(days=1))
            return f"({col} >= {lo} AND {col} < {hi})"
        if name == "TREND":
            return f"({col} >= {_lit(clock - timedelta(days=TREND_DAYS + 1))})"
        if name == "MTD":
            return f"({col} >= {_lit(month_start(clock.date()))})"
        if name == "MTD_SHIPPED":
            return f"t.order_id IN (SELECT s.order_id FROM wms.shipment s WHERE s.ship_ts >= {_lit(month_start(clock.date()))})"
        if name == "FROM_FIRST":
            return (
                f"({col} >= {_lit(self.periods[0].start)})"
                if self.periods
                else f"({col} >= {_lit(clock - timedelta(days=31))})"
            )
        m = re.fullmatch(r"DAYS(\d+)", name)
        if m:
            return f"({col} >= {_lit(clock - timedelta(days=int(m.group(1))))})"
        raise KeyError(f"unknown scope token {name}")

    def expand(self, text: str) -> str:
        return re.sub(r"\{([A-Z_0-9]+)(?::([^{}]+))?\}", lambda m: self.token(m.group(1), m.group(2)), text)

    def bind(self, text: str) -> tuple[str, dict[str, Any] | None]:
        """Expand tokens and pick the key lists the statement names (psycopg binds COPY parameters client-side)."""
        text = self.expand(text)
        names = sorted(set(re.findall(r"%\((\w+)\)s", text)))
        if not names:
            return text.replace("%%", "%"), None
        return text, {n: self.keys.get(n, []) for n in names}


# ===================================================================================================== writing
class RowCounter:
    """Counts CSV records in a byte stream: newlines outside double quotes (COPY quotes fields holding newlines)."""

    def __init__(self) -> None:
        self.newlines = 0
        self.quoted = False

    def feed(self, chunk: bytes) -> None:
        parts = chunk.split(b'"')
        for i, part in enumerate(parts):
            if i:
                self.quoted = not self.quoted
            if not self.quoted:
                self.newlines += part.count(b"\n")

    @property
    def rows(self) -> int:
        return max(0, self.newlines - 1)


@dataclass
class Written:
    path: str
    system: str
    object: str
    scope: str
    time_basis: str | None
    rows: int | None
    columns: list[str]
    columns_omitted: list[str]
    bytes: int = 0
    source: str | None = None

    def as_json(self) -> dict[str, Any]:
        out = {
            "path": self.path,
            "system": self.system,
            "object": self.object,
            "scope": self.scope,
            "time_basis": self.time_basis,
            "rows": self.rows,
            "columns": self.columns,
            "bytes": self.bytes,
        }
        if self.columns_omitted:
            out["columns_omitted"] = self.columns_omitted
        if self.source:
            out["source"] = self.source
        return out


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _table_meta(conn: psycopg.Connection, qualified: str) -> tuple[list[str], list[str]]:
    cols = [
        r[0]
        for r in conn.execute(
            "SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass AND attnum > 0 AND NOT attisdropped ORDER BY attnum",
            (qualified,),
        ).fetchall()
    ]
    pk = [
        r[0]
        for r in conn.execute(
            "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = %s::regclass AND i.indisprimary ORDER BY array_position(i.indkey::int2[], a.attnum)",
            (qualified,),
        ).fetchall()
    ]
    return cols, pk


def _copy_into(
    zf: zipfile.ZipFile, member: str, conn: psycopg.Connection, select: str, params: dict[str, Any] | None
) -> int:
    counter = RowCounter()
    stmt = f"COPY ({select}) TO STDOUT WITH (FORMAT csv, HEADER true)"
    with zf.open(member, "w", force_zip64=True) as dest, conn.cursor() as cur, cur.copy(stmt, params) as cp:
        for chunk in cp:
            data = bytes(chunk)
            dest.write(data)
            counter.feed(data)
    return counter.rows


@contextmanager
def _csv_member(zf: zipfile.ZipFile, member: str) -> Iterator[Any]:
    with zf.open(member, "w", force_zip64=True) as raw, io.TextIOWrapper(raw, encoding="utf-8", newline="") as text:
        yield csv.writer(text, lineterminator="\n")


# ===================================================================================================== extractor
@dataclass
class Progress:
    project: str
    total: int
    started_at: datetime
    done: int = 0
    file: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "file": self.file,
            "done": self.done,
            "total": self.total,
            "started_at": self.started_at.isoformat(),
        }


class Extractor:
    """Builds one extract. Source databases are read as ``uat_reviewer``; Salesforce and carrier data come from
    sim_internal through the app's own access, shaped like their APIs."""

    def __init__(
        self,
        db: Database,
        settings: Settings,
        project: Project,
        scope: Scope,
        status: dict[str, Any],
        on_file: Callable[[str], None] | None = None,
    ) -> None:
        self.db, self.settings, self.project, self.scope, self.status = db, settings, project, scope, status
        self.on_file = on_file or (lambda _p: None)
        self.conns: dict[tuple[str, str], psycopg.Connection] = {}
        self.written: list[Written] = []
        self.notes: list[str] = []

    # -------------------------------------------------------------- connections
    def source_conn(self, dbname: str) -> psycopg.Connection:
        """Iris's own read-only login for wms / ax2012 / iot."""
        key = (dbname, "reviewer")
        if key not in self.conns:
            conn = self.db.connect(dbname, "reviewer", autocommit=True)
            conn.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT}'")
            conn.execute("SET default_transaction_read_only = on")
            self.conns[key] = conn
        return self.conns[key]

    def internal_conn(self) -> psycopg.Connection:
        key = ("sim_internal", "app")
        if key not in self.conns:
            conn = self.db.connect("sim_internal", "app", autocommit=True)
            conn.execute("SET default_transaction_read_only = on")
            self.conns[key] = conn
        return self.conns[key]

    def close(self) -> None:
        for conn in self.conns.values():
            try:
                conn.close()
            except psycopg.Error:
                pass
        self.conns.clear()

    # -------------------------------------------------------------- run
    def plan_size(self) -> int:
        return (
            len(self.project.files)
            + (2 if self.project.platform else 0)
            + (1 if self.project.edi_940 else 0)
            + (1 if self.project.business_files else 0)
        )

    def run(self, zf: zipfile.ZipFile) -> None:
        for key in self.project.keys:
            self.scope.keys[key.name] = self.read_keys(key)
        for src in self.project.files:
            self.on_file(src.path)
            self.write_src(zf, src)
        if self.project.platform:
            self.on_file("platform/table_stats.csv")
            self.write_table_stats(zf)
            self.on_file("platform/daily_activity.csv")
            self.write_daily_activity(zf)
        if self.project.edi_940:
            self.on_file("edi/inbound_940_orders.csv")
            self.write_edi_940(zf)
        if self.project.business_files:
            self.on_file("files/")
            self.write_business_files(zf)

    def read_keys(self, key: Key) -> list[str]:
        conn = self.internal_conn() if key.db == "salesforce" else self.source_conn(key.db)
        text, params = self.scope.bind(key.sql)
        rows = conn.execute(text, params).fetchall()
        return sorted({str(r[0]) for r in rows if r[0] not in (None, "")})

    def write_src(self, zf: zipfile.ZipFile, src: Src) -> None:
        system, name = src.path.split("/", 1)
        obj = name.removesuffix(".csv")
        if system in SOURCE_DBS:
            self.write_table(zf, src, system, obj)
        elif system == "salesforce":
            self.write_salesforce(zf, src, obj)
        elif system == "carrier":
            self.write_carrier(zf, src)
        else:
            raise ValueError(f"no writer for {src.path}")

    def write_table(self, zf: zipfile.ZipFile, src: Src, dbname: str, table: str) -> None:
        conn = self.source_conn(dbname)
        qualified = f"{SCHEMA_OF[dbname]}.{table}"
        cols, pk = _table_meta(conn, qualified)
        keep = [c for c in cols if c not in src.omit]
        order = ", ".join(f"t.{_ident(c)}" for c in (pk or keep[:1]))
        select = (
            f"SELECT {', '.join(f't.{_ident(c)}' for c in keep)} FROM {qualified} t "
            f"WHERE ({src.where}) ORDER BY {order}"
        )
        text, params = self.scope.bind(select)
        t0 = time.monotonic()
        rows = _copy_into(zf, src.path, conn, text, params)
        log.info("extract %s: %s rows in %.1f s", src.path, f"{rows:,}", time.monotonic() - t0)
        self.add(
            src.path, dbname, table, src.scope, TIME_BASIS[dbname], rows, keep, [c for c in src.omit if c in cols], zf
        )

    def write_salesforce(self, zf: zipfile.ZipFile, src: Src, obj_name: str) -> None:
        conn = self.internal_conn()
        custom = conn.execute("SELECT object, api_name, definition FROM sfdc.custom_field ORDER BY added_at").fetchall()
        schema = soql.base_schema().with_custom_fields([(r[0], r[1], r[2]) for r in custom])
        obj = schema.get(obj_name)
        if obj is None:
            raise ValueError(f"unknown Salesforce object {obj_name}")
        fields = [f for f in obj.fields if f.name not in src.omit]
        select = (
            f"SELECT {', '.join(soql.raw_expr('r', f) for f in fields)} FROM sfdc.record r "
            f"WHERE r.object = %(obj)s AND NOT r.is_deleted AND ({self.scope.expand(src.where)}) ORDER BY r.id"
        )
        rows = 0
        with (
            psycopg.connect(self.db.conninfo("sim_internal", "app")) as tx,
            tx.cursor(name="uat_sf") as cur,
            _csv_member(zf, src.path) as writer,
        ):
            tx.read_only = True
            cur.itersize = 5000
            cur.execute(select, {"obj": obj.name})
            writer.writerow([f.name for f in fields])
            for rec in cur:
                writer.writerow([soql.csv_value(f, v) for f, v in zip(fields, rec, strict=True)])
                rows += 1
        self.add(
            src.path,
            "salesforce",
            obj.name,
            src.scope,
            TIME_BASIS["salesforce"],
            rows,
            [f.name for f in fields],
            [n for n in src.omit if obj.field(n)],
            zf,
        )

    def write_carrier(self, zf: zipfile.ZipFile, src: Src) -> None:
        cols = [
            "event_id",
            "tracking_nbr",
            "scac",
            "event_code",
            "event_local_ts",
            "description",
            "exception_code",
            "city",
            "state_prov",
            "country_code",
            "received_at",
            "is_correction",
        ]
        select = (
            "SELECT t.event_id, t.tracking_nbr, t.scac, t.event_code, t.event_local_ts, t.description, t.exception_code, "
            "t.city, t.state_prov, t.country_code, "
            "to_char(t.received_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS received_at, "
            "CASE WHEN t.is_correction THEN 'true' ELSE 'false' END AS is_correction "
            f"FROM carrier.tracking_event t WHERE t.received_at <= TIMESTAMPTZ '{self.scope.sim_clock.isoformat()}' "
            f"AND ({src.where}) ORDER BY t.received_at, t.event_id"
        )
        text, params = self.scope.bind(select)
        rows = _copy_into(zf, src.path, self.internal_conn(), text, params)
        self.add(src.path, "carrier", "tracking_event", src.scope, TIME_BASIS["carrier"], rows, cols, [], zf)

    def add(
        self,
        path: str,
        system: str,
        obj: str,
        scope: str,
        basis: str | None,
        rows: int | None,
        cols: list[str],
        omitted: list[str],
        zf: zipfile.ZipFile,
        source: str | None = None,
    ) -> None:
        size = zf.getinfo(path).file_size
        self.written.append(Written(path, system, obj, scope, basis, rows, cols, omitted, size, source))

    # -------------------------------------------------------------- platform (Project 1)
    def _audit_columns(self, conn: psycopg.Connection, schema: str) -> dict[str, tuple[str | None, str | None]]:
        rows = conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = %s", (schema,)
        ).fetchall()
        cols: dict[str, set[str]] = {}
        for table, column in rows:
            cols.setdefault(table, set()).add(column)
        out = {}
        for table, have in sorted(cols.items()):
            create = next(
                (c for c in ("create_ts", "createddatetime", "ingested_at", "received_ts", "released_ts") if c in have),
                None,
            )
            mod = next((c for c in ("mod_ts", "modifieddatetime") if c in have), None)
            out[table] = (create, mod)
        return out

    def write_table_stats(self, zf: zipfile.ZipFile) -> None:
        path = "platform/table_stats.csv"
        header = ["system", "table", "rows", "audit_column", "latest_audit_value", "time_basis"]
        rows = 0
        with _csv_member(zf, path) as w:
            w.writerow(header)
            for dbname in SOURCE_DBS:
                conn = self.source_conn(dbname)
                schema = SCHEMA_OF[dbname]
                for table, (create, mod) in self._audit_columns(conn, schema).items():
                    audit = mod or create
                    expr = f"max({_ident(audit)})::text" if audit else "NULL"
                    n, latest = conn.execute(f"SELECT count(*), {expr} FROM {schema}.{_ident(table)}").fetchone()
                    w.writerow([dbname, table, n, audit or "", latest or "", TIME_BASIS[dbname] if audit else ""])
                    rows += 1
            conn = self.internal_conn()
            for obj, n, latest in conn.execute(
                "SELECT object, count(*), max(system_modstamp) FROM sfdc.record WHERE NOT is_deleted GROUP BY object ORDER BY object"
            ).fetchall():
                w.writerow(
                    [
                        "salesforce",
                        obj,
                        n,
                        "SystemModstamp",
                        soql.sf_datetime(latest).replace("+0000", "Z") if latest else "",
                        TIME_BASIS["salesforce"],
                    ]
                )
                rows += 1
            n, latest = conn.execute(
                "SELECT count(*), to_char(max(received_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') "
                "FROM carrier.tracking_event WHERE received_at <= %s",
                (self.scope.sim_clock,),
            ).fetchone()
            w.writerow(["carrier", "tracking_event", n, "received_at", latest or "", TIME_BASIS["carrier"]])
            rows += 1
        self.add(
            path,
            "platform",
            "table_stats",
            "every source table and Salesforce object at the sim clock: exact row "
            "count and the newest value of its change column (mod_ts / modifieddatetime, else its insert column)",
            "per row (time_basis column)",
            rows,
            header,
            [],
            zf,
        )

    def write_daily_activity(self, zf: zipfile.ZipFile) -> None:
        path = "platform/daily_activity.csv"
        header = ["system", "table", "day", "created", "changed", "created_column", "changed_column", "time_basis"]
        clock = self.scope.clock_naive
        recent_from = clock - timedelta(days=ACTIVITY_DAYS)
        windows = [
            (datetime.combine(p.start, datetime.min.time()), datetime.combine(p.end, datetime.min.time()))
            for p in self.scope.periods
        ] + [(recent_from, clock)]

        def in_windows(col: str, tz: bool) -> str:
            fmt = "TIMESTAMPTZ '{:%Y-%m-%d %H:%M:%S}+00'" if tz else "'{:%Y-%m-%d %H:%M:%S}'"
            return (
                "(" + " OR ".join(f"({col} >= {fmt.format(a)} AND {col} < {fmt.format(b)})" for a, b in windows) + ")"
            )

        def day(col: str, tz: bool) -> str:
            return f"({col} AT TIME ZONE 'UTC')::date" if tz else f"({col})::date"

        rows = 0
        with _csv_member(zf, path) as w:
            w.writerow(header)
            for dbname in SOURCE_DBS:
                conn = self.source_conn(dbname)
                schema = SCHEMA_OF[dbname]
                tz_cols = {
                    (r[0], r[1])
                    for r in conn.execute(
                        "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = %s "
                        "AND data_type = 'timestamp with time zone'",
                        (schema,),
                    ).fetchall()
                }
                for table, (create, mod) in self._audit_columns(conn, schema).items():
                    if not create and not mod:
                        continue
                    parts = []
                    if create:
                        tz = (table, create) in tz_cols
                        parts.append(
                            f"SELECT {day(_ident(create), tz)} AS d, 1 AS c, 0 AS m FROM {schema}.{_ident(table)} "
                            f"WHERE {in_windows(_ident(create), tz)}"
                        )
                    if mod:
                        tz = (table, mod) in tz_cols
                        newer = f" AND {_ident(mod)} > {_ident(create)}" if create else ""
                        parts.append(
                            f"SELECT {day(_ident(mod), tz)} AS d, 0 AS c, 1 AS m FROM {schema}.{_ident(table)} "
                            f"WHERE {in_windows(_ident(mod), tz)}{newer}"
                        )
                    q = f"SELECT d::text, sum(c), sum(m) FROM ({' UNION ALL '.join(parts)}) x GROUP BY d ORDER BY d"
                    for d, c, m in conn.execute(q).fetchall():
                        w.writerow(
                            [
                                dbname,
                                table,
                                d,
                                c if create else "",
                                m if mod else "",
                                create or "",
                                mod or "",
                                TIME_BASIS[dbname],
                            ]
                        )
                        rows += 1
            conn = self.internal_conn()
            q = (
                f"SELECT object, d::text, sum(c), sum(m) FROM ("
                f"SELECT object, {day('created_date', True)} AS d, 1 AS c, 0 AS m FROM sfdc.record WHERE NOT is_deleted "
                f"AND {in_windows('created_date', True)} UNION ALL "
                f"SELECT object, {day('last_modified_date', True)}, 0, 1 FROM sfdc.record WHERE NOT is_deleted "
                f"AND {in_windows('last_modified_date', True)} AND last_modified_date > created_date"
                f") x GROUP BY 1, 2 ORDER BY 1, 2"
            )
            for obj, d, c, m in conn.execute(q).fetchall():
                w.writerow(["salesforce", obj, d, c, m, "CreatedDate", "LastModifiedDate", TIME_BASIS["salesforce"]])
                rows += 1
            q = (
                f"SELECT {day('received_at', True)}::text AS d, count(*) FROM carrier.tracking_event "
                f"WHERE {in_windows('received_at', True)} AND received_at <= %s GROUP BY 1 ORDER BY 1"
            )
            for d, c in conn.execute(q, (self.scope.sim_clock,)).fetchall():
                w.writerow(["carrier", "tracking_event", d, c, "", "received_at", "", TIME_BASIS["carrier"]])
                rows += 1
        self.add(
            path,
            "platform",
            "daily_activity",
            f"rows created and changed per table and day, in the periods and in "
            f"the {ACTIVITY_DAYS} days before the sim clock (a row changed several times counts once, on its latest "
            "change; deleted rows are not visible)",
            "per row (time_basis column)",
            rows,
            header,
            [],
            zf,
        )

    # -------------------------------------------------------------- EDI 940 index (Projects 5)
    def edi_windows(self) -> list[tuple[datetime, datetime]]:
        clock = self.scope.clock_naive
        out = [
            (datetime.combine(p.start, datetime.min.time()), datetime.combine(p.end, datetime.min.time()))
            for p in self.scope.periods
        ]
        out.append((clock - timedelta(days=EDI_RECENT_DAYS), clock))
        return out

    def write_edi_940(self, zf: zipfile.ZipFile) -> None:
        path = "edi/inbound_940_orders.csv"
        root = Path(self.settings.landing_dir) / "edi"
        windows = self.edi_windows()
        rows = 0
        with _csv_member(zf, path) as w:
            w.writerow(EDI_COLUMNS)
            for file_name, member, ts, data in iter_940(root, windows):
                for rec in parse_940(data):
                    w.writerow(
                        [
                            file_name,
                            member or "",
                            ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            *(rec.get(c, "") for c in EDI_FIELDS),
                        ]
                    )
                    rows += 1
        if not root.is_dir():
            self.notes.append("landing/edi was not found, so edi/inbound_940_orders.csv is empty.")
        self.add(
            path,
            "edi",
            "940 inbound",
            f"one row per 940 order (W05) in interchanges transmitted in a period or in the "
            f"{EDI_RECENT_DAYS} days before the sim clock, parsed from landing/edi (loose files and monthly archives). "
            "Consignee name and address are left out. Files your pipeline moved out of landing/edi are not included.",
            "transmitted_at_utc is UTC (from the file name); ISA / GS dates are as sent",
            rows,
            EDI_COLUMNS,
            [],
            zf,
        )

    # -------------------------------------------------------------- landing business files
    def write_business_files(self, zf: zipfile.ZipFile) -> None:
        base = Path(self.settings.landing_dir) / "files"
        chosen: list[tuple[Path, str, str]] = []
        for kind in self.project.business_files:
            found = BUSINESS_FILES[kind](base, self.scope)
            if not found:
                self.notes.append(f"No {BUSINESS_LABEL[kind]} was found in landing/files at the time of the extract.")
            chosen += [(p, kind, why) for p, why in found]
        for p, kind, why in chosen:
            member = f"files/{p.name}"
            with p.open("rb") as fh, zf.open(member, "w", force_zip64=True) as dest:
                shutil.copyfileobj(fh, dest, 1 << 20)
            self.add(
                member,
                "files",
                BUSINESS_LABEL[kind],
                why,
                None,
                None,
                [],
                [],
                zf,
                source=f"landing/files/{p.relative_to(base).as_posix()}",
            )


# ===================================================================================================== EDI 940
EDI_FIELDS = [
    "isa_sender_id",
    "isa_control_nbr",
    "gs_date",
    "gs_time",
    "gs_control_nbr",
    "st_control_nbr",
    "purpose_code",
    "depositor_order_nbr",
    "client_order_ref",
    "po_number",
    "depositor_code",
    "warehouse_code",
    "requested_ship_date",
    "scac",
    "lines",
    "units",
    "weight",
    "weight_uom",
]
EDI_COLUMNS = ["file", "archive_member", "transmitted_at_utc", *EDI_FIELDS]
_EDI_NAME = re.compile(r"^940_.+_(\d{14})_\d+\.edi$")
_EDI_ZIP = re.compile(r"^940_.+_(\d{6})\.zip$")


def _in_windows(ts: datetime, windows: list[tuple[datetime, datetime]]) -> bool:
    return any(a <= ts < b for a, b in windows)


def iter_940(root: Path, windows: list[tuple[datetime, datetime]]) -> Iterator[tuple[str, str | None, datetime, bytes]]:
    """(file, archive member, transmitted UTC, bytes) for 940 interchanges whose file-name time falls in a window."""
    loose = root / "inbound" / "940"
    if loose.is_dir():
        for p in sorted(loose.rglob("*.edi")):
            m = _EDI_NAME.match(p.name)
            if m:
                ts = datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
                if _in_windows(ts, windows):
                    yield p.relative_to(root.parent).as_posix(), None, ts, p.read_bytes()
    archive = root / "archive" / "inbound" / "940"
    if archive.is_dir():
        for z in sorted(archive.rglob("*.zip")):
            m = _EDI_ZIP.match(z.name)
            if not m:
                continue
            month = datetime.strptime(m.group(1), "%Y%m")
            next_month = (month + timedelta(days=32)).replace(day=1)
            if not any(a < next_month and month < b for a, b in windows):
                continue
            with zipfile.ZipFile(z) as zin:
                for name in sorted(zin.namelist()):
                    mm = _EDI_NAME.match(name)
                    if not mm:
                        continue
                    ts = datetime.strptime(mm.group(1), "%Y%m%d%H%M%S")
                    if _in_windows(ts, windows):
                        yield z.relative_to(root.parent).as_posix(), name, ts, zin.read(name)


def parse_940(data: bytes) -> list[dict[str, Any]]:
    """One dict per 940 transaction set. Tolerates the envelope defects the simulator plants (bad counts, missing
    segments); element values are kept as sent, except fixed-width ISA padding which is trimmed."""
    text = data.decode("ascii", errors="replace")
    sep = text[3] if text.startswith("ISA") and len(text) > 3 else "*"
    isa: list[str] = []
    gs: list[str] = []
    out: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None

    def el(parts: list[str], i: int) -> str:
        return parts[i].strip() if len(parts) > i else ""

    for seg in text.split("~"):
        seg = seg.strip("\r\n ")
        if not seg:
            continue
        e = seg.split(sep)
        tag = e[0]
        if tag == "ISA":
            isa = e
        elif tag == "GS":
            gs = e
        elif tag == "ST":
            if cur is not None:
                out.append(cur)
            cur = {
                "isa_sender_id": el(isa, 6),
                "isa_control_nbr": el(isa, 13),
                "gs_date": el(gs, 4),
                "gs_time": el(gs, 5),
                "gs_control_nbr": el(gs, 6),
                "st_control_nbr": el(e, 2),
                "lines": 0,
            }
        elif cur is None:
            continue
        elif tag == "W05":
            cur.update(purpose_code=el(e, 1), depositor_order_nbr=el(e, 2), client_order_ref=el(e, 3))
        elif tag == "N1" and el(e, 1) == "DE":
            cur["depositor_code"] = el(e, 4)
        elif tag == "N1" and el(e, 1) == "WH":
            cur["warehouse_code"] = el(e, 4)
        elif tag == "N9" and el(e, 1) == "PO":
            cur["po_number"] = el(e, 2)
        elif tag == "G62" and el(e, 1) == "10":
            cur["requested_ship_date"] = el(e, 2)
        elif tag == "W66":
            cur["scac"] = el(e, 10)
        elif tag == "LX":
            cur["lines"] += 1
        elif tag == "W76":
            cur.update(units=el(e, 1), weight=el(e, 2), weight_uom=el(e, 3))
        elif tag == "SE":
            out.append(cur)
            cur = None
    if cur is not None:
        out.append(cur)
    return out


# ===================================================================================================== business files
BUSINESS_LABEL = {
    "budget": "budget workbook",
    "rate_cards": "carrier rate card workbook",
    "warehouse_master": "warehouse master workbook",
}
_RATE = re.compile(r"^([A-Z0-9]+)_rates_(\d{8})\.xlsx$")
_BUDGET = re.compile(r"^budget_FY(\d{4})(_reforecast)?\.xlsx$")
_MASTER = re.compile(r"^warehouse_master_(\d{8})\.xlsx$")


def _budgets(base: Path, scope: Scope) -> list[tuple[Path, str]]:
    years = {p.start.year for p in scope.periods}
    out = []
    for p in sorted((base / "finance" / "budget").glob("budget_FY*.xlsx")):
        m = _BUDGET.match(p.name)
        if m and int(m.group(1)) in years:
            version = "mid-year reforecast" if m.group(2) else "original budget"
            out.append((p, f"{version} for FY{m.group(1)} (the fiscal year of a period)"))
    return out


def _rate_cards(base: Path, scope: Scope) -> list[tuple[Path, str]]:
    if not scope.periods:
        return []
    lo = scope.periods[0].start - timedelta(days=RATE_CARD_LOOKBACK_DAYS)
    hi = scope.periods[-1].end
    by_scac: dict[str, list[tuple[date, Path]]] = {}
    for p in (base / "carriers" / "rate_cards").glob("*_rates_*.xlsx"):
        m = _RATE.match(p.name)
        if m:
            by_scac.setdefault(m.group(1), []).append((datetime.strptime(m.group(2), "%Y%m%d").date(), p))
    out = []
    for scac, versions in sorted(by_scac.items()):
        versions.sort()
        for i, (eff, p) in enumerate(versions):
            nxt = versions[i + 1][0] if i + 1 < len(versions) else date.max
            if eff < hi and nxt > lo:
                out.append(
                    (
                        p,
                        (
                            f"{scac} tariff effective {eff:%Y-%m-%d}, in force for packages shipped from "
                            f"{RATE_CARD_LOOKBACK_DAYS} days before the first period to the end of the last"
                        ),
                    )
                )
    return out


def _warehouse_masters(base: Path, scope: Scope) -> list[tuple[Path, str]]:
    clock = scope.clock_naive.date()
    out = []
    for p in sorted((base / "facilities").glob("warehouse_master_*.xlsx")):
        m = _MASTER.match(p.name)
        if m and datetime.strptime(m.group(1), "%Y%m%d").date() <= clock:
            out.append(
                (
                    p,
                    (
                        f"site master as of {m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]} (capacity in effect "
                        "from that date)"
                    ),
                )
            )
    return out


BUSINESS_FILES: dict[str, Callable[[Path, Scope], list[tuple[Path, str]]]] = {
    "budget": _budgets,
    "rate_cards": _rate_cards,
    "warehouse_master": _warehouse_masters,
}


# ===================================================================================================== service
@dataclass
class ExtractInfo:
    id: str
    filename: str
    path: Path
    size_bytes: int
    project: str
    periods: list[str]
    sim_clock: str
    generated_at: str
    duration_s: float
    files: list[dict[str, Any]]

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "project": self.project,
            "periods": self.periods,
            "sim_clock": self.sim_clock,
            "generated_at": self.generated_at,
            "duration_s": self.duration_s,
            "files": self.files,
            "download_url": f"/api/uat/extract/{self.id}",
        }


def _iso_z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class ExtractService:
    """Process-wide: one extract at a time, the last ``KEEP_EXTRACTS`` kept in a temp folder."""

    def __init__(self, folder: Path | None = None) -> None:
        self.folder = folder or Path(tempfile.gettempdir()) / "harborline-uat-extracts"
        self._lock = threading.Lock()
        self.progress: Progress | None = None
        self._site_cache: tuple[tuple[Any, ...], list[str], datetime | None] | None = None

    # -------------------------------------------------------------- state
    def site_meta(self, db: Database, status: dict[str, Any]) -> tuple[list[str], datetime | None]:
        """Warehouse time zones and the first sensor reading, cached until the build or the clock changes (the
        Control Center polls the options every few seconds)."""
        key = (status.get("built_at"), status.get("sim_clock"), status.get("preset"))
        if self._site_cache is None or self._site_cache[0] != key:
            self._site_cache = (key, self.zones(db), self.iot_from(db))
        return self._site_cache[1], self._site_cache[2]

    def zones(self, db: Database) -> list[str]:
        try:
            with db.connect("wms", "reviewer", autocommit=True) as conn:
                return sorted({r[0] for r in conn.execute("SELECT DISTINCT time_zone FROM wms.warehouse").fetchall()})
        except psycopg.Error:
            return []

    def iot_from(self, db: Database) -> datetime | None:
        """When the first sensor reading landed (builds keep only the last weeks of IoT history)."""
        try:
            with db.connect("iot", "reviewer", autocommit=True) as conn:
                row = conn.execute("SELECT min(ingested_at) FROM telemetry.sensor_reading").fetchone()
                return row[0].astimezone(UTC) if row and row[0] else None
        except psycopg.Error:
            return None

    @staticmethod
    def refusal(status: dict[str, Any]) -> tuple[str, str] | None:
        """Why the data is not still enough for an extract, or None."""
        state = status.get("state")
        if state == "building":
            return "building", (
                "A build is running. When it has finished, refresh your dashboard and then build the extract."
            )
        if (status.get("stream") or {}).get("running") or state == "streaming":
            return "streaming", (
                "The stream is running, so the sources are still changing. Stop the stream, refresh "
                "your dashboard, then build the extract."
            )
        if state == "running_batch":
            return "busy", (
                "A batch or scenario is running. Wait for it to finish, refresh your dashboard, then build the extract."
            )
        if not status.get("built_at") or not status.get("sim_clock"):
            return "not_built", "There is no data yet. Build the dataset first."
        return None

    def options(self, engine: Any, db: Database | None = None) -> dict[str, Any]:
        db = db or get_db()
        status = engine.status()
        refusal = self.refusal(status)
        if refusal is None and self.progress is not None:
            refusal = ("extracting", "An extract is being built. Wait for it to finish.")
        months: list[str] = []
        iot_from: datetime | None = None
        if status.get("built_at") and status.get("sim_clock") and status.get("history_start"):
            clock = datetime.fromisoformat(status["sim_clock"])
            start = datetime.fromisoformat(status["history_start"])
            zones, iot_from = self.site_meta(db, status)
            months = closed_months(start, clock, zones)
        projects = []
        for p in PROJECTS.values():
            projects.append(
                {
                    "key": p.key,
                    "name": p.name,
                    "dashboards": [{"id": d, "name": n} for d, n in p.dashboards],
                    "uses_periods": p.periods != "none",
                    "default_periods": default_periods(months, p.periods, iot_from),
                    "note": " ".join(p.notes[:1]) or None,
                    "not_in_extract": [
                        {"dashboard": g.dashboard, "kpi": g.kpi, "why": g.why} for g in p.not_in_extract
                    ],
                }
            )
        if refusal is None and not months and status.get("sim_clock"):
            refusal = (
                "no_months",
                (
                    "No whole month has closed in the simulated history yet. Run a batch past the end of "
                    "the month, or build a larger preset."
                ),
            )
        return {
            "ready": refusal is None,
            "reason_code": refusal[0] if refusal else None,
            "reason": refusal[1] if refusal else None,
            "sim_clock": status.get("sim_clock"),
            "preset": status.get("preset"),
            "seed": status.get("seed"),
            "history_start": status.get("history_start"),
            "sensor_readings_from": _iso_z(iot_from) if iot_from else None,
            "months": months,
            "max_periods": MAX_PERIODS,
            "projects": projects,
            "in_progress": self.progress.as_json() if self.progress else None,
            "recent": [e.as_json() | {"files": None} for e in self.list()],
        }

    # -------------------------------------------------------------- build
    def build(
        self,
        project_key: str,
        periods: list[str] | None,
        engine: Any,
        db: Database | None = None,
        settings: Settings | None = None,
    ) -> ExtractInfo:
        db = db or get_db()
        settings = settings or get_settings()
        project = PROJECTS.get((project_key or "").strip().upper())
        if project is None:
            raise ValueError(f"Unknown project {project_key!r}. Choose one of {', '.join(PROJECTS)}.")
        if not self._lock.acquire(blocking=False):
            raise ExtractRefused("extracting", "An extract is already being built. Wait for it to finish.")
        run_lock = getattr(engine, "_run_lock", None)
        try:
            status = engine.status()
            refusal = self.refusal(status)
            if refusal:
                raise ExtractRefused(*refusal)
            if run_lock is not None and not run_lock.acquire(blocking=False):
                run_lock = None
                raise ExtractRefused("busy", "The simulator is busy. Wait for it to be idle, then build the extract.")
            try:
                # The run lock keeps batches, scenarios, builds and stream ticks out while the extract reads.
                status = engine.status()
                refusal = self.refusal(status)
                if refusal:
                    raise ExtractRefused(*refusal)
                return self._build(project, periods, status, db, settings)
            finally:
                if run_lock is not None:
                    run_lock.release()
        finally:
            self.progress = None
            self._lock.release()

    def _build(
        self, project: Project, requested: list[str] | None, status: dict[str, Any], db: Database, settings: Settings
    ) -> ExtractInfo:
        clock = datetime.fromisoformat(status["sim_clock"]).astimezone(UTC)
        history_start = datetime.fromisoformat(status["history_start"]).astimezone(UTC)
        zones, iot_from = self.site_meta(db, status)
        available = closed_months(history_start, clock, zones)
        if project.periods != "none" and not available:
            raise ExtractRefused(
                "no_months",
                "No whole month has closed in the simulated history yet. Run a batch past "
                "the end of the month, or build a larger preset.",
            )
        periods = validate_periods(requested, available, project.periods, iot_from)
        scope = Scope(periods, clock)
        t0 = time.monotonic()
        generated = datetime.now(UTC)
        ext_id = uuid.uuid4().hex[:12]
        label = "+".join(p.month for p in periods) or "live"  # "+": a list of months, not a range
        filename = f"harborline_{project.key}_source-extract_{label}_simclock-{clock:%Y%m%dT%H%MZ}.zip"
        self.folder.mkdir(parents=True, exist_ok=True)
        final = self.folder / f"{ext_id}.zip"
        tmp = self.folder / f".{ext_id}.zip.part"
        extractor = Extractor(db, settings, project, scope, status)
        self.progress = Progress(project.key, extractor.plan_size(), generated)

        def on_file(path: str) -> None:
            if self.progress:
                self.progress.done = sum(1 for _ in extractor.written)
                self.progress.file = path

        extractor.on_file = on_file
        try:
            with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as zf:
                extractor.run(zf)
                manifest = self._manifest(project, periods, scope, status, generated, extractor)
                zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
            tmp.replace(final)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        finally:
            extractor.close()
        info = ExtractInfo(
            id=ext_id,
            filename=filename,
            path=final,
            size_bytes=final.stat().st_size,
            project=project.key,
            periods=[p.month for p in periods],
            sim_clock=_iso_z(clock),
            generated_at=_iso_z(generated),
            duration_s=round(time.monotonic() - t0, 1),
            files=[{"path": w.path, "rows": w.rows, "bytes": w.bytes} for w in extractor.written],
        )
        (self.folder / f"{ext_id}.json").write_text(json.dumps(info.as_json()), encoding="utf-8")
        self._prune()
        log.info(
            "UAT extract %s for %s (%s) built in %.1f s: %s bytes",
            ext_id,
            project.key,
            label,
            info.duration_s,
            f"{info.size_bytes:,}",
        )
        return info

    def _manifest(
        self,
        project: Project,
        periods: list[Period],
        scope: Scope,
        status: dict[str, Any],
        generated: datetime,
        extractor: Extractor,
    ) -> dict[str, Any]:
        notes = list(GENERAL_NOTES) + list(project.notes) + extractor.notes
        if any(w.path == "iot/sensor_reading.csv" for w in extractor.written):
            notes.append(self._iot_coverage(extractor))
        window = None
        if project.periods == "none":
            window = {"start": _iso_z(scope.sim_clock - timedelta(days=RECENT_DAYS)), "end": _iso_z(scope.sim_clock)}
        return {
            "extract_version": EXTRACT_VERSION,
            "generated_at_utc": _iso_z(generated),
            "sim_clock": _iso_z(scope.sim_clock),
            "data_as_of": _iso_z(scope.sim_clock),
            "preset": status.get("preset"),
            "seed": status.get("seed"),
            "history": {
                k: _iso_z(datetime.fromisoformat(status[f"history_{k}"])) if status.get(f"history_{k}") else None
                for k in ("start", "end")
            },
            "project": {
                "key": project.key,
                "name": project.name,
                "dashboards": [{"id": d, "name": n} for d, n in project.dashboards],
            },
            "periods": [p.as_json() for p in periods],
            "period_basis": "calendar months; WMS rows by warehouse-local time as stored, AX by posting date, UTC sources "
            "padded as each file's scope says",
            "recent_window": window,
            "read_as": {
                "wms": "uat_reviewer (read-only)",
                "ax2012": "uat_reviewer (read-only)",
                "iot": "uat_reviewer (read-only)",
                "salesforce": "as the REST / Bulk API returns records",
                "carrier": "as the carrier tracking API returns events",
            },
            "files": [w.as_json() for w in extractor.written],
            "notes": notes,
            "not_in_extract": [{"dashboard": g.dashboard, "kpi": g.kpi, "why": g.why} for g in project.not_in_extract],
        }

    @staticmethod
    def _iot_coverage(extractor: Extractor) -> str:
        conn = extractor.source_conn("iot")
        # ingested_at is indexed; device time is never later than it (gateways only ever land readings late).
        lo, hi = conn.execute("SELECT min(ingested_at), max(ingested_at) FROM telemetry.sensor_reading").fetchone()
        if lo is None:
            return "There are no sensor readings in the source yet."
        return (
            f"Sensor readings in the source run from {lo.astimezone(UTC):%Y-%m-%d %H:%M} to "
            f"{hi.astimezone(UTC):%Y-%m-%d %H:%M} UTC (ingested_at; builds keep only the last weeks of IoT history); "
            "months before that have no readings."
        )

    # -------------------------------------------------------------- files on disk
    def list(self) -> list[ExtractInfo]:
        out = []
        if not self.folder.is_dir():
            return out
        for meta in self.folder.glob("*.json"):
            try:
                d = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            path = self.folder / f"{d.get('id')}.zip"
            if path.is_file():
                out.append(
                    ExtractInfo(
                        d["id"],
                        d["filename"],
                        path,
                        int(d["size_bytes"]),
                        d["project"],
                        d["periods"],
                        d["sim_clock"],
                        d["generated_at"],
                        float(d.get("duration_s") or 0),
                        d.get("files") or [],
                    )
                )
        out.sort(key=lambda e: (e.generated_at, e.path.stat().st_mtime_ns), reverse=True)
        return out

    def get(self, ext_id: str) -> ExtractInfo | None:
        if not ID_RE.match(ext_id or ""):
            return None
        return next((e for e in self.list() if e.id == ext_id), None)

    def _prune(self) -> None:
        for old in self.list()[KEEP_EXTRACTS:]:
            old.path.unlink(missing_ok=True)
            (self.folder / f"{old.id}.json").unlink(missing_ok=True)


_service: ExtractService | None = None
_service_lock = threading.Lock()


def get_service() -> ExtractService:
    global _service
    with _service_lock:
        if _service is None:
            _service = ExtractService()
        return _service


def reset_service(folder: Path | None = None) -> ExtractService:
    global _service
    with _service_lock:
        _service = ExtractService(folder)
        return _service
