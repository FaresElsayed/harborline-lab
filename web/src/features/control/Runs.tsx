import { Fragment, useMemo, useState } from 'react';
import clsx from 'clsx';
import { ChevronRight } from 'lucide-react';
import { useRuns } from '@/api/queries';
import type { RunSummary } from '@/api/types';
import { Empty, ErrorNote, PageHeader, Pill, Skeleton, Tabs } from '@/components/ui/misc';
import { fmtUtc } from '@/lib/dates';
import { fmtDuration, fmtInt, fmtSimHours } from '@/lib/format';
import { summarise } from './actions';
import { KIND_LABEL, SYSTEMS } from './systems';

const FILTERS = [
  { value: 'all', label: 'All' },
  { value: 'batch', label: 'Batches' },
  { value: 'scenario', label: 'Scenarios' },
  { value: 'build', label: 'Builds' },
  { value: 'stream_tick', label: 'Stream ticks' },
];

function Breakdown({ r }: { r: RunSummary }) {
  const groups = SYSTEMS.map((s) => ({ s, rows: r.changes.filter((c) => c.system === s.id && c.ins + c.upd + c.del > 0) })).filter((g) => g.rows.length);
  const other = r.changes.filter((c) => !SYSTEMS.some((s) => s.id === c.system));
  if (other.length) groups.push({ s: { id: 'other', label: 'Other', where: '' }, rows: other });
  if (!groups.length) return <p className="py-2 text-base text-muted">This run changed no rows.</p>;
  return (
    <div className="grid grid-cols-2 gap-x-8 gap-y-4 py-2 max-[1100px]:grid-cols-1">
      {groups.map(({ s, rows }) => {
        const t = summarise(rows);
        return (
          <table key={s.id} className="w-full text-sm">
            <caption className="mb-1 text-left text-sm font-semibold text-ink">
              {s.label} <span className="num font-normal text-muted">· +{fmtInt(t.ins)} · {fmtInt(t.upd)} upd · {fmtInt(t.del)} del</span>
            </caption>
            <thead className="sr-only">
              <tr>
                <th>Table</th>
                <th>Inserted</th>
                <th>Updated</th>
                <th>Deleted</th>
              </tr>
            </thead>
            <tbody>
              {rows
                .slice()
                .sort((a, b) => b.ins + b.upd + b.del - (a.ins + a.upd + a.del))
                .map((c) => (
                  <tr key={c.table} className="border-t border-rule">
                    <td className="h-7 font-mono text-[12.5px] text-ink">{c.table}</td>
                    <td className={clsx('num h-7 w-24 text-right', c.ins ? 'text-ink' : 'text-faint')}>{c.ins ? `+${fmtInt(c.ins)}` : 0}</td>
                    <td className={clsx('num h-7 w-24 text-right', c.upd ? 'text-ink' : 'text-faint')}>{fmtInt(c.upd)}</td>
                    <td className={clsx('num h-7 w-24 text-right', c.del ? 'text-ink' : 'text-faint')}>{c.del ? `−${fmtInt(c.del)}` : 0}</td>
                  </tr>
                ))}
            </tbody>
          </table>
        );
      })}
    </div>
  );
}

