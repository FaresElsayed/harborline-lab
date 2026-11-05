"""Generate the data dictionary, the ER diagrams and the plain-text ERDs of handbook/source-systems.html.

Inputs:
* the DDL in simulator/harborline/schema/{wms,ax2012,iot}.sql and simulator/harborline/tracker/tracker.sql (column
  types, keys, inline `--` comments and COMMENT ON statements) and schema/salesforce_objects.yaml;
* the payloads, file layouts and X12 segments the simulator writes for the carrier API, EDI and the business files,
  described below next to the module that writes them (tests/test_erd.py checks every name against that module).

Outputs:
* every block between `<!-- BEGIN GENERATED: <name> -->` and `<!-- END GENERATED: <name> -->` in
  handbook/source-systems.html: the data dictionary and the inline-SVG ER diagrams (`er-*`);
* handbook/erd/<source>.mmd: one Mermaid erDiagram per source with every entity and column and the relationships the
  diagrams draw, for pasting into a chat or a README.

sim_internal.sql is deliberately left out: it is the simulator's private store.

    python -m harborline.tools.build_data_dictionary [--root /path/to/harborline-lab]
"""

from __future__ import annotations

import argparse
import itertools
import re
from collections import Counter
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Any

import yaml

DEFAULT_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = Path(__file__).resolve().parents[1]
SCHEMA_DIR = PACKAGE_DIR / "schema"
TRACKER_SQL = PACKAGE_DIR / "tracker" / "tracker.sql"
KEYWORDS = {"NOT", "NULL", "PRIMARY", "REFERENCES", "DEFAULT", "UNIQUE", "CHECK", "CONSTRAINT", "GENERATED"}


# ----------------------------------------------------------------------------------------------- DDL model
@dataclass
class Column:
    name: str
    type: str
    not_null: bool = False
    pk: bool = False
    unique: bool = False
    references: str | None = None
    default: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class Table:
    name: str
    columns: list[Column] = field(default_factory=list)
    comment: str = ""
    pk: list[str] = field(default_factory=list)
    uniques: list[list[str]] = field(default_factory=list)

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)


@dataclass
class Schema:
    database: str
    schema: str
    header: list[str]
    tables: list[Table]


def split_comment(line: str) -> tuple[str, str]:
    in_quote = False
    for i, ch in enumerate(line):
        if ch == "'":
            in_quote = not in_quote
        elif not in_quote and line.startswith("--", i):
            return line[:i], line[i + 2 :].strip()
    return line, ""


def split_top_level(code: str) -> list[str]:
    parts, depth, buf, in_quote = [], 0, [], False
    for ch in code:
        if ch == "'":
            in_quote = not in_quote
        if not in_quote:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                parts.append("".join(buf).strip())
                buf = []
                continue
        buf.append(ch)
    parts.append("".join(buf).strip())
    return [p for p in parts if p]


def parse_column(item: str) -> Column:
    tokens = item.split()
    name, rest = tokens[0], tokens[1:]
    type_tokens: list[str] = []
    while rest and rest[0].upper() not in KEYWORDS:
        type_tokens.append(rest.pop(0))
    tail = " ".join(rest)
    up = tail.upper()
    col = Column(name=name, type=" ".join(type_tokens))
    col.pk = "PRIMARY KEY" in up
    col.not_null = "NOT NULL" in up or col.pk
    col.unique = bool(re.search(r"\bUNIQUE\b", up))
    ref = re.search(r"REFERENCES\s+([\w.]+)", tail, re.IGNORECASE)
    col.references = ref.group(1) if ref else None
    default = re.search(r"DEFAULT\s+('[^']*'(?:::\w+)?|[^\s,]+)", tail, re.IGNORECASE)
    col.default = default.group(1) if default else None
    return col


def parse_constraint(item: str, table: Table) -> bool:
    m = re.match(r"(PRIMARY\s+KEY|UNIQUE)\s*\(([^)]*)\)", item, re.IGNORECASE)
    if not m:
        return re.match(r"(CONSTRAINT|FOREIGN\s+KEY|CHECK)\b", item, re.IGNORECASE) is not None
    cols = [c.strip() for c in m.group(2).split(",")]
    if m.group(1).upper().startswith("PRIMARY"):
        table.pk = cols
        for c in cols:
            if col := table.column(c):
                col.pk = col.not_null = True
    else:
        table.uniques.append(cols)
    return True


def unquote(sql_literal: str) -> str:
    return sql_literal[1:-1].replace("''", "'")


def parse_ddl(path: Path, database: str) -> Schema:
    text = path.read_text(encoding="utf-8")
    header: list[str] = []
    tables: dict[str, Table] = {}
    schema = ""
    current: Table | None = None
    last_col: Column | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not tables and current is None and stripped.startswith("--"):
            header.append(stripped[2:].strip())
            continue
        if m := re.match(r"CREATE SCHEMA IF NOT EXISTS (\w+)", stripped, re.IGNORECASE):
            schema = schema or m.group(1)
            continue
        if m := re.match(r"CREATE TABLE (?:IF NOT EXISTS )?([\w.]+)\s*\((.*)$", stripped, re.IGNORECASE):
            current = Table(name=m.group(1).split(".")[-1])
            tables[current.name] = current
            last_col = None
            line = m.group(2)
            stripped = line.strip()
            if not stripped:
                continue
        if current is not None:
            if stripped.startswith(");") or stripped == ")":
                current = None
                continue
            code, comment = split_comment(line)
            items = split_top_level(code)
            for item in items:
                if not parse_constraint(item, current):
                    last_col = parse_column(item)
                    current.columns.append(last_col)
            if comment and last_col is not None:
                last_col.notes.append(comment)
            continue
        if m := re.match(r"COMMENT ON TABLE ([\w.]+) IS ('(?:[^']|'')*');", stripped, re.IGNORECASE):
            if t := tables.get(m.group(1).split(".")[-1]):
                t.comment = unquote(m.group(2))
        elif m := re.match(r"COMMENT ON COLUMN ([\w.]+)\.(\w+) IS ('(?:[^']|'')*');", stripped, re.IGNORECASE):
            t = tables.get(m.group(1).split(".")[-1])
            if t and (c := t.column(m.group(2))):
                c.notes.append(unquote(m.group(3)))
    return Schema(database=database, schema=schema, header=header, tables=list(tables.values()))


# ----------------------------------------------------------------------------------------------- HTML: dictionary
def key_marks(col: Column, table: Table) -> str:
    marks = []
    if col.pk:
        marks.append('<span class="hb-chip hb-chip--accent">PK</span>')
    if col.references:
        marks.append(f'<span class="hb-chip" title="References {escape(col.references)}">FK</span>')
    if col.unique or any(u == [col.name] for u in table.uniques):
        marks.append('<span class="hb-chip">unique</span>')
    return " ".join(marks)


def column_notes(col: Column) -> str:
    bits = [escape(n) for n in col.notes]
    if col.references:
        bits.append(f"References <code>{escape(col.references)}</code>.")
    if col.default is not None:
        bits.append(f"Default <code>{escape(col.default)}</code>.")
    return " ".join(bits)


def table_html(system: str, table: Table) -> str:
    rows = "\n".join(
        f'<tr><td><code>{escape(c.name)}</code></td><td class="dd-type">{escape(c.type)}</td>'
        f"<td>{'' if c.not_null else 'yes'}</td><td>{key_marks(c, table)}</td><td>{column_notes(c)}</td></tr>"
        for c in table.columns
    )
    extras = []
    if len(table.pk) > 1:
        extras.append(f"Primary key ({', '.join(table.pk)}).")
    for u in table.uniques:
        if len(u) > 1:
            extras.append(f"Unique ({', '.join(u)}).")
    extra = f'<p class="small muted dd-extra">{escape(" ".join(extras))}</p>' if extras else ""
    comment = f'<span class="dd-comment">{escape(table.comment)}</span>' if table.comment else ""
    return (
        f'<details class="dd-table" id="dd-{system}-{table.name}" data-dd-text="{escape(table.name.lower())}">'
        f'<summary><code class="dd-name">{escape(table.name)}</code>{comment}'
        f'<span class="dd-count">{len(table.columns)} columns</span></summary>'
        f'<div class="hb-table-wrap"><table class="hb-table hb-table--dense dd-cols"><thead><tr>'
        f"<th>Column</th><th>Type</th><th>Nullable</th><th>Key</th><th>Notes</th></tr></thead><tbody>{rows}"
        f"</tbody></table></div>{extra}</details>"
    )


def header_html(lines: list[str]) -> str:
    """DDL header comment (minus its title line) as paragraphs, with `* ` lines as a list."""
    out: list[str] = []
    para: list[str] = []
    items: list[str] = []

    def flush() -> None:
        if para:
            out.append(f"<p>{escape(' '.join(para))}</p>")
            para.clear()
        if items:
            out.append("<ul>" + "".join(f"<li>{escape(i)}</li>" for i in items) + "</ul>")
            items.clear()

    for raw in lines[1:]:
        line = raw.strip()
        if line.startswith("* "):
            if para:
                flush()
            items.append(line[2:])
        elif not line:
            flush()
        else:
            if items:
                flush()
            para.append(line)
    flush()
    return f'<div class="dd-header">{"".join(out)}</div>' if out else ""


def schema_html(system: str, s: Schema) -> str:
    tables = "\n".join(table_html(system, t) for t in s.tables)
    return (
        f'<p class="small muted">Database <code>{escape(s.database)}</code>, schema <code>{escape(s.schema)}</code>, '
        f"{len(s.tables)} tables, {sum(len(t.columns) for t in s.tables)} columns. Generated from "
        f"<code>simulator/harborline/schema/{escape(system)}.sql</code>.</p>"
        + header_html(s.header)
        + f'<div class="dd-list">{tables}</div>'
    )


def sf_field_notes(f: dict[str, Any]) -> str:
    bits = []
    if f.get("label"):
        bits.append(escape(str(f["label"])) + ".")
    if f.get("referenceTo"):
        bits.append("References " + ", ".join(f"<code>{escape(r)}</code>" for r in f["referenceTo"]) + ".")
    if f.get("relationship"):
        bits.append(escape(f["relationship"]).capitalize() + ".")
    if f.get("values"):
        bits.append("Values: " + ", ".join(escape(str(v)) for v in f["values"]) + ".")
    if f.get("updateable"):
        bits.append("<strong>Writable by the integration user.</strong>")
    if f.get("nillable") is False:
        bits.append("Required.")
    return " ".join(bits)


def sf_type(f: dict[str, Any]) -> str:
    t = str(f["type"])
    if f.get("length"):
        t += f"({f['length']})"
    elif f.get("precision") is not None:
        t += f"({f['precision']},{f.get('scale', 0)})"
    return t


def salesforce_html(spec: dict[str, Any]) -> str:
    sysf = ", ".join(f"<code>{escape(f['name'])}</code>" for f in spec["system_fields"])
    blocks = []
    for name, obj in spec["objects"].items():
        fields = list(obj["fields"])
        if obj.get("has_owner"):
            fields.insert(0, {"name": "OwnerId", "type": "reference", "referenceTo": ["User"], "label": "Owner"})
        rows = "\n".join(
            f'<tr><td><code>{escape(f["name"])}</code></td><td class="dd-type">{escape(sf_type(f))}</td>'
            f"<td>{'custom' if f.get('custom') or f['name'].endswith('__c') else ''}</td><td>{sf_field_notes(f)}</td></tr>"
            for f in fields
        )
        notes = [f"Key prefix <code>{escape(str(obj['key_prefix']))}</code>"]
        if obj.get("custom"):
            notes.append("custom object")
        if obj.get("queryable_only"):
            notes.append("query only")
        if nf := obj.get("name_field"):
            notes.append(f"Name is an auto-number ({escape(nf.get('format', ''))})")
        blocks.append(
            f'<details class="dd-table" id="dd-salesforce-{escape(name)}" data-dd-text="{escape(name.lower())}">'
            f'<summary><code class="dd-name">{escape(name)}</code><span class="dd-comment">{"; ".join(notes)}</span>'
            f'<span class="dd-count">{len(fields)} fields</span></summary>'
            f'<div class="hb-table-wrap"><table class="hb-table hb-table--dense dd-cols"><thead><tr>'
            f"<th>Field</th><th>Type</th><th></th><th>Notes</th></tr></thead><tbody>{rows}</tbody></table></div>"
            f"</details>"
        )
    return (
        f'<p class="small muted">API version {escape(str(spec["api_version"]))}, {len(spec["objects"])} objects. '
        f"Generated from <code>simulator/harborline/schema/salesforce_objects.yaml</code>. Every object also has "
        f"{sysf}. Objects with an owner have <code>OwnerId</code>.</p>"
        f'<div class="dd-list">{"".join(blocks)}</div>'
    )


# ----------------------------------------------------------------------------------------------- entity catalog
# Every source as entities with fields: tables (DDL), objects (Salesforce YAML) and, for the sources that are not
# databases, the payloads, sheets and segment groups the simulator writes. Diagrams and the .mmd files read this.
SOURCES = {
    "wms": "Harborline WMS: database wms, schema wms (simulator/harborline/schema/wms.sql)",
    "ax2012": "Microsoft Dynamics AX 2012: database ax2012, schema dbo (simulator/harborline/schema/ax2012.sql)",
    "salesforce": "Salesforce objects, REST API v67.0 (simulator/harborline/schema/salesforce_objects.yaml)",
    "iot": "IoT sensor gateway: database iot, schema telemetry (simulator/harborline/schema/iot.sql)",
    "carrier_api": "Carrier tracking API http://localhost:8080/carrier/v1 (simulator/harborline/api/carrier_api.py) "
    "and its history export (simulator/harborline/sim/files.py)",
    "edi": "EDI X12 004010 files in landing/edi (simulator/harborline/sim/edi.py)",
    "business_files": "Business files in landing/files (simulator/harborline/sim/files.py)",
    "tracker": "Harborline Tracker: database tracker, schema tracker (simulator/harborline/tracker/tracker.sql)",
}
PREFIX = {
    "wms": "wms",
    "ax2012": "ax",
    "salesforce": "sf",
    "iot": "iot",
    "carrier_api": "carrier",
    "edi": "edi",
    "business_files": "file",
    "tracker": "tracker",
}
VIRTUAL = ("carrier_api", "edi", "business_files")


