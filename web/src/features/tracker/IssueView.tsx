import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react';
import { Link } from 'react-router';
import clsx from 'clsx';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { ChevronDown, ExternalLink, GitBranch, GitPullRequest, ImageOff, Link2, Maximize2, Paperclip, X } from 'lucide-react';
import { toast } from 'sonner';
import { checklist, tracker } from '@/api/client';
import { useAddComment, useAddWorklog, useHistory, useIssue, useUpdateIssue } from '@/api/queries';
import type { ChecklistItem, Issue, IssuePatch, StatusName } from '@/api/types';
import { Button, IconButton } from '@/components/ui/button';
import { Checkbox, Input, Label, Select, Textarea, controlClass } from '@/components/ui/field';
import { copyText, Empty, ErrorNote, Skeleton, Tabs } from '@/components/ui/misc';
import { Dialog, Menu, MenuContent, MenuItem, MenuTrigger } from '@/components/ui/overlay';
import { withBase } from '@/lib/base';
import { fmtDateTime, fmtDay, fmtRelative, toDay, today } from '@/lib/dates';
import { fmtHours } from '@/lib/format';
import { usePersisted } from '@/lib/store';
import { Avatar, DueBadge, IssueKey, PriorityIcon, STATUS_TONE, StatusLozenge, TypeIcon } from './atoms';
import { branchName, epicShort, isWorkItem, useTracker } from './data';

const STATUSES: StatusName[] = ['To Do', 'In Progress', 'In Review', 'Done'];

// ---------------------------------------------------------------- small pieces

function StatusMenu({ issue, onChange }: { issue: Issue; onChange: (s: StatusName) => void }) {
  return (
    <Menu>
      <MenuTrigger asChild>
        <button
          type="button"
          className={clsx(
            'press inline-flex h-8 items-center gap-1.5 rounded-m px-2.5 text-base font-semibold transition-[filter] hover:brightness-95 dark:hover:brightness-110',
            STATUS_TONE[issue.status],
          )}
          aria-label={`Status: ${issue.status}. Change status`}
        >
          {issue.status}
          <ChevronDown className="size-3.5" aria-hidden />
        </button>
      </MenuTrigger>
      <MenuContent align="start">
        {STATUSES.filter((s) => s !== issue.status).map((s) => (
          <MenuItem key={s} onSelect={() => onChange(s)}>
            <StatusLozenge status={s} />
          </MenuItem>
        ))}
      </MenuContent>
    </Menu>
  );
}

function SummaryEditor({ issue, startEditing, onSave, size }: { issue: Issue; startEditing: boolean; onSave: (s: string) => void; size: 'panel' | 'page' }) {
  const [editing, setEditing] = useState(startEditing);
  const [value, setValue] = useState(issue.summary);
  const ref = useRef<HTMLTextAreaElement>(null);
  useEffect(() => setValue(issue.summary), [issue.summary]);
  useEffect(() => {
    if (startEditing) setEditing(true);
  }, [startEditing, issue.key]);
  useEffect(() => {
    if (editing) {
      ref.current?.focus();
      ref.current?.select();
    }
  }, [editing]);
  const commit = () => {
    const v = value.trim();
    setEditing(false);
    if (v && v !== issue.summary) onSave(v);
    else setValue(issue.summary);
  };
  const cls = size === 'page' ? 'text-2xl font-semibold leading-8' : 'text-xl font-semibold leading-7';
  if (editing)
    return (
      <textarea
        ref={ref}
        value={value}
        rows={2}
        aria-label="Summary"
        onChange={(e) => setValue(e.target.value.replace(/\n/g, ' '))}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault();
            commit();
          }
          if (e.key === 'Escape') {
            e.stopPropagation();
            setValue(issue.summary);
            setEditing(false);
          }
        }}
        className={clsx(controlClass, cls, 'h-auto w-full resize-none px-2 py-1')}
      />
    );
  return (
    <h2 className={clsx(cls, 'text-ink [overflow-wrap:anywhere]')}>
      <button
        type="button"
        onClick={() => setEditing(true)}
        className="-mx-2 w-[calc(100%+16px)] rounded-m px-2 py-0.5 text-left transition-colors hover:bg-hover"
        title="Edit summary (E)"
      >
        {issue.summary}
      </button>
    </h2>
  );
}

