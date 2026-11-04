import { useEffect, useMemo, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react';
import { Link } from 'react-router';
import clsx from 'clsx';
import {
  DndContext,
  DragOverlay,
  KeyboardSensor,
  PointerSensor,
  useDroppable,
  useSensor,
  useSensors,
  type Announcements,
  type DragEndEvent,
  type DragOverEvent,
  type DragStartEvent,
} from '@dnd-kit/core';
import { SortableContext, sortableKeyboardCoordinates, useSortable, verticalListSortingStrategy } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { ChevronDown, Plus, Search, X } from 'lucide-react';
import { toast } from 'sonner';
import { compareRank, useMoveIssue } from '@/api/queries';
import type { Issue, StatusName } from '@/api/types';
import { Button, buttonClasses } from '@/components/ui/button';
import { Input, Select } from '@/components/ui/field';
import { Empty, ErrorNote, Skeleton } from '@/components/ui/misc';
import { Tooltip } from '@/components/ui/overlay';
import { addDays, dueState, fmtDay, inThisWeek, isWeekend, parseDay, today, workWeek } from '@/lib/dates';
import { fmtHours, plural } from '@/lib/format';
import { ui, usePersisted } from '@/lib/store';
import { Avatar, DueBadge, Estimate, IssueKey, PriorityIcon, TypeIcon } from './atoms';
import { CompleteSprintDialog } from './SprintDialogs';
import { epicShort, isWorkItem, useIssueParam, useTracker } from './data';
import { containerCollision } from './dnd';
import { useIssueNav } from './nav';

const COLUMNS: StatusName[] = ['To Do', 'In Progress', 'In Review', 'Done'];
type GroupBy = 'none' | 'epic' | 'story';

interface Lane {
  id: string;
  title: string | null;
  issue?: Issue;
  items: Issue[];
}

const cid = (lane: string, status: StatusName) => `${lane}|${status}`;
const parseCid = (id: string) => {
  const [lane, status] = id.split('|');
  return { lane, status: status as StatusName };
};

function workingDaysLeft(end: string | null) {
  if (!end) return null;
  let n = 0;
  for (let d = today(); d <= parseDay(end); d = addDays(d, 1)) if (!isWeekend(d)) n += 1;
  return n;
}

// ---------------------------------------------------------------- card

function CardBody({ issue, epicName }: { issue: Issue; epicName?: string }) {
  const t = useTracker();
  const due = dueState(issue.due_date, issue.status === 'Done');
  return (
    <>
      <p className={clsx('line-clamp-3 text-base leading-5 [overflow-wrap:anywhere]', issue.status === 'Done' ? 'text-muted' : 'text-ink')}>{issue.summary}</p>
      {epicName || (due !== 'none' && due !== 'done') ? (
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          {epicName ? <span className="max-w-full truncate rounded-s bg-active px-1.5 text-xs leading-5 text-muted">{epicName}</span> : null}
          {due !== 'none' && due !== 'done' ? <DueBadge issue={issue} compact /> : null}
        </div>
      ) : null}
      <div className="mt-2.5 flex items-center gap-1.5">
        <TypeIcon type={issue.type} />
        <IssueKey k={issue.key} done={issue.status === 'Done'} />
        <span className="flex-1" />
        <Estimate issue={issue} />
        <PriorityIcon priority={issue.priority} />
        <Avatar person={t.person(issue.assignee)} size={24} me={issue.assignee === t.me} />
      </div>
    </>
  );
}

const cardClass =
  'block w-full rounded-m border border-line bg-sheet p-2.5 text-left outline-none transition-[border-color,background-color] duration-150 hover:border-control hover:bg-[color-mix(in_oklab,var(--sheet)_60%,var(--raised))] focus-visible:ring-2 focus-visible:ring-focus focus-visible:ring-offset-1 focus-visible:ring-offset-raised';

function SortableCard({ issue, container, epicName, onOpen }: { issue: Issue; container: string; epicName?: string; onOpen: (k: string) => void }) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: issue.key, data: { container } });
  return (
    <li
      ref={setNodeRef}
      style={{ transform: CSS.Translate.toString(transform), transition }}
      className={clsx('list-none', isDragging && 'opacity-40')}
    >
      <div
        {...attributes}
        {...listeners}
        data-issue-key={issue.key}
        aria-label={`${issue.key} ${issue.summary}. ${issue.type}, ${issue.priority} priority, ${issue.status}.`}
        onClick={() => onOpen(issue.key)}
        onKeyDown={(e: ReactKeyboardEvent) => {
          if (e.key === 'Enter') {
            e.preventDefault();
            onOpen(issue.key);
            return;
          }
          listeners?.onKeyDown?.(e);
        }}
        className={clsx(cardClass, 'cursor-grab active:cursor-grabbing')}
      >
        <CardBody issue={issue} epicName={epicName} />
      </div>
    </li>
  );
}