@dataclass
class Field:
    name: str  # identifier; also the Mermaid attribute name
    type: str
    label: str = ""  # display text when it differs from the name (sheet headers, X12 element references)
    pk: bool = False
    unique: bool = False
    nullable: bool = True
    refs: tuple[str, ...] = ()  # declared reference: table or Salesforce object(s)
    note: str = ""
    evidence: str = ""  # files, payloads, segments: text the producing module must contain

    @property
    def text(self) -> str:
        return self.label or self.name


@dataclass
class Entity:
    source: str
    name: str
    fields: list[Field]
    title: str = ""  # box title of a file, payload or segment group
    note: str = ""
    module: str = ""  # files, payloads, segments: producing module, relative to the harborline package

    def get(self, name: str) -> Field | None:
        return next((f for f in self.fields if f.name == name), None)

    @property
    def display(self) -> str:
        return self.title or self.name


Catalog = dict[str, dict[str, Entity]]


def ddl_entities(source: str, s: Schema) -> dict[str, Entity]:
    out = {}
    for t in s.tables:
        single = {u[0] for u in t.uniques if len(u) == 1}
        fields = [
            Field(
                c.name,
                c.type,
                pk=c.pk,
                unique=c.unique or c.name in single,
                nullable=not c.not_null,
                refs=(c.references.split(".")[-1],) if c.references else (),
            )
            for c in t.columns
        ]
        out[t.name] = Entity(source, t.name, fields, note=t.comment)
    return out


def sf_entities(spec: dict[str, Any]) -> dict[str, Entity]:
    def make(f: dict[str, Any]) -> Field:
        name = str(f["name"])
        return Field(
            name,
            sf_type(f),
            pk=name == "Id",
            nullable=name != "Id" and f.get("nillable") is not False,
            refs=tuple(f.get("referenceTo") or ()),
        )

    system = [make(f) for f in spec["system_fields"]]
    out = {}
    for name, obj in spec["objects"].items():
        fields = [system[0]]
        if obj.get("has_owner"):
            fields.append(Field("OwnerId", "reference", nullable=False, refs=("User",)))
        fields += [make(f) for f in obj["fields"]] + system[1:]
        out[name] = Entity("salesforce", name, fields, note=f"key prefix {obj['key_prefix']}")
    return out


def F(
    name: str, type_: str = "string", label: str = "", note: str = "", ev: str | None = None, pk: bool = False
) -> Field:
    """A field of a payload, sheet or segment group; `ev` is the text that proves the module writes it."""
    return Field(name, type_, label=label, note=note, pk=pk, evidence=(label or name) if ev is None else ev)


def header_field(label: str, type_: str = "string", note: str = "", ev: str | None = None) -> Field:
    """A column of a sheet or file, named after its header (Mermaid needs a plain identifier)."""
    name = re.sub(r"[^A-Za-z0-9]+", "_", label.replace("%", "pct")).strip("_")
    return Field(
        name, type_, label="" if name == label else label, note=note, evidence=f'"{label}"' if ev is None else ev
    )


def carrier_entities() -> dict[str, Entity]:
    api, files = "api/carrier_api.py", "sim/files.py"

    def key(name: str, type_: str = "string", note: str = "", pk: bool = False) -> Field:
        return F(name, type_, note=note, ev=f'"{name}"', pk=pk)

    history = [
        ("event_id", "integer", ""),
        ("tracking_nbr", "string", ""),
        ("scac", "string", ""),
        ("event_code", "string", ""),
        ("event_ts_utc", "string", "when it happened, UTC"),
        ("event_local_ts", "string", "when it happened, carrier's local offset"),
        ("city", "string", ""),
        ("state_prov", "string", ""),
        ("country_code", "string", ""),
        ("description", "string", ""),
        ("exception_code", "string", ""),
        ("received_at_utc", "string", "when the aggregator received it"),
        ("is_correction", "boolean", ""),
    ]
    ents = [
        Entity(
            "carrier_api",
            "tracking_events_page",
            [
                key("events", "array", "tracking events ordered by received_at, event_id"),
                key("next_cursor", note="send back as cursor= for the next page"),
                key("has_more", "boolean"),
            ],
            title="GET /tracking-events",
            note="GET /carrier/v1/tracking-events?since=&cursor=&limit= (limit 1 to 500), header X-API-Key",
            module=api,
        ),
        Entity(
            "carrier_api",
            "tracking_event",
            [
                key("event_id", "integer", pk=True),
                key("tracking_nbr"),
                key("scac"),
                key(
                    "event_code",
                    note="LABEL_CREATED, PICKED_UP, IN_TRANSIT, OUT_FOR_DELIVERY, DELIVERED, EXCEPTION, "
                    "RETURN_TO_SENDER",
                ),
                key("event_local_ts", note="ISO-8601 with the carrier's local UTC offset"),
                key("description"),
                key("exception_code", note="WEATHER, ADDRESS, DAMAGED, REFUSED, CARRIER_DELAY; null otherwise"),
                F("location_city", label="location.city", ev='"city"'),
                F("location_state_prov", label="location.state_prov", ev='"state_prov"'),
                F("location_country_code", label="location.country_code", ev='"country_code"'),
                key("received_at", note="UTC, when the aggregator received it"),
                key("is_correction", "boolean"),
            ],
            title="tracking event (JSON)",
            note="one element of events[]",
            module=api,
        ),
        Entity(
            "carrier_api",
            "shipment_tracking",
            [
                key("tracking_nbr"),
                key("scac"),
                key("status", note="event_code of the latest event"),
                key("events", "array", "every event of the package"),
            ],
            title="GET /shipments/{nbr}",
            note="GET /carrier/v1/shipments/{tracking_nbr}",
            module=api,
        ),
        Entity(
            "carrier_api",
            "tracking_history_csv",
            [F(n, t, note=note) for n, t, note in history],
            title="history export .csv.gz",
            note="landing/files/carrier/history_export/tracking_events_<yyyymm>.csv.gz: one file per month of events "
            "older than the API's 90-day retention",
            module=files,
        ),
    ]
    return {e.name: e for e in ents}


def edi_entities() -> dict[str, Entity]:
    m = "sim/edi.py"
    name_ev = "{kind}_{code}_{ts:%Y%m%d%H%M%S}_{ctrl:09d}.edi"

    def el(ref: str, note: str, ev: str, qual: str = "", type_: str = "string") -> Field:
        """An X12 element: `W05-03`, `ISA13`, or qualified by the segment's first element: `N1-04 (DE)`."""
        seg, _, pos = ref.partition("-")
        name = "_".join(p for p in (seg, qual, pos) if p)
        return Field(name, type_, label=f"{ref} ({qual})" if qual else ref, note=note, evidence=ev)

    def group(name: str, title: str, note: str, fields: list[Field]) -> Entity:
        return Entity("edi", name, fields, title=title, note=note, module=m)

    n4 = [("N4-01", "city"), ("N4-02", "state or province"), ("N4-03", "postal code"), ("N4-04", "country")]
    ents = [
        group(
            "edi_file",
            "file in landing/edi",
            "landing/edi/<inbound|outbound>/<set>/<CLIENT>/<set>_<CLIENT>_<yyyyMMddHHmmss>_<ISA13>.edi, one "
            "interchange per file; older traffic in landing/edi/archive/.../<set>_<CLIENT>_<yyyyMM>.zip",
            [
                Field(
                    "direction", "string", label="<direction>", note="inbound or outbound folder", evidence='"inbound"'
                ),
                Field("set_id", "string", label="<set>", note="940, 856, 945 or 997", evidence=name_ev),
                Field("client", "string", label="<CLIENT>", note="the WMS owner code", evidence=name_ev),
                Field(
                    "file_ts", "string", label="<yyyyMMddHHmmss>", note="when the file was written", evidence=name_ev
                ),
                Field("isa13", "string", label="<ISA13>", note="interchange control number", evidence=name_ev),
            ],
        ),
        group(
            "edi_interchange",
            "ISA … IEA interchange",
            "the envelope of every file: ISA is fixed width (106 characters with the terminator)",
            [
                el("ISA05", "sender id qualifier: 01 (DUNS) or ZZ", "ISA*00*"),
                el("ISA06", "sender id", "ISA*00*"),
                el("ISA07", "receiver id qualifier", "ISA*00*"),
                el("ISA08", "receiver id (HARBORLINE3PL on inbound files)", "ISA*00*"),
                el("ISA09", "date yymmdd", "ISA*00*"),
                el("ISA10", "time HHmm", "ISA*00*"),
                el("ISA12", "version 00401", "ISA*00*"),
                el("ISA13", "interchange control number, 9 digits", "ISA*00*"),
                el("ISA14", "acknowledgment requested: 1 on 940 and 856", "ISA*00*"),
                el("ISA15", "usage: P", "ISA*00*"),
                el("IEA01", "number of groups", "IEA*1*"),
                el("IEA02", "repeats ISA13", "IEA*1*"),
            ],
        ),
        group(
            "edi_group",
            "GS … GE group",
            "functional group inside the interchange",
            [
                el("GS01", "functional id: OW 940, SH 856, SW 945, FA 997", "GS*{fid}"),
                el("GS02", "sender code", "GS*{fid}"),
                el("GS03", "receiver code", "GS*{fid}"),
                el("GS04", "date CCYYMMDD", "GS*{fid}"),
                el("GS05", "time HHmm", "GS*{fid}"),
                el("GS06", "group control number", "GS*{fid}"),
                el("GS07", "X", "GS*{fid}"),
                el("GS08", "004010", "GS*{fid}"),
                el("GE01", "number of transaction sets", "GE*"),
                el("GE02", "repeats GS06", "GE*"),
            ],
        ),
        group(
            "edi_set",
            "ST … SE transaction set",
            "one document (an order, an ASN, a shipping advice or an acknowledgment)",
            [
                el("ST01", "940, 856, 945 or 997", "ST*{code}"),
                el("ST02", "set control number 0001, 0002, …", "ST*{code}"),
                el("SE01", "segments from ST to SE", "SE*"),
                el("SE02", "repeats ST02", "SE*"),
            ],
        ),
        group(
            "edi997_group",
            "997 AK1 … AK9",
            "functional acknowledgment, one per inbound group (outbound/997)",
            [
                el("AK1-01", "functional id acknowledged: OW or SH", "AK1*"),
                el("AK1-02", "GS06 of the inbound group", "AK1*"),
                el("AK9-01", "A accepted, E errors", "AK9*"),
                el("AK9-02", "sets included", "AK9*"),
                el("AK9-03", "sets received", "AK9*"),
                el("AK9-04", "sets accepted", "AK9*"),
            ],
        ),
        group(
            "edi997_set",
            "997 AK2 + AK5 per set",
            "one pair per acknowledged transaction set",
            [
                el("AK2-01", "set id acknowledged", "AK2*"),
                el("AK2-02", "ST02 of the inbound set", "AK2*"),
                el("AK5-01", "A accepted, E error", "AK5*"),
                el("AK5-02", "4 when E", "AK5*E*4"),
            ],
        ),
        group(
            "edi940_header",
            "940 header",
            "warehouse shipping order (inbound/940), segments before the LX loop and the W76 total",
            [
                el("W05-01", "N (original)", "W05*N*"),
                el("W05-02", "depositor order number SO-nnnnnnn", "W05*N*"),
                el("W05-03", "client PO", "W05*N*"),
                el("N1-02", "client name", "N1*DE*", "DE"),
                el("N1-04", "client code", "N1*DE*", "DE"),
                el("N1-02", "warehouse name", "N1*WH*", "WH"),
                el("N1-04", "warehouse code", "N1*WH*", "WH"),
                el("N1-02", "ship-to name", "N1*ST*", "ST"),
                el("N1-04", "store number (qualifier 92), B2B only", "N1*ST*", "ST"),
                el("N3-01", "ship-to street", "N3*"),
                *[el(r, n, "N4*") for r, n in n4],
                el("N9-02", "client PO again", "N9*PO*", "PO"),
                el("G62-02", "requested ship date", "G62*10*", "10"),
                el("W66-01", "PP", "W66*PP*"),
                el("W66-02", "M (LTL) or U (parcel)", "W66*PP*"),
                el("W66-10", "carrier SCAC", "W66*PP*"),
                el("W76-01", "total units", "W76*"),
                el("W76-02", "total weight", "W76*"),
                el("W76-03", "LB", "W76*"),
            ],
        ),
        group(
            "edi940_line",
            "940 line (LX loop)",
            "one LX loop per order line",
            [
                el("LX-01", "line sequence 1, 2, …", "LX*"),
                el("W01-01", "quantity ordered", "W01*"),
                el("W01-02", "unit of measure", "W01*"),
                el("W01-03", "UPC", "W01*"),
                el("W01-04", "VN", "W01*"),
                el("W01-05", "SKU", "W01*"),
                el("G69-01", "description", "G69*"),
            ],
        ),
        group(
            "edi945_header",
            "945 header",
            "warehouse shipping advice (outbound/945) for a shipped EDI order",
            [
                el("W06-01", "F (final)", "W06*F*"),
                el("W06-02", "depositor order number (W05-02 of the 940)", "W06*F*"),
                el("W06-03", "ship date", "W06*F*"),
                el("W06-04", "shipment number", "W06*F*"),
                el("W06-05", "WMS order number", "W06*F*"),
                el("W06-06", "client PO", "W06*F*"),
                el("N1-02", "ship-to name", "N1*ST*", "ST"),
                el("N3-01", "ship-to street", "N3*"),
                *[el(r, n, "N4*") for r, n in n4],
                el("N1-02", "warehouse name", "N1*SF*", "SF"),
                el("N1-04", "warehouse code", "N1*SF*", "SF"),
                el("N9-02", "bill of lading (LTL)", "N9*BM*", "BM"),
                el("N9-02", "tracking number", "N9*CN*", "CN"),
                el("G62-02", "shipped date", "G62*11*", "11"),
                el("W27-01", "M (LTL) or U (parcel)", "W27*"),
                el("W27-02", "SCAC", "W27*"),
                el("W03-01", "units shipped", "W03*"),
                el("W03-02", "weight", "W03*"),
                el("W03-03", "weight unit", "W03*"),
            ],
        ),
        group(
            "edi945_line",
            "945 line (LX loop)",
            "one LX loop per shipped order line",
            [
                el("LX-01", "line sequence 1, 2, …", "LX*"),
                el("W12-01", "CC complete, PR partial, BP backordered", "W12*"),
                el("W12-02", "quantity ordered", "W12*"),
                el("W12-03", "quantity shipped", "W12*"),
                el("W12-04", "difference", "W12*"),
                el("W12-05", "EA", "W12*"),
                el("W12-06", "UPC", "W12*"),
                el("W12-07", "VN", "W12*"),
                el("W12-08", "SKU", "W12*"),
                el("W12-09", "lot, when known", "W12*"),
            ],
        ),
        group(
            "edi856_shipment",
            "856 shipment (HL S)",
            "advance ship notice (inbound/856): header and shipment level",
            [
                el("BSN-01", "00 (original)", "BSN*00*"),
                el("BSN-02", "ASN number", "BSN*00*"),
                el("BSN-03", "date", "BSN*00*"),
                el("BSN-04", "time", "BSN*00*"),
                el("DTM-02", "shipped date", "DTM*011*", "011"),
                el("DTM-02", "expected date", "DTM*017*", "017"),
                el("HL-01", "1", '"HL*1**S"'),
                el("HL-03", "S (shipment)", '"HL*1**S"'),
                el("TD1-02", "pallets", "TD1*PLT*"),
                el("TD1-07", "gross weight, LB", "TD1*PLT*"),
                el("TD5-03", "carrier SCAC", "TD5**2*"),
                el("REF-02", "bill of lading", "REF*BM*", "BM"),
                el("N1-02", "supplier name", "N1*SF*", "SF"),
                el("N1-02", "warehouse name", "N1*ST*", "ST"),
                el("N1-04", "warehouse code", "N1*ST*", "ST"),
                el("CTT-01", "number of HL segments", "CTT*"),
            ],
        ),
        group(
            "edi856_order",
            "856 order (HL O)",
            "order level under the shipment",
            [
                el("HL-01", "2", '"HL*2*1*O"'),
                el("HL-02", "1, the shipment level", '"HL*2*1*O"'),
                el("HL-03", "O (order)", '"HL*2*1*O"'),
                el("PRF-01", "PO number (the ASN number with ASN- replaced by PO)", "PRF*"),
            ],
        ),
        group(
            "edi856_item",
            "856 item (HL I)",
            "one item level per ASN line",
            [
                el("HL-01", "3, 4, …", '*2*I"'),
                el("HL-02", "2, the order level", '*2*I"'),
                el("HL-03", "I (item)", '*2*I"'),
                el("LIN-03", "SKU", "LIN**VN*", "VN"),
                el("LIN", "UPC, the element after UP, when known", "*UP*", "UP"),
                el("LIN", "lot, the element after LT, when lot-controlled", "*LT*", "LT"),
                el("SN1-02", "quantity", "SN1**"),
                el("SN1-03", "unit of measure", "SN1**"),
                el("DTM-02", "expiry date", "DTM*036*", "036"),
            ],
        ),
    ]
    return {e.name: e for e in ents}


