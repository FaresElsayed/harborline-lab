"""SOQL subset compiled to parameterized SQL over ``sim_internal.sfdc.record`` (JSONB ``data``).

Supported: ``SELECT`` field list (one-level parent paths such as ``Account.Name``, ``COUNT()``,
``COUNT(field)``, ``FIELDS(ALL|STANDARD|CUSTOM)``), ``FROM`` with optional alias, ``WHERE`` with
``= != <> < <= > >= LIKE IN NOT IN INCLUDES EXCLUDES``, ``AND / OR / NOT`` and parentheses, date
literals (``TODAY``, ``YESTERDAY``, ``LAST_N_DAYS:n`` ...), ``ORDER BY`` (``ASC|DESC``, ``NULLS FIRST|LAST``),
``LIMIT`` and ``OFFSET``. Comparisons are typed by the field's Salesforce type, string comparisons are
case-insensitive and ``!=`` matches nulls, as in Salesforce. Date literals use the simulator clock in UTC
(the integration user's time zone is GMT).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from harborline.ids import to_id18

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schema" / "salesforce_objects.yaml"

TEXT_TYPES = {"string", "textarea", "email", "phone", "url", "picklist", "combobox", "multipicklist", "encryptedstring"}
NUMBER_TYPES = {"int", "double", "currency", "percent", "long"}
ID_TYPES = {"id", "reference"}

# Fields stored as columns of sfdc.record rather than inside the JSONB document.
SYSTEM_COLUMNS = {
    "Id": "id",
    "IsDeleted": "is_deleted",
    "CreatedDate": "created_date",
    "LastModifiedDate": "last_modified_date",
    "SystemModstamp": "system_modstamp",
}

NON_NILLABLE_SYSTEM = {"Id", "IsDeleted", "CreatedDate", "CreatedById", "LastModifiedDate", "LastModifiedById",
                       "SystemModstamp", "OwnerId"}

OFFSET_MAX = 2000
FIELDS_FN_LIMIT = 200


class SoqlError(Exception):
    """A query error with a Salesforce error code (rendered as ``[{"message", "errorCode"}]``)."""

    def __init__(self, code: str, message: str, soql: str | None = None, pos: int | None = None):
        self.code = code
        self.raw_message = message
        if soql is not None and pos is not None:
            row = soql.count("\n", 0, pos) + 1
            line_start = soql.rfind("\n", 0, pos) + 1
            line_end = soql.find("\n", pos)
            line = soql[line_start : None if line_end < 0 else line_end]
            col = pos - line_start
            message = f"\n{line}\n{' ' * col}^\nERROR at Row:{row}:Column:{col + 1}\n{message}"
        super().__init__(message)
        self.message = message


# --------------------------------------------------------------------------------------------------
# Object model
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SfField:
    name: str
    type: str
    label: str
    length: int = 0
    nillable: bool = True
    updateable: bool = False
    custom: bool = False
    reference_to: tuple[str, ...] = ()
    relationship_name: str | None = None
    picklist: tuple[str, ...] = ()
    precision: int = 0
    scale: int = 0
    auto_number: bool = False
    relationship_order: int | None = None

    @property
    def is_text(self) -> bool:
        return self.type in TEXT_TYPES

    @property
    def is_number(self) -> bool:
        return self.type in NUMBER_TYPES


@dataclass(frozen=True)
class SObject:
    name: str
    key_prefix: str
    label: str
    label_plural: str
    custom: bool
    queryable_only: bool
    fields: tuple[SfField, ...]

    def field(self, name: str) -> SfField | None:
        lowered = name.lower()
        for f in self.fields:
            if f.name.lower() == lowered:
                return f
        return None

    def relationship(self, name: str) -> SfField | None:
        lowered = name.lower()
        for f in self.fields:
            if f.relationship_name and f.relationship_name.lower() == lowered:
                return f
        return None


@dataclass(frozen=True)
class Schema:
    org_id: str
    api_version: str
    objects: dict[str, SObject]  # lower-case name -> object

    def get(self, name: str) -> SObject | None:
        return self.objects.get(name.lower())

    def by_prefix(self, prefix: str) -> SObject | None:
        for obj in self.objects.values():
            if obj.key_prefix == prefix:
                return obj
        return None

    def with_custom_fields(self, rows: list[tuple[str, str, dict[str, Any]]]) -> Schema:
        """Merge runtime schema-drift fields (rows of sfdc.custom_field) into a copy of the schema."""
        if not rows:
            return self
        objects = dict(self.objects)
        for obj_name, api_name, definition in rows:
            obj = objects.get(obj_name.lower())
            if obj is None or obj.field(api_name):
                continue
            spec = {"custom": True, **(definition or {}), "name": api_name}
            objects[obj.name.lower()] = replace(obj, fields=obj.fields + (make_field(spec),))
        return replace(self, objects=objects)


_DEFAULT_LENGTH = {
    "id": 18, "reference": 18, "email": 80, "phone": 40, "url": 255, "picklist": 255, "combobox": 255,
    "multipicklist": 4099, "textarea": 32000, "string": 255,
}


def _label_from_name(name: str, ftype: str) -> str:
    base = name.removesuffix("__c")
    if "_" in base:
        return base.replace("_", " ")
    if ftype in ID_TYPES and base.endswith("Id") and len(base) > 2:
        return _label_from_name(base[:-2], "string") + " ID"
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", base)


def _relationship_name(name: str) -> str | None:
    if name.endswith("__c"):
        return name[:-3] + "__r"
    if name.endswith("Id") and len(name) > 2:
        return name[:-2]
    return None


def make_field(spec: dict[str, Any]) -> SfField:
    name = str(spec["name"])
    ftype = str(spec.get("type", "string")).lower()
    reference_to = tuple(spec.get("referenceTo") or spec.get("reference_to") or ())
    is_ref = ftype == "reference"
    nillable_default = ftype != "boolean" and name not in NON_NILLABLE_SYSTEM
    decimal_type = ftype in ("double", "currency", "percent")
    precision = int(spec.get("precision") or (18 if decimal_type else 0))
    return SfField(
        name=name,
        type=ftype,
        label=str(spec.get("label") or _label_from_name(name, ftype)),
        length=int(spec.get("length") or _DEFAULT_LENGTH.get(ftype, 0)),
        nillable=bool(spec.get("nillable", nillable_default)),
        updateable=bool(spec.get("updateable", False)),
        custom=bool(spec.get("custom", name.endswith("__c"))),
        reference_to=reference_to,
        relationship_name=_relationship_name(name) if is_ref else None,
        picklist=tuple(str(v) for v in (spec.get("values") or ())),
        precision=precision,
        scale=int(spec["scale"]) if spec.get("scale") is not None else (2 if decimal_type else 0),
        auto_number=bool(spec.get("auto_number", False)),
        relationship_order=1 if spec.get("relationship") == "master-detail" else None,
    )


def _plural(label: str) -> str:
    if label.endswith("y") and not label.endswith("ey"):
        return label[:-1] + "ies"
    if label.endswith(("s", "x")):
        return label + "es"
    return label + "s"


@cache
def base_schema(path: str = str(SCHEMA_PATH)) -> Schema:
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    system = {f["name"]: f for f in doc["system_fields"]}
    objects: dict[str, SObject] = {}
    for name, spec in doc["objects"].items():
        own = list(spec.get("fields", []))
        own_names = {f["name"] for f in own}
        specs: list[dict[str, Any]] = [system["Id"], system["IsDeleted"]]
        specs += own
        if spec.get("has_owner") and "OwnerId" not in own_names:
            specs.append({"name": "OwnerId", "type": "reference", "label": "Owner ID", "referenceTo": ["User"]})
        specs += [system[n] for n in ("CreatedDate", "CreatedById", "LastModifiedDate", "LastModifiedById",
                                      "SystemModstamp")]
        auto_name = (spec.get("name_field") or {}).get("type") == "autonumber"
        fields = []
        for f in specs:
            fld = make_field(f)
            if auto_name and fld.name == "Name":
                fld = replace(fld, auto_number=True, nillable=False)
            fields.append(fld)
        label = spec.get("label") or _label_from_name(name, "string")
        objects[name.lower()] = SObject(
            name=name,
            key_prefix=str(spec["key_prefix"]),
            label=label,
            label_plural=spec.get("label_plural") or _plural(label),
            custom=bool(spec.get("custom", name.endswith("__c"))),
            queryable_only=bool(spec.get("queryable_only", False)),
            fields=tuple(fields),
        )
    return Schema(org_id=str(doc["org_id"]), api_version=str(doc["api_version"]), objects=objects)


# --------------------------------------------------------------------------------------------------
# Tokenizer
# --------------------------------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
   |(?P<string>'(?:\\.|[^'\\])*')
   |(?P<datetime>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?(?:Z|[+-]\d{2}:?\d{2}))
   |(?P<date>\d{4}-\d{2}-\d{2}(?![\dT]))
   |(?P<number>-?\d+(?:\.\d+)?)
   |(?P<ident>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*(?::\d+)?)
   |(?P<op>!=|<>|<=|>=|=|<|>)
   |(?P<punct>[(),])
    """,
    re.VERBOSE,
)

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "'": "'", '"': '"', "\\": "\\"}


