/* In-browser tracker backend for VITE_MOCK=1. Mirrors §5.4 closely enough to build every screen against it. */

import type {
  BurndownReport,
  CfdReport,
  ChecklistItem,
  Comment,
  HistoryEntry,
  Issue,
  IssuePatch,
  NewIssue,
  Person,
  Sprint,
  StatusName,
  TrackerMeta,
  VelocityReport,
  Worklog,
} from '@/api/types';
import { addDays, differenceInCalendarDays, isWeekend, parseDay, toDay, today, workingDay } from '@/lib/dates';
import { runJql } from '@/lib/jql';
import { HAND_CATALOG, type CatalogProject } from './catalog';

const generated = import.meta.glob<{ default: CatalogProject[] }>('./fixtures/tickets.generated.json', { eager: true });
const CATALOG: CatalogProject[] = Object.values(generated)[0]?.default ?? HAND_CATALOG;

export const ME = 'me';

export const PEOPLE: Person[] = [
  { key: ME, name: 'You', title: 'Data Engineer', dept: 'IT' },
  { key: 'daniel.reyes', name: 'Daniel Reyes', title: 'Chief Executive Officer', dept: 'EXEC' },
  { key: 'rebecca.lindqvist', name: 'Rebecca Lindqvist', title: 'Chief Financial Officer', dept: 'FIN' },
  { key: 'alan.brooks', name: 'Alan Brooks', title: 'Corporate Controller', dept: 'FIN' },
  { key: 'luis.moreno', name: 'Luis Moreno', title: 'VP Operations', dept: 'OPS' },
  { key: 'shanice.carter', name: 'Shanice Carter', title: 'General Manager, ATL1', dept: 'OPS' },
  { key: 'beth.kowalczyk', name: 'Beth Kowalczyk', title: 'Transportation Manager', dept: 'OPS' },
  { key: 'hannah.price', name: 'Hannah Price', title: 'Director of Sales Operations', dept: 'SLS' },
  { key: 'omar.haddad', name: 'Omar Haddad', title: 'Director of Client Success', dept: 'CS' },
  { key: 'ayesha.khan', name: 'Dr. Ayesha Khan', title: 'Quality & Food Safety Manager', dept: 'QA' },
  { key: 'mei.tanaka', name: 'Mei Tanaka', title: 'Information Security Manager', dept: 'IT' },
  { key: 'sam.okafor', name: 'Sam Okafor', title: 'Director of IT', dept: 'IT' },
  { key: 'marcus.bell', name: 'Marcus Bell', title: 'WMS Application Administrator', dept: 'IT' },
  { key: 'priya.raman', name: 'Priya Raman', title: 'ERP Systems Analyst (AX 2012)', dept: 'FIN' },
  { key: 'dana.whitfield', name: 'Dana Whitfield', title: 'Salesforce Administrator', dept: 'SLS' },
  { key: 'tomas.ortega', name: 'Tomás Ortega', title: 'EDI & Integrations Specialist', dept: 'IT' },
  { key: 'grace.liu', name: 'Grace Liu', title: 'Facilities & Controls Engineer', dept: 'OPS' },
  { key: 'kevin.ohara', name: "Kevin O'Hara", title: 'IT Infrastructure Engineer', dept: 'IT' },
  { key: 'iris.novak', name: 'Iris Novak', title: 'Business Performance Manager', dept: 'EXEC' },
];

const DOD = [
  'Work is on a branch named feat/DATA-<n>-<slug> and every commit message carries the key',
  'PR opened with the template filled in and a written self-review',
  'CI is green: lint, unit tests and dbt build',
  'Docs, data dictionary or an ADR updated where behaviour changed',
  'Acceptance criteria demonstrated, with evidence linked in a comment',
];

interface Store {
  configured: boolean;
  start_date: string | null;
  issues: Issue[];
  sprints: Sprint[];
  history: Record<string, HistoryEntry[]>;
  seq: number;
}

const STORAGE_KEY = 'harborline.mock.tracker.v4';
/** Read once at load: in-app navigation drops the query string, the dataset choice must not change with it. */
export const WORST_CASE = import.meta.env.VITE_DEMO !== '1' && new URLSearchParams(location.search).get('data') === 'worst';
/** The public demo starts from the sample on every load: its notice says nothing is saved. */
const PERSIST = import.meta.env.VITE_DEMO !== '1' && !WORST_CASE;

// ---------------------------------------------------------------- building the catalog into issues

function sprintsFor(start: Date | null): Sprint[] {
  const out: Sprint[] = [];
  for (let n = 1; n <= 16; n += 1) {
    out.push({
      id: n,
      name: `DATA Sprint ${n}`,
      goal: null,
      state: 'future',
      start_date: start ? toDay(workingDay(start, (n - 1) * 10 + 1)) : null,
      end_date: start ? toDay(workingDay(start, n * 10)) : null,
      project: Math.ceil(n / 2),
    });
  }
  return out;
}