def file_entities() -> dict[str, Entity]:
    from harborline.sim import files as sim_files  # item-master headers come straight from the writer

    m = "sim/files.py"
    rc = "landing/files/carriers/rate_cards/<SCAC>_rates_<yyyymmdd>.xlsx"
    wm = "landing/files/facilities/warehouse_master_<yyyymmdd>.xlsx"
    budget = "landing/files/finance/budget/budget_FY<yyyy>.xlsx (and budget_FY<yyyy>_reforecast.xlsx)"
    diesel = "landing/files/reference/diesel_weekly.csv"
    sources = "landing/files/reference/signal_sources.csv"

    def cover(label: str, type_: str = "string", note: str = "") -> Field:
        return header_field(label, type_, note, ev=f'("{label}"')

    def ent(name: str, title: str, note: str, fields: list[Field]) -> Entity:
        return Entity("business_files", name, fields, title=title, note=note, module=m)

    numeric = {"unit_weight", "length", "width", "height", "unit_value"}
    integer = {"case_qty", "cases_per_pallet", "shelf_life_days"}
    styles = [s for s in sim_files.HEADERS if s != "snake"]
    item_cols = [
        Field(
            c,
            "number" if c in numeric else "integer" if c in integer else "date" if c == "last_updated" else "string",
            note="other header styles: " + ", ".join(sim_files.HEADERS[s][c] for s in styles),
            evidence=f'"{c}"',
        )
        for c in sim_files.CANON
    ]
    sites = [
        ("Site Code", "string", "the warehouse code"),
        ("Site Name", "string", ""),
        ("Status", "string", "ACTIVE or PLANNED"),
        ("Street", "string", ""),
        ("City", "string", ""),
        ("State/Prov", "string", ""),
        ("Postal Code", "string", ""),
        ("Country", "string", ""),
        ("Latitude", "number", ""),
        ("Longitude", "number", ""),
        ("Time Zone", "string", "IANA zone"),
        ("Legal Entity", "string", "US01, CA01 or MX01 (upper case)"),
        ("Weight UOM", "string", ""),
        ("Dim UOM", "string", ""),
        ("Square Feet", "integer", ""),
        ("Dock Doors", "integer", ""),
        ("Pallet Positions", "integer", ""),
        ("Pick Faces", "integer", ""),
        ("Temperature Zones", "string", "zone types, comma separated"),
        ("Parcel Cutoff", "string", ""),
        ("LTL Cutoff", "string", ""),
        ("Shifts", "string", ""),
        ("Opened", "date", ""),
        ("General Manager", "string", ""),
    ]
    ents = [
        ent(
            "rate_card_cover",
            "rate card · Cover",
            f"{rc}, sheet Cover: the name in column A, the value in column B",
            [
                cover("SCAC", note="the carrier"),
                cover("Shipper"),
                cover("Account", note="Harborline's account number with the carrier"),
                cover("Effective date", "date"),
                cover("General rate increase", note="percent over last year"),
                cover("Mode", note="PARCEL or LTL"),
                cover("Currency", note="per country, for example US: USD"),
                cover("Weight basis", note="per country, LB or KG"),
                cover("Notes"),
            ],
        ),
        ent(
            "rate_card_service",
            "rate card · <SVC>_<CC>",
            f"{rc}: one sheet per service and country, for example GND_US; parcel sheets have a row per whole LB or KG, "
            "LTL sheets a row per weight break with rates per 100 lb",
            [
                Field(
                    "sheet_SVC",
                    "string",
                    label="<SVC>",
                    note="service code, start of the sheet name",
                    evidence='f"{svc}_{country}"',
                ),
                Field(
                    "sheet_country",
                    "string",
                    label="<CC>",
                    note="country, end of the sheet name",
                    evidence='f"{svc}_{country}"',
                ),
                Field("Weight", "integer", label="Weight (LB|KG)", note="parcel sheets", evidence='f"Weight ({unit})"'),
                header_field("Weight break (LB)", note="LTL sheets: 0-499 to 10000+"),
                *[
                    Field(
                        f"Zone_{z}",
                        "number",
                        label=f"Zone {z}",
                        note="rate in the country's currency",
                        evidence='f"Zone {z}"',
                    )
                    for z in sim_files.ZONES
                ],
            ],
        ),
        ent(
            "rate_card_fuel",
            "rate card · Fuel Surcharge",
            f"{rc}, sheet Fuel Surcharge: one row per 10-cent band of the weekly US diesel price",
            [
                header_field("Diesel from (USD/gal)", "number"),
                header_field("Diesel to (USD/gal)", "number"),
                header_field("Ground %", "number", "parcel cards"),
                header_field("Air %", "number", "parcel cards, air services"),
                header_field("LTL %", "number", "LTL cards"),
            ],
        ),
        ent(
            "diesel_weekly",
            "diesel_weekly.csv",
            f"{diesel}: the weekly price carrier bills are rated from, rewritten in full with one more row each Monday. "
            "A build made before this file existed gets it at its next batch",
            [
                header_field("week_start", "date", "the Monday the price applies from; one row per week"),
                header_field("diesel_usd_per_gal", "number", "find its band on the rate card's Fuel Surcharge sheet"),
            ],
        ),
        ent(
            "signal_sources",
            "signal_sources.csv",
            f"{sources}: where this build's FX, weather, holidays and diesel came from, written once per build",
            [
                header_field("signal", note="fx, weather, holidays or diesel"),
                header_field("key", note="currency, warehouse code, or the countries"),
                header_field("source", note="frankfurter or fallback; open-meteo or synthetic; python-holidays; simulator"),
                header_field("real_from", "date", "empty when no real data was used"),
                header_field("real_to", "date", "later days are generated, so stop comparing here"),
                header_field("note", note="what this means for joins to the public API"),
            ],
        ),
        ent(
            "rate_card_accessorials",
            "rate card · Accessorials",
            f"{rc}, sheet Accessorials",
            [
                header_field("Code", note="RESI, AHS, DAS on parcel cards; LIFTGATE, RESI on LTL cards"),
                header_field("Description"),
                Field(
                    "Amount",
                    "number",
                    label="Amount (<currency>)",
                    note="one column per country",
                    evidence="Amount ({COUNTRY_CCY[c]})",
                ),
            ],
        ),
        ent(
            "budget_sheet",
            "budget · FY<yyyy> Budget",
            f"{budget}, sheet FY<yyyy> Budget or FY<yyyy> Reforecast: headers on row 5, a merged title row per site, "
            "subtotal rows, amounts in USD",
            [
                header_field("Site", note="warehouse code, or CORP"),
                header_field("Account", note="AX main account, for example 4010"),
                header_field("Description"),
                *[
                    Field(
                        mo,
                        "number",
                        label=f"{mo}-yy",
                        note="a reforecast marks actual months with A",
                        evidence=f'"{mo}"',
                    )
                    for mo in sim_files.MONTHS
                ],
                Field("FY_Total", "number", label="FY<yyyy> Total", evidence='f"FY{fy} Total"'),
                header_field("Comment"),
            ],
        ),
        ent(
            "budget_assumptions",
            "budget · Assumptions",
            f"{budget}, sheet Assumptions: headers on row 3",
            [
                header_field("Currency", note="CAD, MXN, EUR, GBP"),
                header_field("Budget rate (USD per unit)", "number"),
            ],
        ),
        ent(
            "warehouse_master_sites",
            "warehouse master · Sites",
            f"{wm}, sheet Sites",
            [header_field(label, t, note) for label, t, note in sites],
        ),
        ent(
            "warehouse_master_zones",
            "warehouse master · Zones",
            f"{wm}, sheet Zones",
            [
                header_field("Site Code"),
                header_field("Zone", note="zone type, for example COOLER"),
                header_field("Temp Min C", "number"),
                header_field("Temp Max C", "number"),
            ],
        ),
        ent(
            "item_master_csv",
            "item master .csv",
            "landing/files/clients/<CODE>/item_master_<yyyymmdd>[_delta].csv: delimiter, encoding, decimal "
            "separator, date format, booleans and header style per client (file_spec.txt); names below are the snake "
            "style",
            [
                Field("client", "string", label="<CODE> folder", note="client code", evidence='"clients"'),
                *item_cols,
                Field(
                    "case_gtin",
                    "string",
                    note="appears after an unannounced layout change; other styles: "
                    + ", ".join(sim_files.DRIFT_HEADER[s] for s in styles),
                    evidence='"case_gtin"',
                ),
                header_field("Notes", note="unannounced extra column"),
            ],
        ),
        ent(
            "item_file_spec",
            "file_spec.txt",
            "landing/files/clients/<CODE>/file_spec.txt: the item-master format agreed at onboarding, one line each",
            [
                header_field(label, ev=f"{label}:")
                for label in (
                    "Client",
                    "File name",
                    "Delimiter",
                    "Encoding",
                    "Decimal separator",
                    "Date format",
                    "Booleans",
                    "Header style",
                    "Cadence",
                    "Line endings",
                )
            ],
        ),
    ]
    return {e.name: e for e in ents}


