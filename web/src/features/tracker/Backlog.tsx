import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react';
import clsx from 'clsx';
import {
  DndContext,
  DragOverlay,
  KeyboardSensor,
  PointerSensor,
  useDroppable,
  useSensor,
  useSensors,
  type DragEndEvent,
  type DragOverEvent,
  type DragStartEvent,
} from '@dnd-kit/core';
import { SortableContext, sortableKeyboardCoordinates, useSortable, verticalListSortingStrategy } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { ChevronDown, Download, Ellipsis, FileDown, Plus, Search, Upload } from 'lucide-react';
import { toast } from 'sonner';
import { tracker } from '@/api/client';
import { compareRank, useMoveIssue, useRestore } from '@/api/queries';
import type { Issue, Sprint } from '@/api/types';
import { Button, IconButton } from '@/components/ui/button';
import { Input, Segmented, Select } from '@/components/ui/field';
import { download, Empty, ErrorNote, Skeleton } from '@/components/ui/misc';
import { Dialog, Menu, MenuContent, MenuItem, MenuTrigger, Tooltip } from '@/components/ui/overlay';
import { fmtDay, toDay, today } from '@/lib/dates';
import { fmtHours, fmtInt, plural } from '@/lib/format';
import { ui, usePersisted } from '@/lib/store';
import { Avatar, DueBadge, Estimate, IssueKey, StatusLozenge, TypeIcon } from './atoms';
import { SPRINT_CAPACITY_HOURS, compareDue, epicShort, insertByDue, sprintHours, useIssueParam, useTracker } from './data';
import { containerCollision, holdStill } from './dnd';
import { YouAreHere } from './here';
import { useIssueNav } from './nav';
import { CompleteSprintDialog, StartSprintDialog } from './SprintDialogs';

const BACKLOG = 'backlog';

function Row({ issue, container, epicName, onOpen, overlay }: { issue: Issue; container: string; epicName?: string; onOpen?: (k: string) => void; overlay?: boolean }) {
  const t = useTracker();
  const sortable = useSortable({ id: issue.key, data: { container }, disabled: overlay });
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = sortable;
  return (
    <li ref={overlay ? undefined : setNodeRef} style={overlay ? undefined : { transform: CSS.Translate.toString(transform), transition }} className={clsx('list-none', isDragging && 'opacity-40')}>
      <div
        {...(overlay ? {} : attributes)}
        {...(overlay ? {} : listeners)}
        data-issue-key={overlay ? undefined : issue.key}
        aria-label={`${issue.key} ${issue.summary}`}
        onClick={() => onOpen?.(issue.key)}
        onKeyDown={(e: ReactKeyboardEvent) => {
          if (e.key === 'Enter') {
            e.preventDefault();
            onOpen?.(issue.key);
            return;
          }
          listeners?.onKeyDown?.(e);
        }}
        className={clsx(
          'grid h-10 cursor-grab grid-cols-[16px_76px_minmax(0,1fr)_auto] items-center gap-2.5 border-t border-rule bg-sheet px-3 outline-none transition-colors duration-100',
          'hover:bg-hover/70 focus-visible:relative focus-visible:z-10 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-focus active:cursor-grabbing',
          overlay && 'lift rounded-m border border-control shadow-drag',
        )}
      >
        <TypeIcon type={issue.type} />
        <IssueKey k={issue.key} done={issue.status === 'Done'} />
        <span className={clsx('truncate text-base', issue.status === 'Done' ? 'text-muted' : 'text-ink')}>{issue.summary}</span>
        <span className="flex items-center gap-2">
          {epicName ? <span className="hidden max-w-44 truncate rounded-s bg-active px-1.5 text-xs leading-5 text-muted xl:inline">{epicName}</span> : null}
          <span className="flex w-[104px] justify-end">
            <DueBadge issue={issue} compact />
          </span>
          <StatusLozenge status={issue.status} className="w-[84px] justify-center" />
          <span className="flex w-10 justify-end">
            <Estimate issue={issue} />
          </span>
          <Avatar person={t.person(issue.assignee)} size={24} me={issue.assignee === t.me} />
        </span>
      </div>
    </li>
  );
}

