/* JQL-lite (DESIGN_SPEC §5.4): `field op value` joined by AND / OR, parentheses, ORDER BY.
   Fields: project type status priority assignee reporter sprint epic labels component duedate created text.
   Ops: = != ~ !~ < > <= >= IN, NOT IN, IS EMPTY, IS NOT EMPTY.
   Functions: now() startOfWeek() endOfWeek() currentUser() openSprints() closedSprints() futureSprints().
   The server evaluates queries; this module is used by the mock server and for the editor's autocomplete. */

import { addDays, endOfWeek, startOfWeek } from 'date-fns';
import type { Issue, Sprint } from '@/api/types';
import { parseDay } from './dates';

export const JQL_FIELDS = [
  'project',
  'type',
  'status',
  'priority',
  'assignee',
  'reporter',
  'sprint',
  'epic',
  'labels',
  'component',
  'duedate',
  'created',
  'text',
] as const;
export type JqlField = (typeof JQL_FIELDS)[number];

export const JQL_FUNCTIONS = ['now()', 'startOfWeek()', 'endOfWeek()', 'currentUser()', 'openSprints()', 'closedSprints()', 'futureSprints()'];
export const JQL_KEYWORDS = ['AND', 'OR', 'NOT', 'IN', 'IS', 'EMPTY', 'ORDER BY', 'ASC', 'DESC'];

export class JqlError extends Error {
  readonly at: number;
  constructor(message: string, at: number) {
    super(message);
    this.at = at;
  }
}

type Tok =
  | { k: 'word'; v: string; at: number }
  | { k: 'str'; v: string; at: number }
  | { k: 'op'; v: string; at: number }
  | { k: 'lp' | 'rp' | 'comma'; at: number }
  | { k: 'fn'; v: string; at: number };

function tokenize(src: string): Tok[] {
  const out: Tok[] = [];
  let i = 0;
  while (i < src.length) {
    const c = src[i];
    if (/\s/.test(c)) {
      i += 1;
      continue;
    }
    if (c === '(') {
      out.push({ k: 'lp', at: i++ });
      continue;
    }
    if (c === ')') {
      out.push({ k: 'rp', at: i++ });
      continue;
    }
    if (c === ',') {
      out.push({ k: 'comma', at: i++ });
      continue;
    }
    if (c === '"' || c === "'") {
      const end = src.indexOf(c, i + 1);
      if (end < 0) throw new JqlError('Unclosed quote', i);
      out.push({ k: 'str', v: src.slice(i + 1, end), at: i });
      i = end + 1;
      continue;
    }
    const op = /^(!=|!~|<=|>=|=|~|<|>)/.exec(src.slice(i));
    if (op) {
      out.push({ k: 'op', v: op[1], at: i });
      i += op[1].length;
      continue;
    }
    const w = /^[\p{L}\p{N}_.\-+:/@]+/u.exec(src.slice(i));
    if (!w) throw new JqlError(`Unexpected character "${c}"`, i);
    const word = w[0];
    if (src.slice(i + word.length, i + word.length + 2) === '()') {
      out.push({ k: 'fn', v: word.toLowerCase(), at: i });
      i += word.length + 2;
      continue;
    }
    out.push({ k: 'word', v: word, at: i });
    i += word.length;
  }
  return out;
}

type Value = { kind: 'lit'; v: string } | { kind: 'fn'; v: string };
type Node =
  | { t: 'and'; a: Node; b: Node }
  | { t: 'or'; a: Node; b: Node }
  | { t: 'not'; a: Node }
  | { t: 'cmp'; field: JqlField; op: string; values: Value[] };

export interface JqlQuery {
  where: Node | null;
  order: Array<{ field: JqlField | 'rank' | 'key'; dir: 1 | -1 }>;
}

const FIELD_ALIASES: Record<string, JqlField> = {
  issuetype: 'type',
  label: 'labels',
  components: 'component',
  due: 'duedate',
  createddate: 'created',
  summary: 'text',
};

function asField(word: string, at: number): JqlField {
  const w = word.toLowerCase();
  const f = (FIELD_ALIASES[w] ?? w) as JqlField;
  if (!JQL_FIELDS.includes(f)) throw new JqlError(`Unknown field "${word}". Try ${JQL_FIELDS.join(', ')}`, at);
  return f;
}