def load_catalog(schema_dir: Path = SCHEMA_DIR, tracker_sql: Path = TRACKER_SQL) -> Catalog:
    cat: Catalog = {s: ddl_entities(s, parse_ddl(schema_dir / f"{s}.sql", s)) for s in ("wms", "ax2012", "iot")}
    cat["salesforce"] = sf_entities(
        yaml.safe_load((schema_dir / "salesforce_objects.yaml").read_text(encoding="utf-8"))
    )
    cat["carrier_api"] = carrier_entities()
    cat["edi"] = edi_entities()
    cat["business_files"] = file_entities()
    cat["tracker"] = ddl_entities("tracker", parse_ddl(tracker_sql, "tracker"))
    return {s: cat[s] for s in SOURCES}


# ----------------------------------------------------------------------------------------------- cross-system keys
MARGIN = 8
Cell = tuple[str, list[str], str] | None  # (object label, key columns, tone)


def diagram_crosskeys() -> str:
    """One row per business key: where it lives in each system and how the join breaks."""
    rows_spec: list[tuple[str, list[str], list[Cell]]] = [
        (
            "Client",
            ["AEs type the code;", "AX number keyed by", "hand, can be blank"],
            [
                ("Salesforce · Account", ["WMS_Owner_Code__c", "ERP_Account_Number__c"], ""),
                ("wms.owner", ["owner_code", "erp_account_nbr"], ""),
                ("ax · custtable", ["accountnum", "(one row per dataareaid)"], ""),
            ],
        ),
        (
            "Package",
            ["reprints change it;", "AX keeps it inside", "free text"],
            [
                ("Carrier API · events", ["tracking_nbr"], "tint"),
                ("wms.shipment, shipment_carton", ["tracking_nbr"], ""),
                ("ax · vendinvoicetrans", ["name: TRK 1Z… WT …"], ""),
            ],
        ),
        (
            "Client PO",
            ["from W05 in the 940"],
            [
                ("EDI 940 · landing/edi", ["W05 element 3"], "tint"),
                ("wms.order_header", ["client_order_ref"], ""),
                None,
            ],
        ),
        (
            "Cases",
            ["typed by client", "success; may be", "blank or wrong"],
            [
                ("Salesforce · Case", ["WMS_Order_Number__c", "Tracking_Number__c"], ""),
                ("wms.order_header, shipment", ["order_nbr", "tracking_nbr"], ""),
                None,
            ],
        ),
        (
            "Rates",
            ["contract vs what", "billing keyed"],
            [
                ("Salesforce · Rate_Card_Line__c", ["Charge_Code__c", "Rate__c"], ""),
                ("wms.billing_rate", ["charge_code", "rate"], ""),
                None,
            ],
        ),
        (
            "Invoice",
            ["filled when", "invoiced"],
            [
                None,
                ("wms.billing_record", ["invoice_ref"], ""),
                ("ax · custinvoicejour", ["invoiceid"], ""),
            ],
        ),
        (
            "People",
            ["personnel number"],
            [
                None,
                ("wms.app_user", ["personnel_nbr"], ""),
                ("ax · hcmworker", ["personnelnumber"], ""),
            ],
        ),
        (
            "Places",
            ["site and zone codes"],
            [
                None,
                ("wms.warehouse, zone", ["warehouse_code", "zone_code"], ""),
                ("iot · telemetry.sensor", ["warehouse_code", "zone_code"], ""),
            ],
        ),
    ]
    label_w, cell_w, gap, head_h, line_h, pad, row_gap = 128, 184, 30, 34, 17, 12, 10
    xs = [MARGIN + label_w + 10 + i * (cell_w + gap) for i in range(3)]
    width = xs[-1] + cell_w + MARGIN
    out = [
        f'<text class="dg-small" x="{x + 2}" y="20">{escape(t)}</text>'
        for x, t in zip(xs, ["Salesforce, APIs, EDI", "WMS", "AX 2012 and IoT"], strict=True)
    ]
    y = head_h
    for name, caveat, cells in rows_spec:
        n = max(1 + len(c[1]) for c in cells if c)
        h = max(n * line_h + pad * 2 - 4, len(caveat) * 15 + 30)
        out.append(f'<text class="dg-title" x="{MARGIN}" y="{y + 22}">{escape(name)}</text>')
        out += [
            f'<text class="dg-small" x="{MARGIN}" y="{y + 40 + i * 15}">{escape(line)}</text>'
            for i, line in enumerate(caveat)
        ]
        for i, cell in enumerate(cells):
            if not cell:
                continue
            obj, keys, tone = cell
            cls = "dg-box dg-box--tint" if tone else "dg-box"
            out.append(f'<rect class="{cls}" x="{xs[i]}" y="{y}" width="{cell_w}" height="{h}" rx="8"/>')
            out.append(f'<text class="dg-small" x="{xs[i] + 12}" y="{y + pad + 9}">{escape(obj)}</text>')
            out += [
                f'<text class="dg-key" x="{xs[i] + 12}" y="{y + pad + 9 + (k + 1) * line_h}">{escape(key)}</text>'
                for k, key in enumerate(keys)
            ]
            if i and cells[i - 1]:
                ly = y + pad + 5 + line_h
                x1, x2 = xs[i - 1] + cell_w, xs[i]
                out.append(f'<path class="dg-line dg-line--accent" d="M{x1} {ly}H{x2}"/>')
                out += [f'<circle class="dg-arrow dg-arrow--accent" cx="{x}" cy="{ly}" r="2.8"/>' for x in (x1, x2)]
        y += h + row_gap
    height = y + MARGIN - row_gap
    title = "Cross-system keys: where each business key lives and how the join breaks"
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}" '
        f'xmlns="http://www.w3.org/2000/svg"><title>{escape(title)}</title>{"".join(out)}</svg>'
    )


# ----------------------------------------------------------------------------------------------- ER diagrams: model
# A diagram is a few columns of boxes. Every line runs in the gap between two neighbouring columns (or loops beside a
# column), on a lane of its own, so lines never cross a box; lanes are ordered to keep crossings down.
BOX_W, HEAD_H, ROW_H, BOX_PAD, V_GAP = 212, 30, 19, 8, 14
LANE_PAD, LANE_PITCH, MIN_GAP, OUTER_TAIL = 12, 8, 44, 6
MAX_WIDTH = 820  # the handbook's content column is about 710 px; wider diagrams get too small to read
KINDS = {
    "fk": "dg-line",  # foreign key the database or Salesforce enforces
    "app": "dg-line dg-line--dash",  # a join only the application (or the people keying it) makes
    "x": "dg-line dg-line--accent",  # a join to another system
    "part": "dg-line",  # nesting inside a file or payload: segments in an envelope, events in a page
}


@dataclass
class Row:
    field: str
    key: bool = False  # business key: drawn in the key colour
    note: str = ""


@dataclass
class Box:
    id: str
    entity: str  # "<source>.<entity>"
    rows: list[str]  # "field", "field:key", "field|note"
    title: str = ""
    gap: int = 0  # extra space above the box
    x: float = 0.0
    y: float = 0.0
    w: float = BOX_W
    col: int = 0

    @property
    def source(self) -> str:
        return self.entity.split(".", 1)[0]

    @property
    def name(self) -> str:
        return self.entity.split(".", 1)[1]

    @property
    def parsed(self) -> list[Row]:
        out = []
        for spec in self.rows:
            main, _, note = spec.partition("|")
            name, _, mark = main.partition(":")
            out.append(Row(name.strip(), mark.strip() == "key", note.strip()))
        return out

    @property
    def h(self) -> float:
        return HEAD_H + len(self.rows) * ROW_H + BOX_PAD

    def row_y(self, name: str) -> float:
        """Where a line meets the box: a field's row, or the title bar for ''."""
        if not name:
            return self.y + HEAD_H / 2 + 1
        for i, r in enumerate(self.parsed):
            if r.field == name:
                return self.y + HEAD_H + i * ROW_H + ROW_H / 2 + 2
        raise KeyError(f"{self.id}: no row {name!r}")


@dataclass
class Edge:
    child: tuple[str, str]  # (box id, field); field '' = the box itself
    parent: tuple[str, str]
    kind: str = "app"
    side: str = "R"  # loops (both ends in one column): run right or left of the column
    label: str = ""  # Mermaid relationship label (default: the column names)
    card: str = ""  # Mermaid cardinality override, e.g. "||--||"


@dataclass
class Diagram:
    name: str  # generated block name, er-...
    source: str
    title: str
    columns: list[list[Box]]
    edges: list[Edge]
    widths: tuple[int, ...] = ()

    @property
    def boxes(self) -> list[Box]:
        return [b for col in self.columns for b in col]


def B(id_: str, entity: str, rows: str, title: str = "", gap: int = 0) -> Box:
    """Rows as one string: `field, field:key, field|note` (notes cannot contain commas)."""
    return Box(id_, entity, [r.strip() for r in rows.split(",")], title=title, gap=gap)


def E(child: str, parent: str, kind: str = "app", side: str = "R", label: str = "", card: str = "") -> Edge:
    """`box.field` or just `box` (the box itself) for each end."""

    def end(s: str) -> tuple[str, str]:
        box, _, col = s.partition(".")
        return box, col

    return Edge(end(child), end(parent), kind, side, label, card)


# ----------------------------------------------------------------------------------------------- ER diagrams: layout
@dataclass
class Route:
    edge: Edge
    gap: int  # -1 = left of column 0; c = right of column c
    ends: list[tuple[str, float]]  # ("L" | "R", y): attached to the box left or right of the gap
    x: float = 0.0

    @property
    def ylo(self) -> float:
        return min(y for _, y in self.ends)

    @property
    def yhi(self) -> float:
        return max(y for _, y in self.ends)


@dataclass
class Layout:
    width: float
    height: float
    boxes: dict[str, Box]
    routes: list[Route]
    gaps: dict[int, tuple[float, float]]

    def points(self, r: Route) -> list[tuple[float, float]]:
        left, right = self.gaps[r.gap]
        (s1, y1), (s2, y2) = r.ends
        return [(left if s1 == "L" else right, y1), (r.x, y1), (r.x, y2), (left if s2 == "L" else right, y2)]


def layout(d: Diagram) -> Layout:
    n = len(d.columns)
    widths = list(d.widths) or [BOX_W] * n
    boxes: dict[str, Box] = {}
    for c, col in enumerate(d.columns):
        y = 0.0
        for b in col:
            y += b.gap
            b.col, b.w, b.y = c, widths[c], y
            y += b.h + V_GAP
            boxes[b.id] = b
    routes = []
    for e in d.edges:
        a, p = boxes[e.child[0]], boxes[e.parent[0]]
        ya, yp = a.row_y(e.child[1]), p.row_y(e.parent[1])
        if a.col == p.col:
            if e.side == "R":
                routes.append(Route(e, a.col, [("L", ya), ("L", yp)]))
            else:
                routes.append(Route(e, a.col - 1, [("R", ya), ("R", yp)]))
        elif abs(a.col - p.col) == 1:
            left, right = (ya, yp) if a.col < p.col else (yp, ya)
            routes.append(Route(e, min(a.col, p.col), [("L", left), ("R", right)]))
        else:
            raise ValueError(f"{d.name}: {e.child} -> {e.parent} skips a column")
    lanes = Counter(r.gap for r in routes)

    def gap_width(g: int) -> float:
        k = lanes.get(g, 0)
        if g in (-1, n - 1):
            return LANE_PAD + (k - 1) * LANE_PITCH + OUTER_TAIL if k else 0
        return max(MIN_GAP, 2 * LANE_PAD + (k - 1) * LANE_PITCH)

    x = MARGIN + gap_width(-1)
    gaps = {-1: (float(MARGIN), x)}
    for c in range(n):
        for b in d.columns[c]:
            b.x = x
        x += widths[c]
        gaps[c] = (x, x + gap_width(c))
        x += gap_width(c)
    for g, (left, right) in gaps.items():
        members = [r for r in routes if r.gap == g]
        if not members:
            continue
        k = len(members)
        if g == -1:
            slots = [right - LANE_PAD - i * LANE_PITCH for i in reversed(range(k))]
        elif g == n - 1:
            slots = [left + LANE_PAD + i * LANE_PITCH for i in range(k)]
        else:
            start = (left + right - (k - 1) * LANE_PITCH) / 2
            slots = [start + i * LANE_PITCH for i in range(k)]
        for r, xs in zip(order_lanes(members), slots, strict=True):
            r.x = xs
    height = max(b.y + b.h for b in boxes.values()) + MARGIN
    return Layout(x + MARGIN, height, boxes, routes, gaps)


BAD = 1000  # a line running along another one into the wrong row


def lane_cost(a: Route, b: Route) -> int:
    """Cost of putting route a on a lane left of route b: crossings, and BAD for a stub that runs into b's corner."""
    cost = 0
    for r, o, r_left in ((a, b, True), (b, a, False)):
        for side, y in r.ends:
            # a stub reaches the other lane only when it heads towards it (right from the left lane, left from the right)
            if (side == "R") != r_left:
                continue
            if o.ylo < y < o.yhi:
                cost += 1
            elif y in (o.ylo, o.yhi) and (side, y) not in o.ends:
                cost += BAD
    return cost


def order_lanes(routes: list[Route]) -> list[Route]:
    """Order the routes of one gap from left to right with as few crossings as possible."""
    k = len(routes)
    cost = [[lane_cost(routes[i], routes[j]) if i != j else 0 for j in range(k)] for i in range(k)]

    def total(order: tuple[int, ...]) -> int:
        return sum(cost[i][j] for i, j in itertools.combinations(order, 2))

    if k <= 8:
        best = min(itertools.permutations(range(k)), key=total)
    else:
        starts = [tuple(range(k)), tuple(sorted(range(k), key=lambda i: routes[i].yhi - routes[i].ylo))]
        results = []
        for order in starts:
            cur = list(order)
            improved = True
            while improved:
                improved = False
                for i, j in itertools.combinations(range(k), 2):
                    trial = cur.copy()
                    trial[i], trial[j] = trial[j], trial[i]
                    if total(tuple(trial)) < total(tuple(cur)):
                        cur, improved = trial, True
            results.append(tuple(cur))
        best = min(results, key=total)
    return [routes[i] for i in best]