function Capacity({ hours }: { hours: number }) {
  const over = hours > SPRINT_CAPACITY_HOURS;
  const pct = Math.min(1, hours / SPRINT_CAPACITY_HOURS);
  return (
    <Tooltip content={`Estimates add up to ${fmtHours(hours)}. One engineer has ${SPRINT_CAPACITY_HOURS} h in a two-week sprint.`}>
      <span className="flex items-center gap-2">
        <span className="h-1.5 w-24 overflow-hidden rounded-full bg-active" aria-hidden>
          <span className={clsx('block h-full rounded-full', over ? 'bg-bad' : 'bg-accent')} style={{ width: `${pct * 100}%` }} />
        </span>
        <span className={clsx('num text-sm', over ? 'font-semibold text-bad' : 'text-muted')}>
          {fmtInt(hours)} / {SPRINT_CAPACITY_HOURS} h{over ? ` · ${fmtInt(hours - SPRINT_CAPACITY_HOURS)} h over` : ''}
        </span>
      </span>
    </Tooltip>
  );
}

function Counts({ items }: { items: Issue[] }) {
  const c = (s: Issue['status']) => items.filter((i) => i.status === s).length;
  const cells: Array<[string, number, string]> = [
    ['To Do', c('To Do'), 'bg-active text-muted'],
    ['In progress or review', c('In Progress') + c('In Review'), 'bg-info-bg text-info'],
    ['Done', c('Done'), 'bg-good-bg text-good'],
  ];
  return (
    <span className="flex items-center gap-1">
      {cells.map(([label, n, cls]) => (
        <Tooltip key={label} content={`${label}: ${n}`}>
          <span className={clsx('num inline-flex h-5 min-w-6 items-center justify-center rounded-full px-1.5 text-xs font-semibold', cls)}>{n}</span>
        </Tooltip>
      ))}
    </span>
  );
}

function Section({
  container,
  title,
  sprint,
  items,
  collapsed,
  onToggle,
  epicNames,
  onOpen,
  canStart,
  onStart,
  onComplete,
  byDue,
}: {
  container: string;
  title: string;
  sprint?: Sprint;
  items: Issue[];
  collapsed: boolean;
  onToggle: () => void;
  epicNames: Map<string, string>;
  onOpen: (k: string) => void;
  canStart?: boolean;
  onStart?: () => void;
  onComplete?: () => void;
  byDue: boolean;
}) {
  const { setNodeRef, isOver } = useDroppable({ id: container, data: { container } });
  const hours = sprintHours(items);
  const severalEpics = new Set(items.map((i) => i.epic_key)).size > 1;
  return (
    <section ref={setNodeRef} aria-label={title} className={clsx('rounded-l border bg-raised transition-colors duration-150', isOver ? 'border-accent' : 'border-line')}>
      <header className="flex min-h-12 flex-wrap items-center gap-x-4 gap-y-1 px-3 py-1.5">
        <button type="button" onClick={onToggle} aria-expanded={!collapsed} className="flex min-w-0 items-center gap-2 rounded-m py-1 pr-2 text-left hover:bg-hover">
          <ChevronDown className={clsx('size-4 shrink-0 text-muted transition-transform duration-200 ease-out', collapsed && '-rotate-90')} aria-hidden />
          <h2 className="truncate text-base font-semibold text-ink">{title}</h2>
          {sprint?.state === 'active' ? <span className="rounded-s bg-accent-tint px-1.5 text-xs font-semibold leading-5 text-accent-strong">Active</span> : null}
          {sprint?.start_date ? (
            <span className="num shrink-0 text-sm text-muted">
              {fmtDay(sprint.start_date)} – {fmtDay(sprint.end_date)}
            </span>
          ) : null}
          <span className="num shrink-0 text-sm text-muted">({plural(items.length, 'issue')})</span>
        </button>
        <span className="flex-1" />
        {sprint ? <Capacity hours={hours} /> : <span className="num text-sm text-muted">{fmtHours(hours)} estimated</span>}
        <Counts items={items} />
        {sprint?.state === 'active' ? (
          <Button size="sm" onClick={onComplete}>
            Complete sprint
          </Button>
        ) : sprint?.state === 'future' ? (
          <Tooltip content={canStart ? undefined : 'Complete the active sprint first'}>
            <span>
              <Button size="sm" disabled={!canStart} onClick={onStart}>
                Start sprint
              </Button>
            </span>
          </Tooltip>
        ) : null}
      </header>
      {sprint?.goal && !collapsed ? <p className="-mt-1 px-10 pb-2 text-sm text-muted">Goal: {sprint.goal}</p> : null}
      {!collapsed ? (
        <SortableContext id={container} items={items.map((i) => i.key)} strategy={byDue ? holdStill : verticalListSortingStrategy}>
          <ul className="overflow-hidden rounded-b-l">
            {items.map((i) => (
              <Row key={i.key} issue={i} container={container} epicName={severalEpics ? epicNames.get(i.key) : undefined} onOpen={onOpen} />
            ))}
            {items.length === 0 ? (
              <li className="list-none border-t border-rule bg-sheet px-4 py-3 text-base text-muted">
                {sprint ? 'Plan this sprint by dragging issues here from the backlog or another sprint.' : 'The backlog is empty. Issues you take out of a sprint land here.'}
              </li>
            ) : null}
          </ul>
        </SortableContext>
      ) : null}
    </section>
  );
}

function RestoreDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (v: boolean) => void }) {
  const restore = useRestore();
  const [file, setFile] = useState<{ name: string; data: unknown; issues: number } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (open) {
      setFile(null);
      setErr(null);
    }
  }, [open]);
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="Restore the tracker from a backup"
      description="Replaces every issue, sprint, comment and work-log entry with the contents of the file. The simulator data is not touched."
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            variant="danger-solid"
            disabled={!file}
            loading={restore.isPending}
            onClick={() =>
              file &&
              restore.mutate(file.data, {
                onSuccess: () => {
                  onOpenChange(false);
                  toast.success('Tracker restored', { description: `${fmtInt(file.issues)} issues loaded from ${file.name}` });
                },
                onError: (e) => toast.error('Restore failed', { description: e.message }),
              })
            }
          >
            Replace tracker data
          </Button>
        </>
      }
    >
      <input
        ref={input}
        type="file"
        accept="application/json,.json"
        className="sr-only"
        onChange={async (e) => {
          const f = e.target.files?.[0];
          if (!f) return;
          try {
            const data = JSON.parse(await f.text()) as { issues?: unknown[] };
            if (!Array.isArray(data.issues)) throw new Error('This file has no issues list. Choose a file made with Download backup.');
            setFile({ name: f.name, data, issues: data.issues.length });
            setErr(null);
          } catch (x) {
            setFile(null);
            setErr(x instanceof Error ? x.message : String(x));
          }
        }}
      />
      <div className="flex flex-wrap items-center gap-3">
        <Button icon={<Upload aria-hidden />} onClick={() => input.current?.click()}>
          Choose backup file
        </Button>
        <span className="text-base text-muted">{file ? `${file.name} · ${plural(file.issues, 'issue')}` : 'A .json file from Download backup'}</span>
      </div>
      {err ? <p className="mt-3 text-sm text-bad">{err}</p> : null}
    </Dialog>
  );
}