function Column({ id, status, items, epicNames, onOpen, showHeader, wip }: { id: string; status: StatusName; items: Issue[]; epicNames: Map<string, string>; onOpen: (k: string) => void; showHeader: boolean; wip: number }) {
  const { setNodeRef, isOver } = useDroppable({ id, data: { container: id } });
  return (
    <section aria-label={`${status}, ${plural(items.length, 'issue')}`} className={clsx('flex min-w-0 flex-col rounded-l bg-raised transition-colors duration-150', isOver && 'bg-active')}>
      {showHeader ? (
        <header className="sticky top-0 z-10 flex h-10 items-center gap-2 rounded-t-l bg-inherit px-3">
          <h2 className="text-sm font-semibold text-muted">{status}</h2>
          <span className="num text-sm text-muted">{wip}</span>
        </header>
      ) : null}
      <SortableContext id={id} items={items.map((i) => i.key)} strategy={verticalListSortingStrategy}>
        <ul ref={setNodeRef} className={clsx('flex min-h-[88px] flex-1 flex-col gap-2 px-2 pb-2', !showHeader && 'pt-2')}>
          {items.map((i) => (
            <SortableCard key={i.key} issue={i} container={id} epicName={epicNames.get(i.key)} onOpen={onOpen} />
          ))}
        </ul>
      </SortableContext>
    </section>
  );
}

// ---------------------------------------------------------------- page

