import { useMutation, useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query';
import { sim, team, tracker, uat, type IssueQuery } from './client';
import type { DqSettings, Issue, IssuePatch, NewIssue, Preset, Sprint, TrackerMeta } from './types';

export const keys = {
  status: ['sim', 'status'] as const,
  runs: ['sim', 'runs'] as const,
  scenarios: ['sim', 'scenarios'] as const,
  connections: ['sim', 'connections'] as const,
  extractOptions: ['uat', 'extract', 'options'] as const,
  meta: ['tracker', 'meta'] as const,
  allIssues: ['tracker', 'issues', 'all'] as const,
  search: (q: IssueQuery) => ['tracker', 'issues', 'q', q] as const,
  issue: (key: string) => ['tracker', 'issue', key] as const,
  history: (key: string) => ['tracker', 'history', key] as const,
  sprints: ['tracker', 'sprints'] as const,
  burndown: (sprint: number) => ['tracker', 'reports', 'burndown', sprint] as const,
  velocity: ['tracker', 'reports', 'velocity'] as const,
  cfd: ['tracker', 'reports', 'cfd'] as const,
};

// ---------------------------------------------------------------- simulator

export const useStatus = () =>
  useQuery({ queryKey: keys.status, queryFn: sim.status, refetchInterval: 5000, refetchIntervalInBackground: false });

export const useRuns = (limit = 100) => useQuery({ queryKey: keys.runs, queryFn: () => sim.runs(limit) });
export const useScenarios = () => useQuery({ queryKey: keys.scenarios, queryFn: sim.scenarios, staleTime: Infinity });
export const useTeamLinks = () => useQuery({ queryKey: ['team-links'], queryFn: team.links, staleTime: 60_000 });
export const useConnections = () => useQuery({ queryKey: keys.connections, queryFn: sim.connections, staleTime: 60_000 });

function refreshSim(qc: QueryClient) {
  void qc.invalidateQueries({ queryKey: keys.status });
  void qc.invalidateQueries({ queryKey: keys.runs });
}

export function useBatch() {
  const qc = useQueryClient();
  return useMutation({ mutationFn: (hours: number) => sim.batch(hours), onSettled: () => refreshSim(qc) });
}

export function useBuild() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { preset: Preset; seed: number }) => sim.build(v.preset, v.seed),
    onSettled: () => refreshSim(qc),
  });
}

export function useCancelBuild() {
  const qc = useQueryClient();
  return useMutation({ mutationFn: () => sim.cancelBuild(), onSettled: () => refreshSim(qc) });
}

export function useStream() {
  const qc = useQueryClient();
  const start = useMutation({
    mutationFn: (v: { interval_s: number; speed: number }) => sim.streamStart(v.interval_s, v.speed),
    onSuccess: (s) => s && qc.setQueryData(keys.status, s),
    onSettled: () => refreshSim(qc),
  });
  const stop = useMutation({
    mutationFn: () => sim.streamStop(),
    onSuccess: (s) => s && qc.setQueryData(keys.status, s),
    onSettled: () => refreshSim(qc),
  });
  return { start, stop };
}

export function useSetDq() {
  const qc = useQueryClient();
  return useMutation({ mutationFn: (dq: DqSettings) => sim.setDq(dq), onSettled: () => refreshSim(qc) });
}

/** Polls faster while an extract is being built, so the page can show which file it is writing. */
export const useExtractOptions = (building = false) =>
  useQuery({ queryKey: keys.extractOptions, queryFn: uat.options, refetchInterval: building ? 900 : 5000, refetchIntervalInBackground: false });

export function useBuildExtract() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { project: string; periods?: string[] }) => uat.build(v.project, v.periods),
    onSettled: () => void qc.invalidateQueries({ queryKey: keys.extractOptions }),
  });
}

export function useRunScenario() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { id: string; params: Record<string, unknown> }) => sim.runScenario(v.id, v.params),
    onSettled: () => refreshSim(qc),
  });
}