export default function BacklogPage() {
  const t = useTracker();
  const { open } = useIssueParam();
  const move = useMoveIssue();
  const [text, setText] = useState('');
  const [type, setType] = useState('all');
  const [epic, setEpic] = useState('all');
  const [showClosed, setShowClosed] = useState(false);
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const [starting, setStarting] = useState<Sprint | null>(null);
  const [completing, setCompleting] = useState<Sprint | null>(null);
  const [restoreOpen, setRestoreOpen] = useState(false);
  const [listOrder, setListOrder] = usePersisted<'rank' | 'due'>('harborline.backlog.order', 'rank');
  const byDue = listOrder === 'due';
  const initialised = useRef(false);

  const sprints = useMemo(() => [...t.sprints].sort((a, b) => a.id - b.id), [t.sprints]);
  useEffect(() => {
    if (initialised.current || !sprints.length) return;
    initialised.current = true;
    const firstOpen = sprints.findIndex((s) => s.state !== 'closed');
    setCollapsed(new Set(sprints.filter((_, i) => i > firstOpen + 1).map((s) => String(s.id))));
  }, [sprints]);

  const epicNames = useMemo(() => {
    const m = new Map<string, string>();
    for (const i of t.issues) if (i.epic_key) m.set(i.key, epicShort(t.byKey.get(i.epic_key)?.summary ?? i.epic_key));
    return m;
  }, [t.issues, t.byKey]);

  const filtered = useMemo(() => {
    const q = text.trim().toLowerCase();
    return t.issues
      .filter((i) => i.type !== 'Epic')
      .filter((i) => type === 'all' || i.type === type)
      .filter((i) => epic === 'all' || i.epic_key === epic)
      .filter((i) => !q || i.summary.toLowerCase().includes(q) || i.key.toLowerCase().includes(q))
      .sort(byDue ? compareDue : compareRank);
  }, [t.issues, text, type, epic, byDue]);

  const shown = sprints.filter((s) => showClosed || s.state !== 'closed');
  const base = useMemo(() => {
    const m: Record<string, string[]> = { [BACKLOG]: [] };
    for (const s of sprints) m[String(s.id)] = [];
    for (const i of filtered) (m[i.sprint_id == null ? BACKLOG : String(i.sprint_id)] ??= []).push(i.key);
    return m;
  }, [filtered, sprints]);
  const [draft, setDraft] = useState<Record<string, string[]> | null>(null);
  const [dragging, setDragging] = useState<string | null>(null);
  const cols = draft ?? base;
  useEffect(() => {
    if (!dragging) setDraft(null);
  }, [base, dragging]);

  const containers = [...shown.map((s) => String(s.id)), BACKLOG];
  const order = containers.filter((c) => !collapsed.has(c)).flatMap((c) => cols[c] ?? []);
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
    setDraft((prev) => {
      const p = prev ?? base;
      if (byDue) return { ...p, [from]: p[from].filter((k) => k !== active.id), [to]: insertByDue(p[to], String(active.id), t.byKey) };
      const dst = [...p[to]];
      const idx = dst.indexOf(String(over.id));
      dst.splice(idx < 0 ? dst.length : idx, 0, String(active.id));
      return { ...p, [from]: p[from].filter((k) => k !== active.id), [to]: dst };
    });
  };
  const onDragEnd = (e: DragEndEvent) => {
    const key = String(e.active.id);
    const over = e.over ? String(e.over.id) : null;
    const container = findContainer(key);
    setDragging(null);
    if (!over || !container) return setDraft(null);
    // In due-date order the date decides the position: a drag can move a ticket between sprints, not rank it.
    if (byDue && (t.byKey.get(key)?.sprint_id ?? null) === (container === BACKLOG ? null : Number(container))) {
      setDraft(null);
      if (over !== key) toast.message('This list is in due-date order', { description: 'Switch the order to Manual to rank by hand.' });
      return;
    }
    const list = [...cols[container]];
    const from = list.indexOf(key);
    const to = over === container ? list.length - 1 : list.indexOf(over);
    if (from >= 0 && to >= 0 && from !== to) {
      list.splice(from, 1);
      list.splice(to, 0, key);
    }
    setDraft((p) => ({ ...(p ?? base), [container]: list }));
    const idx = list.indexOf(key);
    const before = list[idx + 1];
    const after = before ? undefined : list[idx - 1];
    const issue = t.byKey.get(key);
    const sprintId = container === BACKLOG ? null : Number(container);
    const sprintChanged = issue && (issue.sprint_id ?? null) !== sprintId;
    const original = Object.keys(base).find((c) => base[c].includes(key));
    if (!sprintChanged && original && base[original].join() === list.join()) return;
    const label = sprintId == null ? 'the backlog' : sprints.find((s) => s.id === sprintId)?.name;
    const previous = issue?.sprint_id ?? null;
    move.mutate(
      { key, before: byDue ? undefined : before, after: byDue ? undefined : after, patch: sprintChanged ? { sprint_id: sprintId } : undefined },
      {
        onSuccess: () => {
          if (!sprintChanged) return;
          toast.success(`${key} moved to ${label}`, {
            duration: 4000,
            action: { label: 'Undo', onClick: () => move.mutate({ key, patch: { sprint_id: previous } }) },
          });
        },
        onError: (err) => toast.error(`${key} did not move`, { description: err.message }),
        onSettled: () => setDraft(null),
      },
    );
  };

  const toggle = (c: string) =>
    setCollapsed((prev) => {
      const n = new Set(prev);
      if (n.has(c)) n.delete(c);
      else n.add(c);
      return n;
    });

  const items = (c: string) => (cols[c] ?? []).map((k) => t.byKey.get(k)).filter((x): x is Issue => !!x);
  const activeIssue = dragging ? t.byKey.get(dragging) : null;
  const planned = sprints.filter((s) => s.state === 'future');
  const lastPlanned = planned.at(-1);
  const backlogCount = t.issues.filter((i) => i.sprint_id == null && i.type !== 'Epic').length;
  const filtersOn = !!text || type !== 'all' || epic !== 'all';

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-[1360px] px-6 pb-12 pt-5">
        <div className="mb-1 text-sm text-muted">Harborline Tracker / Backlog</div>
        <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
          <div>
            <h1 className="figure text-[1.75rem] font-semibold leading-8 text-ink">Backlog</h1>
            <p className="num mt-1 text-md text-muted">
              {t.active ? `${t.active.name} is running. ` : 'No sprint is running. '}
              {planned.length ? `${plural(planned.length, 'more sprint')} planned to ${fmtDay(lastPlanned?.end_date, true)}. ` : ''}
              {plural(backlogCount, 'issue')} in the backlog.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <Menu>
              <MenuTrigger asChild>
                <IconButton label="Export and backup" variant="secondary">
                  <Ellipsis aria-hidden />
                </IconButton>
              </MenuTrigger>
              <MenuContent>
                <MenuItem icon={<FileDown aria-hidden />} onSelect={() => void download(tracker.exportCsvUrl, 'harborline-jira-import.csv').then(() => toast.success('Jira CSV downloaded', { description: 'Import it in Jira Cloud: Settings, System, External system import, CSV.' })).catch((e: Error) => toast.error('Export failed', { description: e.message }))}>
                  Export Jira CSV
                </MenuItem>
                <MenuItem icon={<Download aria-hidden />} onSelect={() => void download(tracker.backupUrl, `harborline-tracker-backup-${toDay(today())}.json`).then(() => toast.success('Backup downloaded')).catch((e: Error) => toast.error('Backup failed', { description: e.message }))}>
                  Download backup
                </MenuItem>
                <MenuItem icon={<Upload aria-hidden />} onSelect={() => setRestoreOpen(true)}>
                  Restore from backup…
                </MenuItem>
              </MenuContent>
            </Menu>
            <Button variant="primary" icon={<Plus aria-hidden />} onClick={() => ui.set({ createIssue: { sprint_id: null } })}>
              Create issue
            </Button>
          </div>
        </div>

        <YouAreHere className="mt-4" />

        <div className="mt-5 flex flex-wrap items-center gap-2">
          <div className="w-52">
            <Input leading={<Search aria-hidden />} value={text} onChange={(e) => setText(e.target.value)} placeholder="Search the backlog" aria-label="Search the backlog" />
          </div>
          <Select ariaLabel="Issue type" value={type} onValueChange={setType} className="w-36" options={[{ value: 'all', label: 'All types' }, ...(['Story', 'Task', 'Bug', 'Spike'] as const).map((x) => ({ value: x, label: x }))]} />
          <Select ariaLabel="Epic" value={epic} onValueChange={setEpic} className="w-52" options={[{ value: 'all', label: 'All epics' }, ...t.epics.map((e) => ({ value: e.key, label: epicShort(e.summary) }))]} />
          <span className="flex items-center gap-2">
            <span className="text-sm text-muted">Order</span>
            <Segmented
              ariaLabel="Order issues by"
              value={listOrder}
              onValueChange={setListOrder}
              options={[
                { value: 'rank', label: 'Manual', title: 'The order you ranked them in' },
                { value: 'due', label: 'Due date', title: 'Overdue and soonest first, no due date last' },
              ]}
            />
          </span>
          <span className="flex-1" />
          {sprints.some((s) => s.state === 'closed') ? (
            <Button variant="quiet" aria-pressed={showClosed} onClick={() => setShowClosed((v) => !v)}>
              {showClosed ? 'Hide completed sprints' : `Show completed sprints (${sprints.filter((s) => s.state === 'closed').length})`}
            </Button>
          ) : null}
          {filtersOn ? <span className="num text-sm text-muted">{plural(filtered.length, 'issue')} match</span> : null}
        </div>

        {t.issuesQ.isError ? (
          <div className="mt-4">
            <ErrorNote error={t.issuesQ.error} onRetry={() => void t.issuesQ.refetch()} what="the backlog" />
          </div>
        ) : !t.issuesQ.data ? (
          <div className="mt-4 flex flex-col gap-3">
            {Array.from({ length: 3 }, (_, i) => (
              <Skeleton key={i} className="h-48 rounded-l" />
            ))}
          </div>
        ) : (
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
            accessibility={{ screenReaderInstructions: { draggable: 'Press Space to pick up the issue, arrow keys to move it between sprints, Space to drop. Enter opens it.' } }}
          >
            <div className="mt-4 flex flex-col gap-3">
              {shown.map((s) => (
                <Section
                  key={s.id}
                  container={String(s.id)}
                  title={s.name}
                  sprint={s}
                  items={items(String(s.id))}
                  collapsed={collapsed.has(String(s.id))}
                  onToggle={() => toggle(String(s.id))}
                  epicNames={epicNames}
                  onOpen={open}
                  canStart={!t.active && s.id === t.nextFuture?.id}
                  onStart={() => setStarting(s)}
                  onComplete={() => setCompleting(s)}
                  byDue={byDue}
                />
              ))}
              <Section container={BACKLOG} title="Backlog" items={items(BACKLOG)} collapsed={collapsed.has(BACKLOG)} onToggle={() => toggle(BACKLOG)} epicNames={epicNames} onOpen={open} byDue={byDue} />
              {filtered.length === 0 && filtersOn ? (
                <Empty
                  action={
                    <Button
                      size="sm"
                      onClick={() => {
                        setText('');
                        setType('all');
                        setEpic('all');
                      }}
                    >
                      Clear filters
                    </Button>
                  }
                >
                  No issues match these filters.
                </Empty>
              ) : null}
            </div>
            <DragOverlay dropAnimation={{ duration: 180, easing: 'cubic-bezier(0.23, 1, 0.32, 1)' }}>
              {activeIssue ? (
                <div>
                  <ul>
                    <Row issue={activeIssue} container="overlay" overlay epicName={epicNames.get(activeIssue.key)} />
                  </ul>
                </div>
              ) : null}
            </DragOverlay>
          </DndContext>
        )}
        <p className="num mt-4 text-sm text-muted">
          Capacity assumes one engineer at 40 h a week. Stories carry points; tasks, bugs and spikes carry hours.
        </p>
      </div>
      {starting ? <StartSprintDialog sprint={starting} open={!!starting} onOpenChange={(v) => !v && setStarting(null)} /> : null}
      {completing ? <CompleteSprintDialog sprint={completing} open={!!completing} onOpenChange={(v) => !v && setCompleting(null)} /> : null}
      <RestoreDialog open={restoreOpen} onOpenChange={setRestoreOpen} />
    </div>
  );
}