export default function BoardPage() {
  const t = useTracker();
  const { open } = useIssueParam();
  const move = useMoveIssue();
  const [groupBy, setGroupBy] = usePersisted<GroupBy>('harborline.board.group', 'none');
  const [mine, setMine] = useState(false);
  const [overdue, setOverdue] = useState(false);
  const [week, setWeek] = useState(false);
  const [type, setType] = useState('all');
  const [epic, setEpic] = useState('all');
  const [text, setText] = useState('');
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [completeOpen, setCompleteOpen] = useState(false);
  const sprint = t.active;

  const sprintIssues = useMemo(() => t.issues.filter((i) => sprint && i.sprint_id === sprint.id && i.type !== 'Epic'), [t.issues, sprint]);
  const visible = useMemo(() => {
    const q = text.trim().toLowerCase();
    return sprintIssues
      .filter((i) => !mine || i.assignee === t.me)
      .filter((i) => !overdue || dueState(i.due_date, i.status === 'Done') === 'overdue')
      .filter((i) => !week || (inThisWeek(i.due_date) && i.status !== 'Done'))
      .filter((i) => type === 'all' || i.type === type)
      .filter((i) => epic === 'all' || i.epic_key === epic)
      .filter((i) => !q || i.summary.toLowerCase().includes(q) || i.key.toLowerCase().includes(q))
      .sort(compareRank);
  }, [sprintIssues, mine, overdue, week, type, epic, text, t.me]);

  // The epic tag only helps when the sprint spans more than one epic.
  const epicNames = useMemo(() => {
    const m = new Map<string, string>();
    const several = new Set(sprintIssues.map((i) => i.epic_key)).size > 1;
    if (groupBy !== 'epic' && several) for (const i of visible) if (i.epic_key) m.set(i.key, epicShort(t.byKey.get(i.epic_key)?.summary ?? i.epic_key));
    return m;
  }, [visible, sprintIssues, groupBy, t.byKey]);

  const lanes: Lane[] = useMemo(() => {
    if (groupBy === 'none') return [{ id: 'all', title: null, items: visible }];
    if (groupBy === 'epic') {
      const keys = [...new Set(visible.map((i) => i.epic_key ?? 'none'))];
      return keys
        .map((k) => ({ id: k, title: k === 'none' ? 'Issues without an epic' : t.byKey.get(k)?.summary ?? k, issue: t.byKey.get(k), items: visible.filter((i) => (i.epic_key ?? 'none') === k) }))
        .sort((a, b) => (a.id === 'none' ? 1 : b.id === 'none' ? -1 : Number(a.id.split('-')[1]) - Number(b.id.split('-')[1])));
    }
    const storyLanes: Lane[] = [];
    const placed = new Set<string>();
    for (const s of visible.filter((i) => i.type === 'Story').concat(sprintIssues.filter((i) => i.type === 'Story' && !visible.includes(i)))) {
      const kids = visible.filter((i) => i.parent_key === s.key);
      if (!kids.length) continue;
      kids.forEach((k) => placed.add(k.key));
      placed.add(s.key);
      storyLanes.push({ id: s.key, title: s.summary, issue: s, items: kids });
    }
    // Tasks whose story sits in another sprint (carry-over) get a lane named after that story.
    for (const i of visible.filter((x) => !placed.has(x.key) && x.parent_key)) {
      const lane = storyLanes.find((l) => l.id === i.parent_key);
      if (lane) lane.items.push(i);
      else storyLanes.push({ id: i.parent_key!, title: t.byKey.get(i.parent_key!)?.summary ?? i.parent_key!, issue: t.byKey.get(i.parent_key!), items: [i] });
      placed.add(i.key);
    }
    const rest = visible.filter((i) => !placed.has(i.key));
    return rest.length ? [...storyLanes, { id: 'none', title: 'Other issues', items: rest }] : storyLanes;
  }, [groupBy, visible, sprintIssues, t.byKey]);

  // Column contents by container id, with a local copy while a card is in flight.
  const base = useMemo(() => {
    const m: Record<string, string[]> = {};
    for (const l of lanes) for (const s of COLUMNS) m[cid(l.id, s)] = l.items.filter((i) => i.status === s).map((i) => i.key);
    return m;
  }, [lanes]);
  const [draft, setDraft] = useState<Record<string, string[]> | null>(null);
  const [dragging, setDragging] = useState<string | null>(null);
  const cols = draft ?? base;
  useEffect(() => {
    if (!dragging) setDraft(null);
  }, [base, dragging]);

  const order = useMemo(() => lanes.filter((l) => !collapsed.has(l.id)).flatMap((l) => COLUMNS.flatMap((s) => cols[cid(l.id, s)] ?? [])), [lanes, cols, collapsed]);
  useIssueNav(order, t.me);

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates, keyboardCodes: { start: ['Space'], cancel: ['Escape'], end: ['Space', 'Enter'] } }),
  );

  const findContainer = (id: string) => (id in cols ? id : Object.keys(cols).find((c) => cols[c].includes(id)));

  const onDragStart = (e: DragStartEvent) => {
    setDragging(String(e.active.id));
    setDraft(base);
  };
  const onDragOver = (e: DragOverEvent) => {
    const { active, over } = e;
    if (!over) return;
    const from = findContainer(String(active.id));
    const to = findContainer(String(over.id));
    if (!from || !to || from === to) return;
    if (parseCid(from).lane !== parseCid(to).lane) return; // cards stay in their swimlane
    setDraft((prev) => {
      const p = prev ?? base;
      const src = p[from].filter((k) => k !== active.id);
      const dst = [...p[to]];
      const overIdx = dst.indexOf(String(over.id));
      dst.splice(overIdx < 0 ? dst.length : overIdx, 0, String(active.id));
      return { ...p, [from]: src, [to]: dst };
    });
  };
  const onDragEnd = (e: DragEndEvent) => {
    const key = String(e.active.id);
    const over = e.over ? String(e.over.id) : null;
    const container = findContainer(key);
    setDragging(null);
    if (!over || !container) {
      setDraft(null);
      return;
    }
    const list = [...(cols[container] ?? [])];
    const from = list.indexOf(key);
    const overIdx = over === container ? list.length - 1 : list.indexOf(over);
    if (from >= 0 && overIdx >= 0 && from !== overIdx) {
      list.splice(from, 1);
      list.splice(overIdx, 0, key);
    }
    setDraft((p) => ({ ...(p ?? base), [container]: list }));
    const issue = t.byKey.get(key);
    const status = parseCid(container).status;
    const idx = list.indexOf(key);
    const before = list[idx + 1];
    const after = before ? undefined : list[idx - 1];
    const statusChanged = issue && issue.status !== status;
    const original = Object.keys(base).find((c) => base[c].includes(key));
    const rankChanged = !original || base[original].join() !== list.join() || original !== container;
    if (!statusChanged && !rankChanged) return;
    const previous = issue?.status;
    move.mutate(
      { key, before, after, patch: statusChanged ? { status } : undefined },
      {
        onSuccess: () => {
          if (!statusChanged || !previous) return;
          toast.success(`${key} moved to ${status}`, {
            duration: 4000,
            action: { label: 'Undo', onClick: () => move.mutate({ key, patch: { status: previous } }) },
          });
        },
        onError: (err) => toast.error(`${key} did not move`, { description: err.message }),
        onSettled: () => setDraft(null),
      },
    );
  };

  const announcements: Announcements = {
    onDragStart: ({ active }) => `Picked up ${active.id}. Use arrow keys to move between columns, Space to drop, Escape to cancel.`,
    onDragOver: ({ active, over }) => (over ? `${active.id} is over ${parseCid(findContainer(String(over.id)) ?? '').status ?? 'a column'}.` : `${active.id} is not over a column.`),
    onDragEnd: ({ active, over }) => (over ? `${active.id} dropped in ${parseCid(findContainer(String(over.id)) ?? '').status}.` : `${active.id} dropped back where it was.`),
    onDragCancel: ({ active }) => `Moving ${active.id} was cancelled.`,
  };

  if (t.issuesQ.isError) return <div className="p-6"><ErrorNote error={t.issuesQ.error} onRetry={() => void t.issuesQ.refetch()} what="the board" /></div>;

  const filtersOn = mine || overdue || week || type !== 'all' || epic !== 'all' || !!text;
  const doneH = sprintIssues.filter((i) => isWorkItem(i) && i.status === 'Done').reduce((s, i) => s + (i.estimate_hours ?? 0), 0);
  const allH = sprintIssues.filter(isWorkItem).reduce((s, i) => s + (i.estimate_hours ?? 0), 0);
  const overdueCount = sprintIssues.filter((i) => dueState(i.due_date, i.status === 'Done') === 'overdue').length;
  const daysLeft = workingDaysLeft(sprint?.end_date ?? null);
  const sprintEpics = [...new Set(sprintIssues.map((i) => i.epic_key).filter(Boolean))] as string[];
  const activeIssue = dragging ? t.byKey.get(dragging) : null;
  const chip = (on: boolean) =>
    clsx('press inline-flex h-8 items-center gap-1.5 rounded-m border px-2.5 text-base transition-colors duration-150', on ? 'border-accent bg-accent-tint font-medium text-accent-strong' : 'border-control bg-sheet text-ink hover:bg-hover');

  return (
    <div className="h-full overflow-auto">
      <div className="min-w-[960px] px-6 pb-10 pt-5">
        <div className="mb-1 text-sm text-muted">
          <Link to="/tracker/backlog" className="hover:text-ink hover:underline">
            Harborline Tracker
          </Link>{' '}
          / Board
        </div>
        <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
          <div className="min-w-0">
            <h1 className="figure text-[1.75rem] font-semibold leading-8 text-ink">{sprint ? sprint.name : 'Board'}</h1>
            {sprint ? (
              <p className="num mt-1 max-w-[90ch] text-md text-muted">
                {fmtDay(sprint.start_date)} – {fmtDay(sprint.end_date)}
                {daysLeft != null ? ` · ${plural(daysLeft, 'working day')} left` : ''} · {fmtHours(allH - doneH)} of {fmtHours(allH)} still to do
                {overdueCount ? (
                  <>
                    {' · '}
                    <button type="button" onClick={() => setOverdue(true)} className="font-medium text-bad hover:underline">
                      {plural(overdueCount, 'issue')} overdue
                    </button>
                  </>
                ) : null}
              </p>
            ) : null}
            {sprint?.goal ? <p className="mt-0.5 text-base text-muted">Goal: {sprint.goal}</p> : null}
          </div>
          {sprint ? (
            <div className="flex items-center gap-2">
              <Select
                ariaLabel="Swimlanes"
                value={groupBy}
                onValueChange={(v) => setGroupBy(v as GroupBy)}
                className="w-44"
                options={[
                  { value: 'none', label: 'No swimlanes' },
                  { value: 'epic', label: 'Swimlanes by epic' },
                  { value: 'story', label: 'Swimlanes by story' },
                ]}
              />
              <Button onClick={() => setCompleteOpen(true)}>Complete sprint</Button>
              <Button variant="primary" icon={<Plus aria-hidden />} onClick={() => ui.set({ createIssue: { sprint_id: sprint.id } })}>
                Create issue
              </Button>
            </div>
          ) : null}
        </div>

        {!t.issuesQ.data ? (
          <div className="mt-6 grid grid-cols-4 gap-3">
            {COLUMNS.map((c) => (
              <Skeleton key={c} className="h-96 rounded-l" />
            ))}
          </div>
        ) : !sprint ? (
          <div className="mt-6">
            <Empty action={<Link to="/tracker/backlog" className={buttonClasses('primary')}>Go to the backlog</Link>}>
              No sprint is running. Start {t.nextFuture ? t.nextFuture.name : 'the next sprint'} from the backlog to fill this board.
            </Empty>
          </div>
        ) : (
          <>
            <div className="mt-5 flex flex-wrap items-center gap-2" role="toolbar" aria-label="Quick filters">
              <div className="w-52">
                <Input leading={<Search aria-hidden />} value={text} onChange={(e) => setText(e.target.value)} placeholder="Search this board" aria-label="Search this board" />
              </div>
              <button type="button" aria-pressed={mine} className={chip(mine)} onClick={() => setMine((v) => !v)}>
                <Avatar person={t.person(t.me)} size={20} me />
                Only my issues
              </button>
              <button type="button" aria-pressed={overdue} className={chip(overdue)} onClick={() => setOverdue((v) => !v)}>
                Overdue
              </button>
              <button type="button" aria-pressed={week} className={chip(week)} onClick={() => setWeek((v) => !v)}>
                {workWeek().upcoming ? 'Due next week' : 'Due this week'}
              </button>
              <Select
                ariaLabel="Issue type"
                value={type}
                onValueChange={setType}
                className="w-32"
                options={[{ value: 'all', label: 'All types' }, ...(['Story', 'Task', 'Bug', 'Spike'] as const).map((x) => ({ value: x, label: x }))]}
              />
              <Select
                ariaLabel="Epic"
                value={epic}
                onValueChange={setEpic}
                className="w-52"
                options={[{ value: 'all', label: 'All epics' }, ...sprintEpics.map((k) => ({ value: k, label: epicShort(t.byKey.get(k)?.summary ?? k) }))]}
              />
              {filtersOn ? (
                <Button
                  variant="quiet"
                  icon={<X aria-hidden />}
                  onClick={() => {
                    setMine(false);
                    setOverdue(false);
                    setWeek(false);
                    setType('all');
                    setEpic('all');
                    setText('');
                  }}
                >
                  Clear
                </Button>
              ) : null}
              <span className="flex-1" />
              <span className="num text-sm text-muted">
                {filtersOn ? `${visible.length} of ${plural(sprintIssues.length, 'issue')}` : plural(sprintIssues.length, 'issue')}
              </span>
            </div>

            <DndContext
              sensors={sensors}
              collisionDetection={containerCollision(cols)}
              onDragStart={onDragStart}
              onDragOver={onDragOver}
              onDragEnd={onDragEnd}
              onDragCancel={() => {
                setDragging(null);
                setDraft(null);
              }}
              accessibility={{ announcements, screenReaderInstructions: { draggable: 'Press Space to pick up the card, arrow keys to move it, Space to drop. Enter opens the issue.' } }}
            >
              {groupBy !== 'none' ? (
                <div className="sticky top-0 z-20 mt-4 grid grid-cols-4 gap-3 bg-sheet pb-2 pt-1">
                  {COLUMNS.map((s) => (
                    <div key={s} className="flex h-8 items-center gap-2 rounded-m bg-raised px-3">
                      <span className="text-sm font-semibold text-muted">{s}</span>
                      <span className="num text-sm text-muted">{lanes.reduce((n, l) => n + (cols[cid(l.id, s)]?.length ?? 0), 0)}</span>
                    </div>
                  ))}
                </div>
              ) : null}
              {visible.length === 0 ? (
                <div className="mt-4">
                  <Empty>No issues in this sprint match the filters.</Empty>
                </div>
              ) : (
                <div className={clsx('flex flex-col', groupBy === 'none' ? 'mt-4' : 'gap-4')}>
                  {lanes.map((lane) => {
                    const isCollapsed = collapsed.has(lane.id);
                    return (
                      <div key={lane.id}>
                        {lane.title ? (
                          <button
                            type="button"
                            aria-expanded={!isCollapsed}
                            onClick={() =>
                              setCollapsed((prev) => {
                                const n = new Set(prev);
                                if (n.has(lane.id)) n.delete(lane.id);
                                else n.add(lane.id);
                                return n;
                              })
                            }
                            className="mb-2 flex w-full items-center gap-2 rounded-m px-1 py-1 text-left hover:bg-hover"
                          >
                            <ChevronDown className={clsx('size-4 text-muted transition-transform duration-200 ease-out', isCollapsed && '-rotate-90')} aria-hidden />
                            {lane.issue ? <TypeIcon type={lane.issue.type} /> : null}
                            {lane.issue ? <IssueKey k={lane.issue.key} /> : null}
                            <span className="min-w-0 truncate text-base font-semibold text-ink">{lane.title}</span>
                            <span className="num shrink-0 text-sm text-muted">{plural(lane.items.length, 'issue')}</span>
                            {lane.issue && lane.issue.type !== 'Epic' ? (
                              <Tooltip content={`Story status: ${lane.issue.status}`}>
                                <span className="ml-1 text-xs text-muted">· {lane.issue.status}</span>
                              </Tooltip>
                            ) : null}
                          </button>
                        ) : null}
                        {!isCollapsed ? (
                          <div className="grid grid-cols-4 gap-3">
                            {COLUMNS.map((s) => {
                              const id = cid(lane.id, s);
                              const items = (cols[id] ?? []).map((k) => t.byKey.get(k)).filter((x): x is Issue => !!x);
                              return <Column key={s} id={id} status={s} items={items} epicNames={epicNames} onOpen={open} showHeader={groupBy === 'none'} wip={items.length} />;
                            })}
                          </div>
                        ) : null}
                      </div>
                    );
                  })}
                </div>
              )}
              <DragOverlay dropAnimation={{ duration: 180, easing: 'cubic-bezier(0.23, 1, 0.32, 1)' }}>
                {activeIssue ? (
                  // dnd-kit measures the overlay's first child: keep the tilt on an inner element so it never skews collisions.
                  <div>
                    <div className={clsx(cardClass, 'lift cursor-grabbing border-control shadow-drag')}>
                      <CardBody issue={activeIssue} epicName={epicNames.get(activeIssue.key)} />
                    </div>
                  </div>
                ) : null}
              </DragOverlay>
            </DndContext>
            <p className="mt-4 text-sm text-muted">
              Drag cards between columns to change status, or within a column to rank. With the keyboard: J and K move, Space picks up and drops, Enter opens.
            </p>
          </>
        )}
      </div>
      {sprint ? <CompleteSprintDialog sprint={sprint} open={completeOpen} onOpenChange={setCompleteOpen} /> : null}
    </div>
  );
}