# ----------------------------------------------------------------------------------------------- ER diagrams: drawing
NARROW, SEMI, WIDE = set("ijl.,:;'!|I "), set("frt()[]{}/-"), set("mwMW")


def sans_width(s: str, px: float, bold: bool = False) -> float:
    """Generous estimate of a sans-serif string's width (for the layout checks)."""
    em = 0.0
    for ch in s:
        if ch in NARROW:
            em += 0.28
        elif ch in SEMI:
            em += 0.36
        elif ch in WIDE:
            em += 0.85
        elif ch.isupper():
            em += 0.66
        elif ch.isdigit() or ch.islower():
            em += 0.55
        else:
            em += 0.6
    return em * px * (1.04 if bold else 1.0)


def mono_width(s: str) -> float:
    return len(s) * 11.5 * 0.6


def num(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".")


def fk_children(diagrams: list[Diagram]) -> set[tuple[str, str]]:
    """(entity, field) at the many end of a key relation in any diagram: marked FK everywhere."""
    out = set()
    for d in diagrams:
        ent = {b.id: b.entity for b in d.boxes}
        out |= {(ent[e.child[0]], e.child[1]) for e in d.edges if e.kind in ("fk", "app") and e.child[1]}
    return out


def box_title(b: Box, d: Diagram, cat: Catalog) -> str:
    title = b.title or cat[b.source][b.name].display
    return title if b.source == d.source else f"{PREFIX[b.source]} · {title}"


def box_rows(b: Box, d: Diagram, cat: Catalog, fks: set[tuple[str, str]]) -> list[tuple[str, str, str, bool]]:
    """(label, note, mark, key) per row."""
    joined = {e.child for e in d.edges if e.kind == "x"} | {e.parent for e in d.edges if e.kind == "x"}
    ent = cat[b.source][b.name]
    out = []
    for r in b.parsed:
        f = ent.get(r.field)
        if f is None:
            raise KeyError(f"{d.name}: {b.entity} has no field {r.field!r}")
        fk = bool(f.refs) or (b.entity, f.name) in fks
        mark = " ".join(m for m, on in (("PK", f.pk), ("FK", fk)) if on)
        out.append((f.text, r.note, mark, r.key or (b.id, f.name) in joined))
    return out


def svg_box(b: Box, title: str, rows: list[tuple[str, str, str, bool]], external: bool) -> str:
    tone = " dg-box--accent" if external else ""
    x, y, w = b.x, b.y, b.w
    rect = f'<rect class="dg-box{tone}" x="{num(x)}" y="{num(y)}" width="{num(w)}" height="{num(b.h)}" rx="8"/>'
    head = (
        f"M{num(x + 1)} {num(y + HEAD_H)}V{num(y + 8)}a7 7 0 0 1 7-7H{num(x + w - 8)}a7 7 0 0 1 7 7V{num(y + HEAD_H)}z"
    )
    out = [
        f'<g class="dg-entity">{rect}<path class="dg-head" d="{head}"/>',
        f'<text class="dg-title" x="{num(x + 12)}" y="{num(y + 20)}">{escape(title)}</text>',
    ]
    for i, (label, note, mark, key) in enumerate(rows):
        ty = y + HEAD_H + i * ROW_H + ROW_H / 2 + 6
        out.append(
            f'<text class="{"dg-key" if key else "dg-mono"}" x="{num(x + 12)}" y="{num(ty)}">{escape(label)}</text>'
        )
        if note:
            nx = x + 12 + mono_width(label) + 8
            out.append(f'<text class="dg-small" x="{num(nx)}" y="{num(ty)}">{escape(note)}</text>')
        if mark:
            out.append(f'<text class="dg-small" x="{num(x + w - 12)}" y="{num(ty)}" text-anchor="end">{mark}</text>')
    out.append("</g>")
    return "".join(out)


def svg_diagram(d: Diagram, cat: Catalog, fks: set[tuple[str, str]]) -> str:
    lay = layout(d)
    lines, dots = [], []
    shared = Counter((r.gap, side, y) for r in lay.routes for side, y in r.ends)
    for r in lay.routes:
        pts = lay.points(r)
        if shared[(r.gap, *r.ends[1])] > shared[(r.gap, *r.ends[0])]:
            pts.reverse()  # start where several lines meet, so their dashes line up
        (x1, y1), (xm, _), (_, y2), (x2, _) = pts
        lines.append(f'<path class="{KINDS[r.edge.kind]}" d="M{num(x1)} {num(y1)}H{num(xm)}V{num(y2)}H{num(x2)}"/>')
        dot = "dg-arrow dg-arrow--accent" if r.edge.kind == "x" else "dg-arrow"
        dots += [f'<circle class="{dot}" cx="{num(px)}" cy="{num(py)}" r="2.6"/>' for px, py in ((x1, y1), (x2, y2))]
    boxes = [svg_box(b, box_title(b, d, cat), box_rows(b, d, cat, fks), b.source != d.source) for b in d.boxes]
    return (
        f'<svg viewBox="0 0 {num(lay.width)} {num(lay.height)}" role="img" aria-label="{escape(d.title)}" '
        f'xmlns="http://www.w3.org/2000/svg" style="max-width:{num(lay.width)}px;margin:0 auto">'
        f"<title>{escape(d.title)}</title>{''.join(lines)}{''.join(dots)}{''.join(boxes)}</svg>"
    )


def layout_problems(d: Diagram, cat: Catalog, fks: set[tuple[str, str]]) -> list[str]:
    """Overlaps, lines that run into each other or look continuous, and text that does not fit its box."""
    lay = layout(d)
    out = []
    if lay.width > MAX_WIDTH:
        out.append(f"{d.name}: {num(lay.width)} wide, more than {MAX_WIDTH}")
    boxes = list(lay.boxes.values())
    for a, b in itertools.combinations(boxes, 2):
        if a.x < b.x + b.w + 8 and b.x < a.x + a.w + 8 and a.y < b.y + b.h + 8 and b.y < a.y + a.h + 8:
            out.append(f"{d.name}: boxes {a.id} and {b.id} overlap or touch")
    for b in boxes:
        if b.x < 0 or b.y < 0 or b.x + b.w > lay.width or b.y + b.h > lay.height:
            out.append(f"{d.name}: box {b.id} outside the picture")
    segs = []
    for i, r in enumerate(lay.routes):
        pts = lay.points(r)
        segs += [(i, p, q) for p, q in itertools.pairwise(pts) if p != q]
    for i, (x1, y1), (x2, y2) in segs:
        for b in boxes:
            if y1 == y2 and b.y < y1 < b.y + b.h and max(min(x1, x2), b.x) < min(max(x1, x2), b.x + b.w) - 0.01:
                out.append(f"{d.name}: line {i} crosses box {b.id}")
            if x1 == x2 and b.x < x1 < b.x + b.w and max(min(y1, y2), b.y) < min(max(y1, y2), b.y + b.h) - 0.01:
                out.append(f"{d.name}: line {i} crosses box {b.id}")
    for (i, p, q), (j, s, t) in itertools.combinations(segs, 2):
        if i == j:
            continue
        if p[1] == q[1] == s[1] == t[1]:
            lo, hi = max(min(p[0], q[0]), min(s[0], t[0])), min(max(p[0], q[0]), max(s[0], t[0]))
            if hi - lo > 0.01 and not {p, q} & {s, t}:
                out.append(f"{d.name}: lines {i} and {j} run along each other at y={num(p[1])}")
        if p[0] == q[0] == s[0] == t[0]:
            lo, hi = max(min(p[1], q[1]), min(s[1], t[1])), min(max(p[1], q[1]), max(s[1], t[1]))
            if hi - lo > 0.01:
                out.append(f"{d.name}: lines {i} and {j} share a lane at x={num(p[0])}")
    for i, j in itertools.combinations(range(len(lay.routes)), 2):
        a, b = lay.routes[i], lay.routes[j]
        if a.gap != b.gap:
            continue
        for (sa, ya), (sb, yb) in itertools.product(a.ends, b.ends):
            # fine when one of them runs straight across from a row the other also leaves: a line that branches
            shared = (sb, ya) in a.ends or (sa, yb) in b.ends
            if sa != sb and abs(ya - yb) < 3 and not shared:
                out.append(f"{d.name}: lines {i} and {j} meet opposite sides of a gap at y={num(ya)} (looks like one)")
    for b in boxes:
        title = box_title(b, d, cat)
        if 24 + sans_width(title, 14, bold=True) > b.w:
            out.append(f"{d.name}: title {title!r} too wide for {b.id}")
        for label, note, mark, _ in box_rows(b, d, cat, fks):
            need = 24 + mono_width(label)
            need += 8 + sans_width(note, 11.5) if note else 0
            need += 10 + sans_width(mark, 11.5) if mark else 0
            if need > b.w:
                out.append(f"{d.name}: row {label!r} {note!r} too wide for {b.id}")
    return out


# ----------------------------------------------------------------------------------------------- Mermaid ERDs
ANCHORS = {
    "wms": "wms-erd",
    "ax2012": "ax-erd",
    "salesforce": "sf-erd",
    "iot": "iot-erd",
    "carrier_api": "carrier-erd",
    "edi": "edi-erd",
    "business_files": "files-erd",
    "tracker": "tracker-erd",
}


def mermaid_type(t: str) -> str:
    """Mermaid types allow letters, digits, - _ ( ) [ ]: numeric(12,2) becomes numeric(12_2)."""
    return re.sub(r"[\s,]+", "_", t.strip()) or "string"


def mermaid_text(s: str) -> str:
    return " ".join(s.replace('"', "'").split())


def cardinality(e: Edge, child_field: Field | None, child: Entity) -> str:
    if e.card:
        return e.card
    line = "--" if e.kind in ("fk", "part") else ".."
    if e.kind == "part":
        return f"||{line}|{{"
    if e.kind == "x":
        return f"|o{line}o{{"
    one = "|o" if child_field is None or child_field.nullable else "||"
    many = "o|" if child_field is not None and [f for f in child.fields if f.pk] == [child_field] else "o{"
    return f"{one}{line}{many}"


def mermaid(source: str, cat: Catalog, diagrams: list[Diagram], fks: set[tuple[str, str]]) -> str:
    """Every entity of the source with every field, the entities of other systems it joins (only the fields the
    diagrams show), and one relationship per line drawn in the source's diagrams."""
    ds = [d for d in diagrams if d.source == source]

    def mname(entity: str) -> str:
        src, name = entity.split(".", 1)
        return name if src == source else f"{src}_{name}"

    stubs: dict[str, list[str]] = {}
    joins: dict[tuple[str, str], list[str]] = {}
    rels: list[str] = []
    for d in ds:
        boxes = {b.id: b for b in d.boxes}
        for b in d.boxes:
            if b.source != source:
                shown = stubs.setdefault(b.entity, [])
                shown += [r.field for r in b.parsed if r.field not in shown]
        for e in d.edges:
            cb, pb = boxes[e.child[0]], boxes[e.parent[0]]
            cf, pf = e.child[1], e.parent[1]
            child = cat[cb.source][cb.name]
            label = e.label or (f"{cf} = {pf}" if cf and pf and cf != pf else cf or pf or "contains")
            card = cardinality(e, child.get(cf) if cf else None, child)
            rel = f'    {mname(pb.entity)} {card} {mname(cb.entity)} : "{mermaid_text(label)}"'
            if rel not in rels:
                rels.append(rel)
            if cf and pf and e.kind != "part":
                bit = f"{'joins' if e.kind == 'x' else 'references'} {mname(pb.entity)}.{pf}"
                seen = joins.setdefault((cb.entity, cf), [])
                if bit not in seen:
                    seen.append(bit)

    def attr(entity: str, f: Field, stub: bool) -> str:
        keys = [k for k, on in (("PK", f.pk), ("FK", bool(f.refs) or (entity, f.name) in fks), ("UK", f.unique)) if on]
        bits = []
        if not stub and (f.label or f.note):
            bits.append(f"{f.label}: {f.note}" if f.label and f.note else f.label or f.note)
        bits += joins.get((entity, f.name), [])
        if f.refs and not any(b.startswith("references") for b in bits):
            bits.append("references " + ", ".join(f.refs))
        comment = f' "{mermaid_text("; ".join(bits))}"' if bits else ""
        return f"        {mermaid_type(f.type)} {f.name}{' ' + ', '.join(keys) if keys else ''}{comment}"

    out = [
        "erDiagram",
        f"    %% {SOURCES[source]}",
        "    %% Generated by simulator/harborline/tools/build_data_dictionary.py together with the diagrams in",
        f"    %% handbook/source-systems.html#{ANCHORS[source]}. Do not edit by hand.",
        "    %% Every entity with every column. Lines: -- a key the system enforces, or nesting inside a file;",
        "    %% .. a join only the application makes, or a join to another system (entities named <system>_<name>).",
        "    %% Types: commas and spaces in SQL types become _, so numeric(12_2) means numeric(12,2).",
    ]
    for ent in cat[source].values():
        out.append("")
        head = f"{ent.title}: {ent.note}" if ent.title and ent.note else ent.title or ent.note
        if head:
            out.append(f"    %% {mermaid_text(head)}")
        out.append(f"    {ent.name} {{")
        out += [attr(f"{source}.{ent.name}", f, False) for f in ent.fields]
        out.append("    }")
    for entity, shown in stubs.items():
        src, name = entity.split(".", 1)
        ent = cat[src][name]
        out += [
            "",
            f"    %% {mermaid_text(SOURCES[src])}: {name}, only the columns the diagrams show",
            f"    {mname(entity)} {{",
        ]
        out += [attr(entity, f, True) for f in ent.fields if f.name in shown]
        out.append("    }")
    out += ["", *rels, ""]
    return "\n".join(out)


