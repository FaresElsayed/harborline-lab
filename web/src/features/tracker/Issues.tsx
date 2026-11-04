import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react';
import { useLocation, useSearchParams } from 'react-router';
import clsx from 'clsx';
import { Bookmark, CornerDownLeft, Trash2 } from 'lucide-react';
import { toast } from 'sonner';
import { useSearch } from '@/api/queries';
import type { Issue } from '@/api/types';
import { Button, IconButton } from '@/components/ui/button';
import { Input, Label } from '@/components/ui/field';
import { Empty, Skeleton } from '@/components/ui/misc';
import { Dialog, Kbd } from '@/components/ui/overlay';
import { JQL_FIELDS, JQL_FUNCTIONS } from '@/lib/jql';
import { plural } from '@/lib/format';
import { usePersisted } from '@/lib/store';
import { Avatar, DueBadge, Estimate, IssueKey, PriorityIcon, StatusLozenge, TypeIcon } from './atoms';
import { useIssueParam, useTracker } from './data';
import { useIssueNav } from './nav';

interface SavedFilter {
  name: string;
  jql: string;
}

function builtIns(activeSprintName: string | null): SavedFilter[] {
  return [
    { name: 'My open issues', jql: 'assignee = currentUser() AND status != Done ORDER BY duedate ASC' },
    { name: 'Overdue', jql: 'duedate < now() AND status != Done ORDER BY duedate ASC' },
    { name: 'Due this week', jql: 'duedate >= startOfWeek() AND duedate <= endOfWeek() ORDER BY duedate ASC' },
    ...(activeSprintName ? [{ name: 'Current sprint', jql: `sprint = "${activeSprintName}" ORDER BY rank` }] : []),
    { name: 'In review', jql: 'status = "In Review" ORDER BY duedate ASC' },
    { name: 'Bugs', jql: 'type = Bug ORDER BY priority DESC, duedate ASC' },
    { name: 'Spikes', jql: 'type = Spike ORDER BY duedate ASC' },
    { name: 'Dashboards', jql: 'component = Dashboards ORDER BY duedate ASC' },
  ];
}

const OPS = ['=', '!=', '~', '<', '>', '<=', '>=', 'IN', 'NOT IN', 'IS EMPTY'];

/** Suggestions for the token under the caret: a field, an operator, or a value for the field on its left. */
function useSuggestions(jql: string, caret: number) {
  const t = useTracker();
  return useMemo(() => {
    const before = jql.slice(0, caret);
    const tokens = before.trim().split(/\s+/).filter(Boolean);
    const word = /\s$/.test(before) || !before ? '' : (tokens.pop() ?? '');
    const lower = tokens.map((x) => x.toLowerCase());
    const prev = lower.at(-1) ?? '';
    const prev2 = lower.at(-2) ?? '';
    const isOp = (x: string) => ['=', '!=', '~', '!~', '<', '>', '<=', '>=', 'in'].includes(x);
    const isField = (x: string) => (JQL_FIELDS as readonly string[]).includes(x);
    let kind: 'field' | 'op' | 'value' | 'join' = 'field';
    let field = '';
    if (isOp(prev)) {
      kind = 'value';
      field = prev === 'in' && prev2 === 'not' ? lower.at(-3) ?? '' : prev2;
    } else if (isField(prev)) kind = 'op';
    else if (prev && isOp(prev2)) kind = 'join';
    const values: Record<string, string[]> = {
      type: ['Story', 'Task', 'Bug', 'Spike', 'Epic'],
      status: ['"To Do"', '"In Progress"', '"In Review"', 'Done'],
      priority: ['Highest', 'High', 'Medium', 'Low', 'Lowest'],
      assignee: ['currentUser()', ...[...t.people.keys()]],
      reporter: ['currentUser()', ...[...t.people.keys()]],
      sprint: ['openSprints()', 'closedSprints()', ...t.sprints.map((s) => `"${s.name}"`)],
      epic: t.epics.map((e) => e.key),
      labels: t.meta.data?.labels ?? [],
      component: (t.meta.data?.components ?? []).map((c) => (c.includes(' ') ? `"${c}"` : c)),
      duedate: ['now()', 'startOfWeek()', 'endOfWeek()', '-7d', '7d'],
      created: ['now()', 'startOfWeek()', '-7d'],
      project: ['DATA'],
    };
    const pool = kind === 'field' ? [...JQL_FIELDS] : kind === 'op' ? OPS : kind === 'join' ? ['AND', 'OR', 'ORDER BY'] : values[field] ?? JQL_FUNCTIONS;
    const w = word.toLowerCase();
    const list = pool.filter((p) => !w || p.toLowerCase().replace(/^"/, '').startsWith(w.replace(/^"/, ''))).slice(0, 8);
    return { kind, word, list };
  }, [jql, caret, t.people, t.sprints, t.epics, t.meta.data]);
}

