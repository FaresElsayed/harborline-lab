import { isRouteErrorResponse, Link, useRouteError } from 'react-router';
import { Button, buttonClasses } from '@/components/ui/button';
import { withBase } from '@/lib/base';

/** While a screen's code loads: its title row as flat bars at their real size, no shimmer. */
export function PageFallback() {
  return (
    <div className="px-6 pt-6" aria-busy="true" aria-label="Loading">
      <span className="block h-7 w-56 rounded-s bg-active" />
      <span className="mt-2.5 block h-4 w-96 max-w-full rounded-s bg-active" />
    </div>
  );
}

export function NotFound() {
  return (
    <div className="mx-auto max-w-lg px-6 py-24">
      <h1 className="figure text-[1.75rem] font-semibold">Nothing at this address</h1>
      <p className="mt-2 text-md text-muted">The page may have moved. The Control Center and the tracker are one click away.</p>
      <div className="mt-6 flex gap-2">
        <Link to="/control" className={buttonClasses('primary')}>
          Control Center
        </Link>
        <Link to="/tracker/board" className={buttonClasses()}>
          Tracker board
        </Link>
      </div>
    </div>
  );
}

export function RouteError() {
  const err = useRouteError();
  const msg = isRouteErrorResponse(err) ? `${err.status} ${err.statusText}` : err instanceof Error ? err.message : String(err);
  return (
    <div className="grid min-h-dvh place-items-center bg-frame px-6">
      <div className="max-w-lg">
        <h1 className="figure text-[1.75rem] font-semibold">Something broke on this screen</h1>
        <p className="mt-2 text-md text-muted">Reload to try again. If it keeps happening, the message below helps when you report it.</p>
        <pre className="mt-4 overflow-x-auto rounded-m border border-line bg-sheet p-3 font-mono text-sm text-ink">{msg}</pre>
        <div className="mt-6 flex gap-2">
          <Button variant="primary" onClick={() => location.reload()}>
            Reload
          </Button>
          <a href={withBase('/control')} className={buttonClasses()}>
            Go to the Control Center
          </a>
        </div>
      </div>
    </div>
  );
}
