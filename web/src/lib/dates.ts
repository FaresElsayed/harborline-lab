import { addDays, differenceInCalendarDays, format, isValid, parseISO, startOfWeek, endOfWeek } from 'date-fns';

/** 'YYYY-MM-DD' strings are calendar dates: parse them as local midnight, never as UTC. */
export function parseDay(s: string): Date {
  const [y, m, d] = s.slice(0, 10).split('-').map(Number);
  return new Date(y, (m ?? 1) - 1, d ?? 1);
}

export function toDay(d: Date): string {
  return format(d, 'yyyy-MM-dd');
}

export function today(): Date {
  const n = new Date();
  return new Date(n.getFullYear(), n.getMonth(), n.getDate());
}

export const isWeekend = (d: Date) => d.getDay() === 0 || d.getDay() === 6;

/** Working day n (1-based) counted from `start`, skipping Saturdays and Sundays (DESIGN_SPEC §6). */
export function workingDay(start: Date, n: number): Date {
  let d = new Date(start);
  while (isWeekend(d)) d = addDays(d, 1);
  let left = n - 1;
  while (left > 0) {
    d = addDays(d, 1);
    if (!isWeekend(d)) left -= 1;
  }
  return d;
}

/** The Monday on or after `d` (the default program start). */
export function nextMonday(d: Date = today()): Date {
  const dow = d.getDay();
  return addDays(d, dow === 1 ? 0 : (8 - dow) % 7 || 7);
}

export function weekBounds(d: Date = today()) {
  return { start: startOfWeek(d, { weekStartsOn: 1 }), end: endOfWeek(d, { weekStartsOn: 1 }) };
}

export function parseAny(s: string | null | undefined): Date | null {
  if (!s) return null;
  const d = s.length <= 10 ? parseDay(s) : parseISO(s);
  return isValid(d) ? d : null;
}

export function fmtDay(s: string | null | undefined, withYear = false): string {
  const d = parseAny(s);
  if (!d) return '—';
  return format(d, withYear || d.getFullYear() !== new Date().getFullYear() ? 'MMM d, yyyy' : 'MMM d');
}

export function fmtDateTime(s: string | null | undefined): string {
  const d = parseAny(s);
  return d ? format(d, 'MMM d, HH:mm') : '—';
}

/** Sim timestamps are UTC; the control center shows them in UTC so they match what he sees in Postgres. */
export function fmtUtc(s: string | null | undefined, pattern = 'yyyy-MM-dd HH:mm'): string {
  if (!s) return '—';
  const d = parseISO(s);
  if (!isValid(d)) return '—';
  const utc = new Date(d.getTime() + d.getTimezoneOffset() * 60_000);
  return format(utc, pattern);
}

export function fmtRelative(s: string | null | undefined, now = Date.now()): string {
  const d = parseAny(s);
  if (!d) return '—';
  const sec = Math.round((now - d.getTime()) / 1000);
  if (sec < 45) return 'just now';
  const min = Math.round(sec / 60);
  if (min < 60) return `${min} min ago`;
  const h = Math.round(min / 60);
  if (h < 24) return `${h} h ago`;
  const days = Math.round(h / 24);
  if (days < 7) return days === 1 ? 'yesterday' : `${days} days ago`;
  return fmtDay(s);
}

export type DueState = 'none' | 'done' | 'overdue' | 'today' | 'soon' | 'later';

export function dueState(due: string | null | undefined, done: boolean, ref: Date = today()): DueState {
  if (!due) return 'none';
  if (done) return 'done';
  const diff = differenceInCalendarDays(parseDay(due), ref);
  if (diff < 0) return 'overdue';
  if (diff === 0) return 'today';
  if (diff <= 3) return 'soon';
  return 'later';
}

export function dueLabel(due: string | null | undefined, done: boolean, ref: Date = today()): string {
  if (!due) return 'No due date';
  const diff = differenceInCalendarDays(parseDay(due), ref);
  if (done) return fmtDay(due);
  if (diff < 0) return diff === -1 ? 'Overdue 1 day' : `Overdue ${-diff} days`;
  if (diff === 0) return 'Due today';
  if (diff === 1) return 'Due tomorrow';
  return fmtDay(due);
}

/** The working week people mean by "this week": on a Saturday or Sunday that is the coming week. */
export function workWeek(ref: Date = today()) {
  const base = isWeekend(ref) ? nextMonday(ref) : ref;
  return { ...weekBounds(base), upcoming: isWeekend(ref) };
}

export function inThisWeek(due: string | null | undefined, ref: Date = today()): boolean {
  if (!due) return false;
  const { start, end } = workWeek(ref);
  const d = parseDay(due);
  return d >= start && d <= end;
}

export { addDays, differenceInCalendarDays, format };
