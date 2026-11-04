import type {
  Attachment,
  BurndownReport,
  CfdReport,
  CfdRow,
  ChecklistItem,
  Comment,
  Connections,
  DqSettings,
  ExtractOptions,
  ExtractResult,
  Health,
  HistoryEntry,
  Issue,
  IssueLink,
  IssuePatch,
  NewIssue,
  Preset,
  RunSummary,
  Scenario,
  SimStatus,
  Sprint,
  TrackerMeta,
  VelocityReport,
  VelocityRow,
  Worklog,
} from './types';
import { withBase } from '@/lib/base';

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;
  constructor(status: number, detail: string) {
    super(detail || `Request failed (${status})`);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

/** FastAPI errors arrive as {detail: string | [{msg}]}; Salesforce-shaped ones as [{message}]. */
function readDetail(body: unknown): string {
  if (!body) return '';
  if (typeof body === 'string') return body;
  if (Array.isArray(body)) return body.map((e) => (e as { message?: string }).message ?? String(e)).join('; ');
  const d = (body as { detail?: unknown }).detail;
  if (typeof d === 'string') return d;
  if (Array.isArray(d)) return d.map((e) => (e as { msg?: string }).msg ?? JSON.stringify(e)).join('; ');
  return JSON.stringify(body);
}

async function request<T>(method: string, url: string, body?: unknown, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(url, {
      method,
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      ...init,
    });
  } catch {
    throw new ApiError(0, 'Cannot reach the simulator at ' + location.host + '. Is the app container running?');
  }
  const text = await res.text();
  let parsed: unknown = undefined;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = text;
    }
  }
  if (!res.ok) throw new ApiError(res.status, readDetail(parsed) || res.statusText);
  return parsed as T;
}

const get = <T>(url: string) => request<T>('GET', url);
const post = <T>(url: string, body?: unknown) => request<T>('POST', url, body ?? {});
const put = <T>(url: string, body: unknown) => request<T>('PUT', url, body);
const patch = <T>(url: string, body: unknown) => request<T>('PATCH', url, body);

function qs(params: Record<string, string | number | null | undefined>): string {
  const s = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined && v !== '') s.set(k, String(v));
  const out = s.toString();
  return out ? `?${out}` : '';
}

// ---------------------------------------------------------------- normalisers
// The backend and the mock differ in small ways (created_at vs created, worklogs vs worklog, links as an object,
// list endpoints wrapped in {issues}). Everything passes through here so screens see one shape.

type Raw = Record<string, unknown>;
const num = (v: unknown): number | null => (v == null || v === '' ? null : Number.isFinite(Number(v)) ? Number(v) : null);
const str = (v: unknown): string | null => (v == null ? null : String(v));

export function checklist(items: unknown): ChecklistItem[] {
  if (!Array.isArray(items)) return [];
  return items.map((it, i) => {
    if (typeof it === 'string') return { id: String(i), idx: i, text: it, done: false };
    const r = it as Raw;
    const idx = num(r.idx) ?? i;
    return { id: String(r.id ?? idx), idx, text: String(r.text ?? ''), done: Boolean(r.done ?? r.checked ?? false) };
  });
}

function links(r: Raw): IssueLink[] {
  const l = r.links;
  if (Array.isArray(l)) return l as IssueLink[];
  if (l && typeof l === 'object') {
    const o = l as { blocks?: Raw[]; blocked_by?: Raw[] };
    return [
      ...(o.blocked_by ?? []).map((x) => ({ type: 'is blocked by', key: String(x.key) })),
      ...(o.blocks ?? []).map((x) => ({ type: 'blocks', key: String(x.key) })),
    ];
  }
  return ((r.depends_on as string[] | undefined) ?? []).map((k) => ({ type: 'is blocked by', key: k }));
}