function buildIssues(start: Date | null): Issue[] {
  const issues: Issue[] = [];
  let n = 1;
  let rank = 1000;
  const due = (project: number, day: number) => (start ? toDay(workingDay(start, (project - 1) * 20 + day)) : null);
  const created = start ? toDay(addDays(start, -3)) + 'T09:00:00Z' : new Date().toISOString();
  const idToKey = new Map<string, string>();

  for (const p of CATALOG) {
    const epicKey = `DATA-${n++}`;
    idToKey.set(p.epic.id, epicKey);
    issues.push({
      key: epicKey,
      type: 'Epic',
      summary: p.epic.summary,
      status: 'To Do',
      priority: (p.epic.priority as Issue['priority']) ?? 'High',
      assignee: ME,
      reporter: p.epic.reporter,
      epic_key: null,
      parent_key: null,
      sprint_id: null,
      rank: rank++,
      story_points: null,
      estimate_hours: null,
      due_date: due(p.project, 20),
      labels: p.epic.labels ?? [],
      components: [],
      description: p.epic.description ?? '',
      acceptance_criteria: [],
      definition_of_done: [],
      attachments: [],
      comments: [],
      worklog: [],
      project_no: p.project,
      source_id: p.epic.id,
      department: p.epic.department,
      created,
      updated: created,
    });
    for (const s of p.stories) {
      const storyKey = `DATA-${n++}`;
      idToKey.set(s.id, storyKey);
      const sprint = (p.project - 1) * 2 + (s.sprint ?? (s.due_day <= 10 ? 1 : 2));
      issues.push({
        key: storyKey,
        type: 'Story',
        summary: s.summary,
        status: 'To Do',
        priority: s.priority as Issue['priority'],
        assignee: ME,
        reporter: s.reporter,
        epic_key: epicKey,
        parent_key: null,
        sprint_id: sprint,
        rank: rank++,
        story_points: s.story_points,
        estimate_hours: null,
        due_date: due(p.project, s.due_day),
        labels: s.labels ?? [],
        components: s.components ?? [],
        description: s.description ?? '',
        acceptance_criteria: (s.acceptance_criteria ?? []).map((t, i) => ({ id: String(i), text: t, done: false })),
        definition_of_done: (s.definition_of_done?.length ? s.definition_of_done : DOD).map((t, i) => ({ id: String(i), text: t, done: false })),
        depends_on: s.depends_on ?? [],
        attachments: (s.attachments ?? []).map((a) => ({ name: a.split('/').pop() ?? a, path: a })),
        comments: [],
        worklog: [],
        project_no: p.project,
        source_id: s.id,
        created,
        updated: created,
      });
      for (const t of s.tasks) {
        const key = `DATA-${n++}`;
        idToKey.set(t.id, key);
        issues.push({
          key,
          type: t.type,
          summary: t.summary,
          status: 'To Do',
          priority: (t.priority as Issue['priority']) ?? 'Medium',
          assignee: ME,
          reporter: s.reporter,
          epic_key: epicKey,
          parent_key: storyKey,
          sprint_id: (p.project - 1) * 2 + (t.due_day <= 10 ? 1 : 2),
          rank: rank++,
          story_points: null,
          estimate_hours: t.estimate_hours,
          due_date: due(p.project, t.due_day),
          labels: t.labels ?? [],
          components: t.components ?? [],
          description: t.description ?? '',
          acceptance_criteria: (t.acceptance_criteria ?? []).map((x, i) => ({ id: String(i), text: x, done: false })),
          definition_of_done: (t.definition_of_done?.length ? t.definition_of_done : DOD).map((x, i) => ({ id: String(i), text: x, done: false })),
          depends_on: t.depends_on ?? [],
          attachments: [],
          comments: [],
          worklog: [],
          time_spent_hours: 0,
          project_no: p.project,
          source_id: t.id,
          created,
          updated: created,
        });
      }
    }
  }
  // depends_on holds catalog ids; the API speaks issue keys.
  for (const i of issues) {
    i.depends_on = (i.depends_on ?? []).map((d) => idToKey.get(d)).filter((k): k is string => !!k);
    i.links = i.depends_on.map((k) => ({ type: 'is blocked by', key: k }));
  }
  for (const i of issues) {
    for (const k of i.depends_on ?? []) {
      const blocker = issues.find((x) => x.key === k);
      if (blocker) blocker.links = [...(blocker.links ?? []), { type: 'blocks', key: i.key }];
    }
  }
  return issues;
}

// ---------------------------------------------------------------- a believable "two weeks in" history

