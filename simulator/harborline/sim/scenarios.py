"""One-shot business scenarios (Control Center → Scenarios).

The engine validates the params here, calls ``apply_scenario(ctx, scenario_id, params, win)`` on every module that
implements it (in MODULE_ORDER), then simulates a 1-hour window so the effects flow into every system.
Param types: int, number, string, select (``options``), bool. Option lists starting with ``$`` are filled from the
business rules (``$clients``, ``$warehouses``, ``$carriers``, ``$verticals``).
"""

from __future__ import annotations

import copy
import re
from datetime import datetime, timedelta
from typing import Any

import numpy as np

SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "peak_surge", "name": "Peak surge",
        "description": "Order intake jumps for a few days (flash sale or holiday rush): pick backlogs, missed cutoffs, overtime.",
        "params": [
            {"name": "multiplier", "type": "number", "default": 2.5, "min": 1.2, "max": 6.0, "label": "Volume multiplier"},
            {"name": "days", "type": "int", "default": 3, "min": 1, "max": 14, "label": "Days"},
            {"name": "warehouse", "type": "select", "default": "ALL", "options": ["ALL", "$warehouses"], "label": "Warehouse"},
        ],
    },
    {
        "id": "onboard_client", "name": "Onboard a client",
        "description": "A new client signs: Salesforce deal closes, AX customer and WMS owner are created, items and first ASNs arrive.",
        "params": [
            {"name": "vertical", "type": "select", "default": "apparel", "options": ["$verticals"], "label": "Vertical"},
            {"name": "tier", "type": "select", "default": "M", "options": ["S", "M", "L", "XL"], "label": "Volume tier"},
            {"name": "warehouse", "type": "select", "default": "ATL1", "options": ["$warehouses"], "label": "Warehouse"},
            {"name": "currency", "type": "select", "default": "USD", "options": ["USD", "CAD", "MXN", "EUR", "GBP"],
             "label": "Billing currency"},
            {"name": "go_live_days", "type": "int", "default": 14, "min": 0, "max": 90, "label": "Days until go-live"},
            {"name": "name", "type": "string", "default": "", "max_length": 60, "label": "Client name (blank = generate)"},
        ],
    },
    {
        "id": "offboard_client", "name": "Offboard a client",
        "description": "A client gives notice: orders taper, stock ships out, the owner goes OFFBOARDING then INACTIVE, final invoice.",
        "params": [
            {"name": "client", "type": "select", "default": None, "options": ["$clients"], "label": "Client"},
            {"name": "notice_days", "type": "int", "default": 30, "min": 0, "max": 120, "label": "Notice period (days)"},
        ],
    },
    {
        "id": "rate_increase", "name": "Contract rate increase",
        "description": "Sales signs a rate amendment in Salesforce; billing may key it into the WMS on time, late or never.",
        "params": [
            {"name": "client", "type": "select", "default": None, "options": ["$clients"], "label": "Client"},
            {"name": "pct", "type": "number", "default": 4.0, "min": 0.5, "max": 15.0, "label": "Increase %"},
            {"name": "keyed_in_wms", "type": "select", "default": "late", "options": ["on_time", "late", "never"],
             "label": "Keyed in WMS"},
            {"name": "delay_days", "type": "int", "default": 21, "min": 1, "max": 120, "label": "Keying delay (days, if late)"},
        ],
    },
    {
        "id": "schema_drift_release", "name": "Schema-drift release",
        "description": "A WMS release adds columns, Salesforce gets a new custom field and a client changes its item-master CSV layout.",
        "params": [
            {"name": "systems", "type": "select", "default": "all", "options": ["all", "wms", "salesforce", "files"],
             "label": "Systems"},
        ],
    },
    {
        "id": "duplicate_file_resend", "name": "Duplicate file resend",
        "description": "A trading partner or client re-sends files that were already delivered.",
        "params": [
            {"name": "kind", "type": "select", "default": "all", "options": ["all", "edi", "files"], "label": "Files"},
            {"name": "count", "type": "int", "default": 3, "min": 1, "max": 20, "label": "Files to resend"},
        ],
    },
    {
        "id": "carrier_outage", "name": "Carrier feed outage",
        "description": "A carrier's tracking feed stops; events pile up and arrive late in one burst when it recovers.",
        "params": [
            {"name": "scac", "type": "select", "default": "UPSN", "options": ["$carriers"], "label": "Carrier"},
            {"name": "hours", "type": "int", "default": 12, "min": 1, "max": 72, "label": "Outage hours"},
        ],
    },
    {
        "id": "storm", "name": "Winter storm",
        "description": "Snow around a site: late inbound trailers, missed pickups, carrier weather exceptions.",
        "params": [
            {"name": "warehouse", "type": "select", "default": "CHI1", "options": ["$warehouses"], "label": "Warehouse"},
            {"name": "days", "type": "int", "default": 2, "min": 1, "max": 5, "label": "Days"},
            {"name": "severity", "type": "select", "default": "severe", "options": ["moderate", "severe"], "label": "Severity"},
        ],
    },
    {
        "id": "late_data_dump", "name": "Late data dump",
        "description": "Sensor gateways and carrier feeds flush buffered records long after they happened (late-arriving facts).",
        "params": [
            {"name": "source", "type": "select", "default": "all", "options": ["all", "iot", "carrier"], "label": "Source"},
            {"name": "hours_late", "type": "int", "default": 36, "min": 2, "max": 240, "label": "Hours late"},
        ],
    },
    {
        "id": "sf_account_merge", "name": "Salesforce account merge",
        "description": "Sales Ops merges duplicate Accounts: losers are deleted with MasterRecordId set; children move to the winner.",
        "params": [
            {"name": "client", "type": "select", "default": "AUTO", "options": ["AUTO", "$clients"], "label": "Client"},
            {"name": "count", "type": "int", "default": 1, "min": 1, "max": 5, "label": "Merges"},
        ],
    },
]
SCENARIO_IDS = tuple(s["id"] for s in SCENARIOS)

