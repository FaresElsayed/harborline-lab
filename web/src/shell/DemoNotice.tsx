/* Demo build only (VITE_DEMO=1): says what this page is and where to get the real thing. Lazy-loaded behind VITE_DEMO, so it never ships otherwise. */
import { useState } from 'react';
import { X } from 'lucide-react';
import { IconButton } from '@/components/ui/button';

const REPO_URL = import.meta.env.VITE_REPO_URL || 'https://github.com/FaresElsayed/harborline-lab';
const DISMISSED_KEY = 'harborline.demo.notice.dismissed';

function wasDismissed() {
  try {
    return sessionStorage.getItem(DISMISSED_KEY) === '1';
  } catch {
    return false;
  }
}

/**
 * Never sits on a control. On a phone it takes its own strip below the sheet. Wider, it floats on the sheet, and the style
 * below stops every page 52px short of the sheet's edge so that band stays empty. Both go away when it is dismissed.
 */
export function DemoNotice() {
  const [dismissed, setDismissed] = useState(wasDismissed);
  if (dismissed) return null;

  const dismiss = () => {
    setDismissed(true);
    try {
      sessionStorage.setItem(DISMISSED_KEY, '1');
    } catch {
      /* private window: it just comes back on the next load */
    }
  };

  return (
    <div className="pointer-events-none z-20 flex shrink-0 justify-center px-2 pb-[max(6px,env(safe-area-inset-bottom))] pt-1.5 min-[641px]:absolute min-[641px]:inset-x-0 min-[641px]:bottom-[15px] min-[641px]:p-0">
      <style>{'@media (min-width: 641px) { #main { padding-bottom: 52px; } }'}</style>
      <div
        role="note"
        className="pointer-events-auto flex min-w-0 animate-[fade-in_200ms_ease-out] items-center gap-2 rounded-full border border-line bg-sheet py-1 pl-3.5 pr-1 text-sm text-muted shadow-float max-[640px]:rounded-l"
      >
        <p className="flex min-w-0 flex-wrap gap-x-3">
          <span>Live demo with sample data. Nothing is saved.</span>
          <a href={REPO_URL} target="_blank" rel="noreferrer" className="font-medium text-accent-strong hover:underline">
            Get it on GitHub
          </a>
        </p>
        <IconButton label="Dismiss this notice" tip={false} size="sm" variant="quiet" onClick={dismiss} className="rounded-full">
          <X aria-hidden />
        </IconButton>
      </div>
    </div>
  );
}