export default function RunsPage() {
  const runs = useRuns(200);
  const [filter, setFilter] = useState('all');
  const [open, setOpen] = useState<Set<string>>(new Set());
  const counts = useMemo(() => {
    const m: Record<string, number> = { all: runs.data?.length ?? 0 };
    for (const r of runs.data ?? []) m[r.kind] = (m[r.kind] ?? 0) + 1;
    return m;
  }, [runs.data]);
  const rows = (runs.data ?? []).filter((r) => filter === 'all' || r.kind === filter);
  const toggle = (id: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  return (
    <>
      <PageHeader title="Run history" subtitle="Every build, batch, scenario and stream tick, with what it did to each table. Open a row for its breakdown." />
      <Tabs ariaLabel="Run kind" value={filter} onValueChange={setFilter} items={FILTERS.map((f) => ({ ...f, count: counts[f.value] ?? 0 }))} />
      {runs.isError ? (
        <div className="mt-4">
          <ErrorNote error={runs.error} onRetry={() => void runs.refetch()} what="run history" />
        </div>
      ) : runs.isLoading ? (
        <div className="mt-4 flex flex-col gap-2">
          {Array.from({ length: 8 }, (_, i) => (
            <Skeleton key={i} className="h-10 w-full" />
          ))}
        </div>
      ) : rows.length === 0 ? (
        <div className="mt-4">
          <Empty>{filter === 'all' ? 'No runs yet. Build the dataset to get started.' : `No ${FILTERS.find((f) => f.value === filter)?.label.toLowerCase()} in the last ${fmtInt(runs.data?.length ?? 0)} runs.`}</Empty>
        </div>
      ) : (
        <div className="scroll-thin mt-2 overflow-x-auto">
          <table className="w-full min-w-[920px] text-base">
            <thead>
              <tr className="text-left text-sm text-muted">
                <th scope="col" className="h-10 w-8" />
                <th scope="col" className="h-10 font-normal">Run</th>
                <th scope="col" className="h-10 pr-4 font-normal">Started (UTC)</th>
                <th scope="col" className="num h-10 pr-6 text-right font-normal">Took</th>
                <th scope="col" className="h-10 font-normal">Sim window (UTC)</th>
                <th scope="col" className="num h-10 w-28 text-right font-normal">Inserted</th>
                <th scope="col" className="num h-10 w-28 text-right font-normal">Updated</th>
                <th scope="col" className="num h-10 w-28 pr-2 text-right font-normal">Deleted</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const id = String(r.run_id);
                const isOpen = open.has(id);
                const t = summarise(r.changes);
                const took = r.finished_at ? (Date.parse(r.finished_at) - Date.parse(r.started_at)) / 1000 : null;
                const simH = r.sim_from && r.sim_to ? Math.round((Date.parse(r.sim_to) - Date.parse(r.sim_from)) / 3_600_000) : null;
                return (
                  <Fragment key={id}>
                    <tr className={clsx('border-t border-rule', isOpen ? 'bg-raised' : 'hover:bg-hover/60')}>
                      <td className="h-11 pl-1">
                        <button
                          type="button"
                          onClick={() => toggle(id)}
                          aria-expanded={isOpen}
                          aria-controls={`run-${id}`}
                          aria-label={`${isOpen ? 'Hide' : 'Show'} changes for run ${id}`}
                          className="press grid size-7 place-items-center rounded-s text-muted hover:bg-hover hover:text-ink"
                        >
                          <ChevronRight className={clsx('size-4 transition-transform duration-200 ease-out', isOpen && 'rotate-90')} aria-hidden />
                        </button>
                      </td>
                      <td className="h-11 max-w-[360px]">
                        <div className="flex min-w-0 items-center gap-2">
                          <span className="truncate text-ink">{r.label ?? KIND_LABEL[r.kind] ?? r.kind}</span>
                          {r.status === 'error' ? <Pill tone="bad">Failed</Pill> : r.status === 'running' ? <Pill tone="info">Running</Pill> : r.status === 'cancelled' ? <Pill>Cancelled</Pill> : null}
                        </div>
                        {r.status === 'error' && r.error ? <div className="truncate text-sm text-bad">{r.error}</div> : null}
                      </td>
                      <td className="num h-11 whitespace-nowrap pr-4 text-muted">{fmtUtc(r.started_at, 'MMM d, HH:mm:ss')}</td>
                      <td className="num h-11 whitespace-nowrap pr-6 text-right text-muted">{took != null ? fmtDuration(took) : '—'}</td>
                      <td className="num h-11 whitespace-nowrap text-muted">
                        {r.sim_from && r.sim_to ? (
                          <>
                            {fmtUtc(r.sim_from, r.sim_from.slice(0, 4) === r.sim_to.slice(0, 4) ? 'MMM d HH:mm' : 'MMM d, yyyy')} →{' '}
                            {fmtUtc(r.sim_to, r.sim_from.slice(0, 4) === r.sim_to.slice(0, 4) ? 'MMM d HH:mm' : 'MMM d, yyyy')}
                            {simH != null ? <span className="text-faint"> · {fmtSimHours(Math.max(1, simH))}</span> : null}
                          </>
                        ) : (
                          '—'
                        )}
                      </td>
                      <td className="num h-11 text-right text-ink">{t.ins ? `+${fmtInt(t.ins)}` : <span className="text-faint">0</span>}</td>
                      <td className="num h-11 text-right text-ink">{t.upd ? fmtInt(t.upd) : <span className="text-faint">0</span>}</td>
                      <td className="num h-11 pr-2 text-right text-ink">{t.del ? `−${fmtInt(t.del)}` : <span className="text-faint">0</span>}</td>
                    </tr>
                    {isOpen ? (
                      <tr id={`run-${id}`} className="bg-raised">
                        <td />
                        <td colSpan={7} className="pb-3 pr-2">
                          <Breakdown r={r} />
                        </td>
                      </tr>
                    ) : null}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
          <p className="num mt-3 text-sm text-muted">
            Showing {fmtInt(rows.length)} of the last {fmtInt(runs.data?.length ?? 0)} runs.
          </p>
        </div>
      )}
    </>
  );
}
