import { useMemo } from 'react';
import { useSearchParams } from 'react-router';
import { compareRank, useAllIssues, useMeta, useSprints } from '@/api/queries';
import type { Issue, Person, Sprint } from '@/api/types';
import { addDays, isWeekend, parseDay, today } from '@/lib/dates';

export const isWorkItem = (i: Issue) => i.type === 'Task' || i.type === 'Bug' || i.type === 'Spike';

/** Earliest due first, so overdue work leads; no due date goes last; ties keep their rank order. */
export function compareDue(a: Issue, b: Issue): number {
  const da = a.due_date?.slice(0, 10);
  const db = b.due_date?.slice(0, 10);
  if (da !== db) {
    if (!da) return 1;
    if (!db) return -1;
    return da < db ? -1 : 1;
  }
  return compareRank(a, b);
}

/** Where a card lands in a list kept in due-date order. */
export function insertByDue(list: string[], key: string, byKey: Map<string, Issue>): string[] {
  const issue = byKey.get(key);
  const out = [...list];
  const at = issue ? list.findIndex((k) => {
    const other = byKey.get(k);
    return !!other && compareDue(issue, other) < 0;
  }) : -1;
  out.splice(at < 0 ? out.length : at, 0, key);
  return out;
}

const LEVEL_LABEL = /^level-([1-5])$/i;

/** Difficulty comes from one `level-1` to `level-5` label. Null when a ticket has none yet. */
export function levelOf(issue: Pick<Issue, 'labels'>): number | null {
  for (const l of issue.labels) {
    const m = LEVEL_LABEL.exec(l);
    if (m) return Number(m[1]);
  }
  return null;
}

/** The level shows as a chip, so label lists leave it out. */
export const plainLabels = (labels: string[]) => labels.filter((l) => !LEVEL_LABEL.test(l));

export function progressOf(items: Issue[]) {
  const work = items.filter(isWorkItem);
  const total = work.reduce((t, i) => t + (i.estimate_hours ?? 0), 0);
  const done = work.filter((i) => i.status === 'Done').reduce((t, i) => t + (i.estimate_hours ?? 0), 0);
  return { done, total };
}

export function workingDaysLeft(end: string | null | undefined) {
  if (!end) return null;
  let n = 0;
  for (let d = today(); d <= parseDay(end); d = addDays(d, 1)) if (!isWeekend(d)) n += 1;
  return n;
}

/** Reporter ids in tickets are the Team page ids with a dot for the dash: luis.moreno is /team/luis-moreno. */
export const teamPath = (personKey: string) => `/team/${personKey.replace(/\./g, '-')}`;

const SOURCE_PAGES: Array<[label: string, anchor: string, name: string]> = [
  ['wms', 'wms', 'WMS'],
  ['ax2012', 'ax', 'AX 2012'],
  ['salesforce', 'salesforce', 'Salesforce'],
  ['carrier-api', 'carrier', 'carrier API'],
  ['iot', 'iot', 'IoT sensors'],
  ['edi', 'edi', 'EDI'],
  ['files', 'files', 'business files'],
  ['public-api', 'public', 'public APIs'],
];

/** The handbook page most likely to unstick someone on this ticket, when there is an obvious one. */
export function handbookFor(issue: Issue): { page: string; label: string } | null {
  const has = (...labels: string[]) => labels.some((l) => issue.labels.includes(l));
  const inComponent = (c: string) => issue.components.includes(c);
  if (issue.type === 'Epic') return { page: 'project-brief.html#projects', label: 'The eight projects' };
  if (has('sign-off', 'uat')) return { page: 'team.html#reviewer', label: 'Dashboard sign-off' };
  if (has('dashboard', 'dashboards') || inComponent('Dashboards'))
    return { page: issue.project_no ? `dashboards.html#project-${issue.project_no}` : 'dashboards.html', label: issue.project_no ? `Dashboard specs, Project ${issue.project_no}` : 'Dashboard specs' };
  if (has('brand', 'brand-kit')) return { page: 'brand.html', label: 'Brand kit' };
  for (const [label, anchor, name] of SOURCE_PAGES) if (has(label)) return { page: `source-systems.html#${anchor}`, label: `Source systems: ${name}` };
  if (has('cloud', 'free-tier')) return { page: 'getting-started.html#cloud', label: 'Your cloud platform' };
  if (has('setup')) return { page: 'getting-started.html#setup-file', label: 'Run the setup file' };
  if (has('git') || inComponent('CI/CD') || inComponent('Platform') || inComponent('Orchestration')) return { page: 'project-brief.html#engineering', label: 'Git and engineering standards' };
  if (inComponent('Security') || inComponent('Governance')) return { page: 'project-brief.html#rules', label: 'Rules of engagement' };
  if (inComponent('Data Quality')) return { page: 'answer-key.html#scoring', label: 'Score your data-quality findings' };
  if (inComponent('Ingestion') || inComponent('CDC') || inComponent('Streaming')) return { page: 'source-systems.html', label: 'Source systems' };
  if (inComponent('Modeling')) return { page: 'source-systems.html#keys', label: 'How the systems join' };
  return null;
}

