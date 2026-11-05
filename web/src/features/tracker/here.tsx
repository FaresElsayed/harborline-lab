import { useMemo } from 'react';
import { Link } from 'react-router';
import clsx from 'clsx';
import { ArrowRight } from 'lucide-react';
import type { Issue } from '@/api/types';
import { buttonClasses } from '@/components/ui/button';
import { fmtInt, plural } from '@/lib/format';
import { Avatar, DueBadge, IssueKey, LevelChip, TypeIcon } from './atoms';
import { epicShort, nextTicket, progressOf, sprintHours, teamPath, useTracker, workingDaysLeft } from './data';

const percent = (done: number, total: number) => (total ? Math.round((done / total) * 100) : 0);

/** Which project and sprint the program is in now, and how far along each one is. */
export function useProgramPosition() {
  const t = useTracker();
  return useMemo(() => {
    if (!t.configured || !t.issuesQ.data || !t.epics.length) return null;
    const projectOf = (e: Issue) => e.project_no ?? t.epics.indexOf(e) + 1;
    const open = t.epics.find((e) => t.issues.some((i) => i.epic_key === e.key && i.status !== 'Done'));
    const epic = (t.active?.project != null ? t.epics.find((e) => projectOf(e) === t.active?.project) : undefined) ?? open ?? null;
    const sprintItems = t.active ? t.issues.filter((i) => i.sprint_id === t.active?.id && i.type !== 'Epic') : [];
    const sprintTotal = sprintHours(sprintItems);
    const sprintDone = sprintHours(sprintItems.filter((i) => i.status === 'Done'));
    const ordered = [...t.sprints].sort((a, b) => a.id - b.id);
    return {
      epic,
      project: epic ? projectOf(epic) : null,
      projects: t.epics.length,
      progress: epic ? progressOf(t.issues.filter((i) => i.epic_key === epic.key)) : { done: 0, total: 0 },
      sprint: t.active,
      sprintNo: t.active ? ordered.findIndex((s) => s.id === t.active?.id) + 1 : null,
      sprints: ordered.length,
      sprintPct: percent(sprintDone, sprintTotal),
      daysLeft: workingDaysLeft(t.active?.end_date),
      nextSprint: t.nextFuture,
    };
  }, [t.configured, t.issuesQ.data, t.epics, t.issues, t.active, t.sprints, t.nextFuture]);
}

function Meter({ to, label, pct, detail }: { to: string; label: string; pct: number; detail: string }) {
  return (
    <div className="min-w-0 flex-1 basis-60">
      <div className="flex items-baseline justify-between gap-3">
        <Link to={to} className="min-w-0 truncate text-sm font-medium text-ink hover:underline">
          {label}
        </Link>
        <span className="num shrink-0 text-sm text-muted">{pct}%</span>
      </div>
      <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-active" role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}>
        <div className="h-full origin-left rounded-full bg-accent" style={{ transform: `scaleX(${pct / 100})` }} />
      </div>
      <p className="num mt-1 truncate text-xs text-muted">{detail}</p>
    </div>
  );
}

/** "You are here": one quiet strip wherever work gets planned. Renders nothing until the program has a start date. */
export function YouAreHere({ className }: { className?: string }) {
  const pos = useProgramPosition();
  if (!pos) return null;
  return (
    <section aria-label="You are here" className={clsx('flex flex-wrap items-center gap-x-6 gap-y-3 rounded-m border border-line px-3.5 py-2.5', className)}>
      <h2 className="shrink-0 text-sm text-muted">You are here</h2>
      {pos.epic ? (
        <Meter
          to={`/tracker/issue/${pos.epic.key}`}
          label={`Project ${pos.project} of ${pos.projects}: ${epicShort(pos.epic.summary)}`}
          pct={percent(pos.progress.done, pos.progress.total)}
          detail={`${fmtInt(pos.progress.done)} of ${fmtInt(pos.progress.total)} h done`}
        />
      ) : (
        <p className="min-w-0 flex-1 basis-60 text-sm text-ink">Every ticket in all {pos.projects} projects is done.</p>
      )}
      {pos.sprint ? (
        <Meter
          to="/tracker/board"
          label={`Sprint ${pos.sprintNo} of ${pos.sprints}`}
          pct={pos.sprintPct}
          detail={pos.daysLeft ? `${plural(pos.daysLeft, 'working day')} left` : 'Past its end date: complete it from the board'}
        />
      ) : pos.nextSprint ? (
        <p className="min-w-0 flex-1 basis-60 text-sm text-muted">
          No sprint is running.{' '}
          <Link to="/tracker/backlog" className="font-medium text-accent-strong hover:underline">
            Start {pos.nextSprint.name} from the backlog
          </Link>
        </p>
      ) : null}
    </section>
  );
}

/** Shared by both forms of "Your next ticket". */
function useNext() {
  const t = useTracker();
  const issue = useMemo(() => nextTicket(t.issues, t.me), [t.issues, t.me]);
  if (!issue) return { t, issue: null, epic: null, reporter: null, reporterLink: null };
  const epic = issue.epic_key ? t.byKey.get(issue.epic_key) ?? null : null;
  const reporter = t.person(issue.reporter);
  const reporterLink = issue.reporter && issue.reporter !== t.me && t.people.has(issue.reporter) ? teamPath(issue.reporter) : null;
  return { t, issue, epic, reporter, reporterLink };
}