const NOTES = [
  'Checked with the reporter: the definition in the ticket stands. Logged the decision in docs/decisions.md.',
  'First pass merged behind a feature flag. Row counts match the source for 2026-10-01.',
  'Found 37 rows with trailing spaces in owner_code; added a trim in staging and a test so it fails loudly next time.',
  'Slot created as etl_reader. WAL retention is bounded by max_slot_wal_keep_size, so a stopped consumer cannot fill the disk.',
  'PR is up. CI was red on sqlfluff (L010); fixed and re-pushed.',
];

function seedDemo(store: Store) {
  const start = parseDay(store.start_date!);
  const now = today();
  const at = (d: Date, hour: number) => `${toDay(d)}T${String(hour).padStart(2, '0')}:${String((hour * 7) % 60).padStart(2, '0')}:00Z`;
  const sprint1 = store.sprints[0];
  const sprint2 = store.sprints[1];
  sprint1.state = 'closed';
  sprint1.completed_at = at(parseDay(sprint1.end_date!), 17);
  sprint1.goal = 'Repository, CI and the WMS landing zone are in place';
  sprint2.state = 'active';
  sprint2.goal = 'AX, Salesforce and carrier sources land daily; dashboard spec signed off';

  const tasks = store.issues.filter((i) => i.type !== 'Epic' && i.type !== 'Story');
  const s1Tasks = tasks.filter((t) => t.sprint_id === 1);
  const s2Tasks = tasks.filter((t) => t.sprint_id === 2);
  const carry = new Set(s1Tasks.slice(-1).map((t) => t.key));

  const transition = (i: Issue, to: StatusName, when: string) => {
    const from = i.status;
    if (from === to) return;
    (store.history[i.key] ??= []).push({ ts: when, field: 'status', from, to, actor: ME });
    i.status = to;
    i.updated = when;
    if (to === 'Done') i.resolved = when;
  };

  s1Tasks.forEach((t, idx) => {
    const due = parseDay(t.due_date!);
    const begin = addDays(due, -2);
    const startDay = begin < start ? start : begin;
    transition(t, 'In Progress', at(startDay, 9 + (idx % 6)));
    if (carry.has(t.key)) {
      t.sprint_id = 2;
      return;
    }
    transition(t, 'In Review', at(due, 11 + (idx % 4)));
    transition(t, 'Done', at(due, 15 + (idx % 3)));
    const hours = t.estimate_hours ?? 4;
    const spent = Math.max(1, Math.round(hours * (0.8 + ((idx * 37) % 50) / 100)));
    t.worklog = [{ id: `${t.key}-w1`, hours: spent, date: toDay(due), note: 'Implementation and PR review', author: ME, created: at(due, 16) }];
    t.time_spent_hours = spent;
    t.acceptance_criteria = (t.acceptance_criteria as ChecklistItem[]).map((c) => ({ ...c, done: true }));
    t.definition_of_done = (t.definition_of_done as ChecklistItem[]).map((c) => ({ ...c, done: true }));
    t.pr_url = `https://github.com/you/harborline-analytics/pull/${idx + 1}`;
  });

  // Sprint 2: work done up to today, the rest waiting.
  const sprint2Tasks = [...s2Tasks, ...s1Tasks.filter((t) => carry.has(t.key))].sort((a, b) => (a.due_date! < b.due_date! ? -1 : 1));
  sprint2Tasks.forEach((t, idx) => {
    const due = parseDay(t.due_date!);
    const dueIn = differenceInCalendarDays(due, now);
    const sprintStart = parseDay(sprint2.start_date!);
    const when = (d: Date) => (d > now ? now : d < sprintStart ? sprintStart : d);
    if (carry.has(t.key)) {
      transition(t, 'In Review', at(when(addDays(sprintStart, 1)), 10));
      return;
    }
    if (dueIn <= -1 && idx % 7 !== 4) {
      transition(t, 'In Progress', at(when(addDays(due, -2)), 9));
      transition(t, 'In Review', at(when(addDays(due, -1)), 14));
      transition(t, 'Done', at(when(due), 16));
      const spent = Math.max(1, Math.round((t.estimate_hours ?? 4) * 1.1));
      t.worklog = [{ id: `${t.key}-w1`, hours: spent, date: toDay(when(due)), note: null, author: ME, created: at(when(due), 16) }];
      t.time_spent_hours = spent;
      t.acceptance_criteria = (t.acceptance_criteria as ChecklistItem[]).map((c) => ({ ...c, done: true }));
    } else if (dueIn <= -1) {
      transition(t, 'In Progress', at(when(addDays(due, -2)), 10)); // overdue and still open
    } else if (dueIn <= 1) {
      transition(t, 'In Progress', at(when(addDays(now, -1)), 9));
      if (idx % 2 === 0) transition(t, 'In Review', at(now, 8));
      const ac = t.acceptance_criteria as ChecklistItem[];
      t.acceptance_criteria = ac.map((c, i) => ({ ...c, done: i === 0 }));
      t.time_spent_hours = Math.round((t.estimate_hours ?? 4) / 2);
      t.worklog = [{ id: `${t.key}-w1`, hours: t.time_spent_hours, date: toDay(now), note: 'First pass', author: ME, created: at(now, 12) }];
    }
  });

  // Stories follow their tasks.
  for (const s of store.issues.filter((i) => i.type === 'Story')) {
    const kids = store.issues.filter((i) => i.parent_key === s.key);
    if (!kids.length) continue;
    const latest = kids.map((k) => k.updated ?? '').sort().pop() ?? at(now, 9);
    if (kids.every((k) => k.status === 'Done')) {
      transition(s, 'In Progress', kids.map((k) => (store.history[k.key] ?? [])[0]?.ts ?? latest).sort()[0]);
      transition(s, 'Done', latest);
      s.acceptance_criteria = (s.acceptance_criteria as ChecklistItem[]).map((c) => ({ ...c, done: true }));
    } else if (kids.some((k) => k.status !== 'To Do')) {
      transition(s, 'In Progress', kids.map((k) => (store.history[k.key] ?? [])[0]?.ts ?? latest).filter(Boolean).sort()[0]);
    }
  }
  const epic1 = store.issues.find((i) => i.type === 'Epic');
  if (epic1) transition(epic1, 'In Progress', at(start, 9));

  // A few of his own notes.
  const commented = store.issues.filter((i) => i.status !== 'To Do' && i.type !== 'Epic').slice(0, 12);
  commented.forEach((i, idx) => {
    if (idx % 2) return;
    const ts = i.updated ?? at(now, 10);
    i.comments = [{ id: `${i.key}-c1`, author: ME, body: NOTES[idx % NOTES.length], created: ts }];
  });
}

