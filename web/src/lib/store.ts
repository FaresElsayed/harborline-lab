import { useSyncExternalStore } from 'react';

/** Minimal external store: enough for app-wide UI flags without a state library. */
export function createStore<S extends object>(initial: S) {
  let state = initial;
  const listeners = new Set<() => void>();
  const get = () => state;
  const set = (patch: Partial<S> | ((s: S) => Partial<S>)) => {
    const next = typeof patch === 'function' ? patch(state) : patch;
    state = { ...state, ...next };
    listeners.forEach((l) => l());
  };
  const subscribe = (l: () => void) => {
    listeners.add(l);
    return () => {
      listeners.delete(l);
    };
  };
  function use<T>(select: (s: S) => T): T {
    return useSyncExternalStore(subscribe, () => select(state));
  }
  return { get, set, use, subscribe };
}

export interface UiState {
  palette: boolean;
  shortcuts: boolean;
  createIssue: null | { sprint_id?: number | null; parent_key?: string | null; epic_key?: string | null; type?: 'Task' | 'Story' | 'Bug' | 'Spike' };
  buildDialog: boolean;
  navOpen: boolean; // narrow screens: sidebar drawer
  navCollapsed: boolean; // wide screens: user hid the sidebar
  /** Issue the keyboard is on (board / lists): target of e, i and Enter. */
  focusedIssue: string | null;
}

function readCollapsed() {
  try {
    return localStorage.getItem('harborline.nav.collapsed') === '1';
  } catch {
    return false;
  }
}

export const ui = createStore<UiState>({
  palette: false,
  shortcuts: false,
  createIssue: null,
  buildDialog: false,
  navOpen: false,
  navCollapsed: readCollapsed(),
  focusedIssue: null,
});

export function toggleNavCollapsed() {
  const next = !ui.get().navCollapsed;
  ui.set({ navCollapsed: next });
  try {
    localStorage.setItem('harborline.nav.collapsed', next ? '1' : '0');
  } catch {
    /* ignore */
  }
}

/** Per-viewer preferences kept in localStorage (wrapped: storage can be unavailable). */
export function usePersisted<T>(key: string, fallback: T): [T, (v: T) => void] {
  const store = persisted(key, fallback);
  const value = store.use((s) => s.v);
  return [value, (v: T) => store.set({ v })];
}

const persistedStores = new Map<string, ReturnType<typeof createStore<{ v: unknown }>>>();
function persisted<T>(key: string, fallback: T) {
  let s = persistedStores.get(key);
  if (!s) {
    let initial: unknown = fallback;
    try {
      const raw = localStorage.getItem(key);
      if (raw != null) initial = JSON.parse(raw);
    } catch {
      /* ignore */
    }
    s = createStore<{ v: unknown }>({ v: initial });
    s.subscribe(() => {
      try {
        localStorage.setItem(key, JSON.stringify(s!.get().v));
      } catch {
        /* ignore */
      }
    });
    persistedStores.set(key, s);
  }
  return s as unknown as ReturnType<typeof createStore<{ v: T }>>;
}