/** Everything most tracker screens need, derived once from meta + the full issue list. */
export function useTracker() {
  const meta = useMeta();
  const configured = !!meta.data?.configured;
  const issuesQ = useAllIssues(configured);
  const sprintsQ = useSprints();
  const issues = issuesQ.data;
  const sprints: Sprint[] = useMemo(() => sprintsQ.data ?? meta.data?.sprints ?? [], [sprintsQ.data, meta.data?.sprints]);
  const me = meta.data?.me ?? 'me';

  const derived = useMemo(() => {
    const byKey = new Map<string, Issue>();
    for (const i of issues ?? []) byKey.set(i.key, i);
    const people = new Map<string, Person>();
    for (const p of meta.data?.people ?? []) people.set(p.key, p);
    const epics = (issues ?? []).filter((i) => i.type === 'Epic').sort((a, b) => Number(a.key.split('-')[1]) - Number(b.key.split('-')[1]));
    const children = new Map<string, Issue[]>();
    for (const i of issues ?? []) {
      const parent = i.parent_key ?? (i.type === 'Story' ? i.epic_key : null);
      if (!parent) continue;
      const list = children.get(parent) ?? [];
      list.push(i);
      children.set(parent, list);
    }
    for (const list of children.values()) list.sort(compareRank);
    return { byKey, people, epics, children };
  }, [issues, meta.data?.people]);

  const active = sprints.find((s) => s.state === 'active') ?? null;
  const nextFuture = sprints.filter((s) => s.state === 'future').sort((a, b) => a.id - b.id)[0] ?? null;

  return {
    meta,
    issuesQ,
    sprintsQ,
    configured,
    issues: issues ?? [],
    sprints,
    active,
    nextFuture,
    me,
    ...derived,
    person: (k: string | null | undefined) => (k ? derived.people.get(k) ?? { key: k, name: k } : null),
  };
}

/** Catalog epics read "Name: tagline"; tight spaces show the name only. */
export const epicShort = (summary: string) => summary.split(/:\s/)[0];

/** Planned capacity of one engineer: 10 working days × 8 h. */
export const SPRINT_CAPACITY_HOURS = 80;

export function sprintHours(issues: Issue[]) {
  return issues.filter(isWorkItem).reduce((t, i) => t + (i.estimate_hours ?? 0), 0);
}

/** The earliest-due open ticket that is yours. A story sharing a date with its tasks waits behind them, since the tasks are the work. */
export function nextTicket(issues: Issue[], me: string): Issue | null {
  const open = issues.filter((i) => i.type !== 'Epic' && i.status !== 'Done' && (i.assignee === me || i.assignee == null));
  return (
    [...open].sort((a, b) => {
      const da = a.due_date?.slice(0, 10) ?? '9999';
      const db = b.due_date?.slice(0, 10) ?? '9999';
      if (da !== db) return da < db ? -1 : 1;
      if (isWorkItem(a) !== isWorkItem(b)) return isWorkItem(a) ? -1 : 1;
      return compareRank(a, b);
    })[0] ?? null
  );
}

/** Mirrors the backend: fix/ for bugs, feat/ for everything else. */
export function branchName(issue: Pick<Issue, 'key' | 'summary' | 'type'>) {
  const slug = issue.summary
    .normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .replace(/["'’]/g, '')
    .replace(/[^a-z0-9]+/g, '-')
    .split('-')
    .filter(Boolean)
    .slice(0, 6)
    .join('-')
    .slice(0, 48)
    .replace(/-+$/, '');
  return `${issue.type === 'Bug' ? 'fix' : 'feat'}/${issue.key}-${slug}`;
}

/** The issue panel is driven by ?issue=KEY so it survives reloads and can be linked. */
export function useIssueParam() {
  const [params, setParams] = useSearchParams();
  const key = params.get('issue');
  const open = (k: string, opts: { edit?: boolean } = {}) =>
    setParams(
      (p) => {
        p.set('issue', k);
        if (opts.edit) p.set('edit', '1');
        else p.delete('edit');
        return p;
      },
      { replace: false },
    );
  const close = () =>
    setParams(
      (p) => {
        p.delete('issue');
        p.delete('edit');
        return p;
      },
      { replace: false },
    );
  return { key, edit: params.get('edit') === '1', open, close };
}