/* break-ui: worst realistic data, injected at the data boundary (dev only, ?data=worst). */
function addWorstCase(store: Store) {
  const sprint = store.sprints.find((x) => x.state === 'active')?.id ?? 2;
  const epic = store.issues.find((i) => i.type === 'Epic')!;
  const story = store.issues.find((i) => i.type === 'Story' && i.sprint_id === sprint) ?? store.issues.find((i) => i.type === 'Story')!;
  let n = Math.max(...store.issues.map((i) => Number(i.key.split('-')[1]))) + 1;
  let rank = -1000;
  const now = new Date().toISOString();
  const day = (offset: number) => toDay(addDays(today(), offset));
  const make = (over: Partial<Issue>): Issue => ({
    key: `DATA-${n++}`,
    type: 'Task',
    summary: 'Untitled',
    status: 'To Do',
    priority: 'Medium',
    assignee: ME,
    reporter: ME,
    epic_key: epic.key,
    parent_key: story.key,
    sprint_id: sprint,
    rank: rank++,
    story_points: null,
    estimate_hours: 4,
    due_date: day(3),
    labels: [],
    components: [],
    description: '',
    acceptance_criteria: [],
    definition_of_done: DOD.map((t, i) => ({ id: String(i), text: t, done: false })),
    attachments: [],
    comments: [],
    worklog: [],
    project_no: 1,
    created: now,
    updated: now,
    ...over,
  });
  const longUrl = 'https://github.com/aleksandra-wisniewska-kowalczyk/harborline-analytics-portfolio/pull/1284/files#diff-7c4f0e3b9a1d2e5f8a6b0c3d4e7f1a2b5c8d0e3f6a9b2c5d8e1f4a7b0c3d6e9f';
  const worst: Issue[] = [
    make({
      type: 'Story',
      parent_key: null,
      summary:
        'Reconcile AX 2012 CUSTTRANS open items against WMS billing_record accruals per DATAAREAID (us01, ca01, mx01), including settlement reversals, month-end FX revaluation and the 1900-01-01 sentinel dates Priya says appear on unposted journals',
      priority: 'Highest',
      story_points: 100,
      estimate_hours: null,
      due_date: day(-120),
      status: 'In Progress',
      labels: ['ax2012', 'finance', 'reconciliation', 'month-end-close', 'fx', 'settlements', 'data-quality', 'sentinel-dates', 'us01', 'ca01', 'mx01', 'needs-priya'],
      components: ['Ingestion', 'Modeling', 'Data Quality', 'Dashboards', 'Documentation'],
      description:
        '## Context\n\nAlan wants the AR aging to tie out to the penny. ' +
        'This needs the full `generaljournalaccountentry` → `custtrans` → `custsettlement` chain.\n\n' +
        '```sql\nselect t.dataareaid, t.accountnum, t.voucher, t.amountcur, s.settleamountcur, s.transdate, t.closed, coalesce(nullif(t.closed, date \'1900-01-01\'), null) as closed_on\nfrom dbo.custtrans t left join dbo.custsettlement s on s.transrecid = t.recid and s.dataareaid = t.dataareaid\nwhere t.dataareaid in (\'us01\', \'ca01\', \'mx01\')\n```\n\n' +
        `PR draft: ${longUrl}`,
      acceptance_criteria: Array.from({ length: 14 }, (_, i) => ({
        id: String(i),
        idx: i,
        text: i === 3 ? `Evidence linked: ${longUrl}` : `Criterion ${i + 1}: the ${['us01', 'ca01', 'mx01'][i % 3]} ledger ties to AR within 0.01 in the reporting currency for every month since go-live, including reversed settlements`,
        done: i % 3 === 0,
      })),
      comments: [
        {
          id: 'w1',
          author: 'priya.raman',
          body: `Sentinel dates are expected on unposted lines; don't drop them. Full extract here: ${longUrl}`,
          created: now,
        },
        { id: 'w2', author: 'contractor.jo', body: 'ok', created: now },
        // taste-skill-ignore: worst-case test data (break-ui), real users type emoji into comments and summaries
        { id: 'w3', author: ME, body: 'Revisé la conciliación de Harborline Logística México S. de R.L. de C.V. contra Banxico: diferencias de 0,01 MXN por redondeo. 🔍', created: now },
      ],
      worklog: Array.from({ length: 30 }, (_, i) => ({ id: `wl${i}`, hours: i === 0 ? 23.5 : 0.25, date: day(-i), note: i === 0 ? 'Weekend backfill of 36 months of custtrans across three legal entities' : null, author: ME, created: now })),
      time_spent_hours: 30.75,
    }),
    make({
      summary: 'Backfill sfdc.OpportunityHistory__ExternalSystemLoadAuditTrail_v2_20261031T235959Z_snapshot_partitioned_by_owner_code',
      estimate_hours: 999.5,
      assignee: null,
      due_date: null,
      priority: 'Lowest',
    }),
    make({ summary: 'X', type: 'Bug', estimate_hours: null, assignee: 'contractor.jo', priority: 'High', due_date: day(0) }),
    // taste-skill-ignore: worst-case test data (break-ui)
    make({ summary: '🔥 Hotfix: carrier 503 storm — FedEx (FDEG) events arrive 36 h late', type: 'Bug', priority: 'Highest', due_date: day(-1), assignee: 'mei.tanaka' }),
    make({ summary: 'Conciliación MXN: Harborline Logística México S. de R.L. de C.V. contra tipos de cambio de Banxico', type: 'Spike', estimate_hours: 0.5, due_date: day(1) }),
    ...Array.from({ length: 60 }, (_, i) =>
      make({ summary: `Data test for wms.${['order_line', 'inventory_txn', 'shipment_carton', 'billing_record', 'task'][i % 5]} #${i + 1}`, estimate_hours: (i % 6) + 1, due_date: day((i % 9) - 2) }),
    ),
  ];
  // An empty column: nothing In Review.
  for (const i of store.issues) if (i.sprint_id === sprint && i.status === 'In Review') i.status = 'In Progress';
  store.issues.push(...worst);
}

