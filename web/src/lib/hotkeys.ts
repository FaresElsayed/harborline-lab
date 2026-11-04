import { useEffect, useRef } from 'react';

type Handler = (e: KeyboardEvent) => void;

export function isTypingTarget(t: EventTarget | null): boolean {
  const el = t as HTMLElement | null;
  if (!el || !el.tagName) return false;
  if (el.isContentEditable) return true;
  const tag = el.tagName;
  if (tag === 'TEXTAREA' || tag === 'SELECT') return true;
  if (tag === 'INPUT') {
    const type = (el as HTMLInputElement).type;
    return !['checkbox', 'radio', 'button', 'submit', 'range'].includes(type);
  }
  return el.getAttribute('role') === 'combobox';
}

const overlayOpen = () => !!document.querySelector('[role="dialog"][data-state="open"], [role="menu"], [role="listbox"]');

export const isMac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
export const modKey = isMac ? '⌘' : 'Ctrl';

/**
 * Single-key shortcuts in the Jira style ("c", "/", "j", "k", "?") plus "mod+k".
 * Plain keys are ignored while typing or while a dialog or menu is open; "mod+" combos always fire.
 */
export function useHotkeys(map: Record<string, Handler>, enabled = true) {
  const ref = useRef(map);
  ref.current = map;
  useEffect(() => {
    if (!enabled) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.isComposing) return;
      const mod = e.metaKey || e.ctrlKey;
      const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
      if (mod) {
        const h = ref.current[`mod+${key}`];
        if (h) {
          e.preventDefault();
          h(e);
        }
        return;
      }
      if (e.altKey || isTypingTarget(e.target) || overlayOpen()) return;
      const h = ref.current[e.key === '?' ? '?' : key];
      if (h) {
        e.preventDefault();
        h(e);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [enabled]);
}
