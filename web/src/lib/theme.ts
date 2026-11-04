import { useEffect, useSyncExternalStore } from 'react';

export type ThemePref = 'light' | 'dark' | 'system';
const KEY = 'harborline.theme';
const listeners = new Set<() => void>();

function read(): ThemePref {
  try {
    const v = localStorage.getItem(KEY);
    return v === 'light' || v === 'dark' ? v : 'system';
  } catch {
    return 'system';
  }
}

let pref: ThemePref = read();

export function setTheme(next: ThemePref) {
  pref = next;
  try {
    if (next === 'system') localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, next);
  } catch {
    /* private mode: keep it for this session */
  }
  const root = document.documentElement;
  if (next === 'system') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', next);
  listeners.forEach((l) => l());
}

const mq = () => window.matchMedia('(prefers-color-scheme: dark)');

export function useTheme(): { pref: ThemePref; resolved: 'light' | 'dark' } {
  const p = useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => pref,
  );
  const systemDark = useSyncExternalStore(
    (l) => {
      const m = mq();
      m.addEventListener('change', l);
      return () => m.removeEventListener('change', l);
    },
    () => mq().matches,
  );
  return { pref: p, resolved: p === 'system' ? (systemDark ? 'dark' : 'light') : p };
}

/** Cycle light → dark → system. */
export function cycleTheme() {
  setTheme(pref === 'light' ? 'dark' : pref === 'dark' ? 'system' : 'light');
}

export function useApplyTheme() {
  useEffect(() => {
    setTheme(read());
  }, []);
}
