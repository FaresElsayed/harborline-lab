import { useSyncExternalStore } from 'react';
import { useQuery } from '@tanstack/react-query';
import { handbookProgress } from '@/api/client';
import { useAllIssues, useMeta, useStatus } from '@/api/queries';

/**
 * Ticks for the Start here path. They live where START_HERE.html keeps its own (see handbook.js: same localStorage key,
 * same page in /api/handbook/progress), so the app and the handbook's front door always show the same steps ticked.
 */
const STORE_KEY = 'harborline.handbook.v1';
const PAGE = 'start';
const SECTION = 'quickstart';
const isDemo = import.meta.env.VITE_DEMO === '1';

export const STEP_IDS = ['setup', 'brief', 'data', 'platform', 'date', 'nadia', 'ticket'] as const;
export type StepId = (typeof STEP_IDS)[number];

interface PageState {
  items: Record<string, string>;
  data: Record<string, unknown>;
  updated_at: string | null;
  [other: string]: unknown;
}

const empty = (): PageState => ({ items: {}, data: {}, updated_at: null });

function readStore(): { version: 1; pages: Record<string, PageState> } {
  try {
    const parsed = JSON.parse(localStorage.getItem(STORE_KEY) ?? 'null');
    if (parsed?.version === 1 && parsed.pages) return parsed;
  } catch {
    /* private window or a corrupt value: start empty */
  }
  return { version: 1, pages: {} };
}

function readPage(): PageState {
  const page = readStore().pages[PAGE];
  return { ...empty(), ...page, items: page?.items ?? {}, data: page?.data ?? {} };
}

let snapshot = readPage();
const listeners = new Set<() => void>();

function commit(page: PageState) {
  snapshot = page;
  const store = readStore();
  store.pages[PAGE] = page;
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(store));
  } catch {
    /* storage unavailable: the ticks last until the tab closes */
  }
  listeners.forEach((l) => l());
}

function subscribe(listener: () => void) {
  const onStorage = (e: StorageEvent) => {
    if (e.key !== STORE_KEY) return;
    snapshot = readPage();
    listener();
  };
  listeners.add(listener);
  window.addEventListener('storage', onStorage);
  return () => {
    listeners.delete(listener);
    window.removeEventListener('storage', onStorage);
  };
}

const useStartState = () => useSyncExternalStore(subscribe, () => snapshot);

function change(mutate: (page: PageState) => void) {
  const page: PageState = { ...snapshot, items: { ...snapshot.items }, data: { ...snapshot.data } };
  mutate(page);
  page.updated_at = new Date().toISOString();
  commit(page);
  if (!isDemo) void handbookProgress.put(PAGE, page).catch(() => undefined); // still saved in this browser
}

export function setStepDone(id: StepId, done: boolean) {
  change((page) => {
    if (done) page.items[`${SECTION}.${id}`] = new Date().toISOString();
    else {
      delete page.items[`${SECTION}.${id}`];
      delete page.data.finished;
    }
  });
}

/** Finished, or skipped by someone who knows their way around: either way the app stops opening on Start here. */
export function markFinished() {
  change((page) => {
    page.data.finished = new Date().toISOString();
  });
}

export const isFinished = () => !!snapshot.data.finished;

/** Newest wins, the same rule handbook.js uses. */
async function pullProgress() {
  const res = await handbookProgress.get(PAGE);
  const remote = res.state;
  const remoteAt = (remote?.updated_at as string | undefined) ?? res.updated_at;
  const localAt = snapshot.updated_at;
  if (remote?.items && remoteAt && (!localAt || Date.parse(remoteAt) > Date.parse(localAt))) {
    commit({ ...empty(), ...remote, items: remote.items as Record<string, string>, data: (remote.data as Record<string, unknown>) ?? {}, updated_at: remoteAt });
  } else if (localAt && (!remoteAt || Date.parse(localAt) > Date.parse(remoteAt))) {
    await handbookProgress.put(PAGE, snapshot);
  }
  return true;
}

/** Every step's state. A step the tracker or simulator can vouch for ticks itself; the demo's sample data vouches for nothing. */
export function useStartPath() {
  const state = useStartState();
  const status = useStatus();
  const meta = useMeta();
  const issues = useAllIssues(!!meta.data?.configured);
  useQuery({ queryKey: ['handbook', PAGE], queryFn: pullProgress, enabled: !isDemo, staleTime: 5 * 60_000, retry: false, refetchOnWindowFocus: false });

  const auto: Partial<Record<StepId, boolean>> = isDemo
    ? {}
    : {
        setup: !!status.data?.built_at,
        date: !!meta.data?.configured,
        ticket: (issues.data ?? []).some((i) => i.type !== 'Epic' && i.status !== 'To Do'),
      };
  const ticked = (id: StepId) => !!state.items[`${SECTION}.${id}`];
  const isAuto = (id: StepId) => !!auto[id];
  const done = (id: StepId) => ticked(id) || isAuto(id);
  const doneCount = STEP_IDS.filter(done).length;
  return { done, isAuto, doneCount, total: STEP_IDS.length, allDone: doneCount === STEP_IDS.length, finished: !!state.data.finished, current: STEP_IDS.find((id) => !done(id)) ?? null };
}
