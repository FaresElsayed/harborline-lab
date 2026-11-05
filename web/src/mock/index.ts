/* VITE_MOCK=1: answer /api/* in the browser so the UI can be built and reviewed without the backend.
   This module is only imported behind `import.meta.env.VITE_MOCK === '1'`, so it never ships in production. */

import { withBase } from '@/lib/base';
import * as sim from './sim';
import * as tracker from './tracker';
import * as uat from './uat';
import { HttpError } from './tracker';

type Handler = (m: RegExpMatchArray, url: URL, body: unknown) => unknown | Promise<unknown>;
const routes: Array<[method: string, pattern: RegExp, handler: Handler]> = [];
const on = (method: string, pattern: RegExp, handler: Handler) => routes.push([method, pattern, handler]);

const b = (body: unknown) => (body ?? {}) as Record<string, unknown>;

// control
on('GET', /^\/api\/health$/, () => ({ ok: true, db: true }));
on('GET', /^\/api\/sim\/status$/, () => sim.status());
on('POST', /^\/api\/sim\/build$/, (_m, _u, body) => sim.build(b(body).preset as never, Number(b(body).seed), b(body).confirm === true));
on('POST', /^\/api\/sim\/build\/cancel$/, () => sim.cancelBuild());
on('POST', /^\/api\/sim\/batch$/, (_m, _u, body) => sim.batch(Number(b(body).hours)));
on('POST', /^\/api\/sim\/stream\/start$/, (_m, _u, body) => sim.streamStart(Number(b(body).interval_s), Number(b(body).speed)));
on('POST', /^\/api\/sim\/stream\/stop$/, () => sim.streamStop());
on('PUT', /^\/api\/sim\/dq$/, (_m, _u, body) => sim.setDq(body as never));
on('GET', /^\/api\/sim\/scenarios$/, () => sim.scenarios());
on('POST', /^\/api\/sim\/scenarios\/([\w-]+)$/, (m, _u, body) => sim.runScenario(m[1], (b(body).params as Record<string, unknown>) ?? {}));
on('GET', /^\/api\/sim\/runs$/, (_m, u) => sim.runs(Number(u.searchParams.get('limit') ?? 50)));
on('GET', /^\/api\/sim\/connections$/, () => sim.connections());

// saved chats on the Team page
on('GET', /^\/api\/handbook\/team-links$/, () => ({ links: {}, site: null }));

// handbook progress (the app's Start here keeps its ticks in the `start` page)
const progress = new Map<string, { state: unknown; updated_at: string }>();
on('GET', /^\/api\/handbook\/progress\/([\w.-]+)$/, (m) => ({ page: m[1], state: {}, updated_at: null, ...progress.get(m[1]) }));
on('PUT', /^\/api\/handbook\/progress\/([\w.-]+)$/, (m, _u, body) => {
  const row = { state: b(body).state, updated_at: new Date().toISOString() };
  progress.set(m[1], row);
  return { page: m[1], ...row };
});

// the app's version and where newer ones are published
on('GET', /^\/api\/version$/, () => ({ version: '2026.11.9', update_repo: 'harborline-lab/harborline-lab' }));

// UAT source extract
on('GET', /^\/api\/uat\/extract\/options$/, () => uat.options());
on('POST', /^\/api\/uat\/extract$/, (_m, _u, body) => uat.build(String(b(body).project ?? ''), b(body).periods as string[] | undefined));