function Section({ title, meta, children, actions }: { title: string; meta?: ReactNode; children: ReactNode; actions?: ReactNode }) {
  return (
    <section className="mt-7">
      <div className="mb-2.5 flex items-baseline justify-between gap-3">
        <h3 className="text-md font-semibold text-ink">
          {title} {meta ? <span className="num ml-1 text-sm font-normal text-muted">{meta}</span> : null}
        </h3>
        {actions}
      </div>
      {children}
    </section>
  );
}

function Checklist({ items, onToggle, label }: { items: ChecklistItem[]; onToggle: (idx: number, done: boolean) => void; label: string }) {
  const done = items.filter((i) => i.done).length;
  if (!items.length) return <Empty>No {label.toLowerCase()} on this issue.</Empty>;
  return (
    <div>
      <div className="mb-3 h-1.5 overflow-hidden rounded-full bg-active" role="progressbar" aria-label={`${label} progress`} aria-valuenow={done} aria-valuemin={0} aria-valuemax={items.length}>
        <div className="h-full origin-left rounded-full bg-good transition-transform duration-300 ease-out" style={{ transform: `scaleX(${done / items.length})` }} />
      </div>
      <ul className="flex flex-col gap-1">
        {items.map((c, idx) => {
          const id = `${label}-${idx}`;
          return (
            <li key={c.id ?? idx} className="flex items-start gap-2.5 rounded-m px-1.5 py-1 hover:bg-hover/60">
              <Checkbox id={id} checked={c.done} onCheckedChange={(v) => onToggle(idx, v)} className="mt-0.5" />
              <label htmlFor={id} className={clsx('prose-issue cursor-pointer text-base leading-6', c.done && 'text-muted line-through decoration-faint')}>
                <ReactMarkdown remarkPlugins={[remarkGfm]} allowedElements={['p', 'code', 'strong', 'em', 'a']} unwrapDisallowed>
                  {c.text}
                </ReactMarkdown>
              </label>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function IssueLine({ issue, onOpen, note }: { issue: Issue; onOpen: (k: string) => void; note?: ReactNode }) {
  const t = useTracker();
  return (
    <li>
      <button
        type="button"
        onClick={() => onOpen(issue.key)}
        className="flex h-10 w-full items-center gap-2.5 rounded-m px-2 text-left transition-colors hover:bg-hover"
      >
        {note ? <span className="w-24 shrink-0 text-sm text-muted">{note}</span> : null}
        <TypeIcon type={issue.type} />
        <IssueKey k={issue.key} done={issue.status === 'Done'} className="w-[68px] shrink-0" />
        <span className="min-w-0 flex-1 truncate text-base text-ink">{issue.summary}</span>
        <PriorityIcon priority={issue.priority} />
        <Avatar person={t.person(issue.assignee)} size={20} me={issue.assignee === t.me} />
        <StatusLozenge status={issue.status} className="w-[84px] justify-center" />
      </button>
    </li>
  );
}

function Attachments({ items }: { items: Array<{ name: string; path: string; url?: string | null }> }) {
  const [open, setOpen] = useState<number | null>(null);
  const [broken, setBroken] = useState<Set<string>>(new Set());
  const urlOf = (a: { path: string; url?: string | null }) => a.url ?? (import.meta.env.VITE_MOCK === '1' ? withBase(`/brand/${a.path}`) : tracker.attachmentUrl(a.path));
  if (!items.length) return <Empty>No attachments.</Empty>;
  const cur = open != null ? items[open] : null;
  return (
    <>
      <ul className="grid grid-cols-[repeat(auto-fill,minmax(180px,1fr))] gap-3">
        {items.map((a, idx) => (
          <li key={a.path}>
            <button
              type="button"
              onClick={() => setOpen(idx)}
              className="press group block w-full overflow-hidden rounded-m border border-line text-left transition-colors hover:border-control"
            >
              <div className="grid aspect-[16/9] place-items-center bg-raised">
                {broken.has(a.path) ? (
                  <span className="flex flex-col items-center gap-1 text-sm text-muted">
                    <ImageOff className="size-5" aria-hidden />
                    Preview unavailable
                  </span>
                ) : (
                  <img
                    src={urlOf(a)}
                    alt={`Wireframe ${a.name}`}
                    loading="lazy"
                    className="h-full w-full object-cover object-top"
                    onError={() => setBroken((b) => new Set(b).add(a.path))}
                  />
                )}
              </div>
              <div className="flex items-center gap-1.5 border-t border-rule px-2.5 py-1.5 text-sm text-ink">
                <Paperclip className="size-3.5 text-muted" aria-hidden />
                <span className="truncate">{a.name}</span>
              </div>
            </button>
          </li>
        ))}
      </ul>
      <Dialog open={!!cur} onOpenChange={(v) => !v && setOpen(null)} title={cur?.name ?? ''} width={1100}>
        {cur ? (
          broken.has(cur.path) ? (
            <Empty>The wireframe file {cur.path} is missing from the brand kit.</Empty>
          ) : (
            <div className="space-y-3">
              <img src={urlOf(cur)} alt={`Wireframe ${cur.name}`} className="w-full rounded-m border border-line bg-white" />
              <a href={urlOf(cur)} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-sm font-medium text-accent-strong hover:underline">
                Open the original <ExternalLink className="size-3.5" aria-hidden />
              </a>
            </div>
          )
        ) : null}
      </Dialog>
    </>
  );
}

// ---------------------------------------------------------------- activity

function Comments({ issue }: { issue: Issue }) {
  const t = useTracker();
  const add = useAddComment(issue.key);
  const [body, setBody] = useState('');
  const id = useId();
  const comments = [...(issue.comments ?? [])].sort((a, b) => (a.created < b.created ? -1 : 1));
  const submit = () => {
    if (!body.trim()) return;
    add.mutate(body, {
      onSuccess: () => setBody(''),
      onError: (e) => toast.error('Comment not saved', { description: e.message }),
    });
  };
  return (
    <div className="flex flex-col gap-4">
      <form
        className="flex gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <Avatar person={t.person(t.me)} size={28} me />
        <div className="min-w-0 flex-1">
          <label htmlFor={id} className="sr-only">
            Add a comment
          </label>
          <Textarea
            id={id}
            value={body}
            onChange={(e) => setBody(e.target.value)}
            placeholder="Add a note: what you found, what you decided, a link to the PR…"
            onKeyDown={(e) => {
              if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                submit();
              }
            }}
            className="min-h-16"
          />
          {body.trim() ? (
            <div className="mt-2 flex items-center gap-2">
              <Button type="submit" variant="primary" size="sm" loading={add.isPending}>
                Save
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setBody('')}>
                Cancel
              </Button>
              <span className="text-xs text-muted">Markdown works. Ctrl or ⌘ + Enter saves.</span>
            </div>
          ) : null}
        </div>
      </form>
      {comments.length === 0 ? (
        <p className="text-base text-muted">No comments yet. Your notes here become the trail a reviewer reads.</p>
      ) : (
        <ol className="flex flex-col gap-5">
          {comments
            .slice()
            .reverse()
            .map((c) => (
              <li key={c.id} className="flex gap-3">
                <Avatar person={t.person(c.author)} size={28} me={c.author === t.me} />
                <div className="min-w-0 flex-1">
                  <div className="flex items-baseline gap-2 text-sm">
                    <span className="font-semibold text-ink">{t.person(c.author)?.name}</span>
                    <time className="text-muted" dateTime={c.created}>
                      {fmtRelative(c.created)}
                    </time>
                  </div>
                  <div className="prose-issue mt-1">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{c.body}</ReactMarkdown>
                  </div>
                </div>
              </li>
            ))}
        </ol>
      )}
    </div>
  );
}

function WorkLog({ issue }: { issue: Issue }) {
  const t = useTracker();
  const add = useAddWorklog(issue.key);
  const [hours, setHours] = useState('');
  const [date, setDate] = useState(toDay(today()));
  const [note, setNote] = useState('');
  const hid = useId();
  const did = useId();
  const nid = useId();
  const h = Number(hours);
  const valid = h > 0 && h <= 24;
  const logs = [...(issue.worklog ?? [])].sort((a, b) => (a.date < b.date ? 1 : -1));
  return (
    <div className="flex flex-col gap-4">
      <form
        className="grid grid-cols-[90px_150px_1fr_auto] items-end gap-2 max-sm:grid-cols-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (!valid) return;
          add.mutate(
            { hours: h, date, note },
            {
              onSuccess: () => {
                setHours('');
                setNote('');
                toast.success(`${fmtHours(h)} logged on ${issue.key}`);
              },
              onError: (e) => toast.error('Time not logged', { description: e.message }),
            },
          );
        }}
      >
        <div>
          <Label htmlFor={hid}>Hours</Label>
          <Input id={hid} inputMode="decimal" value={hours} onChange={(e) => setHours(e.target.value.replace(/[^\d.]/g, ''))} placeholder="2.5" className="num" aria-invalid={!!hours && !valid} />
        </div>
        <div>
          <Label htmlFor={did}>Date</Label>
          <Input id={did} type="date" value={date} onChange={(e) => setDate(e.target.value)} className="num" />
        </div>
        <div className="max-sm:col-span-2">
          <Label htmlFor={nid}>What you did</Label>
          <Input id={nid} value={note} onChange={(e) => setNote(e.target.value)} placeholder="Optional" />
        </div>
        <Button type="submit" disabled={!valid} loading={add.isPending}>
          Log time
        </Button>
      </form>
      {logs.length === 0 ? (
        <p className="text-base text-muted">No time logged.</p>
      ) : (
        <table className="w-full text-base">
          <thead className="sr-only">
            <tr>
              <th>Date</th>
              <th>Who</th>
              <th>Note</th>
              <th>Hours</th>
            </tr>
          </thead>
          <tbody>
            {logs.map((w) => (
              <tr key={w.id} className="border-t border-rule first:border-0">
                <td className="num h-9 w-24 text-muted">{fmtDay(w.date)}</td>
                <td className="h-9 w-8">
                  <Avatar person={t.person(w.author ?? t.me)} size={20} me={(w.author ?? t.me) === t.me} />
                </td>
                <td className="h-9 truncate text-ink">{w.note ?? <span className="text-faint">—</span>}</td>
                <td className="num h-9 w-16 text-right font-medium text-ink">{fmtHours(w.hours)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function History({ issueKey }: { issueKey: string }) {
  const q = useHistory(issueKey);
  const t = useTracker();
  if (q.isError) return <ErrorNote error={q.error} onRetry={() => void q.refetch()} what="history" />;
  if (q.isLoading) return <Skeleton className="h-24 w-full" />;
  const rows = q.data ?? [];
  if (!rows.length) return <p className="text-base text-muted">No changes since the issue was created.</p>;
  return (
    <ol className="flex flex-col">
      {rows.map((h, i) => (
        <li key={`${h.ts}-${i}`} className="flex gap-3 border-t border-rule py-2.5 first:border-0">
          <Avatar person={t.person(h.actor ?? t.me)} size={20} me={(h.actor ?? t.me) === t.me} />
          <div className="min-w-0 flex-1 text-base">
            <span className="font-medium text-ink">{t.person(h.actor ?? t.me)?.name}</span>{' '}
            <span className="text-muted">changed</span> <span className="font-medium text-ink">{h.field}</span>
            {h.from || h.to ? (
              <div className="mt-0.5 flex flex-wrap items-center gap-1.5 text-sm">
                {h.from ? <span className="text-muted line-through decoration-faint">{h.from}</span> : null}
                {h.from && h.to ? <span className="text-faint">→</span> : null}
                {h.to ? <span className="text-ink">{h.to}</span> : null}
              </div>
            ) : null}
          </div>
          <time className="num shrink-0 text-sm text-muted" dateTime={h.ts}>
            {fmtDateTime(h.ts)}
          </time>
        </li>
      ))}
    </ol>
  );
}

// ---------------------------------------------------------------- details

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[112px_minmax(0,1fr)] items-start gap-3 py-1.5">
      <dt className="pt-1 text-sm text-muted">{label}</dt>
      <dd className="min-w-0 text-base text-ink">{children}</dd>
    </div>
  );
}

function NumberField({ value, onSave, suffix, label }: { value: number | null; onSave: (v: number | null) => void; suffix?: string; label: string }) {
  const [v, setV] = useState(value == null ? '' : String(value));
  useEffect(() => setV(value == null ? '' : String(value)), [value]);
  const commit = () => {
    const n = v === '' ? null : Number(v);
    if (n !== value && (n == null || Number.isFinite(n))) onSave(n);
  };
  return (
    <div className="relative w-28">
      <Input
        aria-label={label}
        inputMode="decimal"
        value={v}
        onChange={(e) => setV(e.target.value.replace(/[^\d.]/g, ''))}
        onBlur={commit}
        onKeyDown={(e) => e.key === 'Enter' && (e.target as HTMLInputElement).blur()}
        className="num h-7 pr-7 text-sm"
        placeholder="—"
      />
      {suffix ? <span className="pointer-events-none absolute inset-y-0 right-2 flex items-center text-xs text-muted">{suffix}</span> : null}
    </div>
  );
}

function PrField({ value, onSave }: { value: string | null; onSave: (v: string | null) => void }) {
  const [v, setV] = useState(value ?? '');
  const [editing, setEditing] = useState(!value);
  useEffect(() => {
    setV(value ?? '');
    setEditing(!value);
  }, [value]);
  const valid = !v || /^https?:\/\/\S+$/.test(v);
  if (!editing && value)
    return (
      <div className="flex min-w-0 items-center gap-1">
        <a href={value} target="_blank" rel="noreferrer" className="flex min-w-0 items-center gap-1.5 text-accent-strong hover:underline">
          <GitPullRequest className="size-3.5 shrink-0" aria-hidden />
          <span className="truncate">{value.replace(/^https?:\/\/(www\.)?/, '')}</span>
        </a>
        <Button size="sm" variant="quiet" onClick={() => setEditing(true)} className="ml-auto">
          Edit
        </Button>
      </div>
    );
  return (
    <div>
      <Input
        aria-label="Pull request link"
        value={v}
        onChange={(e) => setV(e.target.value.trim())}
        onBlur={() => valid && (v || null) !== value && onSave(v || null)}
        onKeyDown={(e) => e.key === 'Enter' && (e.target as HTMLInputElement).blur()}
        placeholder="https://github.com/you/repo/pull/12"
        aria-invalid={!valid}
        className="h-7 text-sm"
      />
      {!valid ? <p className="mt-1 text-xs text-bad">Paste the full PR address, starting with https://</p> : null}
    </div>
  );
}

function Details({ issue, save }: { issue: Issue; save: (p: IssuePatch, msg?: string) => void }) {
  const t = useTracker();
  const sprintOptions = [
    { value: 'backlog', label: 'Backlog' },
    ...t.sprints.filter((s) => s.state !== 'closed' || s.id === issue.sprint_id).map((s) => ({ value: String(s.id), label: s.name, hint: s.state === 'active' ? 'active' : undefined })),
  ];
  const people = [...t.people.values()];
  const spent = issue.time_spent_hours ?? (issue.worklog ?? []).reduce((s, w) => s + w.hours, 0);
  const est = issue.estimate_hours;
  const epic = issue.epic_key ? t.byKey.get(issue.epic_key) : null;
  return (
    <dl className="flex flex-col">
      <Field label="Assignee">
        <div className="flex items-center gap-2">
          <Avatar person={t.person(issue.assignee)} size={24} me={issue.assignee === t.me} />
          <Select
            size="sm"
            ariaLabel="Assignee"
            value={issue.assignee ?? 'unassigned'}
            onValueChange={(v) => save({ assignee: v === 'unassigned' ? null : v })}
            options={[{ value: 'unassigned', label: 'Unassigned' }, ...people.map((p) => ({ value: p.key, label: p.key === t.me ? `${p.name} (me)` : p.name }))]}
            className="min-w-0 flex-1"
          />
        </div>
        {issue.assignee !== t.me ? (
          <button type="button" onClick={() => save({ assignee: t.me }, `${issue.key} assigned to you`)} className="mt-1 text-sm font-medium text-accent-strong hover:underline">
            Assign to me
          </button>
        ) : null}
      </Field>
      <Field label="Reporter">
        <span className="flex items-center gap-2">
          <Avatar person={t.person(issue.reporter)} size={24} me={issue.reporter === t.me} />
          <span className="truncate">{t.person(issue.reporter)?.name ?? '—'}</span>
        </span>
      </Field>
      <Field label="Priority">
        <div className="flex items-center gap-2">
          <PriorityIcon priority={issue.priority} />
          <Select
            size="sm"
            ariaLabel="Priority"
            value={issue.priority}
            onValueChange={(v) => save({ priority: v as Issue['priority'] })}
            options={(['Highest', 'High', 'Medium', 'Low', 'Lowest'] as const).map((p) => ({ value: p, label: p }))}
            className="min-w-0 flex-1"
          />
        </div>
      </Field>
      {issue.type !== 'Epic' ? (
        <Field label="Sprint">
          <Select size="sm" ariaLabel="Sprint" value={issue.sprint_id == null ? 'backlog' : String(issue.sprint_id)} onValueChange={(v) => save({ sprint_id: v === 'backlog' ? null : Number(v) })} options={sprintOptions} />
        </Field>
      ) : null}
      {issue.type === 'Story' ? (
        <Field label="Story points">
          <NumberField label="Story points" value={issue.story_points} onSave={(v) => save({ story_points: v })} />
        </Field>
      ) : isWorkItem(issue) ? (
        <Field label="Estimate">
          <NumberField label="Original estimate in hours" value={issue.estimate_hours} onSave={(v) => save({ estimate_hours: v })} suffix="h" />
        </Field>
      ) : null}
      {isWorkItem(issue) ? (
        <Field label="Time tracking">
          <div>
            <div className="h-1.5 overflow-hidden rounded-full bg-active">
              <div className={clsx('h-full rounded-full', est && spent > est ? 'bg-bad' : 'bg-info')} style={{ width: `${Math.min(100, est ? (spent / est) * 100 : spent ? 100 : 0)}%` }} />
            </div>
            <div className="num mt-1 flex justify-between text-xs text-muted">
              <span>{fmtHours(spent)} logged</span>
              <span>{est ? (spent > est ? `${fmtHours(spent - est)} over` : `${fmtHours(est - spent)} left`) : 'no estimate'}</span>
            </div>
          </div>
        </Field>
      ) : null}
      <Field label="Due date">
        <Input type="date" aria-label="Due date" value={issue.due_date ?? ''} onChange={(e) => save({ due_date: e.target.value || null })} className="num h-7 w-40 text-sm" />
      </Field>
      {epic ? (
        <Field label="Epic">
          <Link to={`/tracker/issue/${epic.key}`} className="flex min-w-0 items-center gap-1.5 hover:underline">
            <TypeIcon type="Epic" className="size-3.5" />
            <span className="truncate">{epicShort(epic.summary)}</span>
          </Link>
        </Field>
      ) : null}
      <Field label="Labels">
        {issue.labels.length ? (
          <span className="flex flex-wrap gap-1">
            {issue.labels.map((l) => (
              <span key={l} className="rounded-s bg-active px-1.5 text-sm leading-5 text-ink">
                {l}
              </span>
            ))}
          </span>
        ) : (
          <span className="text-faint">None</span>
        )}
      </Field>
      <Field label="Components">{issue.components.length ? issue.components.join(', ') : <span className="text-faint">None</span>}</Field>
      {issue.type !== 'Epic' ? (
        <Field label="Pull request">
          <PrField value={issue.pr_url ?? null} onSave={(v) => save({ pr_url: v }, v ? 'PR link saved' : 'PR link removed')} />
        </Field>
      ) : null}
      <div className="num mt-3 border-t border-rule pt-3 text-xs leading-5 text-muted">
        Created {fmtDateTime(issue.created)}
        <br />
        Updated {fmtRelative(issue.updated)}
        {issue.resolved ? (
          <>
            <br />
            Resolved {fmtDateTime(issue.resolved)}
          </>
        ) : null}
        {issue.source_id ? (
          <>
            <br />
            Catalog id <span className="font-mono">{issue.source_id}</span>
          </>
        ) : null}
      </div>
    </dl>
  );
}

// ---------------------------------------------------------------- the view

export function IssueView({
  issueKey,
  variant,
  editSummary,
  onClose,
  onOpenIssue,
}: {
  issueKey: string;
  variant: 'panel' | 'page';
  editSummary?: boolean;
  onClose?: () => void;
  onOpenIssue: (k: string) => void;
}) {
  const q = useIssue(issueKey);
  const t = useTracker();
  const update = useUpdateIssue();
  const [tab, setTab] = useState('comments');
  const issue = q.data;

  const save = (patch: IssuePatch, msg?: string) => {
    if (!issue) return;
    update.mutate(
      { key: issue.key, patch },
      {
        onSuccess: () => msg && toast.success(msg),
        onError: (e) => toast.error(`${issue.key} not saved`, { description: e.message }),
      },
    );
  };

  const ac = useMemo(() => checklist(issue?.acceptance_criteria), [issue?.acceptance_criteria]);
  // The API keeps acceptance criteria; Definition of Done ticks are a personal checklist kept in this browser.
  const [dodTicks, setDodTicks] = usePersisted<Record<string, boolean[]>>('harborline.dod', {});
  const dod = useMemo(() => checklist(issue?.definition_of_done).map((c, i) => ({ ...c, done: dodTicks[issueKey]?.[i] ?? c.done })), [issue?.definition_of_done, dodTicks, issueKey]);

  if (q.isError && !issue)
    return (
      <div className="p-6">
        <ErrorNote error={q.error} onRetry={() => void q.refetch()} what={issueKey} />
      </div>
    );
  if (!issue)
    return (
      <div className="flex flex-col gap-3 p-6" aria-busy>
        <Skeleton className="h-5 w-40" />
        <Skeleton className="h-8 w-3/4" />
        <Skeleton className="h-32 w-full" />
      </div>
    );

  const epic = issue.epic_key ? t.byKey.get(issue.epic_key) : null;
  const parent = issue.parent_key ? t.byKey.get(issue.parent_key) : null;
  const kids = t.children.get(issue.key) ?? [];
  const links = issue.links ?? (issue.depends_on ?? []).map((k) => ({ type: 'is blocked by', key: k }));
  const attachments = (issue.attachments ?? []).map((a) => (typeof a === 'string' ? { name: a.split('/').pop() ?? a, path: a } : a));
  const branch = issue.branch_name ?? branchName(issue);
  const kidsDone = kids.filter((k) => k.status === 'Done').length;

  const header = (
    <div className="flex items-center justify-between gap-3">
      <nav aria-label="Issue path" className="flex min-w-0 items-center gap-1.5 text-sm text-muted">
        {epic ? (
          <>
            <button type="button" onClick={() => onOpenIssue(epic.key)} className="flex min-w-0 items-center gap-1 rounded-s hover:text-ink hover:underline">
              <TypeIcon type="Epic" className="size-3.5" label={false} />
              <span className="truncate">{epicShort(epic.summary)}</span>
            </button>
            <span className="text-faint">/</span>
          </>
        ) : null}
        {parent ? (
          <>
            <button type="button" onClick={() => onOpenIssue(parent.key)} className="flex shrink-0 items-center gap-1 rounded-s hover:text-ink hover:underline">
              <TypeIcon type={parent.type} className="size-3.5" label={false} />
              <span className="font-mono text-[12px]">{parent.key}</span>
            </button>
            <span className="text-faint">/</span>
          </>
        ) : null}
        <span className="flex shrink-0 items-center gap-1">
          <TypeIcon type={issue.type} className="size-3.5" />
          <span className="font-mono text-[12px] font-medium text-ink">{issue.key}</span>
        </span>
      </nav>
      <div className="flex shrink-0 items-center gap-0.5">
        <IconButton label="Copy branch name" tip={<span className="font-mono text-xs">{branch}</span>} onClick={() => void copyText(branch, 'Branch name copied')}>
          <GitBranch aria-hidden />
        </IconButton>
        <IconButton label="Copy link" onClick={() => void copyText(`${location.origin}${withBase(`/tracker/issue/${issue.key}`)}`, 'Link copied')}>
          <Link2 aria-hidden />
        </IconButton>
        {variant === 'panel' ? (
          <>
            <Link to={`/tracker/issue/${issue.key}`} aria-label="Open full page" className="press inline-grid size-8 place-items-center rounded-m text-muted hover:bg-hover hover:text-ink [&_svg]:size-4">
              <Maximize2 aria-hidden />
            </Link>
            <IconButton label="Close panel" tip="Close  Esc" onClick={onClose}>
              <X aria-hidden />
            </IconButton>
          </>
        ) : null}
      </div>
    </div>
  );

  const main = (
    <>
      {issue.description ? (
        <Section title="Description">
          <div className="prose-issue">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{issue.description}</ReactMarkdown>
          </div>
        </Section>
      ) : null}
      {ac.length || issue.type !== 'Epic' ? (
        <Section title="Acceptance criteria" meta={ac.length ? `${ac.filter((c) => c.done).length} of ${ac.length}` : undefined}>
          <Checklist
            label="Acceptance criteria"
            items={ac}
            onToggle={(idx, done) => save({ acceptance_criteria: ac.map((c, i) => (i === idx ? { ...c, done } : c)) })}
          />
        </Section>
      ) : null}
      {dod.length ? (
        <Section title="Definition of Done" meta={`${dod.filter((c) => c.done).length} of ${dod.length}`}>
          <Checklist label="Definition of Done" items={dod} onToggle={(idx, done) => setDodTicks({ ...dodTicks, [issueKey]: dod.map((c, i) => (i === idx ? done : c.done)) })} />
          <p className="mt-2 text-xs text-muted">Ticks here are saved in this browser.</p>
        </Section>
      ) : null}
      {kids.length ? (
        <Section title={issue.type === 'Epic' ? 'Stories and tasks' : 'Tasks'} meta={`${kidsDone} of ${kids.length} done`}>
          <ul className="-mx-2">
            {kids.map((k) => (
              <IssueLine key={k.key} issue={k} onOpen={onOpenIssue} />
            ))}
          </ul>
        </Section>
      ) : null}
      {links.length ? (
        <Section title="Linked issues">
          <ul className="-mx-2">
            {links.map((l) => {
              const other = t.byKey.get(l.key);
              return other ? <IssueLine key={`${l.type}-${l.key}`} issue={other} onOpen={onOpenIssue} note={l.type} /> : null;
            })}
          </ul>
        </Section>
      ) : null}
      {attachments.length ? (
        <Section title="Attachments" meta={String(attachments.length)}>
          <Attachments items={attachments} />
        </Section>
      ) : null}
      <section className="mt-8">
        <Tabs
          ariaLabel="Activity"
          value={tab}
          onValueChange={setTab}
          items={[
            { value: 'comments', label: 'Comments', count: issue.comments?.length ?? 0 },
            { value: 'worklog', label: 'Work log', count: issue.worklog?.length ?? 0 },
            { value: 'history', label: 'History' },
          ]}
        />
        <div className="pt-4">
          {tab === 'comments' ? <Comments issue={issue} /> : tab === 'worklog' ? <WorkLog issue={issue} /> : <History issueKey={issue.key} />}
        </div>
      </section>
    </>
  );

  const statusRow = (
    <div className="mt-3 flex flex-wrap items-center gap-2">
      <StatusMenu issue={issue} onChange={(s) => save({ status: s }, `${issue.key} moved to ${s}`)} />
      <Button size="md" icon={<GitBranch aria-hidden />} onClick={() => void copyText(branch, 'Branch name copied')}>
        Copy branch name
      </Button>
      <DueBadge issue={issue} />
    </div>
  );

  if (variant === 'panel')
    return (
      <div className="px-5 pb-10 pt-3">
        {header}
        <div className="mt-3">
          <SummaryEditor issue={issue} startEditing={!!editSummary} onSave={(s) => save({ summary: s })} size="panel" />
        </div>
        {statusRow}
        <div className="mt-5 rounded-m border border-line px-3 py-1.5">
          <Details issue={issue} save={save} />
        </div>
        {main}
      </div>
    );

  return (
    <div className="mx-auto grid max-w-[1280px] grid-cols-[minmax(0,1fr)_340px] gap-10 px-6 pb-12 pt-5 max-[1100px]:grid-cols-1 max-sm:px-4">
      <div className="min-w-0">
        {header}
        <div className="mt-4">
          <SummaryEditor issue={issue} startEditing={!!editSummary} onSave={(s) => save({ summary: s })} size="page" />
        </div>
        {statusRow}
        {main}
      </div>
      <aside className="min-w-0 max-[1100px]:order-first">
        <div className="sticky top-5 rounded-l border border-line px-4 py-3">
          <h3 className="mb-1 text-md font-semibold">Details</h3>
          <Details issue={issue} save={save} />
        </div>
      </aside>
    </div>
  );
}
