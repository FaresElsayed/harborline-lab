"""JQL-lite: ``field op value`` clauses joined by AND / OR (AND binds tighter), NOT, parentheses, ORDER BY.

Fields: project, type (issuetype), status, priority, assignee, reporter, sprint, epic, parent, key, labels,
component, duedate (due), created, updated, resolved, summary, description, text, storypoints.
Operators: = != ~ !~ < > <= >= IN, NOT IN, IS [NOT] EMPTY.
Functions: now(), currentUser(), startOfDay(), endOfDay(), startOfWeek(), endOfWeek() (weeks start on
Monday), startOfMonth(), endOfMonth(), openSprints(), closedSprints(), futureSprints().
Relative dates: ``-7d``, ``2w``, ``-4h``. Compiles to SQL over ``tracker.issue i`` joined to
``tracker.sprint s`` and ``tracker.rank r``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

STATUSES = ("To Do", "In Progress", "In Review", "Done")
TYPES = ("Epic", "Story", "Task", "Bug", "Spike")
PRIORITIES = ("Lowest", "Low", "Medium", "High", "Highest")  # ordinal order for < >
PROJECT_KEY = "DATA"

FROM_SQL = ("tracker.issue i LEFT JOIN tracker.sprint s ON s.id = i.sprint_id "
            "LEFT JOIN tracker.rank r ON r.issue_key = i.key")

_ALIASES = {
    "issuetype": "type", "due": "duedate", "components": "component", "label": "labels", "issuekey": "key",
    "id": "key", "createddate": "created", "updateddate": "updated", "resolutiondate": "resolved",
    "epic link": "epic", "parentepic": "epic", "story points": "storypoints", "story point estimate": "storypoints",
    "sp": "storypoints",
}
_KINDS = {
    "project": "project", "type": "enum", "status": "enum", "priority": "enum", "assignee": "person",
    "reporter": "person", "sprint": "sprint", "epic": "issue_ref", "parent": "issue_ref", "key": "key",
    "labels": "array", "component": "array", "duedate": "date", "created": "time", "updated": "time",
    "resolved": "time", "summary": "text", "description": "text", "text": "text", "storypoints": "number",
}
_ENUM_VALUES = {"type": TYPES, "status": STATUSES, "priority": PRIORITIES}
_COLUMNS = {
    "type": "i.type", "status": "i.status", "priority": "i.priority", "assignee": "i.assignee",
    "reporter": "i.reporter", "epic": "i.epic_key", "parent": "i.parent_key", "labels": "i.labels",
    "component": "i.components", "duedate": "i.due_date", "created": "i.created_at", "updated": "i.updated_at",
    "resolved": "i.resolved_at", "summary": "i.summary", "description": "i.description",
    "storypoints": "i.story_points",
}
_ORDER = {
    "rank": "r.rank", "key": "i.num", "created": "i.created_at", "updated": "i.updated_at", "duedate": "i.due_date",
    "resolved": "i.resolved_at", "summary": "lower(i.summary)", "type": "i.type", "assignee": "i.assignee",
    "reporter": "i.reporter", "sprint": "i.sprint_id", "epic": "i.epic_key", "storypoints": "i.story_points",
    "priority": "array_position(ARRAY['Lowest','Low','Medium','High','Highest'], i.priority::text)",
    "status": "array_position(ARRAY['To Do','In Progress','In Review','Done'], i.status::text)",
}
_TOKEN_RE = re.compile(
    r"""(?P<ws>\s+)|(?P<string>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|(?P<op>!=|!~|<=|>=|=|~|<|>)
      |(?P<punct>[(),])|(?P<word>[^\s()=!<>~,"']+)""",
    re.VERBOSE,
)


class JqlError(ValueError):
    pass


@dataclass(frozen=True)
class Tok:
    kind: str
    text: str
    pos: int

    @property
    def upper(self) -> str:
        return self.text.upper()


@dataclass(frozen=True)
class Fn:
    name: str  # lower-case
    args: tuple[str, ...]


@dataclass(frozen=True)
class Clause:
    field: str
    op: str  # = != ~ !~ < > <= >= IN NOTIN EMPTY NOTEMPTY
    values: tuple[str | Fn, ...]


@dataclass(frozen=True)
class Bool:
    op: str
    items: tuple[Any, ...]


@dataclass(frozen=True)
class Not:
    item: Any


@dataclass
class JqlQuery:
    where_sql: str = "TRUE"
    params: list[Any] = field(default_factory=list)
    order_sql: str = "r.rank ASC NULLS LAST, i.num ASC"


def _tokenize(text: str) -> list[Tok]:
    out, pos = [], 0
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise JqlError(f"Error in the JQL Query: The character '{text[pos]}' is a reserved JQL character "
                           f"(char {pos}).")
        if m.lastgroup != "ws":
            out.append(Tok(m.lastgroup or "", m.group(), pos))
        pos = m.end()
    return out


def _unquote(text: str) -> str:
    return re.sub(r"\\(.)", r"\1", text[1:-1])


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.toks = _tokenize(text)
        self.i = 0

    def peek(self) -> Tok | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def take(self) -> Tok:
        tok = self.peek()
        if tok is None:
            raise JqlError("Error in the JQL Query: Expecting more input at the end of the query.")
        self.i += 1
        return tok

    def word(self, *words: str) -> bool:
        tok = self.peek()
        if tok is not None and tok.kind == "word" and tok.upper in words:
            self.i += 1
            return True
        return False

    def punct(self, ch: str) -> bool:
        tok = self.peek()
        if tok is not None and tok.kind == "punct" and tok.text == ch:
            self.i += 1
            return True
        return False

    def expect_punct(self, ch: str) -> None:
        if not self.punct(ch):
            tok = self.peek()
            raise JqlError(f"Error in the JQL Query: Expecting '{ch}' but got "
                           f"'{tok.text if tok else '<end>'}'.")

    def parse(self) -> tuple[Any, list[tuple[str, bool]]]:
        cond = None
        tok = self.peek()
        if tok is not None and not (tok.kind == "word" and tok.upper == "ORDER"):
            cond = self.parse_or()
        order: list[tuple[str, bool]] = []
        if self.word("ORDER"):
            if not self.word("BY"):
                raise JqlError("Error in the JQL Query: Expecting 'BY' after 'ORDER'.")
            while True:
                name = self.field_name()
                desc = self.word("DESC")
                if not desc:
                    self.word("ASC")
                order.append((name, desc))
                if not self.punct(","):
                    break
        if self.peek() is not None:
            raise JqlError(f"Error in the JQL Query: Unexpected '{self.peek().text}'.")  # type: ignore[union-attr]
        return cond, order

    def parse_or(self) -> Any:
        items = [self.parse_and()]
        while self.word("OR"):
            items.append(self.parse_and())
        return items[0] if len(items) == 1 else Bool("OR", tuple(items))

    def parse_and(self) -> Any:
        items = [self.parse_not()]
        while self.word("AND"):
            items.append(self.parse_not())
        return items[0] if len(items) == 1 else Bool("AND", tuple(items))

    def parse_not(self) -> Any:
        if self.word("NOT"):
            return Not(self.parse_not())
        if self.punct("("):
            inner = self.parse_or()
            self.expect_punct(")")
            return inner
        return self.parse_clause()

    def field_name(self) -> str:
        tok = self.take()
        if tok.kind not in ("word", "string"):
            raise JqlError(f"Error in the JQL Query: Expecting a field name but got '{tok.text}'.")
        raw = (_unquote(tok.text) if tok.kind == "string" else tok.text).lower()
        name = _ALIASES.get(raw, raw)
        if name not in _KINDS and name != "rank":
            raise JqlError(f"Field '{raw}' does not exist or you do not have permission to view it.")
        return name

    def parse_clause(self) -> Clause:
        name = self.field_name()
        tok = self.take()
        if tok.kind == "op":
            return Clause(name, tok.text, (self.value(),))
        word = tok.upper if tok.kind == "word" else ""
        if word == "IS":
            negate = self.word("NOT")
            if not self.word("EMPTY", "NULL"):
                raise JqlError("Error in the JQL Query: Expecting 'EMPTY' after 'IS'.")
            return Clause(name, "NOTEMPTY" if negate else "EMPTY", ())
        if word == "NOT" and self.word("IN"):
            return Clause(name, "NOTIN", self.value_list())
        if word == "IN":
            return Clause(name, "IN", self.value_list())
        raise JqlError(f"Error in the JQL Query: Expecting an operator after '{name}' but got '{tok.text}'.")

    def value_list(self) -> tuple[str | Fn, ...]:
        if not self.punct("("):
            value = self.value()
            if not isinstance(value, Fn):
                raise JqlError("Error in the JQL Query: Expecting '(' to start a list of values.")
            return (value,)
        values = [self.value()]
        while self.punct(","):
            values.append(self.value())
        self.expect_punct(")")
        return tuple(values)

    def value(self) -> str | Fn:
        tok = self.take()
        if tok.kind == "string":
            return _unquote(tok.text)
        if tok.kind != "word":
            raise JqlError(f"Error in the JQL Query: Expecting a value but got '{tok.text}'.")
        if self.punct("("):
            args: list[str] = []
            if not self.punct(")"):
                while True:
                    arg = self.take()
                    args.append(_unquote(arg.text) if arg.kind == "string" else arg.text)
                    if self.punct(")"):
                        break
                    self.expect_punct(",")
            return Fn(tok.text.lower(), tuple(args))
        return tok.text


# --------------------------------------------------------------------------------------------------
# Compiler
# --------------------------------------------------------------------------------------------------

_REL_RE = re.compile(r"^([+-])?(\d+)([wdhm])$", re.IGNORECASE)
_UNIT = {"w": timedelta(weeks=1), "d": timedelta(days=1), "h": timedelta(hours=1), "m": timedelta(minutes=1)}


class _Compiler:
    def __init__(self, now: datetime, people: list[dict[str, str]]):
        self.now = now.astimezone(UTC)
        self.people = people
        self.params: list[Any] = []

    def p(self, value: Any) -> str:
        self.params.append(value)
        return "%s"

    def node(self, n: Any) -> str:
        if isinstance(n, Bool):
            return "(" + f" {n.op} ".join(self.node(i) for i in n.items) + ")"
        if isinstance(n, Not):
            return f"NOT coalesce({self.node(n.item)}, false)"
        return self.clause(n)

    def clause(self, c: Clause) -> str:
        kind = _KINDS[c.field]
        handler = getattr(self, f"c_{kind}")
        return handler(c)

    # -- helpers
    def scalars(self, c: Clause, fn_ok: tuple[str, ...] = ()) -> list[str]:
        out = []
        for v in c.values:
            if isinstance(v, Fn):
                if v.name not in fn_ok:
                    raise JqlError(f"Function '{v.name}' is not valid for field '{c.field}'.")
                out.append(v.name)
            else:
                out.append(v)
        return out

    def eq_set(self, c: Clause, expr: str, values: list[str]) -> str:
        """= / != / IN / NOT IN over a case-insensitive scalar; != and NOT IN exclude empty values (as Jira)."""
        if c.op in ("EMPTY", "NOTEMPTY"):
            return f"{expr} IS {'NOT ' if c.op == 'NOTEMPTY' else ''}NULL"
        lowered = [v.lower() for v in values]
        if c.op in ("=", "IN"):
            return f"lower({expr}) = ANY({self.p(lowered)}::text[])"
        if c.op in ("!=", "NOTIN"):
            return f"({expr} IS NOT NULL AND NOT (lower({expr}) = ANY({self.p(lowered)}::text[])))"
        raise JqlError(f"The operator '{c.op}' is not supported by the '{c.field}' field.")

    # -- kinds
    def c_project(self, c: Clause) -> str:
        if c.op in ("EMPTY", "NOTEMPTY"):
            return "FALSE" if c.op == "EMPTY" else "TRUE"
        matches = []
        for v in self.scalars(c):
            if v.upper() in (PROJECT_KEY, "HARBORLINE DATA PLATFORM"):
                matches.append("TRUE")
            elif v.isdigit():
                matches.append(f"i.project = {self.p(int(v))}")
            else:
                raise JqlError(f"The value '{v}' does not exist for the field 'project'.")
        expr = "(" + " OR ".join(matches) + ")"
        if c.op in ("=", "IN"):
            return expr
        if c.op in ("!=", "NOTIN"):
            return f"NOT coalesce({expr}, false)"
        raise JqlError(f"The operator '{c.op}' is not supported by the 'project' field.")

    def c_enum(self, c: Clause) -> str:
        col = _COLUMNS[c.field]
        allowed = _ENUM_VALUES[c.field]
        values = self.scalars(c)
        canon = []
        for v in values:
            match = next((a for a in allowed if a.lower() == v.lower()), None)
            if match is None:
                raise JqlError(f"The value '{v}' does not exist for the field '{c.field}'.")
            canon.append(match)
        if c.field == "priority" and c.op in ("<", ">", "<=", ">="):
            ordinal = f"array_position(ARRAY['Lowest','Low','Medium','High','Highest'], {col}::text)"
            return f"{ordinal} {c.op} {self.p(PRIORITIES.index(canon[0]) + 1)}"
        return self.eq_set(c, col, canon)

    def c_person(self, c: Clause) -> str:
        keys = []
        for v in self.scalars(c, ("currentuser",)):
            if v == "currentuser":
                keys.append("me")
                continue
            person = next((p for p in self.people if v.lower() in (p["key"].lower(), p["name"].lower())), None)
            keys.append(person["key"] if person else v)
        return self.eq_set(c, _COLUMNS[c.field], keys)

    def c_sprint(self, c: Clause) -> str:
        if c.op in ("EMPTY", "NOTEMPTY"):
            return f"i.sprint_id IS {'NOT ' if c.op == 'NOTEMPTY' else ''}NULL"
        states = {"opensprints": "active", "closedsprints": "closed", "futuresprints": "future"}
        terms = []
        for v in self.scalars(c, tuple(states)):
            if v in states:
                terms.append(f"s.state = {self.p(states[v])}")
            elif v.isdigit():
                terms.append(f"s.id = {self.p(int(v))}")
            else:
                terms.append(f"lower(s.name) = {self.p(v.lower())}")
        expr = "(" + " OR ".join(terms) + ")"
        if c.op in ("=", "IN"):
            return f"coalesce({expr}, false)"
        if c.op in ("!=", "NOTIN"):
            return f"(i.sprint_id IS NOT NULL AND NOT coalesce({expr}, false))"
        raise JqlError(f"The operator '{c.op}' is not supported by the 'sprint' field.")

    def c_issue_ref(self, c: Clause) -> str:
        col = _COLUMNS[c.field]
        if c.op in ("EMPTY", "NOTEMPTY"):
            return f"{col} IS {'NOT ' if c.op == 'NOTEMPTY' else ''}NULL"
        values = [v.upper() for v in self.scalars(c)]
        ref = (f"{col} IN (SELECT x.key FROM tracker.issue x WHERE upper(x.key) = ANY({self.p(values)}::text[]) "
               f"OR upper(x.source_id) = ANY({self.p(values)}::text[]))")
        if c.op in ("=", "IN"):
            return ref
        if c.op in ("!=", "NOTIN"):
            return f"({col} IS NOT NULL AND NOT {ref})"
        raise JqlError(f"The operator '{c.op}' is not supported by the '{c.field}' field.")

    def c_key(self, c: Clause) -> str:
        values = [v.upper() for v in self.scalars(c)]
        if c.op in ("<", ">", "<=", ">="):
            m = re.fullmatch(rf"{PROJECT_KEY}-(\d+)", values[0])
            if not m:
                raise JqlError(f"The issue key '{values[0]}' for field 'key' is invalid.")
            return f"i.num {c.op} {self.p(int(m.group(1)))}"
        match = (f"(upper(i.key) = ANY({self.p(values)}::text[]) OR "
                 f"upper(i.source_id) = ANY({self.p(values)}::text[]))")
        if c.op in ("=", "IN"):
            return match
        if c.op in ("!=", "NOTIN"):
            return f"NOT {match}"
        raise JqlError(f"The operator '{c.op}' is not supported by the 'key' field.")

    def c_array(self, c: Clause) -> str:
        col = _COLUMNS[c.field]
        if c.op in ("EMPTY", "NOTEMPTY"):
            return f"cardinality({col}) {'>' if c.op == 'NOTEMPTY' else '='} 0"
        values = [v.lower() for v in self.scalars(c)]
        has = f"EXISTS (SELECT 1 FROM unnest({col}) AS v(x) WHERE lower(v.x) = ANY({self.p(values)}::text[]))"
        if c.op in ("=", "IN"):
            return has
        if c.op in ("!=", "NOTIN"):
            return f"(cardinality({col}) > 0 AND NOT {has})"
        raise JqlError(f"The operator '{c.op}' is not supported by the '{c.field}' field.")

    def c_text(self, c: Clause) -> str:
        if c.op in ("EMPTY", "NOTEMPTY"):
            col = _COLUMNS.get(c.field, "i.description")
            return f"coalesce({col}, '') {'<>' if c.op == 'NOTEMPTY' else '='} ''"
        if c.op not in ("~", "!~"):
            raise JqlError(f"The operator '{c.op}' is not supported by the '{c.field}' field; use ~ or !~.")
        needle = self.scalars(c)[0].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{needle.strip('*')}%"
        if c.field == "text":
            expr = (f"(i.summary ILIKE {self.p(pattern)} OR i.description ILIKE {self.p(pattern)} OR EXISTS "
                    f"(SELECT 1 FROM tracker.comment cm WHERE cm.issue_key = i.key AND cm.body ILIKE {self.p(pattern)}))")
        else:
            expr = f"{_COLUMNS[c.field]} ILIKE {self.p(pattern)}"
        return expr if c.op == "~" else f"NOT coalesce({expr}, false)"

    def c_number(self, c: Clause) -> str:
        col = _COLUMNS[c.field]
        if c.op in ("EMPTY", "NOTEMPTY"):
            return f"{col} IS {'NOT ' if c.op == 'NOTEMPTY' else ''}NULL"
        try:
            nums = [float(v) for v in self.scalars(c)]
        except ValueError:
            raise JqlError(f"Field '{c.field}' expects a number.") from None
        if c.op in ("IN", "NOTIN"):
            expr = f"{col} = ANY({self.p(nums)}::numeric[])"
            return expr if c.op == "IN" else f"NOT coalesce({expr}, false)"
        op = "<>" if c.op == "!=" else c.op
        if op not in ("=", "<>", "<", ">", "<=", ">="):
            raise JqlError(f"The operator '{c.op}' is not supported by the '{c.field}' field.")
        return f"{col} {op} {self.p(nums[0])}"

    def c_date(self, c: Clause) -> str:
        return self._temporal(c, is_date=True)

    def c_time(self, c: Clause) -> str:
        return self._temporal(c, is_date=False)

    def _temporal(self, c: Clause, is_date: bool) -> str:
        col = _COLUMNS[c.field]
        if c.op in ("EMPTY", "NOTEMPTY"):
            return f"{col} IS {'NOT ' if c.op == 'NOTEMPTY' else ''}NULL"
        if c.op in ("IN", "NOTIN", "~", "!~"):
            raise JqlError(f"The operator '{c.op}' is not supported by the '{c.field}' field.")
        moment = self.moment(c.values[0], c.field)
        op = "<>" if c.op == "!=" else c.op
        if is_date and isinstance(moment, date) and not isinstance(moment, datetime):
            return f"{col} {op} {self.p(moment)}::date"
        return f"{col} {op} {self.p(moment)}::timestamptz"

    def moment(self, value: str | Fn, field_name: str) -> date | datetime:
        if isinstance(value, Fn):
            return self.function_moment(value)
        text = value.strip()
        rel = _REL_RE.match(text)
        if rel:
            sign = -1 if rel.group(1) == "-" else 1
            return self.now + sign * int(rel.group(2)) * _UNIT[rel.group(3).lower()]
        for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                pass
        for fmt in ("%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=UTC)
            except ValueError:
                pass
        raise JqlError(f"Date value '{value}' for field '{field_name}' is invalid. Valid formats include: "
                       "'yyyy/MM/dd HH:mm', 'yyyy-MM-dd HH:mm', 'yyyy/MM/dd', 'yyyy-MM-dd', or a period format "
                       "e.g. '-5d', '4w 2d'.")

    def function_moment(self, fn: Fn) -> datetime:
        today = datetime(self.now.year, self.now.month, self.now.day, tzinfo=UTC)
        unit = {"day": timedelta(days=1), "week": timedelta(weeks=1)}
        offset = fn.args[0] if fn.args else "0"
        if fn.name == "now":
            return self.now
        m = re.fullmatch(r"(startof|endof)(day|week|month)", fn.name)
        if not m:
            raise JqlError(f"Unable to find JQL function '{fn.name}()'.")
        edge, period = m.groups()
        om = re.fullmatch(r"([+-]?\d+)([wdhm]?)", offset.strip())
        if not om:
            raise JqlError(f"Invalid argument '{offset}' for function '{fn.name}()'.")
        amount, explicit_unit = int(om.group(1)), om.group(2).lower()
        if period == "month":
            idx = today.year * 12 + today.month - 1 + (0 if explicit_unit else amount)
            start = datetime(idx // 12, idx % 12 + 1, 1, tzinfo=UTC)
            nxt = datetime((idx + 1) // 12, (idx + 1) % 12 + 1, 1, tzinfo=UTC)
            base = start if edge == "startof" else nxt - timedelta(milliseconds=1)
            return base + (amount * _UNIT[explicit_unit] if explicit_unit else timedelta(0))
        start = today if period == "day" else today - timedelta(days=today.weekday())
        if explicit_unit:
            shift = amount * _UNIT[explicit_unit]
        else:
            shift = amount * unit[period]
        base = start if edge == "startof" else start + unit[period] - timedelta(milliseconds=1)
        return base + shift

    def order(self, items: list[tuple[str, bool]]) -> str:
        parts = []
        for name, desc in items:
            if name not in _ORDER:
                raise JqlError(f"Field '{name}' does not support sorting.")
            parts.append(f"{_ORDER[name]} {'DESC NULLS LAST' if desc else 'ASC NULLS LAST'}")
        return ", ".join([*parts, "i.num ASC"])


def compile_jql(text: str | None, now: datetime | None = None, people: list[dict[str, str]] | None = None) -> JqlQuery:
    query = JqlQuery()
    if not text or not text.strip():
        return query
    cond, order = _Parser(text).parse()
    comp = _Compiler(now or datetime.now(UTC), people or [])
    if cond is not None:
        query.where_sql = comp.node(cond)
    if order:
        query.order_sql = comp.order(order)
    query.params = comp.params
    return query
