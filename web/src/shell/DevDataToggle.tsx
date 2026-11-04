/* Dev-only (VITE_MOCK=1): switch the mock between the demo dataset and the break-ui worst case (?data=worst).
   Plain chrome on purpose: it is not part of the design under test. Never ships (lazy-loaded behind VITE_MOCK). */
import type { CSSProperties } from 'react';

const track: CSSProperties = { fontFamily: 'system-ui, sans-serif', fontSize: 12, background: '#e5e5e5', color: '#404040' };
const on: CSSProperties = { background: '#fff', color: '#000' };

export function DevDataToggle() {
  const worst = (window as unknown as { __harborlineMock?: { worstCase?: boolean } }).__harborlineMock?.worstCase ?? false;
  const go = (w: boolean) => {
    const url = new URL(location.href);
    if (w) url.searchParams.set('data', 'worst');
    else url.searchParams.delete('data');
    location.href = url.toString();
  };
  return (
    <div role="group" aria-label="Mock dataset" style={track} className="fixed bottom-3 left-1/2 z-[80] flex -translate-x-1/2 gap-0.5 rounded-full p-0.5">
      <button type="button" aria-pressed={!worst} onClick={() => go(false)} style={worst ? undefined : on} className="h-7 rounded-full px-3">
        Demo data
      </button>
      <button type="button" aria-pressed={worst} onClick={() => go(true)} style={worst ? on : undefined} className="h-7 rounded-full px-3">
        Worst case
      </button>
    </div>
  );
}