function ResultRow({ issue, onOpen }: { issue: Issue; onOpen: (k: string) => void }) {
  const t = useTracker();
  const sprint = t.sprints.find((s) => s.id === issue.sprint_id);
  return (
    <tr
      tabIndex={0}
      data-issue-key={issue.key}
      onClick={() => onOpen(issue.key)}
      onKeyDown={(e) => e.key === 'Enter' && onOpen(issue.key)}
      className="cursor-pointer border-t border-rule outline-none hover:bg-hover/60 focus-visible:bg-hover focus-visible:shadow-[inset_2px_0_0_var(--hl-focus)]"
    >
      <td className="h-11 pl-3">
        <TypeIcon type={issue.type} />
      </td>
      <td className="h-11">
        <IssueKey k={issue.key} done={issue.status === 'Done'} />
      </td>
      <td className="h-11 max-w-0">
        <span className="block truncate text-ink">{issue.summary}</span>
      </td>
      <td className="h-11">
        <StatusLozenge status={issue.status} />
      </td>
      <td className="h-11">
        <PriorityIcon priority={issue.priority} />
      </td>
      <td className="h-11">
        <Avatar person={t.person(issue.assignee)} size={24} me={issue.assignee === t.me} />
      </td>
      <td className="h-11">
        <DueBadge issue={issue} compact />
      </td>
      <td className="h-11 text-right">
        <Estimate issue={issue} />
      </td>
      <td className="num h-11 pr-3 text-right text-sm text-muted">{issue.type === 'Epic' ? '—' : sprint ? sprint.name.replace('DATA ', '') : 'Backlog'}</td>
    </tr>
  );
}