export function normIssue(raw: unknown): Issue {
  const r = raw as Raw;
  const comments = r.comments as Raw[] | undefined;
  const worklog = (r.worklog ?? r.worklogs) as Raw[] | undefined;
  return {
    ...(r as unknown as Issue),
    key: String(r.key),
    rank: (r.rank as number | string | undefined) ?? 0,
    sprint_id: num(r.sprint_id),
    story_points: num(r.story_points),
    estimate_hours: num(r.estimate_hours),
    time_spent_hours: num(r.time_spent_hours ?? r.logged_hours),
    labels: (r.labels as string[] | undefined) ?? [],
    components: (r.components as string[] | undefined) ?? [],
    created: str(r.created ?? r.created_at),
    updated: str(r.updated ?? r.updated_at),
    resolved: str(r.resolved ?? r.resolved_at),
    project_no: num(r.project_no ?? r.project),
    acceptance_criteria: r.acceptance_criteria === undefined ? undefined : checklist(r.acceptance_criteria),
    definition_of_done: r.definition_of_done === undefined ? undefined : checklist(r.definition_of_done),
    comments: comments?.map((c) => ({ id: c.id as number, author: String(c.author ?? 'me'), body: String(c.body ?? ''), created: String(c.created ?? c.created_at ?? '') })),
    worklog: worklog?.map((w) => ({
      id: w.id as number,
      hours: Number(w.hours),
      date: String(w.date ?? w.work_date ?? ''),
      note: str(w.note),
      author: str(w.author),
      created: str(w.created ?? w.created_at),
    })),
    links: links(r),
    attachments: (r.attachment_urls as Attachment[] | undefined) ?? (r.attachments as Issue['attachments']),
  };
}

function normIssues(raw: unknown): Issue[] {
  const list = Array.isArray(raw) ? raw : ((raw as { issues?: unknown[] } | null)?.issues ?? []);
  return list.map(normIssue);
}

function normHistory(raw: unknown): HistoryEntry[] {
  const list = (Array.isArray(raw) ? raw : []) as Raw[];
  return list
    .map((h) => ({
      ts: String(h.ts ?? h.changed_at ?? ''),
      field: String(h.field ?? ''),
      from: str(h.from ?? h.from_value),
      to: str(h.to ?? h.to_value),
      actor: str(h.actor ?? h.author),
    }))
    .sort((a, b) => (a.ts < b.ts ? 1 : -1));
}

function normSprint(raw: unknown): Sprint {
  const r = raw as Raw;
  return {
    id: Number(r.id),
    name: String(r.name ?? `Sprint ${r.id}`),
    goal: str(r.goal),
    state: (r.state as Sprint['state']) ?? 'future',
    start_date: str(r.start_date),
    end_date: str(r.end_date),
    completed_at: str(r.completed_at),
    project: num(r.project),
  };
}

function normMeta(raw: unknown): TrackerMeta {
  const r = raw as TrackerMeta;
  return { ...r, sprints: (r.sprints ?? []).map(normSprint), me: r.me ?? 'me' };
}

function normBurndown(raw: unknown): BurndownReport {
  const r = raw as Raw & { days?: Raw[] };
  const sprint = r.sprint as Raw | undefined;
  return {
    sprint_id: Number(r.sprint_id ?? sprint?.id ?? 0),
    unit: String(r.unit ?? r.metric ?? 'hours'),
    committed: Number(r.committed ?? 0),
    days: (r.days ?? [])
      .filter((d) => d.working !== false)
      .map((d) => ({ date: String(d.date), remaining: num(d.remaining), ideal: Number(d.ideal ?? 0) })),
  };
}

function normVelocity(raw: unknown): VelocityReport {
  const rows = (Array.isArray(raw) ? raw : ((raw as Raw)?.sprints as Raw[] | undefined) ?? []) as Raw[];
  const unit = Array.isArray(raw) ? 'hours' : String((raw as Raw).unit ?? 'hours');
  return {
    unit,
    sprints: rows
      .filter((x) => x.state !== 'future')
      .map((x): VelocityRow => ({
        sprint_id: Number(x.sprint_id ?? x.id),
        name: String(x.name),
        committed: Number(x.committed ?? x.committed_hours ?? 0),
        completed: Number(x.completed ?? x.completed_hours ?? 0),
      })),
  };
}

function normCfd(raw: unknown): CfdReport {
  if (Array.isArray(raw)) return { rows: raw as CfdRow[] };
  const r = raw as { rows?: CfdRow[]; days?: CfdRow[] };
  return { rows: r.rows ?? r.days ?? [] };
}

/** The backend addresses acceptance criteria by position: [{idx, checked}]. */
function patchBody(p: IssuePatch): Record<string, unknown> {
  const { acceptance_criteria, ...rest } = p;
  return acceptance_criteria ? { ...rest, acceptance_criteria: acceptance_criteria.map((c, i) => ({ idx: c.idx ?? i, checked: c.done })) } : rest;
}

// ---------------------------------------------------------------- control

export const team = {
  links: () => get<{ links: Record<string, string> }>('/api/handbook/team-links'),
};

