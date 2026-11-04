import { useMemo, useState, type ReactNode } from 'react';
import { Area, AreaChart, Bar, BarChart, CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { useBurndown, useCfd, useVelocity } from '@/api/queries';
import { Segmented, Select } from '@/components/ui/field';
import { Empty, ErrorNote, Skeleton } from '@/components/ui/misc';
import { fmtDay, format, parseDay, toDay, today } from '@/lib/dates';
import { fmtHours, fmtInt } from '@/lib/format';
import { useTracker } from './data';

const tick = { fill: 'var(--muted)', fontSize: 12 };
const axisProps = { tickLine: false, axisLine: false, tick } as const;

/** Round step that gives 4-6 ticks with the top tick above the maximum. */
function niceMax(max: number) {
  if (max <= 0) return { top: 10, ticks: [0, 5, 10] };
  const raw = max / 4;
  const pow = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * pow).find((s) => s >= raw) ?? raw;
  const top = Math.ceil((max * 1.04) / step) * step;
  const ticks: number[] = [];
  for (let v = 0; v <= top + 1e-9; v += step) ticks.push(Math.round(v * 100) / 100);
  return { top, ticks };
}

function TooltipBox({ title, rows }: { title: ReactNode; rows: Array<{ label: string; value: string; swatch?: string; dashed?: boolean }> }) {
  return (
    <div className="num min-w-44 rounded-m border border-line bg-sheet px-3 py-2 text-sm shadow-float">
      <div className="mb-1 font-semibold text-ink">{title}</div>
      {rows.map((r) => (
        <div key={r.label} className="flex items-center justify-between gap-4 py-0.5">
          <span className="flex items-center gap-1.5 text-muted">
            {r.swatch ? <span className="h-0.5 w-3" style={{ background: r.swatch, opacity: r.dashed ? 0.7 : 1 }} aria-hidden /> : null}
            {r.label}
          </span>
          <span className="font-medium text-ink">{r.value}</span>
        </div>
      ))}
    </div>
  );
}

function Legend({ items }: { items: Array<{ label: string; color: string; kind: 'line' | 'dash' | 'box' }> }) {
  return (
    <div className="flex flex-wrap items-center gap-4 text-sm text-muted">
      {items.map((i) => (
        <span key={i.label} className="flex items-center gap-1.5">
          {i.kind === 'box' ? (
            <span className="size-2.5 rounded-[3px]" style={{ background: i.color }} aria-hidden />
          ) : (
            <svg width="16" height="8" aria-hidden>
              <line x1="0" y1="4" x2="16" y2="4" stroke={i.color} strokeWidth="2" strokeDasharray={i.kind === 'dash' ? '3 3' : undefined} />
            </svg>
          )}
          {i.label}
        </span>
      ))}
    </div>
  );
}

function ChartFrame({ title, finding, legend, children, table, height = 300 }: { title: string; finding: ReactNode; legend?: ReactNode; children: ReactNode; table: ReactNode; height?: number }) {
  const [view, setView] = useState<'chart' | 'table'>('chart');
  return (
    <section className="min-w-0 rounded-l border border-line p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-md font-semibold text-ink">{title}</h2>
          <p className="num mt-0.5 max-w-[80ch] text-base text-muted">{finding}</p>
        </div>
        <Segmented
          size="sm"
          ariaLabel={`${title} view`}
          value={view}
          onValueChange={setView}
          options={[
            { value: 'chart', label: 'Chart' },
            { value: 'table', label: 'Table' },
          ]}
        />
      </div>
      {view === 'chart' ? (
        <>
          {legend ? <div className="mt-4">{legend}</div> : null}
          <div className="mt-3" style={{ height }}>
            {children}
          </div>
        </>
      ) : (
        <div className="scroll-thin mt-4 overflow-auto" style={{ height: height + 28 }}>
          {table}
        </div>
      )}
    </section>
  );
}