export default function IssuesPage() {
  const t = useTracker();
  const { open } = useIssueParam();
  const location = useLocation();
  const [params, setParams] = useSearchParams();
  const filters = builtIns(t.active?.name ?? null);
  const initial = params.get('jql') ?? filters[0].jql;
  const [text, setText] = useState(initial);
  const [jql, setJql] = useState(initial);
  const [caret, setCaret] = useState(initial.length);
  const [focused, setFocused] = useState(false);
  const [sel, setSel] = useState(0);
  const [saved, setSaved] = usePersisted<SavedFilter[]>('harborline.filters', []);
  const [saveOpen, setSaveOpen] = useState(false);
  const [name, setName] = useState('');
  const input = useRef<HTMLInputElement>(null);
  const q = useSearch({ jql }, !!jql.trim());
  const sug = useSuggestions(text, caret);

  useEffect(() => {
    if ((location.state as { focusSearch?: number } | null)?.focusSearch) input.current?.focus();
  }, [location.state]);
  useEffect(() => setSel(0), [sug.list.join()]);

  const run = (v = text) => {
    setJql(v);
    setParams(
      (p) => {
        p.set('jql', v);
        return p;
      },
      { replace: true },
    );
  };
  const pick = (s: string) => {
    const before = text.slice(0, caret - sug.word.length);
    const after = text.slice(caret);
    const next = `${before}${s} ${after.trimStart()}`;
    setText(next);
    const c = before.length + s.length + 1;
    setCaret(c);
    requestAnimationFrame(() => input.current?.setSelectionRange(c, c));
  };
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    const showing = focused && sug.list.length > 0;
    if (showing && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
      e.preventDefault();
      setSel((s) => (s + (e.key === 'ArrowDown' ? 1 : -1) + sug.list.length) % sug.list.length);
    } else if (showing && e.key === 'Tab' && sug.word) {
      e.preventDefault();
      pick(sug.list[sel]);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      run();
      input.current?.blur();
    } else if (e.key === 'Escape') input.current?.blur();
  };

  const rows = q.data ?? [];
  useIssueNav(rows.map((r) => r.key), t.me);
  const errorMsg = q.isError ? (q.error instanceof Error ? q.error.message : String(q.error)) : null;
  const all = [...filters.map((f) => ({ ...f, own: false })), ...saved.map((f) => ({ ...f, own: true }))];
  const current = all.find((f) => f.jql === jql);

  return (
    <div className="h-full overflow-y-auto">
      <div className="grid grid-cols-[220px_minmax(0,1fr)] gap-8 px-6 pb-12 pt-5 max-[1100px]:grid-cols-1 max-[1100px]:gap-4">
        <aside aria-label="Filters" className="pt-[72px] max-[1100px]:pt-0">
          <h2 className="mb-2 px-2.5 text-sm text-muted">Filters</h2>
          <ul className="flex flex-col gap-0.5 max-[1100px]:flex-row max-[1100px]:flex-wrap">
            {all.map((f) => (
              <li key={`${f.own}-${f.name}`} className="group relative">
                <button
                  type="button"
                  onClick={() => {
                    setText(f.jql);
                    run(f.jql);
                  }}
                  className={clsx(
                    'flex h-8 w-full items-center gap-2 rounded-m px-2.5 text-left text-base transition-colors',
                    jql === f.jql ? 'bg-active font-medium text-ink' : 'text-muted hover:bg-hover hover:text-ink',
                  )}
                >
                  {f.own ? <Bookmark className="size-3.5 shrink-0" aria-hidden /> : null}
                  <span className="truncate">{f.name}</span>
                </button>
                {f.own ? (
                  <IconButton
                    label={`Delete filter ${f.name}`}
                    size="sm"
                    className="absolute right-0.5 top-0.5 opacity-0 focus-visible:opacity-100 group-hover:opacity-100"
                    onClick={() => {
                      setSaved(saved.filter((s) => s.name !== f.name));
                      toast.success(`Filter “${f.name}” deleted`);
                    }}
                  >
                    <Trash2 aria-hidden />
                  </IconButton>
                ) : null}
              </li>
            ))}
          </ul>
        </aside>

        <div className="min-w-0">
          <div className="mb-1 text-sm text-muted">Harborline Tracker / Issues</div>
          <h1 className="figure text-[1.75rem] font-semibold leading-8 text-ink">{current?.name ?? 'Search issues'}</h1>

          <form
            className="relative mt-4"
            onSubmit={(e) => {
              e.preventDefault();
              run();
            }}
          >
            <label htmlFor="jql" className="sr-only">
              JQL query
            </label>
            <div className="flex gap-2">
              <div className="relative min-w-0 flex-1">
                <input
                  ref={input}
                  id="jql"
                  value={text}
                  spellCheck={false}
                  autoComplete="off"
                  role="combobox"
                  aria-expanded={focused && sug.list.length > 0}
                  aria-controls="jql-suggestions"
                  aria-autocomplete="list"
                  aria-describedby="jql-help"
                  aria-invalid={!!errorMsg}
                  onFocus={() => setFocused(true)}
                  onBlur={() => setTimeout(() => setFocused(false), 120)}
                  onChange={(e) => {
                    setText(e.target.value);
                    setCaret(e.target.selectionStart ?? e.target.value.length);
                  }}
                  onSelect={(e) => setCaret((e.target as HTMLInputElement).selectionStart ?? 0)}
                  onKeyDown={onKey}
                  className="h-10 w-full rounded-m border border-control bg-sheet px-3 font-mono text-[13px] text-ink transition-[border-color,box-shadow] hover:border-faint/70 focus-visible:border-focus focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus/25 aria-[invalid=true]:border-bad"
                />
                {focused && sug.list.length ? (
                  <ul id="jql-suggestions" role="listbox" className="absolute left-0 top-11 z-20 w-72 rounded-m border border-line bg-sheet p-1 shadow-float">
                    {sug.list.map((s, i) => (
                      <li
                        key={s}
                        role="option"
                        aria-selected={i === sel}
                        onMouseDown={(e) => {
                          e.preventDefault();
                          pick(s);
                        }}
                        className={clsx('flex h-8 cursor-default items-center justify-between rounded-s px-2 font-mono text-[13px]', i === sel ? 'bg-active text-ink' : 'text-ink')}
                      >
                        {s}
                        {i === sel && sug.word ? <Kbd>Tab</Kbd> : null}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </div>
              <Button type="submit" variant="primary" size="lg" icon={<CornerDownLeft aria-hidden />}>
                Search
              </Button>
              <Button size="lg" icon={<Bookmark aria-hidden />} onClick={() => (setName(''), setSaveOpen(true))} disabled={!jql.trim() || !!current}>
                Save filter
              </Button>
            </div>
            <p id="jql-help" className={clsx('mt-1.5 text-sm', errorMsg ? 'text-bad' : 'text-muted')}>
              {errorMsg ?? (
                <>
                  <span className="font-mono text-[12px]">field operator value</span> joined with AND or OR, then ORDER BY. Tab completes fields, operators and values.
                </>
              )}
            </p>
          </form>

          <div className="mt-4 flex items-baseline justify-between">
            <span className="num text-sm text-muted">{q.data ? plural(rows.length, 'issue') : ' '}</span>
            <span className="text-sm text-muted">
              <Kbd>J</Kbd> <Kbd>K</Kbd> move · <Kbd>Enter</Kbd> opens
            </span>
          </div>
          {q.isLoading ? (
            <div className="mt-2 flex flex-col gap-2">
              {Array.from({ length: 8 }, (_, i) => (
                <Skeleton key={i} className="h-10 w-full" />
              ))}
            </div>
          ) : errorMsg ? null : rows.length === 0 ? (
            <div className="mt-2">
              <Empty>No issues match this query.</Empty>
            </div>
          ) : (
            <div className="scroll-thin mt-1 overflow-x-auto">
              <table className="w-full min-w-[820px] table-fixed text-base">
                <thead>
                  <tr className="text-left text-sm text-muted">
                    <th scope="col" className="h-9 w-10 pl-3 font-normal">
                      <span className="sr-only">Type</span>
                    </th>
                    <th scope="col" className="h-9 w-[88px] font-normal">Key</th>
                    <th scope="col" className="h-9 font-normal">Summary</th>
                    <th scope="col" className="h-9 w-[104px] font-normal">Status</th>
                    <th scope="col" className="h-9 w-10 font-normal">
                      <span className="sr-only">Priority</span>
                    </th>
                    <th scope="col" className="h-9 w-10 font-normal">
                      <span className="sr-only">Assignee</span>
                    </th>
                    <th scope="col" className="h-9 w-[124px] font-normal">Due</th>
                    <th scope="col" className="h-9 w-14 text-right font-normal">Est.</th>
                    <th scope="col" className="h-9 w-24 pr-3 text-right font-normal">Sprint</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((i) => (
                    <ResultRow key={i.key} issue={i} onOpen={open} />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>

      <Dialog
        open={saveOpen}
        onOpenChange={setSaveOpen}
        title="Save this filter"
        description={<span className="font-mono text-[12px]">{jql}</span>}
        footer={
          <>
            <Button variant="ghost" onClick={() => setSaveOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="primary"
              disabled={!name.trim() || all.some((f) => f.name.toLowerCase() === name.trim().toLowerCase())}
              onClick={() => {
                setSaved([...saved, { name: name.trim(), jql }]);
                setSaveOpen(false);
                toast.success(`Filter “${name.trim()}” saved`, { description: 'Saved in this browser.' });
              }}
            >
              Save
            </Button>
          </>
        }
      >
        <Label htmlFor="filter-name">Name</Label>
        <Input id="filter-name" value={name} onChange={(e) => setName(e.target.value)} autoFocus placeholder="AX tickets due before the month-end close" />
      </Dialog>
    </div>
  );
}
