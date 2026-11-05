import { useMemo } from 'react';
import { Link } from 'react-router';
import clsx from 'clsx';
import { Check } from 'lucide-react';
import { compareDue, useTracker } from '@/features/tracker/data';
import { IssueKey } from '@/features/tracker/atoms';
import { handbookUrl } from '@/lib/base';

const LABEL = 'first-release';

function useFirstRelease() {
  const t = useTracker();
  return useMemo(() => t.issues.filter((i) => i.labels?.includes(LABEL)).sort(compareDue), [t.issues]);
}

/** The section sits below the fold, so the top of the page points to it until it's finished. */
export function FirstReleaseLink() {
  const items = useFirstRelease();
  if (!items.length || items.every((i) => i.status === 'Done')) return null;
  return (
    <button type="button" className="mt-2 rounded-s text-left text-sm font-medium text-accent-strong hover:underline" onClick={() => document.getElementById('first-release')?.scrollIntoView({ block: 'start' })}>
      Short on time? Ship a first release
    </button>
  );
}

/** The shortest honest way to a finished piece of work: the tickets labelled first-release, in the order they're due. */
export function FirstRelease({ className }: { className?: string }) {
  const items = useFirstRelease();
  if (!items.length) return null;
  const done = items.filter((i) => i.status === 'Done').length;
  const hours = items.reduce((sum, i) => sum + (i.estimate_hours ?? 0), 0);
  const finished = done === items.length;

  return (
    <section aria-labelledby="first-release" className={clsx('rounded-l border border-line px-4 py-3.5', className)}>
      <div className="flex flex-wrap items-baseline justify-between gap-x-4">
        <h2 id="first-release" className="text-md font-semibold text-ink">
          Short on time? Ship a first release
        </h2>
        <span className="num text-sm text-muted">
          {done} of {items.length} done
        </span>
      </div>
      <p className="mt-1 max-w-[62ch] text-base text-muted">
        {items.length} tickets, about {hours} hours: two long days. You finish with your first table in the cloud, one number on a dashboard that matches the source, and merged pull requests to show for it. The rest of Project 1 stays on your board for later.{' '}
        <a href={handbookUrl('getting-started.html#first-release')} target="_blank" rel="noreferrer" className="font-medium text-accent-strong hover:underline">
          How it works
        </a>
      </p>
      <div className="mt-2.5 h-1.5 overflow-hidden rounded-full bg-active" role="progressbar" aria-label="First release progress" aria-valuemin={0} aria-valuemax={items.length} aria-valuenow={done}>
        <div className="h-full origin-left rounded-full bg-good transition-transform duration-300 ease-out" style={{ transform: `scaleX(${done / items.length})` }} />
      </div>
      {finished ? <p className="mt-2.5 text-base text-ink">First release done. Tag it v0.0.1, then pick up the tickets you skipped: they’re still on your board.</p> : null}
      <ol className="mt-2">
        {items.map((issue) => {
          const isDone = issue.status === 'Done';
          return (
            <li key={issue.key} className="border-t border-rule first:border-t-0">
              <Link to={`/tracker/issue/${issue.key}`} className="-mx-1.5 flex items-baseline gap-2 rounded-s px-1.5 py-1.5 transition-colors hover:bg-hover">
                {done ? <span className="w-3.5 shrink-0 self-center">{isDone ? <Check className="size-3.5 text-good" aria-label="Done" /> : null}</span> : null}
                <IssueKey k={issue.key} done={isDone} className="w-16" />
                <span className={clsx('min-w-0 flex-1 text-base', isDone ? 'text-muted' : 'text-ink')}>{issue.summary}</span>
                <span className="num shrink-0 text-sm text-muted">
                  {!isDone && issue.status !== 'To Do' ? `${issue.status} · ` : ''}
                  {issue.estimate_hours ?? 0}h
                </span>
              </Link>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