export const sim = {
  health: () => get<Health>('/api/health'),
  status: () => get<SimStatus>('/api/sim/status'),
  build: (preset: Preset, seed: number) => post<{ run_id: number | string }>('/api/sim/build', { preset, seed, confirm: true, replace: true }),
  cancelBuild: () => post<SimStatus>('/api/sim/build/cancel'),
  batch: (hours: number) => post<RunSummary>('/api/sim/batch', { hours }),
  streamStart: (interval_s: number, speed: number) => post<SimStatus>('/api/sim/stream/start', { interval_s, speed }),
  streamStop: () => post<SimStatus>('/api/sim/stream/stop'),
  setDq: (dq: DqSettings) => put<DqSettings>('/api/sim/dq', dq),
  scenarios: () => get<Scenario[]>('/api/sim/scenarios'),
  runScenario: (id: string, params: Record<string, unknown>) =>
    post<RunSummary>(`/api/sim/scenarios/${encodeURIComponent(id)}`, { params }),
  runs: (limit = 50) => get<RunSummary[]>(`/api/sim/runs${qs({ limit })}`),
  connections: () => get<Connections>('/api/sim/connections'),
  eventsUrl: '/api/sim/events',
};

// ---------------------------------------------------------------- UAT source extract

export const uat = {
  options: () => get<ExtractOptions>('/api/uat/extract/options'),
  /** Periods omitted: the server's defaults for the project. */
  build: (project: string, periods?: string[]) => post<ExtractResult>('/api/uat/extract', { project, periods }),
  downloadUrl: (id: string) => `/api/uat/extract/${encodeURIComponent(id)}`,
};

// ---------------------------------------------------------------- tracker

const T = '/api/tracker';

export interface IssueQuery {
  jql?: string;
  sprint?: number | 'backlog' | null;
  epic?: string | null;
  status?: string | null;
  type?: string | null;
}

export const tracker = {
  meta: () => get<unknown>(`${T}/meta`).then(normMeta),
  setup: (start_date: string) => post<unknown>(`${T}/setup`, { start_date }).then(normMeta),
  issues: (q: IssueQuery = {}) =>
    get<unknown>(`${T}/issues${qs({ jql: q.jql, sprint: q.sprint ?? undefined, epic: q.epic, status: q.status, type: q.type })}`).then(normIssues),
  issue: (key: string) => get<unknown>(`${T}/issues/${encodeURIComponent(key)}`).then(normIssue),
  update: (key: string, body: IssuePatch) => patch<unknown>(`${T}/issues/${encodeURIComponent(key)}`, patchBody(body)).then(normIssue),
  create: (body: NewIssue) => post<unknown>(`${T}/issues`, body).then(normIssue),
  comment: (key: string, body: string) => post<Comment>(`${T}/issues/${encodeURIComponent(key)}/comments`, { body }),
  worklog: (key: string, hours: number, date: string, note: string) =>
    post<Worklog>(`${T}/issues/${encodeURIComponent(key)}/worklog`, { hours, date, note }),
  history: (key: string) => get<unknown>(`${T}/issues/${encodeURIComponent(key)}/history`).then(normHistory),
  /** Returns at least {key, rank}; the mock returns the whole issue. */
  rank: (key: string, pos: { before?: string; after?: string }) =>
    post<Partial<Issue> & { key: string }>(`${T}/issues/${encodeURIComponent(key)}/rank`, pos),
  sprints: () => get<unknown[]>(`${T}/sprints`).then((l) => l.map(normSprint)),
  startSprint: (id: number) => post<unknown>(`${T}/sprints/${id}/start`).then(normSprint),
  completeSprint: (id: number, move_open_to: number | 'backlog') =>
    post<unknown>(`${T}/sprints/${id}/complete`, { move_open_to }).then(normSprint),
  burndown: (sprint: number) => get<unknown>(`${T}/reports/burndown${qs({ sprint })}`).then(normBurndown),
  velocity: () => get<unknown>(`${T}/reports/velocity`).then(normVelocity),
  cfd: () => get<unknown>(`${T}/reports/cfd`).then(normCfd),
  restore: (backup: unknown) => post<unknown>(`${T}/restore`, backup),
  exportCsvUrl: `${T}/export/jira.csv`,
  backupUrl: `${T}/backup`,
  /** Wireframes live in the brand kit; in mock mode Vite (or the demo's dist) serves it at /brand. */
  attachmentUrl: (p: string) =>
    import.meta.env.VITE_MOCK === '1' ? withBase(`/brand/${p}`) : `${T}/attachments/${p.split('/').map(encodeURIComponent).join('/')}`,
};