# ----------------------------------------------------------------------------------------------- ER diagrams: content
# Boxes list the key columns and the few that matter (the dictionary and the .mmd files have every column).
# Edge kinds: fk (declared), app (only the application), x (another system), part (nesting in a file or payload).
def wms_diagrams() -> list[Diagram]:
    return [
        Diagram(
            "er-wms-master",
            "wms",
            "WMS sites, clients, items, carriers and users",
            [
                [
                    B("zone", "wms.zone", "zone_id, warehouse_id, zone_code:key, zone_type"),
                    B("loc", "wms.location", "location_id, warehouse_id, zone_id, location_code:key, location_type"),
                    B("carrier", "wms.carrier", "carrier_id, scac:key, mode"),
                    B("svc", "wms.carrier_service", "service_id, carrier_id, service_code:key"),
                ],
                [
                    B("wh", "wms.warehouse", "warehouse_id, warehouse_code:key, time_zone, weight_uom"),
                    B("ow", "wms.owner_warehouse", "owner_id, warehouse_id, active_from, active_to"),
                    B("user", "wms.app_user", "user_id, personnel_nbr:key, warehouse_id, user_role"),
                    B("rel", "wms.sys_release", "release_id, version, released_ts"),
                ],
                [
                    B(
                        "owner",
                        "wms.owner",
                        "owner_id, owner_code:key, erp_account_nbr:key, primary_warehouse_id",
                        gap=24,
                    ),
                    B("mat", "wms.material", "material_id, owner_id, sku:key, upc"),
                    B("uom", "wms.material_uom", "material_id, uom_code, qty_per_uom"),
                ],
            ],
            [
                E("zone.warehouse_id", "wh.warehouse_id", "fk"),
                E("loc.warehouse_id", "wh.warehouse_id", "fk"),
                E("loc.zone_id", "zone.zone_id", "fk", side="L"),
                E("svc.carrier_id", "carrier.carrier_id", "fk", side="L"),
                E("ow.warehouse_id", "wh.warehouse_id", "fk"),
                E("user.warehouse_id", "wh.warehouse_id"),
                E("ow.owner_id", "owner.owner_id", "fk"),
                E("owner.primary_warehouse_id", "wh.warehouse_id", "fk"),
                E("mat.owner_id", "owner.owner_id", "fk"),
                E("uom.material_id", "mat.material_id", "fk"),
            ],
        ),
        Diagram(
            "er-wms-inventory",
            "wms",
            "WMS inbound and stock on hand",
            [
                [
                    B("asn", "wms.asn_header", "asn_id, asn_nbr:key, source, asn_status"),
                    B("asnl", "wms.asn_line", "asn_id, line_nbr, material_id, expected_qty"),
                    B("rcv", "wms.receipt_header", "receipt_id, receipt_nbr:key, asn_id, dock_door, trailer_nbr"),
                    B("rcvl", "wms.receipt_line", "receipt_id, line_nbr, material_id, lpn_id, received_qty"),
                ],
                [
                    B("mat", "wms.material", "material_id, owner_id, sku:key", gap=24),
                    B("lpn", "wms.license_plate", "lpn_id, lpn_nbr:key, location_id, lpn_status"),
                    B("loc", "wms.location", "location_id, zone_id, location_code:key"),
                ],
                [
                    B(
                        "inv",
                        "wms.inventory",
                        "inventory_id, material_id, lpn_id, location_id, lot_nbr, qty_on_hand, inv_status",
                    ),
                    B("cc", "wms.cycle_count", "count_id, material_id, location_id, variance_qty, count_status"),
                ],
            ],
            [
                E("asnl.asn_id", "asn.asn_id", "fk", side="L"),
                E("rcv.asn_id", "asn.asn_id", side="L"),
                E("rcvl.receipt_id", "rcv.receipt_id", "fk", side="L"),
                E("asnl.material_id", "mat.material_id"),
                E("rcvl.material_id", "mat.material_id"),
                E("rcvl.lpn_id", "lpn.lpn_id"),
                E("lpn.location_id", "loc.location_id"),
                E("inv.location_id", "loc.location_id"),
                E("inv.lpn_id", "lpn.lpn_id"),
                E("inv.material_id", "mat.material_id"),
                E("cc.location_id", "loc.location_id"),
                E("cc.material_id", "mat.material_id"),
            ],
        ),
        Diagram(
            "er-wms-ledger",
            "wms",
            "WMS inventory ledger and what each row points to",
            [
                [
                    B("rcv", "wms.receipt_header", "receipt_id|RECEIPT, receipt_nbr:key"),
                    B("task", "wms.task", "task_id|TASK, task_type"),
                    B("cc", "wms.cycle_count", "count_id|COUNT, count_ts"),
                    B("loc", "wms.location", "location_id, location_code:key"),
                ],
                [
                    B(
                        "txn",
                        "wms.inventory_txn",
                        "txn_id, txn_ts, txn_type, qty, ref_type:key, ref_id, from_location_id, to_location_id, material_id, lpn_id, user_id",
                        gap=24,
                    ),
                ],
                [
                    B("order", "wms.order_header", "order_id|ORDER, order_nbr:key"),
                    B("ret", "wms.return_header", "return_id|RETURN, rma_nbr:key"),
                    B("mat", "wms.material", "material_id, sku:key"),
                    B("lpn", "wms.license_plate", "lpn_id, lpn_nbr:key"),
                    B("user", "wms.app_user", "user_id, username:key"),
                ],
            ],
            [
                E("txn.ref_id", "rcv.receipt_id", label="ref_id where ref_type = RECEIPT"),
                E("txn.ref_id", "task.task_id", label="ref_id where ref_type = TASK"),
                E("txn.ref_id", "cc.count_id", label="ref_id where ref_type = COUNT"),
                E("txn.from_location_id", "loc.location_id"),
                E("txn.to_location_id", "loc.location_id"),
                E("txn.ref_id", "order.order_id", label="ref_id where ref_type = ORDER"),
                E("txn.ref_id", "ret.return_id", label="ref_id where ref_type = RETURN"),
                E("txn.material_id", "mat.material_id"),
                E("txn.lpn_id", "lpn.lpn_id"),
                E("txn.user_id", "user.user_id"),
            ],
        ),
        Diagram(
            "er-wms-outbound",
            "wms",
            "WMS orders, labor tasks, shipments and returns",
            [
                [
                    B("owner", "wms.owner", "owner_id, owner_code:key", gap=38),
                    B("line", "wms.order_line", "order_id, line_nbr, material_id, ordered_qty, shipped_qty"),
                    B("task", "wms.task", "task_id, task_type, order_id, assigned_user_id"),
                    B("user", "wms.app_user", "user_id, personnel_nbr:key, user_role"),
                ],
                [
                    B(
                        "order",
                        "wms.order_header",
                        "order_id, order_nbr:key, owner_id, client_order_ref:key, carrier_service_id, order_status",
                    ),
                    B("mat", "wms.material", "material_id, sku:key"),
                    B("svc", "wms.carrier_service", "service_id, carrier_id, service_code:key"),
                ],
                [
                    B(
                        "ship",
                        "wms.shipment",
                        "shipment_id, shipment_nbr:key, order_id, carrier_service_id, scac, tracking_nbr:key",
                    ),
                    B("ctn", "wms.shipment_carton", "carton_id, shipment_id, carton_nbr, tracking_nbr:key"),
                    B("ret", "wms.return_header", "return_id, rma_nbr:key, original_order_id, carrier_tracking_nbr"),
                    B("retl", "wms.return_line", "return_id, line_nbr, material_id, disposition"),
                ],
            ],
            [
                E("order.owner_id", "owner.owner_id"),
                E("line.order_id", "order.order_id", "fk"),
                E("line.material_id", "mat.material_id"),
                E("task.order_id", "order.order_id"),
                E("task.assigned_user_id", "user.user_id", side="L"),
                E("order.carrier_service_id", "svc.service_id"),
                E("ship.order_id", "order.order_id"),
                E("ship.carrier_service_id", "svc.service_id"),
                E("ret.original_order_id", "order.order_id"),
                E("retl.material_id", "mat.material_id"),
                E("ctn.shipment_id", "ship.shipment_id", "fk"),
                E("retl.return_id", "ret.return_id", "fk"),
            ],
        ),
        Diagram(
            "er-wms-billing",
            "wms",
            "WMS billing and where it goes",
            [
                [
                    B("owner", "wms.owner", "owner_id, owner_code:key, erp_account_nbr:key"),
                    B("wh", "wms.warehouse", "warehouse_id, warehouse_code:key"),
                    B("rcl", "salesforce.Rate_Card_Line__c", "Rate_Card__c, Charge_Code__c, Rate__c"),
                ],
                [
                    B(
                        "rate",
                        "wms.billing_rate",
                        "rate_id, owner_id, warehouse_id, charge_code, rate, effective_date",
                        gap=24,
                    ),
                    B(
                        "rec",
                        "wms.billing_record",
                        "billing_id, owner_id, warehouse_id, charge_code, activity_date, amount, billing_status, invoice_ref",
                    ),
                ],
                [
                    B("cit", "ax2012.custinvoicetrans", "invoiceid, itemid|charge code, lineamount", gap=228),
                    B("cij", "ax2012.custinvoicejour", "invoiceid, invoiceaccount, invoicedate", gap=24),
                ],
            ],
            [
                E("rate.owner_id", "owner.owner_id"),
                E("rate.warehouse_id", "wh.warehouse_id"),
                E("rec.owner_id", "owner.owner_id"),
                E("rec.warehouse_id", "wh.warehouse_id"),
                E("rate.charge_code", "rcl.Charge_Code__c", "x"),
                E("rec.invoice_ref", "cij.invoiceid", "x"),
                E("rec.charge_code", "cit.itemid", "x"),
                E("cit.invoiceid", "cij.invoiceid"),
            ],
        ),
    ]