function fresh(): Store {
  return { configured: false, start_date: null, issues: buildIssues(null), sprints: sprintsFor(null), history: {}, seq: 1 };
}

function configure(start: string, demo: boolean): Store {
  const s = parseDay(start);
  const next: Store = { configured: true, start_date: start, issues: buildIssues(s), sprints: sprintsFor(s), history: {}, seq: 1 };
  next.seq = next.issues.length + 1;
  if (demo) seedDemo(next);
  return next;
}

function demoStart(): string {
  // Two sprints in: the Monday two weeks before this week's Monday.
  const t = today();
  const dow = t.getDay() === 0 ? 7 : t.getDay();
  return toDay(addDays(t, -(dow - 1) - 14));
}

let store: Store = load();

function load(): Store {
  if (!PERSIST) return seedFor();
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) return JSON.parse(raw) as Store;
  } catch {
    /* storage unavailable: fall through */
  }
  return seedFor();
}

function seedFor(): Store {
  const params = new URLSearchParams(location.search);
  if (params.has('fresh')) return fresh();
  const s = configure(demoStart(), true);
  if (WORST_CASE) addWorstCase(s);
  return s;
}

function save() {
  if (!PERSIST) return; // never persist the worst case or the demo
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(store));
  } catch {
    /* quota or privacy mode: keep in memory */
  }
}

export function resetTracker(mode: 'demo' | 'fresh') {
  store = mode === 'fresh' ? fresh() : configure(demoStart(), true);
  save();
}

// ---------------------------------------------------------------- API

export class HttpError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

const personName = (k: string) => PEOPLE.find((p) => p.key === k)?.name ?? k;
const STATUSES: StatusName[] = ['To Do', 'In Progress', 'In Review', 'Done'];