// ---------------------------------------------------------------- tracker

export const useMeta = () => useQuery({ queryKey: keys.meta, queryFn: tracker.meta, staleTime: 30_000 });
export const useAllIssues = (enabled = true) =>
  useQuery({ queryKey: keys.allIssues, queryFn: () => tracker.issues(), enabled, staleTime: 15_000 });
export const useSearch = (q: IssueQuery, enabled = true) =>
  useQuery({ queryKey: keys.search(q), queryFn: () => tracker.issues(q), enabled, placeholderData: (prev) => prev });
export const useSprints = () => useQuery({ queryKey: keys.sprints, queryFn: tracker.sprints, staleTime: 15_000 });
export const useHistory = (key: string) => useQuery({ queryKey: keys.history(key), queryFn: () => tracker.history(key) });
export const useBurndown = (sprint: number | null) =>
  useQuery({ queryKey: keys.burndown(sprint ?? -1), queryFn: () => tracker.burndown(sprint!), enabled: sprint != null });
export const useVelocity = () => useQuery({ queryKey: keys.velocity, queryFn: tracker.velocity });
export const useCfd = () => useQuery({ queryKey: keys.cfd, queryFn: tracker.cfd });

export function useIssue(key: string | null) {
  const qc = useQueryClient();
  return useQuery({
    queryKey: keys.issue(key ?? ''),
    queryFn: () => tracker.issue(key!),
    enabled: !!key,
    placeholderData: () => qc.getQueryData<Issue[]>(keys.allIssues)?.find((i) => i.key === key),
  });
}

/** Write an issue into every cache that holds it, so board, backlog, panel and search agree at once. */
function putIssue(qc: QueryClient, next: Partial<Issue> & { key: string }) {
  // List rows carry no comments or work log: never let a list-shaped update blank out the detail's.
  const { comments, worklog, ...listShape } = next;
  qc.setQueryData<Issue[]>(keys.allIssues, (prev) => prev?.map((i) => (i.key === next.key ? { ...i, ...listShape } : i)));
  qc.setQueryData<Issue>(keys.issue(next.key), (prev) =>
    prev ? { ...prev, ...listShape, ...(comments ? { comments } : {}), ...(worklog ? { worklog } : {}) } : undefined,
  );
}

function refreshTracker(qc: QueryClient, key?: string) {
  void qc.invalidateQueries({ queryKey: ['tracker', 'issues'] });
  void qc.invalidateQueries({ queryKey: ['tracker', 'reports'] });
  if (key) {
    void qc.invalidateQueries({ queryKey: keys.issue(key) });
    void qc.invalidateQueries({ queryKey: keys.history(key) });
  }
}

export function useUpdateIssue() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { key: string; patch: IssuePatch }) => tracker.update(v.key, v.patch),
    onMutate: async ({ key, patch }) => {
      await qc.cancelQueries({ queryKey: ['tracker', 'issues'] });
      const prevAll = qc.getQueryData<Issue[]>(keys.allIssues);
      const prevOne = qc.getQueryData<Issue>(keys.issue(key));
      const base = prevOne ?? prevAll?.find((i) => i.key === key);
      if (base) putIssue(qc, { ...base, ...(patch as Partial<Issue>) });
      return { prevAll, prevOne };
    },
    onError: (_e, { key }, ctx) => {
      if (ctx?.prevAll) qc.setQueryData(keys.allIssues, ctx.prevAll);
      if (ctx?.prevOne) qc.setQueryData(keys.issue(key), ctx.prevOne);
    },
    onSuccess: (issue) => issue && putIssue(qc, issue),
    onSettled: (_d, _e, { key }) => refreshTracker(qc, key),
  });
}

function numericRank(r: Issue['rank']): number | null {
  const n = typeof r === 'number' ? r : Number(r);
  return Number.isFinite(n) ? n : null;
}