// tracker
const T = '\\/api\\/tracker';
const KEY = '([A-Za-z]+-\\d+)';
const r = (s: string) => new RegExp(`^${T}${s}$`);
on('GET', r('\\/meta'), () => tracker.meta());
on('POST', r('\\/setup'), (_m, _u, body) => tracker.setup(String(b(body).start_date ?? '')));
on('GET', r('\\/issues'), (_m, u) => tracker.listIssues(u.searchParams));
on('POST', r('\\/issues'), (_m, _u, body) => tracker.createIssue(body as never));
on('GET', r(`\\/issues\\/${KEY}`), (m) => tracker.getIssue(m[1]));
on('PATCH', r(`\\/issues\\/${KEY}`), (m, _u, body) => tracker.patchIssue(m[1], body as never));
on('POST', r(`\\/issues\\/${KEY}\\/comments`), (m, _u, body) => tracker.addComment(m[1], String(b(body).body ?? '')));
on('POST', r(`\\/issues\\/${KEY}\\/worklog`), (m, _u, body) =>
  tracker.addWorklog(m[1], Number(b(body).hours), String(b(body).date), String(b(body).note ?? '')),
);
on('GET', r(`\\/issues\\/${KEY}\\/history`), (m) => tracker.history(m[1]));
on('POST', r(`\\/issues\\/${KEY}\\/rank`), (m, _u, body) => tracker.rankIssue(m[1], body as never));
on('GET', r('\\/sprints'), () => tracker.sprints());
on('POST', r('\\/sprints\\/(\\d+)\\/start'), (m) => tracker.startSprint(Number(m[1])));
on('POST', r('\\/sprints\\/(\\d+)\\/complete'), (m, _u, body) => {
  const to = b(body).move_open_to;
  return tracker.completeSprint(Number(m[1]), to === 'backlog' || to == null ? 'backlog' : Number(to));
});
on('GET', r('\\/reports\\/burndown'), (_m, u) => tracker.burndown(Number(u.searchParams.get('sprint'))));
on('GET', r('\\/reports\\/velocity'), () => tracker.velocity());
on('GET', r('\\/reports\\/cfd'), () => tracker.cfd());
on('GET', r('\\/backup'), () => tracker.backup());
on('POST', r('\\/restore'), (_m, _u, body) => tracker.restore(body));

const json = (status: number, data: unknown) =>
  new Response(data === undefined ? null : JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });

const delay = () => new Promise((res) => setTimeout(res, 90 + Math.random() * 160));

async function handle(method: string, url: URL, init?: RequestInit): Promise<Response> {
  if (url.pathname === '/api/tracker/export/jira.csv') {
    return new Response(tracker.jiraCsv(), { headers: { 'Content-Type': 'text/csv', 'Content-Disposition': 'attachment; filename="harborline-jira.csv"' } });
  }
  const extract = method === 'GET' ? url.pathname.match(/^\/api\/uat\/extract\/([0-9a-f]{12})$/) : null;
  if (extract) {
    const z = uat.zip(extract[1]);
    if (!z) return json(404, { detail: 'That extract is no longer available. Build a new one.' });
    return new Response(z.blob, { headers: { 'Content-Type': 'application/zip', 'Content-Disposition': `attachment; filename="${z.filename}"` } });
  }
  if (url.pathname.startsWith('/api/tracker/attachments/')) {
    const path = url.pathname.replace('/api/tracker/attachments/', '');
    return fetchOriginal(withBase(`/brand/${path}`));
  }
  let body: unknown = undefined;
  if (init?.body && typeof init.body === 'string') {
    try {
      body = JSON.parse(init.body);
    } catch {
      return json(400, { detail: 'Body is not JSON' });
    }
  }
  for (const [m, pattern, handler] of routes) {
    if (m !== method) continue;
    const match = url.pathname.match(pattern);
    if (!match) continue;
    await delay();
    try {
      return json(200, await handler(match, url, body));
    } catch (e) {
      if (e instanceof HttpError) return json(e.status, { detail: e.message });
      console.error(e);
      return json(500, { detail: (e as Error).message });
    }
  }
  return json(404, { detail: `No mock for ${method} ${url.pathname}` });
}

let fetchOriginal: typeof fetch = fetch;

/** EventSource stand-in fed by the mock simulator. */
class MockEventSource extends EventTarget {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  readyState = 0;
  readonly url: string;
  readonly withCredentials = false;
  onopen: ((ev: Event) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  private off: (() => void) | null = null;

  constructor(url: string | URL) {
    super();
    this.url = String(url);
    setTimeout(() => {
      if (this.readyState === 2) return;
      this.readyState = 1;
      this.onopen?.(new Event('open'));
      this.off = sim.subscribe((event, data) => {
        this.dispatchEvent(new MessageEvent(event, { data: JSON.stringify(data) }));
      });
    }, 150);
  }
  close() {
    this.readyState = 2;
    this.off?.();
  }
}

export function installMocks() {
  fetchOriginal = window.fetch.bind(window);
  window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, location.origin);
    const method = (init?.method ?? (input instanceof Request ? input.method : 'GET')).toUpperCase();
    if (url.origin === location.origin && url.pathname.startsWith('/api/')) return handle(method, url, init);
    return fetchOriginal(input, init);
  };
  (window as unknown as { EventSource: unknown }).EventSource = MockEventSource;
  (window as unknown as { __harborlineMock: unknown }).__harborlineMock = { resetTracker: tracker.resetTracker, worstCase: tracker.WORST_CASE };
  console.info('[harborline] mock API active (VITE_MOCK=1)');
}