export function parseJql(src: string): JqlQuery {
  const toks = tokenize(src);
  let p = 0;
  const peek = () => toks[p];
  const isWord = (t: Tok | undefined, ...ws: string[]) => t?.k === 'word' && ws.includes(t.v.toUpperCase());

  const value = (): Value => {
    const t = toks[p++];
    if (!t) throw new JqlError('Expected a value', src.length);
    if (t.k === 'str' || t.k === 'word') return { kind: 'lit', v: t.v };
    if (t.k === 'fn') return { kind: 'fn', v: t.v };
    throw new JqlError('Expected a value', t.at);
  };

  const clause = (): Node => {
    const t = toks[p++];
    if (!t || t.k !== 'word') throw new JqlError('Expected a field name', t?.at ?? src.length);
    const field = asField(t.v, t.at);
    const o = peek();
    if (isWord(o, 'IS')) {
      p += 1;
      const neg = isWord(peek(), 'NOT');
      if (neg) p += 1;
      if (!isWord(peek(), 'EMPTY', 'NULL')) throw new JqlError('Expected EMPTY', peek()?.at ?? src.length);
      p += 1;
      return { t: 'cmp', field, op: neg ? 'is not empty' : 'is empty', values: [] };
    }
    let op: string;
    if (isWord(o, 'IN')) {
      p += 1;
      op = 'in';
    } else if (isWord(o, 'NOT') && isWord(toks[p + 1], 'IN')) {
      p += 2;
      op = 'not in';
    } else if (o?.k === 'op') {
      p += 1;
      op = o.v;
    } else throw new JqlError(`Expected an operator after "${t.v}"`, o?.at ?? src.length);

    if (op === 'in' || op === 'not in') {
      const lp = toks[p];
      if (lp?.k === 'fn') {
        p += 1;
        return { t: 'cmp', field, op, values: [{ kind: 'fn', v: lp.v }] };
      }
      if (lp?.k !== 'lp') throw new JqlError('Expected "(" after IN', lp?.at ?? src.length);
      p += 1;
      const values: Value[] = [value()];
      while (toks[p]?.k === 'comma') {
        p += 1;
        values.push(value());
      }
      if (toks[p]?.k !== 'rp') throw new JqlError('Expected ")"', toks[p]?.at ?? src.length);
      p += 1;
      return { t: 'cmp', field, op, values };
    }
    return { t: 'cmp', field, op, values: [value()] };
  };

  const factor = (): Node => {
    if (isWord(peek(), 'NOT')) {
      p += 1;
      return { t: 'not', a: factor() };
    }
    if (peek()?.k === 'lp') {
      p += 1;
      const e = expr();
      if (peek()?.k !== 'rp') throw new JqlError('Expected ")"', peek()?.at ?? src.length);
      p += 1;
      return e;
    }
    return clause();
  };
  const term = (): Node => {
    let a = factor();
    while (isWord(peek(), 'AND')) {
      p += 1;
      a = { t: 'and', a, b: factor() };
    }
    return a;
  };
  function expr(): Node {
    let a = term();
    while (isWord(peek(), 'OR')) {
      p += 1;
      a = { t: 'or', a, b: term() };
    }
    return a;
  }

  let where: Node | null = null;
  if (p < toks.length && !isWord(peek(), 'ORDER')) where = expr();
  const order: JqlQuery['order'] = [];
  if (isWord(peek(), 'ORDER')) {
    p += 1;
    if (!isWord(peek(), 'BY')) throw new JqlError('Expected BY after ORDER', peek()?.at ?? src.length);
    p += 1;
    do {
      if (toks[p]?.k === 'comma') p += 1;
      const t = toks[p++];
      if (!t || t.k !== 'word') throw new JqlError('Expected a field to order by', t?.at ?? src.length);
      const w = t.v.toLowerCase();
      const field = w === 'rank' || w === 'key' ? w : asField(t.v, t.at);
      let dir: 1 | -1 = 1;
      if (isWord(peek(), 'ASC', 'DESC')) dir = (toks[p++] as { v: string }).v.toUpperCase() === 'DESC' ? -1 : 1;
      order.push({ field, dir });
    } while (toks[p]?.k === 'comma');
  }
  if (p < toks.length) throw new JqlError('Unexpected text', toks[p].at);
  return { where, order };
}

// ---------------------------------------------------------------- evaluation

export interface JqlContext {
  me: string;
  now: Date;
  sprints: Sprint[];
  issueByKey: Map<string, Issue>;
  peopleName: (key: string) => string;
}

const PRIORITY_RANK: Record<string, number> = { lowest: 1, low: 2, medium: 3, high: 4, highest: 5 };

function resolveDate(v: Value, ctx: JqlContext): Date | null {
  if (v.kind === 'fn') {
    if (v.v === 'now') return ctx.now;
    if (v.v === 'startofweek') return startOfWeek(ctx.now, { weekStartsOn: 1 });
    if (v.v === 'endofweek') return endOfWeek(ctx.now, { weekStartsOn: 1 });
    return null;
  }
  const rel = /^([+-]?\d+)([dw])$/i.exec(v.v);
  if (rel) return addDays(ctx.now, Number(rel[1]) * (rel[2].toLowerCase() === 'w' ? 7 : 1));
  if (/^\d{4}-\d{2}-\d{2}/.test(v.v)) return parseDay(v.v);
  return null;
}

function dayOnly(d: Date) {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}