@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    pos: int

    @property
    def upper(self) -> str:
        return self.text.upper()


def _unescape(body: str) -> str:
    out, i = [], 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            # \% and \_ stay escaped: they only mean something inside LIKE patterns.
            out.append("\\" + nxt if nxt in "%_" else _ESCAPES.get(nxt, nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def tokenize(soql: str) -> list[Token]:
    tokens: list[Token] = []
    pos = 0
    while pos < len(soql):
        m = _TOKEN_RE.match(soql, pos)
        if not m:
            snippet = soql[pos : pos + 1]
            raise SoqlError("MALFORMED_QUERY", f"unexpected token: '{snippet}'", soql, pos)
        kind = m.lastgroup or ""
        if kind != "ws":
            tokens.append(Token(kind, m.group(), pos))
        pos = m.end()
    return tokens


# --------------------------------------------------------------------------------------------------
# AST + parser
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldPath:
    text: str
    pos: int

    @property
    def parts(self) -> list[str]:
        return self.text.split(".")


@dataclass(frozen=True)
class CountItem:
    field: FieldPath | None
    alias: str | None
    pos: int


@dataclass(frozen=True)
class FieldsFn:
    kind: str  # ALL | STANDARD | CUSTOM
    pos: int


@dataclass(frozen=True)
class Value:
    kind: str  # string | number | bool | null | date | datetime | literal
    raw: Any
    pos: int
    n: int | None = None  # for LAST_N_DAYS:n style literals


@dataclass(frozen=True)
class Cmp:
    field: FieldPath
    op: str  # = != < <= > >= LIKE IN NOTIN INCLUDES EXCLUDES
    value: Value | tuple[Value, ...]
    pos: int


@dataclass(frozen=True)
class BoolOp:
    op: str  # AND | OR
    items: tuple[Any, ...]


@dataclass(frozen=True)
class NotOp:
    item: Any


@dataclass(frozen=True)
class OrderItem:
    field: FieldPath
    desc: bool
    nulls: str | None


@dataclass
class Query:
    select: list[FieldPath | CountItem | FieldsFn]
    obj: str
    obj_pos: int
    alias: str | None = None
    where: Any = None
    order: list[OrderItem] = field(default_factory=list)
    limit: int | None = None
    offset: int | None = None


DATE_LITERALS = {
    "TODAY", "YESTERDAY", "TOMORROW", "LAST_WEEK", "THIS_WEEK", "NEXT_WEEK", "LAST_MONTH", "THIS_MONTH",
    "NEXT_MONTH", "LAST_90_DAYS", "NEXT_90_DAYS", "THIS_QUARTER", "LAST_QUARTER", "NEXT_QUARTER", "THIS_YEAR",
    "LAST_YEAR", "NEXT_YEAR",
}
N_DATE_LITERALS = {"LAST_N_DAYS", "NEXT_N_DAYS", "N_DAYS_AGO", "LAST_N_WEEKS", "NEXT_N_WEEKS", "LAST_N_MONTHS",
                   "NEXT_N_MONTHS"}
_RESERVED_AFTER_FROM = {"WHERE", "ORDER", "LIMIT", "OFFSET", "GROUP", "HAVING", "WITH", "FOR", "USING", "UPDATE"}
_UNSUPPORTED = {"GROUP", "HAVING", "WITH", "FOR", "USING", "TYPEOF", "UPDATE"}
_SUPPORTED_HINT = " (this sandbox supports SELECT, FROM, WHERE, ORDER BY, LIMIT and OFFSET)"


class Parser:
    def __init__(self, soql: str):
        self.soql = soql
        self.tokens = tokenize(soql)
        self.i = 0

    # -- token helpers
    def peek(self, offset: int = 0) -> Token | None:
        j = self.i + offset
        return self.tokens[j] if j < len(self.tokens) else None

    def error(self, message: str, tok: Token | None = None, code: str = "MALFORMED_QUERY") -> SoqlError:
        pos = tok.pos if tok else len(self.soql.rstrip())
        return SoqlError(code, message, self.soql, pos)

    def unexpected(self, tok: Token | None) -> SoqlError:
        if tok is None:
            return self.error("unexpected token: <EOF>")
        hint = _SUPPORTED_HINT if tok.upper in _UNSUPPORTED else ""
        return self.error(f"unexpected token: '{tok.text}'{hint}", tok)

    def next(self) -> Token:
        tok = self.peek()
        if tok is None:
            raise self.unexpected(None)
        self.i += 1
        return tok

    def accept_kw(self, *words: str) -> Token | None:
        tok = self.peek()
        if tok is not None and tok.kind == "ident" and tok.upper in words:
            self.i += 1
            return tok
        return None

    def expect_kw(self, word: str) -> Token:
        tok = self.accept_kw(word)
        if tok is None:
            raise self.unexpected(self.peek())
        return tok

    def accept_punct(self, ch: str) -> Token | None:
        tok = self.peek()
        if tok is not None and tok.kind == "punct" and tok.text == ch:
            self.i += 1
            return tok
        return None

    def expect_punct(self, ch: str) -> Token:
        tok = self.accept_punct(ch)
        if tok is None:
            raise self.unexpected(self.peek())
        return tok

    # -- grammar
    def parse(self) -> Query:
        if not self.tokens:
            raise SoqlError("MALFORMED_QUERY", "A query string has to be specified")
        self.expect_kw("SELECT")
        select = self.parse_select_list()
        self.expect_kw("FROM")
        obj_tok = self.next()
        if obj_tok.kind != "ident" or "." in obj_tok.text:
            raise self.unexpected(obj_tok)
        query = Query(select=select, obj=obj_tok.text, obj_pos=obj_tok.pos)
        alias_tok = self.peek()
        if alias_tok is not None and alias_tok.kind == "ident" and alias_tok.upper not in _RESERVED_AFTER_FROM:
            if alias_tok.upper == "AS":
                self.i += 1
                alias_tok = self.next()
            else:
                self.i += 1
            query.alias = alias_tok.text
        if self.accept_kw("WHERE"):
            query.where = self.parse_condition()
        if self.accept_kw("ORDER"):
            self.expect_kw("BY")
            query.order = self.parse_order()
        if self.accept_kw("LIMIT"):
            query.limit = self.parse_int()
        if self.accept_kw("OFFSET"):
            tok = self.peek()
            query.offset = self.parse_int()
            if query.offset > OFFSET_MAX:
                raise SoqlError("NUMBER_OUTSIDE_VALID_RANGE", f"Maximum SOQL offset allowed is {OFFSET_MAX}",
                                self.soql, tok.pos if tok else None)
        if self.peek() is not None:
            raise self.unexpected(self.peek())
        return query

    def parse_int(self) -> int:
        tok = self.next()
        if tok.kind != "number" or not tok.text.isdigit():
            raise self.unexpected(tok)
        return int(tok.text)

    def parse_select_list(self) -> list[FieldPath | CountItem | FieldsFn]:
        items: list[FieldPath | CountItem | FieldsFn] = []
        while True:
            tok = self.next()
            if tok.kind == "punct" and tok.text == "(":
                raise self.error("Parent-to-child subqueries are not supported" + _SUPPORTED_HINT, tok)
            if tok.kind != "ident":
                raise self.unexpected(tok)
            if tok.upper == "COUNT" and self.accept_punct("("):
                arg = None
                if not self.accept_punct(")"):
                    ftok = self.next()
                    if ftok.kind != "ident":
                        raise self.unexpected(ftok)
                    arg = FieldPath(ftok.text, ftok.pos)
                    self.expect_punct(")")
                alias = None
                nxt = self.peek()
                if nxt is not None and nxt.kind == "ident" and nxt.upper != "FROM":
                    alias = self.next().text
                items.append(CountItem(arg, alias, tok.pos))
            elif tok.upper == "FIELDS" and self.accept_punct("("):
                kind_tok = self.next()
                if kind_tok.upper not in ("ALL", "STANDARD", "CUSTOM"):
                    raise self.unexpected(kind_tok)
                self.expect_punct(")")
                items.append(FieldsFn(kind_tok.upper, tok.pos))
            elif tok.upper in _UNSUPPORTED:
                raise self.unexpected(tok)
            else:
                nxt = self.peek()
                if nxt is not None and nxt.kind == "punct" and nxt.text == "(":
                    raise self.error(f"Aggregate or function '{tok.text}' is not supported" + _SUPPORTED_HINT, tok)
                items.append(FieldPath(tok.text, tok.pos))
            if not self.accept_punct(","):
                return items

    def parse_condition(self) -> Any:
        items = [self.parse_unary()]
        joiner: str | None = None
        while True:
            tok = self.peek()
            if tok is None or tok.kind != "ident" or tok.upper not in ("AND", "OR"):
                break
            if joiner is not None and tok.upper != joiner:
                # SOQL requires parentheses when AND and OR are mixed at the same level.
                raise self.unexpected(tok)
            joiner = tok.upper
            self.i += 1
            items.append(self.parse_unary())
        return items[0] if len(items) == 1 else BoolOp(joiner or "AND", tuple(items))

    def parse_unary(self) -> Any:
        if self.accept_kw("NOT"):
            return NotOp(self.parse_unary())
        if self.accept_punct("("):
            cond = self.parse_condition()
            self.expect_punct(")")
            return cond
        return self.parse_comparison()

    def parse_comparison(self) -> Cmp:
        ftok = self.next()
        if ftok.kind != "ident":
            raise self.unexpected(ftok)
        path = FieldPath(ftok.text, ftok.pos)
        tok = self.next()
        if tok.kind == "op":
            op = "!=" if tok.text == "<>" else tok.text
            return Cmp(path, op, self.parse_value(), tok.pos)
        word = tok.upper if tok.kind == "ident" else ""
        if word == "LIKE":
            return Cmp(path, "LIKE", self.parse_value(), tok.pos)
        if word == "NOT" and self.accept_kw("IN"):
            return Cmp(path, "NOTIN", self.parse_value_list(), tok.pos)
        if word in ("IN", "INCLUDES", "EXCLUDES"):
            return Cmp(path, word, self.parse_value_list(), tok.pos)
        raise self.unexpected(tok)

    def parse_value_list(self) -> tuple[Value, ...]:
        self.expect_punct("(")
        values = [self.parse_value()]
        while self.accept_punct(","):
            values.append(self.parse_value())
        self.expect_punct(")")
        return tuple(values)

    def parse_value(self) -> Value:
        tok = self.next()
        if tok.kind == "string":
            return Value("string", _unescape(tok.text[1:-1]), tok.pos)
        if tok.kind == "number":
            return Value("number", Decimal(tok.text), tok.pos)
        if tok.kind == "date":
            try:
                return Value("date", date.fromisoformat(tok.text), tok.pos)
            except ValueError:
                raise self.error(f"line 1:{tok.pos} no viable alternative at character '{tok.text}'", tok) from None
        if tok.kind == "datetime":
            return Value("datetime", _parse_datetime(tok.text, self, tok), tok.pos)
        if tok.kind == "ident":
            word = tok.upper
            if word in ("TRUE", "FALSE"):
                return Value("bool", word == "TRUE", tok.pos)
            if word == "NULL":
                return Value("null", None, tok.pos)
            if word in DATE_LITERALS:
                return Value("literal", word, tok.pos)
            name, _, n = word.partition(":")
            if name in N_DATE_LITERALS and n.isdigit():
                return Value("literal", name, tok.pos, int(n))
            if name in N_DATE_LITERALS:
                raise self.error(f"Invalid date literal: {tok.text}", tok)
        raise self.unexpected(tok)

    def parse_order(self) -> list[OrderItem]:
        items = []
        while True:
            ftok = self.next()
            if ftok.kind != "ident":
                raise self.unexpected(ftok)
            desc = False
            if self.accept_kw("DESC"):
                desc = True
            else:
                self.accept_kw("ASC")
            nulls = None
            if self.accept_kw("NULLS"):
                ntok = self.accept_kw("FIRST", "LAST")
                if ntok is None:
                    raise self.unexpected(self.peek())
                nulls = ntok.upper
            items.append(OrderItem(FieldPath(ftok.text, ftok.pos), desc, nulls))
            if not self.accept_punct(","):
                return items


def _parse_datetime(text: str, parser: Parser, tok: Token) -> datetime:
    norm = text.replace("Z", "+00:00")
    if re.search(r"[+-]\d{4}$", norm):
        norm = norm[:-2] + ":" + norm[-2:]
    try:
        return datetime.fromisoformat(norm).astimezone(UTC)
    except ValueError:
        raise parser.error(f"line 1:{tok.pos} no viable alternative at character '{text}'", tok) from None


def parse(soql: str) -> Query:
    return Parser(soql).parse()


# --------------------------------------------------------------------------------------------------
# Date literal ranges
# --------------------------------------------------------------------------------------------------


def _month_start(d: date, add: int = 0) -> date:
    idx = d.year * 12 + d.month - 1 + add
    return date(idx // 12, idx % 12 + 1, 1)


def date_literal_range(value: Value, today: date) -> tuple[date, date]:
    """[start, end) dates covered by a SOQL date literal, relative to ``today``."""
    name, n = value.raw, value.n or 0
    week_start = today - timedelta(days=(today.weekday() + 1) % 7)  # weeks start on Sunday (en_US)
    quarter_start = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
    ranges: dict[str, tuple[date, date]] = {
        "TODAY": (today, today + timedelta(days=1)),
        "YESTERDAY": (today - timedelta(days=1), today),
        "TOMORROW": (today + timedelta(days=1), today + timedelta(days=2)),
        "THIS_WEEK": (week_start, week_start + timedelta(days=7)),
        "LAST_WEEK": (week_start - timedelta(days=7), week_start),
        "NEXT_WEEK": (week_start + timedelta(days=7), week_start + timedelta(days=14)),
        "THIS_MONTH": (_month_start(today), _month_start(today, 1)),
        "LAST_MONTH": (_month_start(today, -1), _month_start(today)),
        "NEXT_MONTH": (_month_start(today, 1), _month_start(today, 2)),
        "LAST_90_DAYS": (today - timedelta(days=90), today + timedelta(days=1)),
        "NEXT_90_DAYS": (today + timedelta(days=1), today + timedelta(days=91)),
        "THIS_QUARTER": (quarter_start, _month_start(quarter_start, 3)),
        "LAST_QUARTER": (_month_start(quarter_start, -3), quarter_start),
        "NEXT_QUARTER": (_month_start(quarter_start, 3), _month_start(quarter_start, 6)),
        "THIS_YEAR": (date(today.year, 1, 1), date(today.year + 1, 1, 1)),
        "LAST_YEAR": (date(today.year - 1, 1, 1), date(today.year, 1, 1)),
        "NEXT_YEAR": (date(today.year + 1, 1, 1), date(today.year + 2, 1, 1)),
        # LAST_N_DAYS:n starts n days ago and includes today.
        "LAST_N_DAYS": (today - timedelta(days=n), today + timedelta(days=1)),
        "NEXT_N_DAYS": (today + timedelta(days=1), today + timedelta(days=n + 1)),
        "N_DAYS_AGO": (today - timedelta(days=n), today - timedelta(days=n - 1)),
        "LAST_N_WEEKS": (week_start - timedelta(days=7 * n), week_start),
        "NEXT_N_WEEKS": (week_start + timedelta(days=7), week_start + timedelta(days=7 * (n + 1))),
        "LAST_N_MONTHS": (_month_start(today, -n), _month_start(today)),
        "NEXT_N_MONTHS": (_month_start(today, 1), _month_start(today, n + 1)),
    }
    return ranges[name]


def _day_start(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


# --------------------------------------------------------------------------------------------------
# Compiler
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Join:
    alias: str
    relationship: str  # canonical relationship name (e.g. Account, Owner, Rate_Card__r)
    ref_field: SfField
    targets: tuple[str, ...]


@dataclass(frozen=True)
class OutCol:
    """One projected field. ``join`` is None for the queried object's own fields."""

    field: SfField
    join: Join | None
    select_index: int  # index into the page-query row


@dataclass
class Compiled:
    soql: str
    obj: SObject
    count_only: bool = False
    count_alias: str | None = None
    count_field_sql: str | None = None
    columns: list[OutCol] = field(default_factory=list)
    joins: dict[str, Join] = field(default_factory=dict)
    select_exprs: list[str] = field(default_factory=list)
    parent_index: dict[str, int] = field(default_factory=dict)  # join alias -> row index of parent id (object next)
    where_sql: str = "TRUE"
    params: list[Any] = field(default_factory=list)
    order_sql: str = ""
    limit: int | None = None
    offset: int | None = None

    def _from(self) -> str:
        parts = ["sfdc.record r"]
        for j in self.joins.values():
            parts.append(f"LEFT JOIN sfdc.record {j.alias} ON {j.alias}.id = ({_ref_expr('r', j.ref_field)})::bpchar")
        return " ".join(parts)

    def _filter(self, include_deleted: bool, purge_before: datetime) -> tuple[str, list[Any]]:
        if include_deleted:
            vis = "(NOT r.is_deleted OR coalesce(r.deleted_date, r.system_modstamp) >= %s)"
            vis_params: list[Any] = [purge_before]
        else:
            vis, vis_params = "NOT r.is_deleted", []
        return f"r.object = %s AND {vis} AND ({self.where_sql})", [self.obj.name, *vis_params, *self.params]

    def ids_sql(self, include_deleted: bool, purge_before: datetime) -> tuple[str, list[Any]]:
        where, params = self._filter(include_deleted, purge_before)
        order = f"{self.order_sql}, r.id" if self.order_sql else "r.id"
        sql = f"SELECT r.id FROM {self._from()} WHERE {where} ORDER BY {order}"
        if self.limit is not None:
            sql += f" LIMIT {int(self.limit)}"
        if self.offset:
            sql += f" OFFSET {int(self.offset)}"
        return sql, params

    def count_sql(self, include_deleted: bool, purge_before: datetime) -> tuple[str, list[Any]]:
        where, params = self._filter(include_deleted, purge_before)
        if self.count_field_sql:
            where = f"{where} AND {self.count_field_sql} IS NOT NULL"
        inner = f"SELECT 1 FROM {self._from()} WHERE {where}"
        if self.limit is not None:
            inner += f" LIMIT {int(self.limit)}"
        if self.offset:
            inner += f" OFFSET {int(self.offset)}"
        return f"SELECT count(*) FROM ({inner}) s", params

    def page_sql(self) -> str:
        cols = ", ".join(["r.id", "r.object", *self.select_exprs])
        return f"SELECT {cols} FROM {self._from()} WHERE r.id = ANY(%s::bpchar[])"

    @property
    def headers(self) -> list[str]:
        """Column names as Salesforce prints them in Bulk CSV results (``Account.Name``)."""
        return [f"{c.join.relationship}.{c.field.name}" if c.join else c.field.name for c in self.columns]


def _ref_expr(alias: str, f: SfField) -> str:
    if f.name == "MasterRecordId":
        return f"coalesce({alias}.master_record_id::text, nullif({alias}.data->>'MasterRecordId', ''))"
    return f"nullif({alias}.data->>'{f.name}', '')"


def raw_expr(alias: str, f: SfField) -> str:
    """Projection expression: the stored value (column or JSONB node), converted in Python."""
    if f.name in SYSTEM_COLUMNS:
        return f"{alias}.{SYSTEM_COLUMNS[f.name]}"
    if f.name == "MasterRecordId":
        return _ref_expr(alias, f)
    return f"{alias}.data->'{f.name}'"


def typed_expr(alias: str, f: SfField) -> str:
    """Typed SQL expression used for filtering and sorting."""
    if f.name in SYSTEM_COLUMNS:
        return f"{alias}.{SYSTEM_COLUMNS[f.name]}"
    if f.type in ID_TYPES:
        return _ref_expr(alias, f)
    text = f"{alias}.data->>'{f.name}'"
    if f.type == "boolean":
        return f"coalesce(CASE WHEN jsonb_typeof({alias}.data->'{f.name}') = 'boolean' THEN ({text})::boolean END, false)"
    if f.is_number:
        return f"CASE WHEN jsonb_typeof({alias}.data->'{f.name}') = 'number' THEN ({text})::numeric END"
    if f.type == "date":
        return f"CASE WHEN pg_input_is_valid({text}, 'date') THEN ({text})::date END"
    if f.type == "datetime":
        return f"CASE WHEN pg_input_is_valid({text}, 'timestamptz') THEN ({text})::timestamptz END"
    return f"nullif({text}, '')"


class Compiler:
    def __init__(self, soql: str, schema: Schema, now: datetime):
        self.soql = soql
        self.schema = schema
        self.today = now.astimezone(UTC).date()
        self.query = parse(soql)
        obj = schema.get(self.query.obj)
        if obj is None:
            raise SoqlError(
                "INVALID_TYPE",
                f"sObject type '{self.query.obj}' is not supported. If you are attempting to use a custom object, "
                "be sure to append the '__c' after the entity name. Please reference your WSDL or the describe call "
                "for the appropriate names.",
                soql,
                self.query.obj_pos,
            )
        self.out = Compiled(soql=soql, obj=obj, limit=self.query.limit, offset=self.query.offset)

    # -- field resolution
    def resolve(self, path: FieldPath) -> tuple[str, SfField, Join | None]:
        parts = path.parts
        obj = self.out.obj
        if len(parts) > 1 and (parts[0].lower() == obj.name.lower() or
                               (self.query.alias and parts[0].lower() == self.query.alias.lower())):
            parts = parts[1:]
        if len(parts) == 1:
            f = obj.field(parts[0])
            if f is None:
                raise self._no_column(parts[0], obj.name, path.pos)
            return "r", f, None
        if len(parts) > 2:
            raise SoqlError("INVALID_FIELD", "Only one level of parent relationship is supported in this sandbox: "
                            f"'{path.text}'", self.soql, path.pos)
        ref = obj.relationship(parts[0])
        if ref is None:
            raise SoqlError(
                "INVALID_FIELD",
                f"Didn't understand relationship '{parts[0]}' in field path. If you are attempting to use a custom "
                "relationship, be sure to append the '__r' after the custom relationship name. Please reference your "
                "WSDL or the describe call for the appropriate names.",
                self.soql,
                path.pos,
            )
        target_field = None
        for target_name in ref.reference_to:
            target = self.schema.get(target_name)
            if target and (target_field := target.field(parts[1])):
                break
        if target_field is None:
            raise self._no_column(parts[1], ref.reference_to[0] if ref.reference_to else parts[0], path.pos)
        join = self.out.joins.get(ref.name)
        if join is None:
            join = Join(f"p{len(self.out.joins) + 1}", ref.relationship_name or parts[0], ref, ref.reference_to)
            self.out.joins[ref.name] = join
        return join.alias, target_field, join

    def _no_column(self, name: str, obj_name: str, pos: int) -> SoqlError:
        return SoqlError(
            "INVALID_FIELD",
            f"No such column '{name}' on entity '{obj_name}'. If you are attempting to use a custom field, be sure "
            "to append the '__c' after the custom field name. Please reference your WSDL or the describe call for "
            "the appropriate names.",
            self.soql,
            pos,
        )

    # -- select
    def compile_select(self) -> None:
        items = self.query.select
        counts = [i for i in items if isinstance(i, CountItem)]
        if counts:
            if len(items) > 1:
                raise SoqlError("MALFORMED_QUERY", "COUNT() cannot be combined with other fields in this sandbox",
                                self.soql, counts[0].pos)
            item = counts[0]
            self.out.count_only = True
            if item.field is not None:
                alias, f, _ = self.resolve(item.field)
                self.out.count_field_sql = typed_expr(alias, f)
                self.out.count_alias = item.alias or "expr0"
            return
        seen: set[str] = set()
        for item in items:
            paths = self._expand_fields(item) if isinstance(item, FieldsFn) else [item]
            for path in paths:
                alias, f, join = self.resolve(path)
                key = f"{join.relationship if join else ''}.{f.name}".lower()
                if key in seen:
                    if isinstance(item, FieldsFn):
                        continue
                    raise SoqlError("DUPLICATE_FIELD", f"duplicate field selected: {f.name}", self.soql, path.pos)
                seen.add(key)
                self.out.columns.append(OutCol(f, join, len(self.out.select_exprs) + 2))
                self.out.select_exprs.append(raw_expr(alias, f))
        # Parent attributes need the parent's id and object type.
        for join in self.out.joins.values():
            self.out.parent_index[join.alias] = len(self.out.select_exprs) + 2
            self.out.select_exprs.append(f"{join.alias}.id")
            self.out.select_exprs.append(f"{join.alias}.object")

    def _expand_fields(self, fn: FieldsFn) -> list[FieldPath]:
        if fn.kind in ("ALL", "CUSTOM") and (self.query.limit is None or self.query.limit > FIELDS_FN_LIMIT):
            raise SoqlError("MALFORMED_QUERY", f"The SOQL FIELDS function must have a LIMIT of at most "
                            f"{FIELDS_FN_LIMIT}", self.soql, fn.pos)
        fields = self.out.obj.fields
        if fn.kind == "CUSTOM":
            fields = tuple(f for f in fields if f.custom)
        elif fn.kind == "STANDARD":
            fields = tuple(f for f in fields if not f.custom)
        return [FieldPath(f.name, fn.pos) for f in fields]

    # -- where
    def compile_where(self) -> None:
        if self.query.where is not None:
            self.out.where_sql = self.cond(self.query.where)

    def cond(self, node: Any) -> str:
        if isinstance(node, BoolOp):
            return "(" + f" {node.op} ".join(self.cond(i) for i in node.items) + ")"
        if isinstance(node, NotOp):
            return f"(NOT coalesce({self.cond(node.item)}, false))"
        return self.comparison(node)

    def param(self, value: Any, cast: str = "") -> str:
        self.out.params.append(value)
        return f"%s{cast}"

    def _type_error(self, f: SfField, v: Value, expected: str) -> SoqlError:
        quoted = "should be enclosed in quotes" if expected == "string" else "should not be enclosed in quotes"
        return SoqlError("INVALID_QUERY_FILTER_OPERATOR",
                         f"value of filter criterion for field '{f.name}' must be of type {expected} and {quoted}",
                         self.soql, v.pos)

    def comparison(self, c: Cmp) -> str:
        alias, f, _ = self.resolve(c.field)
        expr = typed_expr(alias, f)
        if c.op in ("IN", "NOTIN", "INCLUDES", "EXCLUDES"):
            return self.list_comparison(c, f, expr)
        v = c.value
        assert isinstance(v, Value)
        if c.op == "LIKE":
            if not f.is_text and f.type not in ID_TYPES:
                raise SoqlError("INVALID_QUERY_FILTER_OPERATOR", f"invalid operator on {f.type} field: LIKE",
                                self.soql, c.pos)
            if v.kind != "string":
                raise self._type_error(f, v, "string")
            return f"{expr} ILIKE {self.param(v.raw)}"
        if v.kind == "null":
            if c.op not in ("=", "!="):
                raise SoqlError("INVALID_QUERY_FILTER_OPERATOR", f"invalid operator on null value: {c.op}",
                                self.soql, c.pos)
            if f.type == "boolean":
                return "FALSE" if c.op == "=" else "TRUE"
            return f"{expr} IS {'NOT ' if c.op == '!=' else ''}NULL"
        if v.kind == "literal":
            return self.date_literal_cmp(f, expr, c.op, v)
        if f.type in ID_TYPES:
            cast = "::bpchar" if f.name == "Id" else "::text"
            return self.scalar_cmp(expr, c.op, self.param(self.id_value(f, v), cast), ci=False)
        if f.is_text:
            if v.kind != "string":
                raise self._type_error(f, v, "string")
            return self.scalar_cmp(expr, c.op, self.param(v.raw), ci=True)
        if f.type == "boolean":
            if v.kind != "bool" or c.op not in ("=", "!="):
                raise self._type_error(f, v, "boolean")
            return f"{expr} {'=' if c.op == '=' else '<>'} {self.param(v.raw)}"
        if f.is_number:
            if v.kind != "number":
                raise self._type_error(f, v, "number")
            return self.scalar_cmp(expr, c.op, self.param(v.raw, "::numeric"), ci=False)
        if f.type == "date":
            if v.kind != "date":
                raise self._type_error(f, v, "date")
            return self.scalar_cmp(expr, c.op, self.param(v.raw, "::date"), ci=False)
        if f.type == "datetime":
            if v.kind != "datetime":
                raise self._type_error(f, v, "dateTime")
            return self.scalar_cmp(expr, c.op, self.param(v.raw, "::timestamptz"), ci=False)
        if v.kind != "string":
            raise self._type_error(f, v, "string")
        return self.scalar_cmp(expr, c.op, self.param(v.raw), ci=True)

    def id_value(self, f: SfField, v: Value) -> str:
        if v.kind != "string":
            raise self._type_error(f, v, "string")
        raw = str(v.raw)
        if not re.fullmatch(r"[A-Za-z0-9]{15}|[A-Za-z0-9]{18}", raw):
            raise SoqlError("INVALID_QUERY_FILTER_OPERATOR", f"invalid ID field: {raw}", self.soql, v.pos)
        return to_id18(raw)

    @staticmethod
    def scalar_cmp(expr: str, op: str, placeholder: str, ci: bool) -> str:
        left, right = (f"lower({expr})", f"lower({placeholder})") if ci else (expr, placeholder)
        if op == "=":
            return f"{left} = {right}"
        if op == "!=":
            return f"{left} IS DISTINCT FROM {right}"
        return f"{left} {op} {right}"

    def list_comparison(self, c: Cmp, f: SfField, expr: str) -> str:
        values = c.value
        assert isinstance(values, tuple)
        if c.op in ("INCLUDES", "EXCLUDES"):
            if f.type != "multipicklist":
                raise SoqlError("INVALID_QUERY_FILTER_OPERATOR", f"{c.op} is only valid on multi-select picklists",
                                self.soql, c.pos)
            arr = f"string_to_array(lower(coalesce({expr}, '')), ';')"
            terms = []
            for v in values:
                if v.kind != "string":
                    raise self._type_error(f, v, "string")
                parts = [p.strip().lower() for p in str(v.raw).split(";") if p.strip()]
                terms.append(f"{arr} @> {self.param(parts, '::text[]')}")
            any_match = "(" + " OR ".join(terms) + ")"
            return any_match if c.op == "INCLUDES" else f"NOT {any_match}"
        non_null = [v for v in values if v.kind != "null"]
        has_null = len(non_null) != len(values)
        if f.type in ID_TYPES:
            cast = "::bpchar[]" if f.name == "Id" else "::text[]"
            arr, left = [self.id_value(f, v) for v in non_null], expr
        elif f.is_text:
            for v in non_null:
                if v.kind != "string":
                    raise self._type_error(f, v, "string")
            arr, cast, left = [str(v.raw).lower() for v in non_null], "::text[]", f"lower({expr})"
        elif f.is_number:
            for v in non_null:
                if v.kind != "number":
                    raise self._type_error(f, v, "number")
            arr, cast, left = [v.raw for v in non_null], "::numeric[]", expr
        elif f.type == "date":
            for v in non_null:
                if v.kind != "date":
                    raise self._type_error(f, v, "date")
            arr, cast, left = [v.raw for v in non_null], "::date[]", expr
        elif f.type == "datetime":
            for v in non_null:
                if v.kind != "datetime":
                    raise self._type_error(f, v, "dateTime")
            arr, cast, left = [v.raw for v in non_null], "::timestamptz[]", expr
        else:
            raise SoqlError("INVALID_QUERY_FILTER_OPERATOR", f"invalid operator on {f.type} field: IN",
                            self.soql, c.pos)
        member = f"coalesce({left} = ANY({self.param(arr, cast)}), false)"
        if has_null:
            member = f"({member} OR {expr} IS NULL)"
        return member if c.op == "IN" else f"NOT {member}"

    def date_literal_cmp(self, f: SfField, expr: str, op: str, v: Value) -> str:
        if f.type not in ("date", "datetime"):
            raise SoqlError("INVALID_QUERY_FILTER_OPERATOR",
                            f"value of filter criterion for field '{f.name}' must be of type {f.type} and should "
                            "not be a date literal", self.soql, v.pos)
        start_d, end_d = date_literal_range(v, self.today)
        cast = "::date" if f.type == "date" else "::timestamptz"
        bounds = (start_d, end_d) if f.type == "date" else (_day_start(start_d), _day_start(end_d))

        def start() -> str:
            return self.param(bounds[0], cast)

        def end() -> str:
            return self.param(bounds[1], cast)

        if op in ("=", "!="):
            inside = f"{expr} >= {start()} AND {expr} < {end()}"
            return f"({inside})" if op == "=" else f"NOT coalesce({inside}, false)"
        if op in ("<", ">="):
            return f"{expr} {op} {start()}"
        return f"{expr} {'<' if op == '<=' else '>='} {end()}"

    # -- order
    def compile_order(self) -> None:
        parts = []
        for item in self.query.order:
            alias, f, _ = self.resolve(item.field)
            expr = typed_expr(alias, f)
            if f.is_text and f.type not in ID_TYPES:
                expr = f"lower({expr})"
            nulls = item.nulls or "FIRST"
            parts.append(f"{expr} {'DESC' if item.desc else 'ASC'} NULLS {nulls}")
        self.out.order_sql = ", ".join(parts)

    def compile(self) -> Compiled:
        self.compile_select()
        self.compile_where()
        self.compile_order()
        return self.out


def compile_soql(soql: str, schema: Schema, now: datetime) -> Compiled:
    return Compiler(soql.strip(), schema, now).compile()


# --------------------------------------------------------------------------------------------------
# Value formatting (Salesforce wire formats)
# --------------------------------------------------------------------------------------------------


def sf_datetime(value: datetime) -> str:
    """``2026-01-05T14:03:22.000+0000`` (REST JSON format)."""
    v = value.astimezone(UTC)
    return v.strftime("%Y-%m-%dT%H:%M:%S.") + f"{v.microsecond // 1000:03d}+0000"


def out_value(f: SfField, raw: Any) -> Any:
    """Convert a stored value to its REST JSON representation (booleans are never null; '' is null)."""
    if f.type == "boolean":
        if isinstance(raw, bool):
            return raw
        return raw is not None and str(raw).lower() == "true"
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return sf_datetime(raw)
    if isinstance(raw, str) and f.name == "Id":
        return raw.strip()
    return raw


def csv_value(f: SfField, raw: Any) -> str:
    value = out_value(f, raw)
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if f.type == "datetime" and isinstance(value, str) and value.endswith("+0000"):
        return value[:-5] + "Z"
    return str(value)