function find(key: string): Issue {
  const i = store.issues.find((x) => x.key.toLowerCase() === key.toLowerCase());
  if (!i) throw new HttpError(404, `Issue ${key} does not exist`);
  return i;
}

/** List responses leave out comments and work log; GET /issues/{key} carries them. */
function slim(i: Issue): Issue {
  const out = { ...i, time_spent_hours: i.time_spent_hours ?? 0 };
  delete out.comments;
  delete out.worklog;
  return out;
}

export function meta(): TrackerMeta {
  return {
    project: { key: 'DATA', name: 'Harborline Data Platform' },
    statuses: STATUSES,
    issue_types: ['Epic', 'Story', 'Task', 'Bug', 'Spike'],
    priorities: ['Highest', 'High', 'Medium', 'Low', 'Lowest'],
    people: PEOPLE,
    components: [...new Set(store.issues.flatMap((i) => i.components))].sort(),
    labels: [...new Set(store.issues.flatMap((i) => i.labels))].sort(),
    sprints: store.sprints,
    start_date: store.start_date,
    configured: store.configured,
    me: ME,
  };
}

export function setup(start_date: string): TrackerMeta {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(start_date)) throw new HttpError(422, 'start_date must be YYYY-MM-DD');
  if (isWeekend(parseDay(start_date))) throw new HttpError(422, 'Pick a weekday: the program counts working days');
  store = configure(start_date, false);
  save();
  return meta();
}

export function listIssues(q: URLSearchParams): Issue[] {
  let out = store.issues;
  const sprint = q.get('sprint');
  if (sprint === 'backlog') out = out.filter((i) => i.sprint_id == null && i.type !== 'Epic');
  else if (sprint) out = out.filter((i) => String(i.sprint_id) === sprint);
  for (const f of ['status', 'type'] as const) {
    const v = q.get(f);
    if (v) out = out.filter((i) => i[f].toLowerCase() === v.toLowerCase());
  }
  const epic = q.get('epic');
  if (epic) out = out.filter((i) => i.epic_key === epic);
  const jql = q.get('jql');
  if (jql) {
    try {
      out = runJql(out, jql, {
        me: ME,
        now: new Date(),
        sprints: store.sprints,
        issueByKey: new Map(store.issues.map((i) => [i.key, i])),
        peopleName: personName,
      });
    } catch (e) {
      throw new HttpError(400, (e as Error).message);
    }
  }
  return out.map(slim);
}

export const getIssue = (key: string): Issue => ({ ...find(key) });

function record(i: Issue, field: string, from: unknown, to: unknown) {
  const show = (v: unknown) => (v == null ? null : Array.isArray(v) ? v.join(', ') : String(v));
  (store.history[i.key] ??= []).push({ ts: new Date().toISOString(), field, from: show(from), to: show(to), actor: ME });
}

export function patchIssue(key: string, patch: IssuePatch): Issue {
  const i = find(key);
  for (const [field, value] of Object.entries(patch) as Array<[keyof IssuePatch, unknown]>) {
    const before = (i as unknown as Record<string, unknown>)[field];
    if (field === 'status' && !STATUSES.includes(value as StatusName)) throw new HttpError(422, `Unknown status ${String(value)}`);
    if (field === 'acceptance_criteria') {
      // Same shape as the backend: [{idx, checked}].
      const prev = ((before as ChecklistItem[]) ?? []).map((c) => ({ ...c }));
      for (const item of value as Array<{ idx: number; checked: boolean }>) {
        const c = prev[item.idx];
        if (!c) throw new HttpError(400, `${i.key} has no acceptance criterion #${item.idx}`);
        if (c.done !== item.checked) record(i, `Acceptance criterion ${item.idx + 1}`, c.done ? 'checked' : 'unchecked', item.checked ? 'checked' : 'unchecked');
        c.done = item.checked;
      }
      i.acceptance_criteria = prev;
      continue;
    } else if (JSON.stringify(before) !== JSON.stringify(value)) {
      const label = field === 'sprint_id' ? 'sprint' : field;
      const fmt = (v: unknown) => (field === 'sprint_id' ? (v == null ? 'Backlog' : store.sprints.find((s) => s.id === v)?.name) : field === 'assignee' ? (v ? personName(String(v)) : 'Unassigned') : v);
      record(i, label, fmt(before), fmt(value));
    }
    (i as unknown as Record<string, unknown>)[field] = value;
  }
  if (patch.status === 'Done') i.resolved = new Date().toISOString();
  else if (patch.status) i.resolved = null;
  i.updated = new Date().toISOString();
  save();
  return { ...i };
}