def ax_diagrams() -> list[Diagram]:
    return [
        Diagram(
            "er-ax",
            "ax2012",
            "AX 2012 customers, parties, addresses and receivables",
            [
                [
                    B("loc", "ax2012.logisticslocation", "recid, locationid, description"),
                    B("addr", "ax2012.logisticspostaladdress", "recid, location, city, validfrom, validto"),
                    B("dpl", "ax2012.dirpartylocation", "recid, party, location, isprimary"),
                ],
                [
                    B(
                        "party",
                        "ax2012.dirpartytable",
                        "recid, partynumber:key, name, instancerelationtype, primaryaddresslocation",
                        gap=24,
                    ),
                    B("cust", "ax2012.custtable", "recid, dataareaid:key, accountnum:key, party, currency, paymtermid"),
                    B("cs", "ax2012.custsettlement", "recid, transrecid, offsetrecid, settleamountcur"),
                ],
                [
                    B(
                        "cij",
                        "ax2012.custinvoicejour",
                        "recid, dataareaid:key, invoiceid:key, invoiceaccount, ledgervoucher, invoiceamount",
                        gap=24,
                    ),
                    B("cit", "ax2012.custinvoicetrans", "recid, invoiceid, itemid|charge code, lineamount"),
                    B("ct", "ax2012.custtrans", "recid, accountnum, voucher, invoice, settleamountcur, closed"),
                ],
            ],
            [
                E("addr.location", "loc.recid", side="L"),
                E("dpl.location", "loc.recid", side="L"),
                E("dpl.party", "party.recid"),
                E("party.primaryaddresslocation", "loc.recid"),
                E("cust.party", "party.recid"),
                E("cij.invoiceaccount", "cust.accountnum"),
                E("ct.accountnum", "cust.accountnum"),
                E("cs.transrecid", "ct.recid"),
                E("cs.offsetrecid", "ct.recid"),
                E("cit.invoiceid", "cij.invoiceid"),
                E("ct.invoice", "cij.invoiceid"),
            ],
        ),
        Diagram(
            "er-ax-payables",
            "ax2012",
            "AX 2012 vendors, payables and journals",
            [
                [
                    B("party", "ax2012.dirpartytable", "recid, name"),
                    B("vend", "ax2012.vendtable", "recid, dataareaid:key, accountnum:key, party, vendgroup, currency"),
                ],
                [
                    B(
                        "vij",
                        "ax2012.vendinvoicejour",
                        "recid, invoiceid:key, internalinvoiceid:key, invoiceaccount, ledgervoucher",
                        gap=24,
                    ),
                    B(
                        "vit",
                        "ax2012.vendinvoicetrans",
                        "recid, internalinvoiceid, name|TRK … WT … ZN …, ledgerdimension, lineamount",
                    ),
                    B("vt", "ax2012.vendtrans", "recid, accountnum, voucher, invoice, closed"),
                ],
                [
                    B("ljt", "ax2012.ledgerjournaltable", "recid, dataareaid:key, journalnum:key, journalname, posted"),
                    B(
                        "ljl",
                        "ax2012.ledgerjournaltrans",
                        "recid, journalnum, voucher, ledgerdimension, amountcurdebit, amountcurcredit",
                    ),
                    B("davc", "ax2012.dimensionattributevaluecombination", "recid, displayvalue:key, mainaccount"),
                ],
            ],
            [
                E("vend.party", "party.recid", side="L"),
                E("vij.invoiceaccount", "vend.accountnum"),
                E("vt.accountnum", "vend.accountnum"),
                E("vit.internalinvoiceid", "vij.internalinvoiceid"),
                E("vt.invoice", "vij.invoiceid"),
                E("vit.ledgerdimension", "davc.recid"),
                E("ljl.journalnum", "ljt.journalnum"),
                E("ljl.ledgerdimension", "davc.recid"),
            ],
            widths=(196, 204, 272),
        ),
        Diagram(
            "er-ax-ledger",
            "ax2012",
            "AX 2012 general ledger and the vouchers behind it",
            [
                [
                    B("cij", "ax2012.custinvoicejour", "dataareaid:key, invoiceid:key, ledgervoucher:key", gap=24),
                    B("vij", "ax2012.vendinvoicejour", "dataareaid:key, invoiceid:key, ledgervoucher:key"),
                    B("ljl", "ax2012.ledgerjournaltrans", "dataareaid:key, journalnum:key, voucher:key"),
                ],
                [
                    B(
                        "gje",
                        "ax2012.generaljournalentry",
                        "recid, ledger, accountingdate, journalcategory, subledgervoucher, subledgervoucherdataareaid",
                    ),
                    B(
                        "gjae",
                        "ax2012.generaljournalaccountentry",
                        "recid, generaljournalentry, ledgerdimension, postingtype, accountingcurrencyamount, iscredit",
                    ),
                ],
                [
                    B("ledger", "ax2012.ledger", "recid, name, primaryforlegalentity, accountingcurrency", gap=24),
                    B("da", "ax2012.dataarea", "id, recid, name"),
                    B("davc", "ax2012.dimensionattributevaluecombination", "recid, displayvalue:key, mainaccount"),
                    B("main", "ax2012.mainaccount", "recid, mainaccountid:key, name, type"),
                ],
            ],
            [
                E("gje.subledgervoucher", "cij.ledgervoucher"),
                E("gje.subledgervoucher", "vij.ledgervoucher"),
                E("gje.subledgervoucher", "ljl.voucher"),
                E("gjae.generaljournalentry", "gje.recid"),
                E("gje.ledger", "ledger.recid"),
                E("gjae.ledgerdimension", "davc.recid"),
                E("ledger.primaryforlegalentity", "da.recid"),
                E("davc.mainaccount", "main.recid"),
            ],
            widths=(188, 224, 272),
        ),
        Diagram(
            "er-ax-reference",
            "ax2012",
            "AX 2012 reference data: companies, currencies, payment terms, workers and enums",
            [
                [
                    B("cust", "ax2012.custtable", "dataareaid:key, accountnum:key, paymtermid, currency"),
                    B("pt", "ax2012.paymterm", "recid, dataareaid:key, paymtermid:key, numofdays"),
                    B("enum", "ax2012.srsanalysisenums", "enumname, enumitemvalue, enumitemlabel", gap=24),
                ],
                [
                    B("da", "ax2012.dataarea", "id, recid, name", gap=24),
                    B("cur", "ax2012.currency", "recid, currencycode:key"),
                    B("type", "ax2012.exchangeratetype", "recid, name"),
                    B(
                        "pair",
                        "ax2012.exchangeratecurrencypair",
                        "recid, exchangeratetype, fromcurrencycode, tocurrencycode",
                    ),
                    B(
                        "rate",
                        "ax2012.exchangerate",
                        "recid, exchangeratecurrencypair, exchangerate|×100, validfrom, validto",
                    ),
                ],
                [
                    B("party", "ax2012.dirpartytable", "recid, name"),
                    B("pn", "ax2012.dirpersonname", "recid, person, firstname, lastname, validfrom, validto"),
                    B("worker", "ax2012.hcmworker", "recid, person, personnelnumber:key"),
                    B("emp", "ax2012.hcmemployment", "recid, worker, legalentity, validfrom, validto"),
                ],
            ],
            [
                E("cust.paymtermid", "pt.paymtermid", side="L"),
                E("cust.dataareaid", "da.id"),
                E("pt.dataareaid", "da.id"),
                E("cust.currency", "cur.currencycode"),
                E("pair.exchangeratetype", "type.recid"),
                E("pair.fromcurrencycode", "cur.currencycode"),
                E("pair.tocurrencycode", "cur.currencycode"),
                E("rate.exchangeratecurrencypair", "pair.recid"),
                E("emp.legalentity", "da.recid"),
                E("pn.person", "party.recid"),
                E("worker.person", "party.recid"),
                E("emp.worker", "worker.recid"),
            ],
            widths=(212, 220, 212),
        ),
    ]


def salesforce_diagrams() -> list[Diagram]:
    return [
        Diagram(
            "er-salesforce",
            "salesforce",
            "Salesforce accounts, contacts, cases and activities",
            [
                [B("user", "salesforce.User", "Id, Name, ManagerId, IsActive", gap=24)],
                [
                    B(
                        "acct",
                        "salesforce.Account",
                        "Id, OwnerId, ParentId, MasterRecordId, WMS_Owner_Code__c:key, ERP_Account_Number__c:key",
                    ),
                ],
                [
                    B("contact", "salesforce.Contact", "Id, AccountId, Email", gap=24),
                    B(
                        "case",
                        "salesforce.Case",
                        "Id, AccountId, ContactId, WMS_Order_Number__c:key, Tracking_Number__c:key",
                    ),
                    B("task", "salesforce.Task", "Id, WhoId, WhatId, AccountId, TaskSubtype"),
                ],
            ],
            [
                E("user.ManagerId", "user.Id", "fk", side="L"),
                E("acct.OwnerId", "user.Id", "fk"),
                E("acct.ParentId", "acct.Id", "fk"),
                E("acct.MasterRecordId", "acct.Id", "fk"),
                E("contact.AccountId", "acct.Id", "fk"),
                E("case.AccountId", "acct.Id", "fk"),
                E("task.AccountId", "acct.Id", "fk"),
                E("task.WhatId", "acct.Id", "fk"),
                E("case.ContactId", "contact.Id", "fk"),
                E("task.WhoId", "contact.Id", "fk"),
                E("task.WhatId", "case.Id", "fk"),
            ],
        ),
        Diagram(
            "er-salesforce-deals",
            "salesforce",
            "Salesforce pipeline, contracts, rate cards and price books",
            [
                [
                    B("acct", "salesforce.Account", "Id, Name"),
                    B("contract", "salesforce.Contract", "Id, AccountId, ContractNumber:key, SLA_Ship_Hours__c"),
                    B("rc", "salesforce.Rate_Card__c", "Id, Account__c, Contract__c, Status__c"),
                    B("rcl", "salesforce.Rate_Card_Line__c", "Id, Rate_Card__c, Charge_Code__c:key, Rate__c"),
                ],
                [
                    B("opp", "salesforce.Opportunity", "Id, AccountId, ContractId, Pricebook2Id, StageName", gap=24),
                    B("oh", "salesforce.OpportunityHistory", "Id, OpportunityId, StageName, CloseDate"),
                    B(
                        "oli",
                        "salesforce.OpportunityLineItem",
                        "Id, OpportunityId, PricebookEntryId, Product2Id, Quantity",
                    ),
                ],
                [
                    B("pb", "salesforce.Pricebook2", "Id, Name, IsStandard"),
                    B("pbe", "salesforce.PricebookEntry", "Id, Pricebook2Id, Product2Id, UnitPrice"),
                    B("prod", "salesforce.Product2", "Id, ProductCode:key, Family, QuantityUnitOfMeasure"),
                ],
            ],
            [
                E("contract.AccountId", "acct.Id", "fk"),
                E("rc.Account__c", "acct.Id", "fk", side="L"),
                E("rc.Contract__c", "contract.Id", "fk", side="L"),
                E("rcl.Rate_Card__c", "rc.Id", "fk", side="L"),
                E("opp.AccountId", "acct.Id", "fk"),
                E("opp.ContractId", "contract.Id", "fk"),
                E("oh.OpportunityId", "opp.Id", "fk"),
                E("oli.OpportunityId", "opp.Id", "fk"),
                E("opp.Pricebook2Id", "pb.Id", "fk"),
                E("oli.PricebookEntryId", "pbe.Id", "fk"),
                E("oli.Product2Id", "prod.Id", "fk"),
                E("pbe.Pricebook2Id", "pb.Id", "fk"),
                E("pbe.Product2Id", "prod.Id", "fk"),
            ],
        ),
    ]


def iot_diagrams() -> list[Diagram]:
    return [
        Diagram(
            "er-iot",
            "iot",
            "IoT telemetry and how it joins the WMS",
            [
                [
                    B(
                        "rd",
                        "iot.sensor_reading",
                        "reading_id, sensor_id, device_ts_ms, value, battery_pct, ingested_at",
                        gap=24,
                    ),
                ],
                [
                    B(
                        "sensor",
                        "iot.sensor",
                        "sensor_id, warehouse_code, zone_code, sensor_type, unit, calibration_due",
                    ),
                    B(
                        "door",
                        "iot.dock_door_event",
                        "event_id, warehouse_code, door_nbr, event_type, trailer_nbr, device_ts_ms, ingested_at",
                    ),
                ],
                [
                    B("wh", "wms.warehouse", "warehouse_id, warehouse_code, time_zone", gap=24),
                    B("zone", "wms.zone", "zone_id, warehouse_id, zone_code, zone_type, temp_min_c, temp_max_c"),
                    B("loc", "wms.location", "location_id, zone_id, location_code|DOOR-07"),
                    B("rcv", "wms.receipt_header", "receipt_id, warehouse_id, dock_door|D07, trailer_nbr"),
                ],
            ],
            [
                E("rd.sensor_id", "sensor.sensor_id"),
                E("sensor.warehouse_code", "wh.warehouse_code", "x"),
                E("sensor.zone_code", "zone.zone_code", "x"),
                E("door.warehouse_code", "wh.warehouse_code", "x"),
                E("door.door_nbr", "loc.location_code", "x", label="door_nbr 7 = location_code DOOR-07"),
                E("door.door_nbr", "rcv.dock_door", "x", label="door_nbr 7 = dock_door D07"),
                E("door.trailer_nbr", "rcv.trailer_nbr", "x"),
                E("zone.warehouse_id", "wh.warehouse_id", "fk"),
                E("loc.zone_id", "zone.zone_id", "fk"),
                E("rcv.warehouse_id", "wh.warehouse_id"),
            ],
        ),
    ]


def carrier_diagrams() -> list[Diagram]:
    return [
        Diagram(
            "er-carrier",
            "carrier_api",
            "Carrier tracking API and where tracking numbers join",
            [
                [
                    B("page", "carrier_api.tracking_events_page", "events, next_cursor, has_more"),
                    B(
                        "ev",
                        "carrier_api.tracking_event",
                        "event_id, tracking_nbr:key, scac, event_code, event_local_ts, exception_code, received_at, is_correction",
                    ),
                    B("shp", "carrier_api.shipment_tracking", "tracking_nbr, status, events"),
                    B(
                        "hist",
                        "carrier_api.tracking_history_csv",
                        "event_id, tracking_nbr, event_ts_utc, received_at_utc",
                    ),
                ],
                [
                    B("ship", "wms.shipment", "shipment_id, shipment_nbr, scac, tracking_nbr, shipment_status", gap=71),
                    B("ctn", "wms.shipment_carton", "carton_id, shipment_id, carton_nbr, tracking_nbr"),
                    B("carrier", "wms.carrier", "carrier_id, scac"),
                ],
                [
                    B(
                        "vit",
                        "ax2012.vendinvoicetrans",
                        "internalinvoiceid, name|TRK <tracking_nbr> WT …, lineamount",
                        gap=96,
                    ),
                    B("vij", "ax2012.vendinvoicejour", "internalinvoiceid, invoiceid, invoiceaccount, invoicedate"),
                    B("vend", "ax2012.vendtable", "accountnum, vendgroup|CARRIER, party"),
                ],
            ],
            [
                E("ev", "page.events", "part", side="L", label="events[]", card="||--o{"),
                E("ev", "shp.events", "part", side="L", label="events[]"),
                E("hist.event_id", "ev.event_id", side="L", label="same event, older than 90 days"),
                E("ev.tracking_nbr", "ship.tracking_nbr", "x"),
                E("ev.tracking_nbr", "ctn.tracking_nbr", "x", label="tracking_nbr (first carton)"),
                E("ev.scac", "carrier.scac", "x"),
                E("ctn.shipment_id", "ship.shipment_id", "fk"),
                E("vit.name", "ship.tracking_nbr", "x", label="name contains TRK <tracking_nbr>"),
                E("vit.internalinvoiceid", "vij.internalinvoiceid"),
                E("vij.invoiceaccount", "vend.accountnum"),
            ],
        ),
    ]


