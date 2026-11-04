import { useEffect } from 'react';
import { toast } from 'sonner';
import { useUpdateIssue } from '@/api/queries';
import { useHotkeys } from '@/lib/hotkeys';
import { ui } from '@/lib/store';
import { useIssueParam } from './data';

function focusKey(key: string) {
  const el = document.querySelector<HTMLElement>(`[data-issue-key="${CSS.escape(key)}"]`);
  if (!el) return;
  el.focus({ preventScroll: true });
  el.scrollIntoView({ block: 'nearest', inline: 'nearest' });
}

function currentKey(): string | null {
  const active = document.activeElement as HTMLElement | null;
  return active?.closest<HTMLElement>('[data-issue-key]')?.dataset.issueKey ?? ui.get().focusedIssue;
}

/**
 * Jira's list keys on whatever screen is showing issues: j / k move, e edits the summary, i assigns to me.
 * `keys` is the visible order; Enter is handled by each row or card (it opens the issue panel).
 */
export function useIssueNav(keys: string[], me: string) {
  const { open } = useIssueParam();
  const update = useUpdateIssue();

  useEffect(() => {
    const onFocus = (e: FocusEvent) => {
      const k = (e.target as HTMLElement | null)?.closest?.<HTMLElement>('[data-issue-key]')?.dataset.issueKey;
      if (k) ui.set({ focusedIssue: k });
    };
    document.addEventListener('focusin', onFocus);
    return () => document.removeEventListener('focusin', onFocus);
  }, []);

  const move = (d: 1 | -1) => {
    if (!keys.length) return;
    const cur = currentKey();
    const idx = cur ? keys.indexOf(cur) : -1;
    const next = keys[idx < 0 ? (d === 1 ? 0 : keys.length - 1) : Math.min(keys.length - 1, Math.max(0, idx + d))];
    ui.set({ focusedIssue: next });
    focusKey(next);
  };

  useHotkeys({
    j: () => move(1),
    k: () => move(-1),
    e: () => {
      const k = currentKey();
      if (k) open(k, { edit: true });
    },
    i: () => {
      const k = currentKey();
      if (!k) return;
      update.mutate(
        { key: k, patch: { assignee: me } },
        {
          onSuccess: () => toast.success(`${k} assigned to you`),
          onError: (e) => toast.error(`Could not assign ${k}`, { description: e.message }),
        },
      );
    },
  });
}