function fieldValues(issue: Issue, field: JqlField, ctx: JqlContext): string[] {
  switch (field) {
    case 'project':
      return ['DATA', String(issue.project_no ?? '')];
    case 'type':
      return [issue.type];
    case 'status':
      return [issue.status];
    case 'priority':
      return [issue.priority];
    case 'assignee':
      return issue.assignee ? [issue.assignee, ctx.peopleName(issue.assignee)] : [];
    case 'reporter':
      return issue.reporter ? [issue.reporter, ctx.peopleName(issue.reporter)] : [];
    case 'sprint': {
      const s = ctx.sprints.find((x) => x.id === issue.sprint_id);
      return s ? [String(s.id), s.name, s.name.replace(/^DATA Sprint /i, '')] : [];
    }
    case 'epic': {
      const e = issue.epic_key ? ctx.issueByKey.get(issue.epic_key) : undefined;
      const self = issue.type === 'Epic' ? [issue.key, issue.summary] : [];
      return issue.epic_key ? [issue.epic_key, e?.summary ?? '', ...self] : self;
    }
    case 'labels':
      return issue.labels;
    case 'component':
      return issue.components;
    case 'duedate':
      return issue.due_date ? [issue.due_date] : [];
    case 'created':
      return issue.created ? [issue.created] : [];
    case 'text':
      return [issue.key, issue.summary, issue.description ?? ''];
  }
}

function literal(v: Value, ctx: JqlContext): string[] {
  if (v.kind === 'lit') return [v.v];
  if (v.v === 'currentuser') return [ctx.me];
  const pick = (state: Sprint['state']) => ctx.sprints.filter((s) => s.state === state).map((s) => String(s.id));
  if (v.v === 'opensprints') return [...pick('active'), ...pick('future')];
  if (v.v === 'closedsprints') return pick('closed');
  if (v.v === 'futuresprints') return pick('future');
  return [];
}

function cmp(issue: Issue, n: Extract<Node, { t: 'cmp' }>, ctx: JqlContext): boolean {
  const have = fieldValues(issue, n.field, ctx);
  const lower = have.map((h) => h.toLowerCase());
  if (n.op === 'is empty') return have.length === 0 || have.every((h) => !h);
  if (n.op === 'is not empty') return have.some((h) => !!h);
  const wants = n.values.flatMap((v) => literal(v, ctx)).map((w) => w.toLowerCase());

  if (n.op === '~' || n.op === '!~') {
    const hit = wants.some((w) => lower.some((h) => h.includes(w)));
    return n.op === '~' ? hit : !hit;
  }
  if (n.op === '=' || n.op === 'in') return wants.some((w) => lower.includes(w));
  if (n.op === '!=' || n.op === 'not in') return !wants.some((w) => lower.includes(w));

  // ordered comparisons
  const sign = (a: number, b: number) =>
    n.op === '<' ? a < b : n.op === '<=' ? a <= b : n.op === '>' ? a > b : n.op === '>=' ? a >= b : false;
  if (n.field === 'priority') {
    const a = PRIORITY_RANK[lower[0] ?? ''];
    const b = PRIORITY_RANK[wants[0] ?? ''];
    return a != null && b != null && sign(a, b);
  }
  if (n.field === 'duedate' || n.field === 'created') {
    if (!have[0]) return false;
    const b = resolveDate(n.values[0], ctx);
    if (!b) throw new JqlError(`"${n.values[0].v}" is not a date. Use 2026-11-02, -7d, now() or endOfWeek()`, 0);
    return sign(dayOnly(new Date(have[0].length <= 10 ? parseDay(have[0]) : new Date(have[0]))), dayOnly(b));
  }
  const a = Number(have[0]);
  const b = Number(wants[0]);
  return Number.isFinite(a) && Number.isFinite(b) && sign(a, b);
}

function evaluate(issue: Issue, n: Node, ctx: JqlContext): boolean {
  if (n.t === 'and') return evaluate(issue, n.a, ctx) && evaluate(issue, n.b, ctx);
  if (n.t === 'or') return evaluate(issue, n.a, ctx) || evaluate(issue, n.b, ctx);
  if (n.t === 'not') return !evaluate(issue, n.a, ctx);
  return cmp(issue, n, ctx);
}

export function runJql(issues: Issue[], src: string, ctx: JqlContext): Issue[] {
  const q = parseJql(src);
  const out = q.where ? issues.filter((i) => evaluate(i, q.where!, ctx)) : [...issues];
  if (q.order.length) {
    out.sort((a, b) => {
      for (const { field, dir } of q.order) {
        const va = sortValue(a, field);
        const vb = sortValue(b, field);
        if (va < vb) return -dir;
        if (va > vb) return dir;
      }
      return 0;
    });
  }
  return out;
}

function sortValue(i: Issue, field: JqlQuery['order'][number]['field']): string | number {
  switch (field) {
    case 'priority':
      return PRIORITY_RANK[i.priority.toLowerCase()] ?? 0;
    case 'key':
      return Number(i.key.split('-')[1] ?? 0);
    case 'rank':
      return typeof i.rank === 'number' ? i.rank : Number(i.rank) || 0;
    case 'duedate':
      return i.due_date ?? '9999';
    case 'created':
      return i.created ?? '';
    case 'sprint':
      return i.sprint_id ?? 9999;
    default:
      return String((i as unknown as Record<string, unknown>)[field] ?? '').toLowerCase();
  }
}