_NAME_FIRST = (
    "Alder", "Bramble", "Cinderleaf", "Driftwood", "Emberline", "Fablewood", "Garnet", "Heronsgate", "Indigo Bay",
    "Juniper", "Larkspur", "Meridian", "Nettlefield", "Onyxridge", "Pinecrest", "Quillmark", "Rowan", "Saffron",
    "Thistledown", "Umberfield", "Vesper", "Willowmere", "Yarrow", "Zephyr", "Coppervale", "Silverbirch", "Tamarind",
    "Ashgrove", "Bluefen", "Cedarline",
)
_NAME_SECOND = {
    "apparel": ("Apparel", "Threads", "Knitwear", "Outfitters", "Clothing Co."),
    "electronics": ("Audio", "Devices", "Electronics", "Labs"),
    "food_bev": ("Provisions", "Pantry", "Foods", "Creamery", "Roasters"),
    "health_beauty": ("Skin", "Botanicals", "Beauty", "Apothecary"),
    "home_goods": ("Home", "Living", "Interiors", "Goods"),
    "auto_parts": ("Auto Supply", "Parts Co.", "Motorworks"),
    "pet": ("Pet Co.", "Pet Supply", "Paws"),
    "supplements": ("Nutrition", "Wellness", "Vitamins"),
    "b2b_general": ("Supply", "Industrial", "Distribution"),
}


def get(scenario_id: str) -> dict[str, Any]:
    for spec in SCENARIOS:
        if spec["id"] == scenario_id:
            return spec
    raise KeyError(f"unknown scenario {scenario_id!r}")