export function rankIssue(key: string, pos: { before?: string; after?: string }): Issue {
  const i = find(key);
  const sorted = [...store.issues].sort((a, b) => Number(a.rank) - Number(b.rank)).filter((x) => x.key !== i.key);
  const anchor = pos.before ?? pos.after;
  if (!anchor) throw new HttpError(422, 'Give before or after');
  const idx = sorted.findIndex((x) => x.key === anchor);
  if (idx < 0) throw new HttpError(404, `Issue ${anchor} does not exist`);
  const a = Number(sorted[idx].rank);
  const neighbour = pos.before ? sorted[idx - 1] : sorted[idx + 1];
  const b = neighbour ? Number(neighbour.rank) : pos.before ? a - 2 : a + 2;
  i.rank = (a + b) / 2;
  record(i, 'Rank', null, pos.before ? `Ranked higher than ${anchor}` : `Ranked lower than ${anchor}`);
  save();
  return { ...i };
}

export function createIssue(body: NewIssue): Issue {
  if (!body.summary?.trim()) throw new HttpError(422, 'Summary is required');
  const max = Math.max(...store.issues.map((i) => Number(i.key.split('-')[1])));
  const key = `DATA-${max + 1}`;
  const now = new Date().toISOString();
  const epic = body.epic_key ? find(body.epic_key) : null;
  const issue: Issue = {
    key,
    type: body.type,
    summary: body.summary.trim(),
    status: 'To Do',
    priority: body.priority ?? 'Medium',
    assignee: body.assignee === undefined ? ME : body.assignee,
    reporter: ME,
    epic_key: body.epic_key ?? null,
    parent_key: body.parent_key ?? null,
    sprint_id: body.sprint_id ?? null,
    rank: Math.max(...store.issues.map((i) => Number(i.rank))) + 1,
    story_points: body.story_points ?? null,
    estimate_hours: body.estimate_hours ?? null,
    due_date: body.due_date ?? null,
    labels: body.labels ?? [],
    components: [],
    description: body.description ?? '',
    acceptance_criteria: [],
    definition_of_done: DOD.map((t, i) => ({ id: String(i), text: t, done: false })),
    attachments: [],
    comments: [],
    worklog: [],
    project_no: epic?.project_no ?? null,
    created: now,
    updated: now,
  };
  store.issues.push(issue);
  record(issue, 'created', null, issue.type);
  save();
  return issue;
}

export function addComment(key: string, body: string): Comment {
  const i = find(key);
  if (!body.trim()) throw new HttpError(422, 'Comment is empty');
  const c: Comment = { id: `${key}-c${Date.now()}`, author: ME, body: body.trim(), created: new Date().toISOString() };
  i.comments = [...(i.comments ?? []), c];
  i.updated = c.created;
  save();
  return c;
}

export function addWorklog(key: string, hours: number, date: string, note: string): Worklog {
  const i = find(key);
  if (!(hours > 0) || hours > 24) throw new HttpError(422, 'Hours must be between 0 and 24');
  const w: Worklog = { id: `${key}-w${Date.now()}`, hours, date, note: note || null, author: ME, created: new Date().toISOString() };
  i.worklog = [...(i.worklog ?? []), w];
  i.time_spent_hours = (i.time_spent_hours ?? 0) + hours;
  record(i, 'Time spent', null, `${hours}h logged`);
  save();
  return w;
}

export const history = (key: string): HistoryEntry[] => {
  find(key);
  return [...(store.history[key] ?? [])].sort((a, b) => (a.ts < b.ts ? 1 : -1));
};

export const sprints = (): Sprint[] => store.sprints;

export function startSprint(id: number): Sprint {
  const s = store.sprints.find((x) => x.id === id);
  if (!s) throw new HttpError(404, 'No such sprint');
  if (store.sprints.some((x) => x.state === 'active')) throw new HttpError(409, 'Complete the active sprint before starting another');
  if (s.state !== 'future') throw new HttpError(409, `${s.name} is already ${s.state}`);
  s.state = 'active';
  save();
  return s;
}

export function completeSprint(id: number, moveTo: number | 'backlog'): Sprint {
  const s = store.sprints.find((x) => x.id === id);
  if (!s || s.state !== 'active') throw new HttpError(409, 'Only the active sprint can be completed');
  for (const i of store.issues.filter((x) => x.sprint_id === id && x.status !== 'Done')) {
    record(i, 'sprint', s.name, moveTo === 'backlog' ? 'Backlog' : store.sprints.find((x) => x.id === moveTo)?.name);
    i.sprint_id = moveTo === 'backlog' ? null : moveTo;
  }
  s.state = 'closed';
  s.completed_at = new Date().toISOString();
  save();
  return s;
}

// ---------------------------------------------------------------- reports

const isWork = (i: Issue) => i.type === 'Task' || i.type === 'Bug' || i.type === 'Spike';

function doneAt(i: Issue): string | null {
  const h = (store.history[i.key] ?? []).filter((e) => e.field === 'status' && e.to === 'Done').pop();
  return h?.ts ?? (i.status === 'Done' ? i.resolved ?? i.updated ?? null : null);
}

