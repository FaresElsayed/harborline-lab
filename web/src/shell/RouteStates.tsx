import { isRouteErrorResponse, Link, useRouteError } from 'react-router';
import { Button, buttonClasses } from '@/components/ui/button';
import { handbookUrl, withBase } from '@/lib/base';

/** Under a "can't reach the simulator" error: what usually fixed it, and who to ask when it didn't. */
export function OfflineHelp() {
  const link = 'font-medium text-accent-strong hover:underline';
  return (
    <p className="mt-2 text-base text-muted">
      This usually means Docker Desktop isn’t running. Open it and run the start file in the package folder (<code className="font-mono text-[13px]">start.bat</code> or{' '}
      <code className="font-mono text-[13px]">start.sh</code>); running the setup file again also works and is always safe. Still stuck? See{' '}
      <a href={handbookUrl('known-issues.html')} target="_blank" rel="noreferrer" className={link}>
        Known issues
      </a>{' '}
      or paste the error to{' '}
      <Link to="/team/kevin-ohara" className={link}>
        Kevin O’Hara
      </Link>
      .
    </p>
  );
}

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
      <p className="mt-2 text-md text-muted">The page may have moved. Start here, the Control Center and the tracker are one click away.</p>
      <div className="mt-6 flex flex-wrap gap-2">
        <Link to="/start" className={buttonClasses('primary')}>
          Start here
        </Link>
        <Link to="/control" className={buttonClasses()}>
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
        <p className="mt-2 text-md text-muted">
          Reload to try again. If it keeps happening, paste the message below to{' '}
          <a href={withBase('/team/kevin-ohara')} className="font-medium text-accent-strong hover:underline">
            Kevin O’Hara
          </a>{' '}
          or look in{' '}
          <a href={handbookUrl('known-issues.html')} target="_blank" rel="noreferrer" className="font-medium text-accent-strong hover:underline">
            Known issues
          </a>
          .
        </p>
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
