import { useEffect, useSyncExternalStore } from 'react';
import { useQueryClient, type QueryClient } from '@tanstack/react-query';
import { sim } from './client';
import { keys } from './queries';
import type { LogEvent, ProgressEvent, RunSummary, SimStatus } from './types';

/* A small external store for what arrives over SSE and is not a query result: build progress, log lines and the
   stream's change feed. Queries stay the source of truth for status and runs; events patch their caches. */

export type LinkState = 'connecting' | 'open' | 'retrying';

interface LiveState {
  link: LinkState;
  progress: ProgressEvent | null;
  logs: LogEvent[];
  feed: RunSummary[];
  lastEventAt: number | null;
}

const MAX_LOGS = 200;
const MAX_FEED = 40;

let state: LiveState = { link: 'connecting', progress: null, logs: [], feed: [], lastEventAt: null };
const listeners = new Set<() => void>();

function set(patch: Partial<LiveState>) {
  state = { ...state, ...patch };
  listeners.forEach((l) => l());
}

export function useLive<T>(select: (s: LiveState) => T): T {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => select(state),
  );
}

function parse<T>(ev: Event): T | null {
  try {
    return JSON.parse((ev as MessageEvent<string>).data) as T;
  } catch {
    return null;
  }
}

function onRun(qc: QueryClient, run: RunSummary) {
  qc.setQueryData<RunSummary[]>(keys.runs, (prev) => {
    if (!prev) return prev;
    const rest = prev.filter((r) => r.run_id !== run.run_id);
    return [run, ...rest].slice(0, 200);
  });
  if (run.kind === 'stream_tick') {
    set({ feed: [run, ...state.feed.filter((r) => r.run_id !== run.run_id)].slice(0, MAX_FEED) });
  }
  if (run.kind === 'build' && run.status !== 'running') set({ progress: null });
  void qc.invalidateQueries({ queryKey: keys.status });
}

/** Opens one EventSource for the whole app. Reconnects with backoff; status polling covers the gaps. */
export function useSimEvents() {
  const qc = useQueryClient();
  useEffect(() => {
    let es: EventSource | null = null;
    let retry = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let disposed = false;

    const open = () => {
      if (disposed) return;
      set({ link: retry === 0 ? 'connecting' : 'retrying' });
      es = new EventSource(sim.eventsUrl);
      es.onopen = () => {
        retry = 0;
        set({ link: 'open' });
      };
      es.onerror = () => {
        es?.close();
        set({ link: 'retrying' });
        retry += 1;
        timer = setTimeout(open, Math.min(30_000, 1000 * 2 ** Math.min(retry, 5)));
      };
      es.addEventListener('state', (ev) => {
        const s = parse<SimStatus>(ev);
        if (s) qc.setQueryData(keys.status, s);
        set({ lastEventAt: Date.now() });
      });
      es.addEventListener('progress', (ev) => {
        const p = parse<ProgressEvent>(ev);
        if (!p) return;
        const pct = p.pct > 1 ? p.pct / 100 : p.pct;
        set({ progress: { ...p, pct }, lastEventAt: Date.now() });
      });
      es.addEventListener('log', (ev) => {
        const l = parse<LogEvent>(ev);
        if (l) set({ logs: [...state.logs, l].slice(-MAX_LOGS), lastEventAt: Date.now() });
      });
      es.addEventListener('run', (ev) => {
        const r = parse<RunSummary>(ev);
        if (r) onRun(qc, r);
        set({ lastEventAt: Date.now() });
      });
    };
    open();
    return () => {
      disposed = true;
      clearTimeout(timer);
      es?.close();
    };
  }, [qc]);
}

export function clearLogs() {
  set({ logs: [] });
}