const projectName = (epic: Issue | null, issue: Issue) => (epic ? `Project ${epic.project_no ?? issue.project_no ?? '?'}: ${epicShort(epic.summary)}` : 'No project');

/** The full card, for Start here. */
export function NextTicketCard({ className }: { className?: string }) {
  const { t, issue, epic, reporter, reporterLink } = useNext();
  const shell = clsx('rounded-l border border-line px-4 py-3.5', className);
  if (t.meta.isError && !t.meta.data) return null;
  if (t.meta.data && !t.configured)
    return (
      <section aria-labelledby="next-ticket" className={shell}>
        <h2 id="next-ticket" className="text-md font-semibold text-ink">
          Your next ticket
        </h2>
        <p className="mt-1.5 text-base text-muted">Your tickets get their due dates from your start date, so set that first.</p>
        <Link to="/tracker" className={buttonClasses('secondary', 'md', 'mt-3')}>
          Set your start date
        </Link>
      </section>
    );
  if (!t.issuesQ.data)
    return (
      <section aria-labelledby="next-ticket" className={shell} aria-busy>
        <h2 id="next-ticket" className="text-md font-semibold text-ink">
          Your next ticket
        </h2>
        <span className="mt-2 block h-16 rounded-s bg-active" />
      </section>
    );
  if (!issue)
    return (
      <section aria-labelledby="next-ticket" className={shell}>
        <h2 id="next-ticket" className="text-md font-semibold text-ink">
          Your next ticket
        </h2>
        <p className="mt-1.5 text-base text-muted">Nothing open: every ticket assigned to you is done. Time to polish your portfolio.</p>
      </section>
    );
  return (
    <section aria-labelledby="next-ticket" className={shell}>
      <h2 id="next-ticket" className="text-md font-semibold text-ink">
        Your next ticket
      </h2>
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <TypeIcon type={issue.type} />
        <IssueKey k={issue.key} />
        <DueBadge issue={issue} />
        <LevelChip issue={issue} />
      </div>
      <Link to={`/tracker/issue/${issue.key}`} className="mt-1.5 block text-md font-medium leading-6 text-ink [overflow-wrap:anywhere] hover:underline">
        {issue.summary}
      </Link>
      <dl className="mt-2.5 flex flex-col gap-1 text-sm">
        <div className="flex gap-2">
          <dt className="w-20 shrink-0 text-muted">Project</dt>
          <dd className="min-w-0 text-ink">{projectName(epic, issue)}</dd>
        </div>
        <div className="flex items-center gap-2">
          <dt className="w-20 shrink-0 text-muted">Asked by</dt>
          <dd className="flex min-w-0 items-center gap-1.5 text-ink">
            <Avatar person={reporter} size={20} me={issue.reporter === t.me} />
            {reporterLink ? (
              <Link to={reporterLink} className="truncate hover:underline">
                {reporter?.name}
              </Link>
            ) : (
              <span className="truncate">{reporter?.name ?? 'Nobody named'}</span>
            )}
          </dd>
        </div>
      </dl>
      <div className="mt-3 flex flex-wrap gap-2">
        <Link to={`/tracker/issue/${issue.key}`} className={buttonClasses('primary')}>
          Open {issue.key}
        </Link>
        <Link to="/tracker/my-work" className={buttonClasses('ghost')}>
          My work
        </Link>
      </div>
    </section>
  );
}

/** One line, for the Control Center. Stays out of the way when there is nothing to say. */
export function NextTicketLine({ className }: { className?: string }) {
  const { t, issue, epic, reporter } = useNext();
  if (t.meta.data && !t.configured)
    return (
      <p className={clsx('rounded-m border border-line px-3.5 py-2 text-base text-muted', className)}>
        No tickets yet: they need a start date.{' '}
        <Link to="/tracker" className="font-medium text-accent-strong hover:underline">
          Set it in the tracker
        </Link>
      </p>
    );
  if (!issue) return null;
  return (
    <Link
      to={`/tracker/issue/${issue.key}`}
      aria-label={`Your next ticket: ${issue.key} ${issue.summary}`}
      className={clsx('group flex flex-wrap items-center gap-x-3 gap-y-1 rounded-m border border-line px-3.5 py-2 transition-colors duration-150 hover:border-control hover:bg-hover/60', className)}
    >
      <span className="shrink-0 text-sm text-muted">Your next ticket</span>
      <span className="flex min-w-0 flex-1 basis-72 items-center gap-2">
        <IssueKey k={issue.key} />
        <span className="min-w-0 truncate text-base font-medium text-ink">{issue.summary}</span>
      </span>
      <span className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-muted">
        <DueBadge issue={issue} compact />
        <span>Project {epic?.project_no ?? issue.project_no ?? '?'}</span>
        {reporter && issue.reporter !== t.me ? <span>Asked by {reporter.name}</span> : null}
        <ArrowRight className="size-3.5 text-muted transition-transform duration-150 group-hover:translate-x-0.5 max-sm:hidden" aria-hidden />
      </span>
    </Link>
  );
}