function Table({ head, rows }: { head: string[]; rows: Array<Array<string | number>> }) {
  return (
    <table className="w-full text-base">
      <thead className="sticky top-0 bg-sheet">
        <tr className="text-sm text-muted">
          {head.map((h, i) => (
            <th key={h} scope="col" className={`h-9 font-normal ${i ? 'num text-right' : 'text-left'}`}>
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((r, ri) => (
          <tr key={ri} className="border-t border-rule">
            {r.map((c, i) => (
              <td key={i} className={`h-8 ${i ? 'num text-right text-ink' : 'text-ink'}`}>
                {c}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// ---------------------------------------------------------------- burndown

function Burndown() {
  const t = useTracker();
  const options = t.sprints.filter((s) => s.state !== 'future').sort((a, b) => b.id - a.id);
  const [sprintId, setSprintId] = useState<number | null>(null);
  const id = sprintId ?? t.active?.id ?? options[0]?.id ?? null;
  const q = useBurndown(id);
  const sprint = t.sprints.find((s) => s.id === id);
  const data = q.data?.days ?? [];
  const todayKey = toDay(today());
  const last = [...data].reverse().find((d) => d.remaining != null);
  const idealNow = last ? data.find((d) => d.date === last.date)?.ideal : null;
  const gap = last && idealNow != null ? last.remaining! - idealNow : null;
  const { top, ticks } = niceMax(q.data?.committed ?? 0);

  const finding =
    !q.data || !last
      ? 'Remaining estimate at the end of each working day against a straight line to zero.'
      : `${fmtHours(last.remaining)} of ${fmtHours(q.data.committed)} left on ${fmtDay(last.date)}; the ideal line is at ${fmtHours(idealNow)}${
          gap == null ? '' : gap > 0.5 ? `, so you are ${fmtHours(Math.round(gap * 10) / 10)} behind it.` : gap < -0.5 ? `, so you are ${fmtHours(Math.round(-gap * 10) / 10)} ahead of it.` : ', right on it.'
        }`;

  return (
    <ChartFrame
      height={260}
      title={`Burndown${sprint ? ` · ${sprint.name}` : ''}`}
      finding={finding}
      legend={
        <div className="flex flex-wrap items-center justify-between gap-3">
          <Legend
            items={[
              { label: 'Remaining hours', color: 'var(--hl-viz-1)', kind: 'line' },
              { label: 'Ideal', color: 'var(--hl-axis)', kind: 'dash' },
            ]}
          />
          {options.length > 1 ? (
            <Select size="sm" ariaLabel="Sprint" value={String(id ?? '')} onValueChange={(v) => setSprintId(Number(v))} className="w-48" options={options.map((s) => ({ value: String(s.id), label: s.name, hint: s.state === 'active' ? 'active' : undefined }))} />
          ) : null}
        </div>
      }
      table={<Table head={['Day', 'Remaining (h)', 'Ideal (h)']} rows={data.map((d) => [fmtDay(d.date), d.remaining == null ? '—' : fmtInt(d.remaining), fmtInt(d.ideal)])} />}
    >
      {q.isError ? (
        <ErrorNote error={q.error} onRetry={() => void q.refetch()} what="the burndown" />
      ) : !q.data ? (
        <Skeleton className="h-full w-full" />
      ) : !data.length ? (
        <Empty>No working days in this sprint yet.</Empty>
      ) : (
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 16, right: 16, bottom: 0, left: 0 }}>
            <CartesianGrid vertical={false} stroke="var(--hl-grid)" />
            <XAxis dataKey="date" {...axisProps} tickFormatter={(d: string) => format(parseDay(d), 'MMM d')} interval="preserveStartEnd" minTickGap={24} padding={{ left: 8, right: 8 }} />
            <YAxis {...axisProps} width={44} domain={[0, top]} ticks={ticks} tickFormatter={(v: number) => `${v}h`} />
            {data.some((d) => d.date === todayKey) ? <ReferenceLine x={todayKey} stroke="var(--hl-viz-1)" strokeOpacity={0.35} label={{ value: 'Today', position: 'insideTopRight', fill: 'var(--muted)', fontSize: 11 }} /> : null}
            <Line type="linear" dataKey="ideal" stroke="var(--hl-axis)" strokeWidth={1.5} strokeDasharray="4 4" dot={false} isAnimationActive={false} />
            <Line type="linear" dataKey="remaining" stroke="var(--hl-viz-1)" strokeWidth={2.25} dot={false} activeDot={{ r: 4, strokeWidth: 2, stroke: 'var(--sheet)' }} connectNulls={false} isAnimationActive={false} />
            <Tooltip
              cursor={{ stroke: 'var(--hl-axis)', strokeWidth: 1 }}
              content={({ active, payload, label }) =>
                active && payload?.length ? (
                  <TooltipBox
                    title={fmtDay(String(label))}
                    rows={[
                      { label: 'Remaining', value: payload.find((p) => p.dataKey === 'remaining')?.value == null ? '—' : fmtHours(Number(payload.find((p) => p.dataKey === 'remaining')?.value)), swatch: 'var(--hl-viz-1)' },
                      { label: 'Ideal', value: fmtHours(Number(payload.find((p) => p.dataKey === 'ideal')?.value ?? 0)), swatch: 'var(--hl-axis)', dashed: true },
                    ]}
                  />
                ) : null
              }
            />
          </LineChart>
        </ResponsiveContainer>
      )}
    </ChartFrame>
  );
}

// ---------------------------------------------------------------- velocity

function Velocity() {
  const q = useVelocity();
  const t = useTracker();
  const rows = q.data?.sprints ?? [];
  const closedIds = new Set(t.sprints.filter((s) => s.state === 'closed').map((s) => s.id));
  const closed = rows.filter((r) => closedIds.has(r.sprint_id));
  const avg = closed.length ? closed.reduce((n, r) => n + r.completed, 0) / closed.length : 0;
  const lastClosed = closed.at(-1);
  const { top, ticks } = niceMax(Math.max(0, ...rows.map((r) => Math.max(r.committed, r.completed))));
  const unit = q.data?.unit === 'points' ? 'pts' : 'h';
  const fmt = (v: number) => `${fmtInt(v)} ${unit}`;
  return (
    <ChartFrame
      height={240}
      title="Velocity"
      finding={
        lastClosed
          ? `${lastClosed.name}: ${fmt(lastClosed.completed)} completed of ${fmt(lastClosed.committed)} committed (${Math.round((lastClosed.completed / Math.max(1, lastClosed.committed)) * 100)}%). ${closed.length > 1 ? `Average ${fmt(Math.round(avg))} per completed sprint.` : 'The running sprint is shown as it stands today.'}`
          : 'Committed against completed estimate per sprint. The first bar fills in when Sprint 1 is completed.'
      }
      legend={
        <Legend
          items={[
            { label: 'Committed', color: 'var(--hl-viz-other)', kind: 'box' },
            { label: 'Completed', color: 'var(--hl-viz-1)', kind: 'box' },
          ]}
        />
      }
      table={<Table head={['Sprint', `Committed (${unit})`, `Completed (${unit})`]} rows={rows.map((r) => [r.name, fmtInt(r.committed), fmtInt(r.completed)])} />}
    >
      {q.isError ? (
        <ErrorNote error={q.error} onRetry={() => void q.refetch()} what="velocity" />
      ) : !q.data ? (
        <Skeleton className="h-full w-full" />
      ) : !rows.length ? (
        <Empty>Velocity appears after your first sprint starts.</Empty>
      ) : (
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={rows} margin={{ top: 16, right: 16, bottom: 0, left: 0 }} barGap={2} barCategoryGap="28%">
            <CartesianGrid vertical={false} stroke="var(--hl-grid)" />
            <XAxis dataKey="name" {...axisProps} tickFormatter={(n: string) => n.replace(/^DATA /, '')} />
            <YAxis {...axisProps} width={48} domain={[0, top]} ticks={ticks} tickFormatter={(v: number) => `${v}${unit === 'h' ? 'h' : ''}`} />
            <Tooltip
              cursor={{ fill: 'var(--hover)' }}
              content={({ active, payload, label }) =>
                active && payload?.length ? (
                  <TooltipBox
                    title={String(label)}
                    rows={[
                      { label: 'Committed', value: fmt(Number(payload.find((p) => p.dataKey === 'committed')?.value ?? 0)), swatch: 'var(--hl-viz-other)' },
                      { label: 'Completed', value: fmt(Number(payload.find((p) => p.dataKey === 'completed')?.value ?? 0)), swatch: 'var(--hl-viz-1)' },
                    ]}
                  />
                ) : null
              }
            />
            <Bar dataKey="committed" fill="var(--hl-viz-other)" radius={[4, 4, 0, 0]} maxBarSize={40} isAnimationActive={false} />
            <Bar dataKey="completed" fill="var(--hl-viz-1)" radius={[4, 4, 0, 0]} maxBarSize={40} isAnimationActive={false} />
          </BarChart>
        </ResponsiveContainer>
      )}
    </ChartFrame>
  );
}

// ---------------------------------------------------------------- cumulative flow

const FLOW = [
  { key: 'Done', color: 'var(--hl-seq-7)' },
  { key: 'In Review', color: 'var(--hl-seq-5)' },
  { key: 'In Progress', color: 'var(--hl-seq-3)' },
  { key: 'To Do', color: 'var(--hl-seq-1)' },
] as const;

function Cfd() {
  const q = useCfd();
  const rows = q.data?.rows ?? [];
  const last = rows.at(-1);
  const total = last ? FLOW.reduce((t, f) => t + last[f.key], 0) : 0;
  const { top, ticks } = niceMax(total);
  const wip = last ? last['In Progress'] + last['In Review'] : 0;
  return (
    <ChartFrame
      height={240}
      title="Cumulative flow"
      finding={
        last
          ? `On ${fmtDay(last.date)}: ${fmtInt(last.Done)} of ${fmtInt(total)} work items done, ${fmtInt(wip)} in progress or review. A widening middle band means work is starting faster than it finishes.`
          : 'Work items in each status per working day, stacked.'
      }
      legend={<Legend items={[...FLOW].reverse().map((f) => ({ label: f.key, color: f.color, kind: 'box' as const }))} />}
      table={<Table head={['Day', 'To Do', 'In Progress', 'In Review', 'Done']} rows={rows.map((r) => [fmtDay(r.date), r['To Do'], r['In Progress'], r['In Review'], r.Done])} />}
    >
      {q.isError ? (
        <ErrorNote error={q.error} onRetry={() => void q.refetch()} what="cumulative flow" />
      ) : !q.data ? (
        <Skeleton className="h-full w-full" />
      ) : rows.length < 2 ? (
        <Empty>Cumulative flow needs at least two working days of history.</Empty>
      ) : (
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={rows} margin={{ top: 16, right: 16, bottom: 0, left: 0 }}>
            <CartesianGrid vertical={false} stroke="var(--hl-grid)" />
            <XAxis dataKey="date" {...axisProps} tickFormatter={(d: string) => format(parseDay(d), 'MMM d')} interval="preserveStartEnd" minTickGap={32} />
            <YAxis {...axisProps} width={40} domain={[0, top]} ticks={ticks} allowDecimals={false} />
            <Tooltip
              cursor={{ stroke: 'var(--hl-axis)', strokeWidth: 1 }}
              content={({ active, payload, label }) =>
                active && payload?.length ? (
                  <TooltipBox
                    title={fmtDay(String(label))}
                    rows={[...FLOW].reverse().map((f) => ({ label: f.key, value: fmtInt(Number(payload.find((p) => p.dataKey === f.key)?.value ?? 0)), swatch: f.color }))}
                  />
                ) : null
              }
            />
            {FLOW.map((f) => (
              <Area key={f.key} type="linear" dataKey={f.key} stackId="flow" stroke="var(--sheet)" strokeWidth={2} fill={f.color} fillOpacity={1} isAnimationActive={false} />
            ))}
          </AreaChart>
        </ResponsiveContainer>
      )}
    </ChartFrame>
  );
}

export default function ReportsPage() {
  const t = useTracker();
  const started = useMemo(() => t.sprints.some((s) => s.state !== 'future'), [t.sprints]);
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-[1360px] px-6 pb-12 pt-5">
        <div className="mb-1 text-sm text-muted">Harborline Tracker / Reports</div>
        <h1 className="figure text-[1.75rem] font-semibold leading-8 text-ink">Reports</h1>
        <p className="mt-1 text-md text-muted">Hours on tasks, bugs and spikes. Story points stay on stories and are not counted twice.</p>
        <div className="mt-5">
          {!started ? (
            <Empty>Reports fill in once Sprint 1 starts. Start it from the backlog.</Empty>
          ) : (
            <div className="grid grid-cols-2 gap-4 max-[1100px]:grid-cols-1">
              <div className="col-span-2 max-[1100px]:col-span-1">
                <Burndown />
              </div>
              <Velocity />
              <Cfd />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
