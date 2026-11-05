import { useMemo, useState } from 'react';
import clsx from 'clsx';
import { ChevronRight } from 'lucide-react';
import type { Issue } from '@/api/types';
import { ErrorNote, Skeleton } from '@/components/ui/misc';
import { Tooltip } from '@/components/ui/overlay';
import { addDays, differenceInCalendarDays, dueState, fmtDay, format, parseDay, today, workingDay } from '@/lib/dates';
import { fmtInt, plural } from '@/lib/format';
import { IssueKey, StatusLozenge, TypeIcon } from './atoms';
import { epicShort, progressOf, useIssueParam, useTracker } from './data';
import { YouAreHere } from './here';

const WEEKS = 32;
const DEPTS: Array<[RegExp, string]> = [
  [/exec|ceo/i, 'var(--hl-dept-exec)'],
  [/quality|food/i, 'var(--hl-dept-qa)'],
  [/sales/i, 'var(--hl-dept-sales)'],
  [/client success/i, 'var(--hl-dept-cs)'],
  [/transport|freight/i, 'var(--hl-dept-transport)'],
  [/financ/i, 'var(--hl-dept-fin)'],
  [/operation/i, 'var(--hl-dept-ops)'],
  [/\bIT\b|technology/i, 'var(--hl-dept-it)'],
];
const deptColor = (dept: string) => DEPTS.find(([re]) => re.test(dept))?.[1] ?? 'var(--faint)';

interface Bar {
  issue: Issue;
  start: Date;
  end: Date;
  done: number;
  total: number;
}