function statusOn(i: Issue, day: string): StatusName | null {
  if ((i.created ?? '').slice(0, 10) > day) return null;
  let st: StatusName = 'To Do';
  for (const e of store.history[i.key] ?? []) {
    if (e.field === 'status' && e.ts.slice(0, 10) <= day) st = e.to as StatusName;
  }
  return st;
}

export function burndown(sprintId: number): BurndownReport {
  const s = store.sprints.find((x) => x.id === sprintId);
  if (!s || !s.start_date || !s.end_date) throw new HttpError(404, 'No such sprint');
  const items = store.issues.filter((i) => isWork(i) && i.sprint_id === sprintId);
  const committed = items.reduce((t, i) => t + (i.estimate_hours ?? 0), 0);
  const days: BurndownReport['days'] = [];
  const startD = parseDay(s.start_date);
  const workDays: Date[] = [];
  for (let d = startD; d <= parseDay(s.end_date); d = addDays(d, 1)) if (!isWeekend(d)) workDays.push(d);
  const t = toDay(today());
  workDays.forEach((d, idx) => {
    const day = toDay(d);
    const burned = items.filter((i) => (doneAt(i) ?? '9999').slice(0, 10) <= day).reduce((x, i) => x + (i.estimate_hours ?? 0), 0);
    days.push({
      date: day,
      remaining: day <= t ? committed - burned : null,
      ideal: Math.round(committed * (1 - (idx + 1) / workDays.length) * 10) / 10,
    });
  });
  return { sprint_id: sprintId, unit: 'hours', committed, days };
}

export function velocity(): VelocityReport {
  return {
    unit: 'hours',
    sprints: store.sprints
      .filter((s) => s.state !== 'future')
      .map((s) => {
        const items = store.issues.filter((i) => isWork(i) && i.sprint_id === s.id);
        const carried = store.issues.filter(
          (i) => isWork(i) && (store.history[i.key] ?? []).some((e) => e.field === 'sprint' && e.from === s.name),
        );
        const all = [...items, ...carried.filter((c) => !items.includes(c))];
        const end = s.end_date ?? '9999';
        return {
          sprint_id: s.id,
          name: s.name,
          committed: all.reduce((t, i) => t + (i.estimate_hours ?? 0), 0),
          completed: items.filter((i) => (doneAt(i) ?? '9999').slice(0, 10) <= end).reduce((t, i) => t + (i.estimate_hours ?? 0), 0),
        };
      }),
  };
}

export function cfd(): CfdReport {
  if (!store.start_date) return { rows: [] };
  const rows: CfdReport['rows'] = [];
  const end = today();
  const work = store.issues.filter(isWork).filter((i) => i.sprint_id != null && i.sprint_id <= (store.sprints.find((s) => s.state === 'active')?.id ?? 2));
  for (let d = parseDay(store.start_date); d <= end; d = addDays(d, 1)) {
    if (isWeekend(d)) continue;
    const day = toDay(d);
    const row = { date: day, 'To Do': 0, 'In Progress': 0, 'In Review': 0, Done: 0 };
    for (const i of work) {
      const st = statusOn(i, day);
      if (st) row[st] += 1;
    }
    rows.push(row);
  }
  return { rows };
}

export function backup() {
  return { version: 1, exported_at: new Date().toISOString(), ...store };
}

export function restore(body: unknown) {
  const b = body as Partial<Store>;
  if (!b || !Array.isArray(b.issues) || !Array.isArray(b.sprints)) throw new HttpError(422, 'Not a tracker backup');
  store = { configured: !!b.configured, start_date: b.start_date ?? null, issues: b.issues, sprints: b.sprints, history: b.history ?? {}, seq: b.seq ?? 1 };
  save();
  return { ok: true };
}

export function jiraCsv(): string {
  const cols = ['Summary', 'Issue Type', 'Issue Id', 'Parent Id', 'Status', 'Priority', 'Due date', 'Original Estimate', 'Story Points', 'Labels', 'Component', 'Sprint', 'Description'];
  const id = (k: string | null) => (k ? k.split('-')[1] : '');
  const q = (v: unknown) => `"${String(v ?? '').replace(/"/g, '""')}"`;
  const lines = store.issues.map((i) =>
    [
      i.summary,
      i.type === 'Task' || i.type === 'Bug' || i.type === 'Spike' ? 'Sub-task' : i.type,
      id(i.key),
      id(i.parent_key ?? i.epic_key),
      i.status,
      i.priority,
      i.due_date ?? '',
      i.estimate_hours ? i.estimate_hours * 3600 : '',
      i.story_points ?? '',
      i.labels.join(' '),
      i.components.join(','),
      store.sprints.find((s) => s.id === i.sprint_id)?.name ?? '',
      i.description ?? '',
    ]
      .map(q)
      .join(','),
  );
  return [cols.join(','), ...lines].join('\n');
}