export function compareRank(a: Issue, b: Issue): number {
  const na = numericRank(a.rank);
  const nb = numericRank(b.rank);
  if (na !== null && nb !== null) return na - nb;
  return String(a.rank).localeCompare(String(b.rank));
}

/** Move `key` before or after a neighbour; optional patch (status / sprint) goes in the same gesture. */
export function useMoveIssue() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (v: { key: string; before?: string; after?: string; patch?: IssuePatch }) => {
      let out: (Partial<Issue> & { key: string }) | null = null;
      if (v.patch && Object.keys(v.patch).length) out = await tracker.update(v.key, v.patch);
      if (v.before || v.after) out = await tracker.rank(v.key, { before: v.before, after: v.after });
      return out;
    },
    onMutate: async ({ key, before, after, patch }) => {
      await qc.cancelQueries({ queryKey: ['tracker', 'issues'] });
      const prevAll = qc.getQueryData<Issue[]>(keys.allIssues);
      if (prevAll) {
        const neighbour = prevAll.find((i) => i.key === (before ?? after));
        const sorted = [...prevAll].sort(compareRank);
        let rank: Issue['rank'] | undefined;
        if (neighbour) {
          const idx = sorted.findIndex((i) => i.key === neighbour.key);
          const other = before ? sorted.slice(0, idx).reverse().find((i) => i.key !== key) : sorted.slice(idx + 1).find((i) => i.key !== key);
          const n0 = numericRank(neighbour.rank);
          const n1 = other ? numericRank(other.rank) : null;
          if (n0 !== null) rank = n1 !== null ? (n0 + n1) / 2 : before ? n0 - 1 : n0 + 1;
        }
        qc.setQueryData<Issue[]>(keys.allIssues, (prev) =>
          prev?.map((i) => (i.key === key ? { ...i, ...(patch as Partial<Issue>), ...(rank !== undefined ? { rank } : {}) } : i)),
        );
      }
      return { prevAll };
    },
    onError: (_e, _v, ctx) => ctx?.prevAll && qc.setQueryData(keys.allIssues, ctx.prevAll),
    onSuccess: (issue) => issue && putIssue(qc, issue),
    onSettled: (_d, _e, { key }) => refreshTracker(qc, key),
  });
}

export function useCreateIssue() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: NewIssue) => tracker.create(body),
    onSuccess: (issue) => {
      qc.setQueryData<Issue[]>(keys.allIssues, (prev) => (prev ? [...prev, issue] : prev));
      refreshTracker(qc);
    },
  });
}

export function useAddComment(key: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: string) => tracker.comment(key, body),
    onSettled: () => refreshTracker(qc, key),
  });
}

export function useAddWorklog(key: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { hours: number; date: string; note: string }) => tracker.worklog(key, v.hours, v.date, v.note),
    onSettled: () => refreshTracker(qc, key),
  });
}

function refreshSprints(qc: QueryClient) {
  void qc.invalidateQueries({ queryKey: keys.sprints });
  void qc.invalidateQueries({ queryKey: keys.meta });
  refreshTracker(qc);
}

export function useStartSprint() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => tracker.startSprint(id),
    onSuccess: (s) => qc.setQueryData<Sprint[]>(keys.sprints, (prev) => prev?.map((p) => (p.id === s.id ? s : p))),
    onSettled: () => refreshSprints(qc),
  });
}

export function useCompleteSprint() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { id: number; move_open_to: number | 'backlog' }) => tracker.completeSprint(v.id, v.move_open_to),
    onSettled: () => refreshSprints(qc),
  });
}

export function useSetup() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (start_date: string) => tracker.setup(start_date),
    onSuccess: (meta) => meta && qc.setQueryData<TrackerMeta>(keys.meta, meta),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ['tracker'] });
    },
  });
}

export function useRestore() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (backup: unknown) => tracker.restore(backup),
    onSettled: () => void qc.invalidateQueries({ queryKey: ['tracker'] }),
  });
}