export default function TimelinePage() {
  const t = useTracker();
  const { open } = useIssueParam();
  const [expanded, setExpanded] = useState<Set<string> | null>(null);
  const startDate = t.meta.data?.start_date ? parseDay(t.meta.data.start_date) : null;
  const programStart = startDate ? addDays(startDate, -((startDate.getDay() + 6) % 7)) : null; // Monday of week 1
  const totalDays = WEEKS * 7;

  const rows = useMemo(() => {
    if (!startDate) return [];
    return t.epics.map((e, idx) => {
      const project = e.project_no ?? idx + 1;
      const kids = t.issues.filter((i) => i.epic_key === e.key);
      const stories = kids.filter((i) => i.type === 'Story').sort((a, b) => ((a.due_date ?? '') < (b.due_date ?? '') ? -1 : 1));
      const epicBar: Bar = { issue: e, start: workingDay(startDate, (project - 1) * 20 + 1), end: e.due_date ? parseDay(e.due_date) : workingDay(startDate, project * 20), ...progressOf(kids) };
      let prevEnd = epicBar.start;
      const storyBars: Bar[] = stories.map((s) => {
        const tasks = kids.filter((k) => k.parent_key === s.key);
        const end = s.due_date ? parseDay(s.due_date) : epicBar.end;
        const firstDue = tasks.map((k) => k.due_date).filter(Boolean).sort()[0];
        const start = firstDue && parseDay(firstDue) < prevEnd ? parseDay(firstDue) : prevEnd;
        prevEnd = addDays(end, 1);
        return { issue: s, start, end, ...progressOf(tasks) };
      });
      return { epic: epicBar, stories: storyBars };
    });
  }, [t.epics, t.issues, startDate]);

  if (t.issuesQ.isError) return <div className="p-6"><ErrorNote error={t.issuesQ.error} onRetry={() => void t.issuesQ.refetch()} what="the timeline" /></div>;

  const pos = (d: Date) => (programStart ? (differenceInCalendarDays(d, programStart) / totalDays) * 100 : 0);
  const now = today();
  const todayPct = pos(now);
  const months: Array<{ label: string; left: number }> = [];
  if (programStart) {
    const d = new Date(programStart.getFullYear(), programStart.getMonth() + 1, 1);
    for (; differenceInCalendarDays(d, programStart) < totalDays; d.setMonth(d.getMonth() + 1)) months.push({ label: format(d, d.getMonth() === 0 ? 'MMM yyyy' : 'MMM'), left: pos(d) });
  }
  const openSet = expanded ?? new Set(rows.filter((r) => now >= r.epic.start && now <= r.epic.end).map((r) => r.epic.issue.key));
  const behind = rows.filter((r) => r.epic.issue.status !== 'Done' && dueState(r.epic.issue.due_date, false) === 'overdue');
  const current = rows.find((r) => now >= r.epic.start && now <= r.epic.end);

  const Track = ({ bar, story }: { bar: Bar; story?: boolean }) => {
    const left = pos(bar.start);
    const width = Math.max(0.8, pos(addDays(bar.end, 1)) - left);
    const pct = bar.total ? bar.done / bar.total : bar.issue.status === 'Done' ? 1 : 0;
    const late = dueState(bar.issue.due_date, bar.issue.status === 'Done') === 'overdue';
    return (
      <Tooltip
        content={
          <span className="num">
            {bar.issue.key} · {fmtDay(bar.issue.due_date)} due · {fmtInt(bar.done)} of {fmtInt(bar.total)} h done ({Math.round(pct * 100)}%)
          </span>
        }
      >
        <button
          type="button"
          onClick={() => open(bar.issue.key)}
          aria-label={`${bar.issue.summary}: ${fmtDay(bar.start.toISOString())} to ${fmtDay(bar.issue.due_date)}, ${Math.round(pct * 100)}% done${late ? ', overdue' : ''}`}
          className={clsx(
            'press absolute top-1/2 -translate-y-1/2 overflow-hidden rounded-[4px] outline-none transition-[filter] hover:brightness-95 focus-visible:ring-2 focus-visible:ring-focus dark:hover:brightness-125',
            story ? 'h-3.5 bg-accent/15' : 'h-6 bg-accent/20',
            late && 'ring-1 ring-bad',
          )}
          style={{ left: `${left}%`, width: `${width}%` }}
        >
          <span className="absolute inset-y-0 left-0 bg-accent" style={{ width: `${pct * 100}%` }} />
          {!story && width > 9 ? (
            <span className={clsx('num relative z-10 px-2 text-xs font-semibold leading-6', pct > 0.35 ? 'text-on-accent' : 'text-accent-strong')}>{Math.round(pct * 100)}%</span>
          ) : null}
        </button>
      </Tooltip>
    );
  };

  return (
    <div className="h-full overflow-y-auto">
      <div className="px-6 pb-12 pt-5">
        <div className="mb-1 text-sm text-muted">Harborline Tracker / Timeline</div>
        <h1 className="figure text-[1.75rem] font-semibold leading-8 text-ink">Timeline</h1>
        <p className="num mt-1 text-md text-muted">
          {programStart ? (
            <>
              32 weeks from {fmtDay(t.meta.data?.start_date, true)}.{' '}
              {current ? (
                <>
                  Now in <span className="font-medium text-ink">{epicShort(current.epic.issue.summary)}</span>, {Math.round((current.epic.total ? current.epic.done / current.epic.total : 0) * 100)}% of its hours done.
                </>
              ) : now < (rows[0]?.epic.start ?? now) ? (
                'The program has not started yet.'
              ) : null}{' '}
              {behind.length ? <span className="font-medium text-bad">{plural(behind.length, 'epic')} past due.</span> : null}
            </>
          ) : (
            'Set the start date to place the epics on the calendar.'
          )}
        </p>
        <YouAreHere className="mt-4" />

        {!t.issuesQ.data ? (
          <Skeleton className="mt-6 h-96 rounded-l" />
        ) : (
          <div className="mt-6 rounded-l border border-line">
            <div className="grid grid-cols-[280px_minmax(0,1fr)] border-b border-line">
              <div className="flex h-12 items-end px-4 pb-2 text-sm text-muted">Epic</div>
              <div className="relative h-12 overflow-hidden">
                {months.map((m) => (
                  <span key={m.label + m.left} className="absolute top-2 text-sm font-medium text-ink" style={{ left: `calc(${m.left}% + 6px)` }}>
                    {m.label}
                  </span>
                ))}
                {Array.from({ length: WEEKS }, (_, w) => (
                  <span key={w} className="num absolute bottom-1.5 -translate-x-1/2 text-[11px] text-muted" style={{ left: `${((w + 0.5) / WEEKS) * 100}%` }}>
                    {(w + 1) % 4 === 1 ? `W${w + 1}` : ''}
                  </span>
                ))}
                {todayPct >= 0 && todayPct <= 100 ? (
                  <span className="num absolute bottom-0 z-10 -translate-x-1/2 rounded-t-s bg-viz-1 px-1.5 text-[11px] font-semibold leading-4 text-white" style={{ left: `${todayPct}%` }}>
                    Today
                  </span>
                ) : null}
              </div>
            </div>
            <div className="relative">
              {/* week grid and today marker span every row */}
              <div className="pointer-events-none absolute inset-y-0 left-[280px] right-0" aria-hidden>
                {Array.from({ length: WEEKS / 4 + 1 }, (_, i) => (
                  <span key={i} className="absolute inset-y-0 w-px bg-rule" style={{ left: `${((i * 4) / WEEKS) * 100}%` }} />
                ))}
                {todayPct >= 0 && todayPct <= 100 ? <span className="absolute inset-y-0 w-px bg-viz-1" style={{ left: `${todayPct}%` }} /> : null}
              </div>
              <ul>
                {rows.map(({ epic, stories }) => {
                  const isOpen = openSet.has(epic.issue.key);
                  const dept = epic.issue.department ?? '';
                  return (
                    <li key={epic.issue.key} className="border-b border-rule last:border-0">
                      <div className="grid grid-cols-[280px_minmax(0,1fr)]">
                        <div className="flex h-12 min-w-0 items-center gap-2 pl-2 pr-3">
                          <button
                            type="button"
                            aria-expanded={isOpen}
                            aria-label={`${isOpen ? 'Collapse' : 'Expand'} ${epic.issue.summary}`}
                            onClick={() => {
                              const n = new Set(openSet);
                              if (n.has(epic.issue.key)) n.delete(epic.issue.key);
                              else n.add(epic.issue.key);
                              setExpanded(n);
                            }}
                            className="press grid size-7 shrink-0 place-items-center rounded-s text-muted hover:bg-hover hover:text-ink"
                          >
                            <ChevronRight className={clsx('size-4 transition-transform duration-200 ease-out', isOpen && 'rotate-90')} aria-hidden />
                          </button>
                          <TypeIcon type="Epic" />
                          <button type="button" onClick={() => open(epic.issue.key)} className="min-w-0 text-left">
                            <span className="block truncate text-base font-medium text-ink hover:underline" title={epic.issue.summary}>
                              {epicShort(epic.issue.summary)}
                            </span>
                            <span className="flex min-w-0 items-center gap-1.5 whitespace-nowrap text-xs text-muted">
                              <span className="size-2 shrink-0 rounded-full" style={{ background: deptColor(dept) }} aria-hidden />
                              <span className="truncate">
                                {dept || 'Project'} · {format(epic.start, 'MMM d')} – {epic.issue.due_date ? format(parseDay(epic.issue.due_date), 'MMM d') : '—'}
                              </span>
                            </span>
                          </button>
                        </div>
                        <div className="relative h-12">
                          <Track bar={epic} />
                        </div>
                      </div>
                      {isOpen
                        ? stories.map((s) => (
                            <div key={s.issue.key} className="grid grid-cols-[280px_minmax(0,1fr)] border-t border-rule bg-raised/50">
                              <button type="button" onClick={() => open(s.issue.key)} className="flex h-10 min-w-0 items-center gap-2 pl-11 pr-3 text-left hover:bg-hover">
                                <TypeIcon type="Story" className="size-3.5" />
                                <IssueKey k={s.issue.key} />
                                <span className="min-w-0 flex-1 truncate text-sm text-ink">{s.issue.summary}</span>
                                {s.issue.status !== 'To Do' ? <StatusLozenge status={s.issue.status} /> : null}
                              </button>
                              <div className="relative h-10">
                                <Track bar={s} story />
                              </div>
                            </div>
                          ))
                        : null}
                    </li>
                  );
                })}
              </ul>
            </div>
          </div>
        )}
        <div className="mt-3 flex flex-wrap items-center gap-5 text-sm text-muted">
          <span className="flex items-center gap-1.5">
            <span className="h-2.5 w-6 overflow-hidden rounded-[3px] bg-accent/20">
              <span className="block h-full w-1/2 bg-accent" />
            </span>
            Filled part: hours done
          </span>
          <span className="flex items-center gap-1.5">
            <span className="h-2.5 w-6 rounded-[3px] bg-accent/20 ring-1 ring-bad" /> Past due
          </span>
          <span className="flex items-center gap-1.5">
            <span className="h-3 w-px bg-viz-1" /> Today
          </span>
        </div>
      </div>
    </div>
  );
}
