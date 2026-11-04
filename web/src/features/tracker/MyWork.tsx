import { useMemo } from 'react';
import { Link } from 'react-router';
import { ArrowRight } from 'lucide-react';
import type { Issue } from '@/api/types';
import { Empty, ErrorNote, Skeleton } from '@/components/ui/misc';
import { dueState, fmtDay, inThisWeek, workWeek } from '@/lib/dates';
import { fmtHours, plural } from '@/lib/format';
import { Avatar, DueBadge, Estimate, IssueKey, PriorityIcon, StatusLozenge, TypeIcon } from './atoms';
import { useIssueParam, useTracker } from './data';
import { useIssueNav } from './nav';

const CAP = 8;
const byDue = (a: Issue, b: Issue) => ((a.due_date ?? '9999') < (b.due_date ?? '9999') ? -1 : 1);

function List({ title, items, jql, empty, onOpen, tone }: { title: string; items: Issue[]; jql: string; empty: string; onOpen: (k: string) => void; tone?: 'bad' }) {
  const t = useTracker();
  const hours = items.reduce((s, i) => s + (i.estimate_hours ?? 0), 0);
  return (
    <section aria-labelledby={`mw-${title}`} className="rounded-l border border-line">
      <header className="flex min-h-12 items-center justify-between gap-3 px-4 pt-2">
        <h2 id={`mw-${title}`} className="text-md font-semibold text-ink">
          {title}{' '}
          <span className={`num ml-1 text-base font-normal ${tone === 'bad' && items.length ? 'text-bad' : 'text-muted'}`}>
            {items.length}
            {hours ? ` · ${fmtHours(hours)}` : ''}
          </span>
        </h2>
        {items.length > CAP ? (
          <Link to={`/tracker/issues?jql=${encodeURIComponent(jql)}`} className="flex items-center gap-1 text-sm font-medium text-accent-strong hover:underline">
            View all {items.length} <ArrowRight className="size-3.5" aria-hidden />
          </Link>
        ) : null}
      </header>
      {items.length === 0 ? (
        <div className="px-4 pb-4 pt-1">
          <Empty>{empty}</Empty>
        </div>
      ) : (
        <ul className="pb-1">
          {items.slice(0, CAP).map((i) => {
            const parent = i.parent_key ? t.byKey.get(i.parent_key) : null;
            return (
              <li key={i.key}>
                <button
                  type="button"
                  data-issue-key={i.key}
                  onClick={() => onOpen(i.key)}
                  className="grid h-14 w-full grid-cols-[16px_minmax(0,1fr)_auto] items-center gap-3 border-t border-rule px-4 text-left outline-none transition-colors hover:bg-hover/60 focus-visible:bg-hover focus-visible:shadow-[inset_2px_0_0_var(--hl-focus)]"
                >
                  <TypeIcon type={i.type} />
                  <span className="min-w-0">
                    <span className="block truncate text-base text-ink">{i.summary}</span>
                    <span className="flex min-w-0 items-center gap-1.5 text-sm text-muted">
                      <IssueKey k={i.key} />
                      {parent ? <span className="truncate">· {parent.summary}</span> : null}
                    </span>
                  </span>
                  <span className="flex items-center gap-2">
                    <DueBadge issue={i} compact />
                    <StatusLozenge status={i.status} className="w-[84px] justify-center max-md:hidden" />
                    <span className="flex w-10 justify-end">
                      <Estimate issue={i} />
                    </span>
                    <PriorityIcon priority={i.priority} />
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

export default function MyWorkPage() {
  const t = useTracker();
  const { open } = useIssueParam();
  const mine = useMemo(() => t.issues.filter((i) => i.assignee === t.me && i.type !== 'Epic'), [t.issues, t.me]);
  const overdue = mine.filter((i) => dueState(i.due_date, i.status === 'Done') === 'overdue').sort(byDue);
  const week = mine.filter((i) => i.status !== 'Done' && inThisWeek(i.due_date) && dueState(i.due_date, false) !== 'overdue').sort(byDue);
  // Overdue work is listed once, under Overdue.
  const doing = mine.filter((i) => (i.status === 'In Progress' || i.status === 'In Review') && !overdue.includes(i)).sort(byDue);
  const upcoming = workWeek().upcoming;
  const next = mine
    .filter((i) => i.status === 'To Do' && !overdue.includes(i) && !week.includes(i))
    .sort(byDue)
    .slice(0, 5);
  useIssueNav([...overdue, ...week, ...doing, ...next].slice(0, 200).map((i) => i.key), t.me);

  if (t.issuesQ.isError) return <div className="p-6"><ErrorNote error={t.issuesQ.error} onRetry={() => void t.issuesQ.refetch()} what="your work" /></div>;

  const me = t.person(t.me);
  const finding = !t.issuesQ.data
    ? 'Loading your issues…'
    : overdue.length
      ? `${plural(overdue.length, 'issue')} overdue, the oldest due ${fmtDay(overdue[0].due_date)}. ${week.length ? `${plural(week.length, 'more issue')} due ${upcoming ? 'next' : 'this'} week.` : `Nothing else is due ${upcoming ? 'next' : 'this'} week.`}`
      : week.length
        ? `Nothing overdue. ${plural(week.length, 'issue')} due ${upcoming ? 'next' : 'this'} week.`
        : `Nothing overdue and nothing due ${upcoming ? 'next' : 'this'} week.`;

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-[1200px] px-6 pb-12 pt-5">
        <div className="mb-1 text-sm text-muted">Harborline Tracker / My work</div>
        <div className="flex items-center gap-3">
          <Avatar person={me} size={32} me />
          <h1 className="figure text-[1.75rem] font-semibold leading-8 text-ink">My work</h1>
        </div>
        <p className="num mt-1 text-md text-muted">{finding}</p>
        {!t.issuesQ.data ? (
          <div className="mt-6 grid grid-cols-2 gap-4">
            {Array.from({ length: 4 }, (_, i) => (
              <Skeleton key={i} className="h-64 rounded-l" />
            ))}
          </div>
        ) : (
          <div className="mt-6 grid grid-cols-2 items-start gap-4 max-[1100px]:grid-cols-1">
            <List
              title="Overdue"
              tone="bad"
              items={overdue}
              onOpen={open}
              jql="assignee = currentUser() AND duedate < now() AND status != Done ORDER BY duedate ASC"
              empty="Nothing overdue. Keep it that way."
            />
            <List
              title={upcoming ? 'Due next week' : 'Due this week'}
              items={week}
              onOpen={open}
              jql="assignee = currentUser() AND duedate >= now() AND duedate <= 7d AND status != Done ORDER BY duedate ASC"
              empty="Nothing else is due this week."
            />
            <List
              title="In progress, on time"
              items={doing}
              onOpen={open}
              jql='assignee = currentUser() AND status IN ("In Progress", "In Review") ORDER BY duedate ASC'
              empty="Nothing in progress. Pick the next issue from the board."
            />
            <List title="Up next" items={next} onOpen={open} jql='assignee = currentUser() AND status = "To Do" ORDER BY duedate ASC' empty="Your queue is empty." />
          </div>
        )}
      </div>
    </div>
  );
}
