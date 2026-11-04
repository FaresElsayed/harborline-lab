import { useMemo } from 'react';
import { useSearchParams } from 'react-router';
import { compareRank, useAllIssues, useMeta, useSprints } from '@/api/queries';
import type { Issue, Person, Sprint } from '@/api/types';

export const isWorkItem = (i: Issue) => i.type === 'Task' || i.type === 'Bug' || i.type === 'Spike';

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
