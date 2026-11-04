import type { ReactNode } from 'react';
import clsx from 'clsx';
import { CalendarDays } from 'lucide-react';
import type { Issue, IssueType, Person, Priority, StatusName } from '@/api/types';
import { Tooltip } from '@/components/ui/overlay';
import { dueLabel, dueState } from '@/lib/dates';

// ---------------------------------------------------------------- issue type

const TYPE_FILL: Record<IssueType, string> = {
  Epic: 'var(--hl-viz-7)',
  Story: 'var(--hl-viz-6)',
  Task: 'var(--hl-viz-1)',
  Bug: 'var(--hl-bad)',
  Spike: 'var(--hl-viz-3)',
};

const TYPE_GLYPH: Record<IssueType, ReactNode> = {
  Epic: <path d="M8.9 2.6 4.6 8.7h3l-.6 4.7 4.4-6.2H8.3z" fill="#fff" />,
  Story: <path d="M5 3.5h6v9l-3-2.1-3 2.1z" fill="#fff" />,
  Task: <path d="m4.6 8.2 2.2 2.2 4.6-4.8" fill="none" stroke="#fff" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" />,
  Bug: <circle cx="8" cy="8" r="3.1" fill="#fff" />,
  Spike: <path d="M6.4 3.4h3.2M7 3.6v3.2L4.6 11.4c-.3.6.1 1.2.8 1.2h5.2c.7 0 1.1-.6.8-1.2L9 6.8V3.6" fill="none" stroke="#fff" strokeWidth="1.5" strokeLinejoin="round" />,
};

export function TypeIcon({ type, className, label = true }: { type: IssueType; className?: string; label?: boolean }) {
  return (
    <svg viewBox="0 0 16 16" className={clsx('size-4 shrink-0', className)} role={label ? 'img' : undefined} aria-label={label ? type : undefined} aria-hidden={label ? undefined : true}>
      <rect width="16" height="16" rx="3.5" fill={TYPE_FILL[type]} />
      {TYPE_GLYPH[type]}
    </svg>
  );
}

// ---------------------------------------------------------------- priority

const PRIORITY: Record<Priority, { color: string; d: string }> = {
  Highest: { color: 'var(--hl-bad)', d: 'm3.5 8.5 4.5-4 4.5 4M3.5 12.5l4.5-4 4.5 4' },
  High: { color: 'var(--signal-text)', d: 'm3.5 10.5 4.5-4.5 4.5 4.5' },
  Medium: { color: 'var(--hl-warn)', d: 'M3.5 6.2h9M3.5 9.8h9' },
  Low: { color: 'var(--hl-info)', d: 'm3.5 5.5 4.5 4.5 4.5-4.5' },
  Lowest: { color: 'var(--hl-info)', d: 'm3.5 3.5 4.5 4 4.5-4M3.5 7.5l4.5 4 4.5-4' },
};

export function PriorityIcon({ priority, className }: { priority: Priority; className?: string }) {
  const p = PRIORITY[priority] ?? PRIORITY.Medium;
  return (
    <svg viewBox="0 0 16 16" className={clsx('size-4 shrink-0', className)} role="img" aria-label={`${priority} priority`}>
      <path d={p.d} fill="none" stroke={p.color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

// ---------------------------------------------------------------- status

export const STATUS_TONE: Record<StatusName, string> = {
  'To Do': 'bg-active text-muted',
  'In Progress': 'bg-info-bg text-info',
  'In Review': 'bg-accent-tint text-accent-strong',
  Done: 'bg-good-bg text-good',
};

export function StatusLozenge({ status, className }: { status: StatusName; className?: string }) {
  return (
    <span className={clsx('inline-flex h-5 flex-none items-center whitespace-nowrap rounded-s px-1.5 text-xs font-semibold', STATUS_TONE[status], className)}>
      {status}
    </span>
  );
}

// ---------------------------------------------------------------- people

const HUES = [210, 150, 30, 330, 90, 270, 0, 190, 60, 300, 120, 240];

function hueFor(key: string) {
  let h = 0;
  for (const c of key) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return HUES[h % HUES.length];
}

export function initials(name: string) {
  // Names ("Dr. Ayesha Khan") and raw people keys ("contractor.jo") both give two letters.
  const parts = name.replace(/^Dr\.\s+/, '').split(/[\s._-]+/).filter(Boolean);
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

export function Avatar({ person, size = 24, me }: { person: Person | null | undefined; size?: 20 | 24 | 28 | 32; me?: boolean }) {
  if (!person) {
    return (
      <span
        className="inline-grid shrink-0 place-items-center rounded-full border border-dashed border-control text-faint"
        style={{ width: size, height: size }}
        role="img"
        aria-label="Unassigned"
      >
        <svg viewBox="0 0 16 16" className="size-3" aria-hidden>
          <circle cx="8" cy="5.5" r="2.6" fill="none" stroke="currentColor" strokeWidth="1.4" />
          <path d="M3.2 13.2c.7-2.4 2.6-3.6 4.8-3.6s4.1 1.2 4.8 3.6" fill="none" stroke="currentColor" strokeWidth="1.4" />
        </svg>
      </span>
    );
  }
  const label = me ? 'ME' : initials(person.name);
  return (
    <span
      className="avatar inline-grid shrink-0 select-none place-items-center rounded-full font-semibold tracking-[0.01em]"
      style={{ width: size, height: size, fontSize: size <= 20 ? 9 : size <= 24 ? 10 : 11, ['--h' as string]: me ? 192 : hueFor(person.key) }}
      role="img"
      aria-label={person.name}
      title={person.name}
    >
      {label}
    </span>
  );
}

// ---------------------------------------------------------------- due date

export function DueBadge({ issue, compact }: { issue: Pick<Issue, 'due_date' | 'status'>; compact?: boolean }) {
  const st = dueState(issue.due_date, issue.status === 'Done');
  if (st === 'none') return null;
  const text = dueLabel(issue.due_date, issue.status === 'Done');
  const tone =
    st === 'overdue'
      ? 'bg-bad-bg text-bad font-semibold'
      : st === 'today' || st === 'soon'
        ? 'bg-warn-bg text-warn font-semibold'
        : 'text-muted';
  return (
    <Tooltip content={`Due ${issue.due_date}`}>
      <span className={clsx('num inline-flex h-5 flex-none items-center gap-1 whitespace-nowrap rounded-s px-1.5 text-xs', tone)}>
        {!compact || st === 'later' || st === 'done' ? <CalendarDays className="size-3" aria-hidden /> : null}
        {text}
      </span>
    </Tooltip>
  );
}

export function Estimate({ issue }: { issue: Pick<Issue, 'story_points' | 'estimate_hours'> }) {
  if (issue.story_points != null)
    return (
      <span className="num inline-flex h-5 min-w-5 flex-none items-center justify-center rounded-full bg-active px-1.5 text-xs font-semibold text-muted" title={`${issue.story_points} story points`}>
        {issue.story_points}
      </span>
    );
  if (issue.estimate_hours != null)
    return (
      <span className="num inline-flex h-5 flex-none items-center rounded-full bg-active px-1.5 text-xs font-semibold text-muted" title={`Original estimate ${issue.estimate_hours} hours`}>
        {issue.estimate_hours}h
      </span>
    );
  return null;
}

export function IssueKey({ k, className, done }: { k: string; className?: string; done?: boolean }) {
  return <span className={clsx('shrink-0 whitespace-nowrap font-mono text-[12px] font-medium text-muted', done && 'line-through decoration-faint', className)}>{k}</span>;
}