def edi_diagrams() -> list[Diagram]:
    return [
        Diagram(
            "er-edi-envelope",
            "edi",
            "EDI files, envelopes and 997 acknowledgments",
            [
                [
                    B("file", "edi.edi_file", "direction, set_id, client, file_ts, isa13"),
                    B("owner", "wms.owner", "owner_id, owner_code"),
                ],
                [
                    B("isa", "edi.edi_interchange", "ISA06, ISA08, ISA13, ISA14, IEA01", gap=24),
                    B("gs", "edi.edi_group", "GS01, GS02, GS03, GS06, GE01"),
                    B("st", "edi.edi_set", "ST01, ST02, SE01"),
                ],
                [
                    B("ak1", "edi.edi997_group", "AK1_01, AK1_02, AK9_01, AK9_02, AK9_04", gap=171),
                    B("ak2", "edi.edi997_set", "AK2_01, AK2_02, AK5_01"),
                ],
            ],
            [
                E("isa.ISA13", "file.isa13", "part", card="||--||"),
                E("file.client", "owner.owner_code", "x", side="L"),
                E("gs", "isa", "part"),
                E("st", "gs", "part"),
                E("ak1.AK1_01", "gs.GS01"),
                E("ak1.AK1_02", "gs.GS06"),
                E("ak2.AK2_01", "st.ST01"),
                E("ak2.AK2_02", "st.ST02"),
                E("ak2", "ak1", "part"),
            ],
        ),
        Diagram(
            "er-edi-orders",
            "edi",
            "EDI 940 in and 945 out: how they join WMS orders and shipments",
            [
                [
                    B("h940", "edi.edi940_header", "W05_02, W05_03, N1_DE_04, N1_WH_04, G62_10_02, W66_10"),
                    B("l940", "edi.edi940_line", "LX_01, W01_01, W01_03, W01_05, G69_01"),
                ],
                [
                    B("owner", "wms.owner", "owner_id, owner_code", gap=24),
                    B("wh", "wms.warehouse", "warehouse_id, warehouse_code"),
                    B("order", "wms.order_header", "order_id, order_nbr, client_order_ref, channel|EDI"),
                    B("mat", "wms.material", "material_id, sku"),
                    B("ship", "wms.shipment", "shipment_id, shipment_nbr, order_id, tracking_nbr, bol_nbr, scac"),
                ],
                [
                    B(
                        "h945",
                        "edi.edi945_header",
                        "W06_02|= 940 W05-02, W06_04, W06_05, W06_06, N9_BM_02, N9_CN_02, W27_02",
                        gap=90,
                    ),
                    B("l945", "edi.edi945_line", "LX_01, W12_01, W12_02, W12_03, W12_08"),
                ],
            ],
            [
                E("l940", "h940", "part", side="L"),
                E("h940.W05_03", "order.client_order_ref", "x"),
                E("h940.N1_DE_04", "owner.owner_code", "x"),
                E("h940.N1_WH_04", "wh.warehouse_code", "x"),
                E("l940.W01_05", "mat.sku", "x"),
                E("h945.W06_04", "ship.shipment_nbr", "x"),
                E("h945.W06_05", "order.order_nbr", "x"),
                E("h945.W06_06", "order.client_order_ref", "x"),
                E("h945.N9_BM_02", "ship.bol_nbr", "x"),
                E("h945.N9_CN_02", "ship.tracking_nbr", "x"),
                E("h945.W27_02", "ship.scac", "x"),
                E("l945.W12_08", "mat.sku", "x"),
                E("l945", "h945", "part"),
            ],
        ),
        Diagram(
            "er-edi-asn",
            "edi",
            "EDI 856 in: how advance ship notices join WMS ASNs",
            [
                [
                    B(
                        "s856",
                        "edi.edi856_shipment",
                        "BSN_02, DTM_017_02|→ expected_date, TD5_03|SCAC, N1_SF_02|→ supplier_name, N1_ST_04",
                    ),
                    B("o856", "edi.edi856_order", "HL_01, HL_02, PRF_01"),
                    B(
                        "i856",
                        "edi.edi856_item",
                        "HL_02, LIN_VN_03, LIN_LT|→ lot_nbr, SN1_02|→ expected_qty, DTM_036_02|→ expiry_date",
                    ),
                ],
                [
                    B(
                        "asn",
                        "wms.asn_header",
                        "asn_id, asn_nbr, warehouse_id, supplier_name, source|EDI856, expected_date",
                    ),
                    B("asnl", "wms.asn_line", "asn_id, line_nbr, material_id, expected_qty, lot_nbr, expiry_date"),
                    B("mat", "wms.material", "material_id, sku"),
                    B("wh", "wms.warehouse", "warehouse_id, warehouse_code"),
                ],
            ],
            [
                E("o856", "s856", "part", side="L"),
                E("i856", "o856", "part", side="L"),
                E("s856.BSN_02", "asn.asn_nbr", "x"),
                E("s856.N1_ST_04", "wh.warehouse_code", "x"),
                E("i856.LIN_VN_03", "mat.sku", "x"),
                E("asnl.asn_id", "asn.asn_id", "fk"),
                E("asnl.material_id", "mat.material_id"),
                E("asn.warehouse_id", "wh.warehouse_id"),
            ],
        ),
    ]


def file_diagrams() -> list[Diagram]:
    return [
        Diagram(
            "er-files-ops",
            "business_files",
            "Carrier rate cards and client item masters, and what they join in the WMS",
            [
                [
                    B(
                        "cover",
                        "business_files.rate_card_cover",
                        "SCAC, Effective_date, General_rate_increase, Mode, Currency",
                    ),
                    B("svc", "business_files.rate_card_service", "sheet_SVC, sheet_country, Weight, Zone_2|… Zone 8"),
                    B(
                        "fuel",
                        "business_files.rate_card_fuel",
                        "Diesel_from_USD_gal, Diesel_to_USD_gal, Ground_pct, Air_pct, LTL_pct",
                    ),
                    B("acc", "business_files.rate_card_accessorials", "Code, Description, Amount"),
                ],
                [
                    B("carrier", "wms.carrier", "carrier_id, scac, mode", gap=24),
                    B("svcw", "wms.carrier_service", "service_id, carrier_id, service_code"),
                    B("owner", "wms.owner", "owner_id, owner_code", gap=48),
                    B("mat", "wms.material", "material_id, owner_id, sku, upc"),
                ],
                [
                    B(
                        "csv",
                        "business_files.item_master_csv",
                        "client, sku, description, upc, base_uom, unit_weight, weight_uom, status, last_updated",
                        gap=120,
                    ),
                    B(
                        "spec",
                        "business_files.item_file_spec",
                        "Client, Delimiter, Encoding, Date_format, Header_style",
                    ),
                ],
            ],
            [
                E("cover.SCAC", "carrier.scac", "x"),
                E("svc.sheet_SVC", "svcw.service_code", "x"),
                E("svcw.carrier_id", "carrier.carrier_id", "fk"),
                E("mat.owner_id", "owner.owner_id", "fk"),
                E("csv.client", "owner.owner_code", "x"),
                E("csv.sku", "mat.sku", "x"),
                E("csv.upc", "mat.upc", "x"),
                E("spec.Client", "csv.client", label="same client folder"),
            ],
        ),
        Diagram(
            "er-files-reference",
            "business_files",
            "Reference files: the diesel series carrier bills are rated from, and where the build's signals came from",
            [
                [
                    B("diesel", "business_files.diesel_weekly", "week_start, diesel_usd_per_gal"),
                    B("src", "business_files.signal_sources", "signal, key, source, real_from, real_to, note", gap=24),
                ],
                [
                    B(
                        "fuelband",
                        "business_files.rate_card_fuel",
                        "Diesel_from_USD_gal, Diesel_to_USD_gal, Ground_pct, Air_pct, LTL_pct",
                    ),
                ],
            ],
            [
                E("diesel.diesel_usd_per_gal", "fuelband.Diesel_from_USD_gal", label="price falls in a band"),
            ],
        ),
        Diagram(
            "er-files-finance",
            "business_files",
            "Warehouse master and budget workbooks, and what they join in the WMS and AX",
            [
                [
                    B(
                        "sites",
                        "business_files.warehouse_master_sites",
                        "Site_Code, Site_Name, Status, Time_Zone, Legal_Entity|upper case, Dock_Doors, Temperature_Zones",
                    ),
                    B("zones", "business_files.warehouse_master_zones", "Site_Code, Zone, Temp_Min_C, Temp_Max_C"),
                ],
                [
                    B("wh", "wms.warehouse", "warehouse_id, warehouse_code, time_zone", gap=24),
                    B("zone", "wms.zone", "zone_id, warehouse_id, zone_code, zone_type"),
                    B("da", "ax2012.dataarea", "id, name"),
                    B("main", "ax2012.mainaccount", "recid, mainaccountid, name"),
                    B("cur", "ax2012.currency", "recid, currencycode"),
                ],
                [
                    B(
                        "budget",
                        "business_files.budget_sheet",
                        "Site, Account, Description, Jan|… Dec, FY_Total, Comment",
                        gap=48,
                    ),
                    B("assump", "business_files.budget_assumptions", "Currency, Budget_rate_USD_per_unit"),
                ],
            ],
            [
                E("sites.Site_Code", "wh.warehouse_code", "x"),
                E("sites.Legal_Entity", "da.id", "x"),
                E("zones.Site_Code", "sites.Site_Code", side="L"),
                E("zones.Zone", "zone.zone_type", "x"),
                E("zone.warehouse_id", "wh.warehouse_id", "fk"),
                E("budget.Site", "wh.warehouse_code", "x"),
                E("budget.Account", "main.mainaccountid", "x"),
                E("assump.Currency", "cur.currencycode", "x"),
            ],
        ),
    ]


def tracker_diagrams() -> list[Diagram]:
    return [
        Diagram(
            "er-tracker",
            "tracker",
            "Tracker database: issues, sprints and your own work",
            [
                [
                    B(
                        "sprint",
                        "tracker.sprint",
                        "id, name, project, state, start_date, end_date, completed_at",
                        gap=24,
                    ),
                    B("settings", "tracker.settings", "key, value"),
                    B("rank", "tracker.rank", "issue_key, rank"),
                    B("ac", "tracker.ac_check", "issue_key, idx, checked, checked_at"),
                ],
                [
                    B(
                        "issue",
                        "tracker.issue",
                        "key, num, source_id, type, status, priority, sprint_id, epic_key, parent_key, story_points, estimate_hours, created_at, resolved_at",
                    ),
                ],
                [
                    B("comment", "tracker.comment", "id, issue_key, author, created_at", gap=24),
                    B("worklog", "tracker.worklog", "id, issue_key, hours, work_date"),
                    B("history", "tracker.history", "id, issue_key, field, to_value, changed_at"),
                    B("link", "tracker.link", "id, from_key, to_key, link_type"),
                ],
            ],
            [
                E("issue.sprint_id", "sprint.id", "fk"),
                E("rank.issue_key", "issue.key", "fk"),
                E("ac.issue_key", "issue.key", "fk"),
                E("issue.epic_key", "issue.key"),
                E("issue.parent_key", "issue.key"),
                E("comment.issue_key", "issue.key", "fk"),
                E("worklog.issue_key", "issue.key", "fk"),
                E("history.issue_key", "issue.key", "fk"),
                E("link.from_key", "issue.key", "fk"),
                E("link.to_key", "issue.key", "fk"),
            ],
        ),
    ]


def diagrams() -> list[Diagram]:
    """Every ER diagram, in page order (fresh objects: layout() writes positions into the boxes)."""
    return [
        *wms_diagrams(),
        *ax_diagrams(),
        *salesforce_diagrams(),
        *carrier_diagrams(),
        *iot_diagrams(),
        *edi_diagrams(),
        *file_diagrams(),
        *tracker_diagrams(),
    ]


# ----------------------------------------------------------------------------------------------- page patching
def replace_block(html: str, name: str, content: str) -> str:
    pattern = re.compile(
        rf"(<!-- BEGIN GENERATED: {re.escape(name)} -->)(.*?)(<!-- END GENERATED: {re.escape(name)} -->)", re.DOTALL
    )
    if not pattern.search(html):
        raise SystemExit(f"marker for {name!r} not found in source-systems.html")
    return pattern.sub(lambda m: f"{m.group(1)}\n{content}\n{m.group(3)}", html, count=1)


def outputs(schema_dir: Path = SCHEMA_DIR, tracker_sql: Path = TRACKER_SQL) -> tuple[dict[str, str], dict[str, str]]:
    """(generated page blocks by name, Mermaid text by source)."""
    schemas = {
        "wms": parse_ddl(schema_dir / "wms.sql", "wms"),
        "ax2012": parse_ddl(schema_dir / "ax2012.sql", "ax2012"),
        "iot": parse_ddl(schema_dir / "iot.sql", "iot"),
    }
    sf = yaml.safe_load((schema_dir / "salesforce_objects.yaml").read_text(encoding="utf-8"))
    blocks = {f"dictionary-{k}": schema_html(k, s) for k, s in schemas.items()}
    blocks["dictionary-salesforce"] = salesforce_html(sf)
    blocks["er-crosskeys"] = diagram_crosskeys()
    cat = load_catalog(schema_dir, tracker_sql)
    ds = diagrams()
    fks = fk_children(ds)
    for d in ds:
        blocks[d.name] = svg_diagram(d, cat, fks)
    return blocks, {s: mermaid(s, cat, ds, fks) for s in SOURCES}


def build(root: Path, schema_dir: Path) -> dict[str, str]:
    return outputs(schema_dir)[0]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="simulator package root (holds handbook/)")
    parser.add_argument("--schema-dir", type=Path, default=SCHEMA_DIR, help="folder with the DDL and Salesforce YAML")
    args = parser.parse_args(argv)
    page = args.root / "handbook" / "source-systems.html"
    html = page.read_text(encoding="utf-8")
    blocks, mmd = outputs(args.schema_dir)
    for name, content in blocks.items():
        html = replace_block(html, name, content)
    page.write_text(html, encoding="utf-8")
    erd = args.root / "handbook" / "erd"
    erd.mkdir(exist_ok=True)
    for source, text in mmd.items():
        (erd / f"{source}.mmd").write_text(text, encoding="utf-8")
    cat = load_catalog(args.schema_dir)
    ds = diagrams()
    fks = fk_children(ds)
    for problem in (p for d in ds for p in layout_problems(d, cat, fks)):
        print(f"warning: {problem}")
    print(f"updated {page}: {', '.join(blocks)}")
    print(f"wrote {erd}: {', '.join(f'{s}.mmd' for s in mmd)}")


if __name__ == "__main__":
    main()
