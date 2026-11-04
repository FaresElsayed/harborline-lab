import { useEffect, useRef } from 'react';
import { Outlet, useLocation } from 'react-router';
import { AnimatePresence, motion } from 'motion/react';
import { useMeta } from '@/api/queries';
import { ErrorNote, Skeleton } from '@/components/ui/misc';
import { isTypingTarget } from '@/lib/hotkeys';
import { useIssueParam } from './data';
import { IssueView } from './IssueView';
import { Setup } from './Setup';

/** Right-side issue panel: floats over the screen it was opened from, which stays usable. */
function IssuePanel() {
  const { key, edit, open, close } = useIssueParam();
  const ref = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!key) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.defaultPrevented || isTypingTarget(e.target)) return;
      if (document.querySelector('[role="dialog"][data-state="open"], [role="menu"], [role="listbox"]')) return;
      const returnTo = key;
      close();
      requestAnimationFrame(() => document.querySelector<HTMLElement>(`[data-issue-key="${CSS.escape(returnTo)}"]`)?.focus());
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [key, close]);

  useEffect(() => {
    if (key) ref.current?.focus({ preventScroll: true });
  }, [key]);

  return (
    <AnimatePresence>
      {key ? (
        <motion.aside
          key="panel"
          ref={ref}
          tabIndex={-1}
          aria-label={`Issue ${key}`}
          initial={{ opacity: 0, transform: 'translateX(24px)' }}
          animate={{ opacity: 1, transform: 'translateX(0px)' }}
          exit={{ opacity: 0, transform: 'translateX(16px)', transition: { duration: 0.14, ease: [0.23, 1, 0.32, 1] } }}
          transition={{ duration: 0.2, ease: [0.32, 0.72, 0, 1] }}
          className="absolute inset-y-0 right-0 z-30 w-[min(560px,100%)] overflow-y-auto border-l border-line bg-sheet shadow-float outline-none"
        >
          <IssueView key={key} issueKey={key} variant="panel" editSummary={edit} onClose={close} onOpenIssue={(k) => open(k)} />
        </motion.aside>
      ) : null}
    </AnimatePresence>
  );
}

export default function TrackerLayout() {
  const meta = useMeta();
  const location = useLocation();
  const fullPage = location.pathname.startsWith('/tracker/issue/');

  if (meta.isError && !meta.data)
    return (
      <div className="p-6">
        <ErrorNote error={meta.error} onRetry={() => void meta.refetch()} what="the tracker" />
      </div>
    );
  if (!meta.data)
    return (
      <div className="flex flex-col gap-3 p-6" aria-busy>
        <Skeleton className="h-8 w-56" />
        <Skeleton className="h-5 w-96" />
        <div className="mt-4 grid grid-cols-4 gap-3">
          {Array.from({ length: 4 }, (_, i) => (
            <Skeleton key={i} className="h-80" />
          ))}
        </div>
      </div>
    );
  if (!meta.data.configured) return <Setup />;

  return (
    <div className="relative h-full">
      <Outlet />
      {!fullPage ? <IssuePanel /> : null}
    </div>
  );
}