def _expand(options: list[str], rules: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for opt in options:
        if opt == "$clients":
            out += [c["code"] for c in rules.get("clients", []) if c.get("churn_month") is None]
        elif opt == "$warehouses":
            out += [w["code"] for w in rules.get("warehouses", [])]
        elif opt == "$carriers":
            out += [c["scac"] for c in rules.get("carriers", [])]
        elif opt == "$verticals":
            out += list(rules.get("verticals", {}))
        else:
            out.append(opt)
    return out


def catalog(rules: dict[str, Any]) -> list[dict[str, Any]]:
    """Scenario list for the API, with dynamic option lists resolved."""
    out = []
    for spec in SCENARIOS:
        spec = copy.deepcopy(spec)
        for p in spec["params"]:
            if "options" in p:
                p["options"] = _expand(p["options"], rules)
                if p["default"] is None and p["options"]:
                    p["default"] = p["options"][0]
        out.append(spec)
    return out


def validate(spec: dict[str, Any], params: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
    """Coerce and check params against the spec. Raises ValueError with a message for the user."""
    clean: dict[str, Any] = {}
    for p in spec["params"]:
        name = p["name"]
        value = params.get(name, p["default"])
        if p["type"] == "int":
            value = int(value)
        elif p["type"] == "number":
            value = float(value)
        elif p["type"] == "bool":
            value = value if isinstance(value, bool) else str(value).strip().lower() in ("1", "true", "yes", "on")
        elif p["type"] == "string":
            value = "" if value is None else str(value).strip()
            if len(value) > p.get("max_length", 200):
                raise ValueError(f"{name}: at most {p.get('max_length', 200)} characters")
        elif p["type"] == "select":
            options = _expand(p["options"], rules)
            if value is None:
                raise ValueError(f"{name} is required")
            if str(value) not in options:
                raise ValueError(f"{name} must be one of {options}")
            value = str(value)
        if "min" in p and value < p["min"] or "max" in p and value > p["max"]:
            raise ValueError(f"{name} must be between {p['min']} and {p['max']}")
        clean[name] = value
    return clean


def _first_word(name: str) -> str:
    return re.split(r"[\s&]+", name.strip().lower())[0]


def make_client(rules: dict[str, Any], params: dict[str, Any], start: datetime, history_end: datetime,
                rng: np.random.Generator) -> dict[str, Any]:
    """A client dict shaped like ``business_rules.clients[]`` (+ ``dynamic``, ``go_live_date``) for onboard_client."""
    clients = rules.get("clients", [])
    names = {c["name"].lower() for c in clients}
    firsts = {_first_word(c["name"]) for c in clients}
    codes = {c["code"] for c in clients}
    vertical = params["vertical"]
    name = params.get("name") or ""
    if name:
        if name.lower() in names:
            raise ValueError(f"a client called {name!r} already exists")
    else:
        candidates = [
            f"{first} {second}" for first in _NAME_FIRST for second in _NAME_SECOND.get(vertical, ("Brands",))
            if _first_word(first) not in firsts and f"{first} {second}".lower() not in names
        ]
        if not candidates:
            raise ValueError("no unused client name left; pass a name")
        name = candidates[int(rng.integers(0, len(candidates)))]
    letters = re.sub(r"[^A-Z]", "", name.upper())
    code = letters[:7] or "NEWCLNT"
    n = 1
    while code in codes:
        code = f"{letters[:6]}{n}"
        n += 1
    warehouse = params["warehouse"]
    entity = next((w["legal_entity"] for w in rules.get("warehouses", []) if w["code"] == warehouse), "us01")
    go_live = (start + timedelta(days=int(params["go_live_days"]))).date()
    onboard_month = (go_live.year * 12 + go_live.month) - (history_end.year * 12 + history_end.month)
    return {
        "code": code, "name": name, "vertical": vertical, "tier": params["tier"], "currency": params["currency"],
        "entity": entity, "warehouses": [warehouse], "onboard_month": onboard_month,
        "dynamic": True, "go_live_date": go_live.isoformat(),
    }
