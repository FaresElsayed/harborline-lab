import { useMemo, useState, useId } from 'react';
import { Link } from 'react-router';
import clsx from 'clsx';
import { AnimatePresence, motion } from 'motion/react';
import { ArrowRight, Hammer, Play, Square } from 'lucide-react';
import { toast } from 'sonner';
import { useCancelBuild, useRuns, useStatus } from '@/api/queries';
import { useLive } from '@/api/live';
import type { RunSummary, SimStatus } from '@/api/types';
import { Button, buttonClasses } from '@/components/ui/button';
import { Input, Label, Segmented, Select } from '@/components/ui/field';
import { Dot, Empty, ErrorNote, PageHeader, Panel, Pill, Skeleton, Tabs } from '@/components/ui/misc';
import { Dialog, Tooltip } from '@/components/ui/overlay';
import { fmtRelative, fmtUtc } from '@/lib/dates';
import { bytesText, fmtDuration, fmtInt, fmtSimHours, plural } from '@/lib/format';
import { ui } from '@/lib/store';
import { stateView } from '@/shell/TopBar';
import { summarise, useSimActions } from './actions';
import { BuildDialog, PRESET_INFO } from './BuildDialog';
import { KIND_LABEL, SYSTEMS } from './systems';

const HOUR = 3_600_000;

function aheadText(sim: string, real: string) {
  const diff = Date.parse(sim) - Date.parse(real);
  const abs = Math.abs(diff) / 1000;
  if (abs < 60) return 'level with real time';
  return `${fmtDuration(abs).replace(/ \d+ s$/, '')} ${diff > 0 ? 'ahead of' : 'behind'} real time`;
}

// ---------------------------------------------------------------- status strip

function Cell({ label, children, sub, className }: { label: string; children: React.ReactNode; sub?: React.ReactNode; className?: string }) {
  return (
    <div className={clsx('min-w-0 px-4 py-3.5', className)}>
      <div className="text-sm text-muted">{label}</div>
      <div className="mt-1 truncate">{children}</div>
      {sub ? <div className="mt-1 line-clamp-2 text-sm text-muted">{sub}</div> : null}
    </div>
  );
}

function StatusStrip({ s }: { s: SimStatus | undefined }) {
  const offline = false;
  const view = stateView(s, offline);
  const tables = s?.totals.length ?? 0;
  const dbs = new Set((s?.totals ?? []).filter((t) => ['wms', 'ax2012', 'iot'].includes(t.system)).map((t) => t.system)).size;
  return (
    <div className="grid grid-cols-[1.6fr_1fr_1fr_1fr_1fr] divide-x divide-line rounded-l border border-line max-[1100px]:grid-cols-2 max-[1100px]:divide-x-0 max-[1100px]:[&>*]:border-line max-[1100px]:[&>*:first-child]:col-span-2 max-[1100px]:[&>*:nth-child(n+2)]:border-t max-[1100px]:[&>*:nth-child(odd):not(:first-child)]:border-l">
      <Cell
        label="Sim clock (UTC)"
        sub={s?.sim_clock ? <>Real clock {fmtUtc(s.real_clock, 'HH:mm')}, so the sim is {aheadText(s.sim_clock, s.real_clock)}</> : 'Build the dataset to start the clock'}
      >
        {s ? (
          <span className="figure num text-[1.875rem] font-semibold leading-9 text-ink">{s.sim_clock ? fmtUtc(s.sim_clock, 'EEE MMM d · HH:mm') : '—'}</span>
        ) : (
          <Skeleton className="h-9 w-56" />
        )}
      </Cell>
      <Cell label="State" sub={s?.last_run ? <>Last run {fmtRelative(s.last_run.finished_at ?? s.last_run.started_at)}</> : 'No runs yet'}>
        {s ? (
          <span className="flex items-center gap-2 text-xl font-semibold text-ink">
            <Dot tone={view.tone} pulse={view.pulse} />
            {view.label}
          </span>
        ) : (
          <Skeleton className="h-7 w-28" />
        )}
      </Cell>
      <Cell
        label="Dataset"
        sub={s?.built_at ? <>Built {fmtUtc(s.built_at, 'MMM d, HH:mm')} · {PRESET_INFO[s.preset ?? 'large']?.months ?? '—'} months</> : 'Not built'}
      >
        {s ? (
          <span className="figure num text-xl font-semibold text-ink">
            {s.preset ? PRESET_INFO[s.preset].label : '—'} <span className="text-md font-normal text-muted">seed {s.seed ?? '—'}</span>
          </span>
        ) : (
          <Skeleton className="h-7 w-32" />
        )}
      </Cell>
      <Cell label="Databases" sub={s ? `${dbs} databases · ${tables} tables and feeds` : undefined}>
        {s ? <span className="figure num text-xl font-semibold text-ink">{bytesText(s.db_size_bytes)}</span> : <Skeleton className="h-7 w-20" />}
      </Cell>
      <Cell label="Landing folder" sub={s ? `${bytesText(s.landing.bytes)} in landing/` : undefined}>
        {s ? <span className="figure num text-xl font-semibold text-ink">{plural(s.landing.files, 'file')}</span> : <Skeleton className="h-7 w-24" />}
      </Cell>
    </div>
  );
}

// ---------------------------------------------------------------- simulated time (signature)

/**
 * The built history and everything generated since, on one track. History is long (months) and the
 * generated part short (hours), so the track is split: 64% for the history, 36% zoomed on "since build".
 */
function SimTimeline({ s, runs }: { s: SimStatus; runs: RunSummary[] }) {
  if (!s.history_start || !s.history_end || !s.sim_clock) return null;
  const hs = Date.parse(s.history_start);
  const he = Date.parse(s.history_end);
  const sc = Date.parse(s.sim_clock);
  const zoomEnd = Math.max(sc, he + 24 * HOUR) + 2 * HOUR;
  const span = zoomEnd - he;
  const since = Math.max(0, sc - he);
  const recent = runs.filter((r) => r.kind !== 'build' && r.sim_from && r.sim_to && Date.parse(r.sim_to) > he).slice(0, 120);
  const pos = (t: number) => `${Math.min(100, Math.max(0, ((t - he) / span) * 100))}%`;
  const years: number[] = [];
  for (let y = new Date(hs).getUTCFullYear() + 1; y <= new Date(he).getUTCFullYear(); y += 1) years.push(y);
  const scenarios = recent.filter((r) => r.kind === 'scenario');

  return (
    <section aria-labelledby="simtime" className="mt-4 rounded-l border border-line px-4 pb-3 pt-3">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 id="simtime" className="text-md font-semibold">
          Simulated time
        </h2>
        <div className="flex items-center gap-4 text-sm text-muted">
          <span className="flex items-center gap-1.5">
            <span className="h-2 w-4 rounded-[2px] bg-accent/25" aria-hidden /> Built history
          </span>
          <span className="flex items-center gap-1.5">
            <span className="h-2 w-4 rounded-[2px] bg-accent" aria-hidden /> Generated since the build
          </span>
          {scenarios.length ? (
            <span className="flex items-center gap-1.5">
              <span className="h-3 w-[3px] rounded-full bg-signal" aria-hidden /> Scenario
            </span>
          ) : null}
        </div>
      </div>
      <div className="mt-3 grid grid-cols-[64fr_36fr] gap-1.5" role="img" aria-label={`History from ${fmtUtc(s.history_start, 'MMM d, yyyy')} to ${fmtUtc(s.history_end, 'MMM d, yyyy')}, then ${fmtSimHours(Math.round(since / HOUR))} generated since the build, up to ${fmtUtc(s.sim_clock)} UTC`}>
        <div>
          <div className="relative h-3 rounded-l-[4px] bg-accent/25">
            {years.map((y) => (
              <span key={y} className="absolute inset-y-0 w-px bg-sheet" style={{ left: `${((Date.UTC(y, 0, 1) - hs) / (he - hs)) * 100}%` }} />
            ))}
          </div>
          <div className="num relative mt-1.5 flex h-4 justify-between text-xs text-muted">
            <span>{fmtUtc(s.history_start, 'MMM d, yyyy')}</span>
            {years.map((y) => {
              const left = ((Date.UTC(y, 0, 1) - hs) / (he - hs)) * 100;
              return left > 12 && left < 88 ? (
                <span key={y} className="absolute -translate-x-1/2" style={{ left: `${left}%` }}>
                  {y}
                </span>
              ) : null;
            })}
            <span>{fmtUtc(s.history_end, 'MMM d')}</span>
          </div>
        </div>
        <div>
          <div className="relative h-3 rounded-r-[4px] bg-raised ring-1 ring-inset ring-line">
            <div className="absolute inset-y-0 left-0 bg-accent" style={{ width: pos(sc) }} />
            {recent.map((r) => (
              <span key={r.run_id} className="absolute inset-y-0 w-px bg-sheet/70" style={{ left: pos(Date.parse(r.sim_from!)) }} />
            ))}
            {scenarios.map((r) => (
              <Tooltip key={r.run_id} content={r.label}>
                <span className="absolute -top-1 h-5 w-[3px] -translate-x-1/2 rounded-full bg-signal" style={{ left: pos(Date.parse(r.sim_from!)) }} />
              </Tooltip>
            ))}
          </div>
          <div className="num mt-1.5 flex h-4 justify-between text-xs text-muted">
            <span>+{fmtSimHours(Math.round(since / HOUR))} generated</span>
            <span className="font-medium text-ink">{fmtUtc(s.sim_clock, 'MMM d, HH:mm')}</span>
          </div>
        </div>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------- batch

const BATCH_PRESETS = [
  { value: '1', label: '1 h' },
  { value: '6', label: '6 h' },
  { value: '24', label: '1 day' },
  { value: '72', label: '3 days' },
  { value: '168', label: '7 days' },
];

function BatchPanel({ s }: { s: SimStatus | undefined }) {
  const { runBatch, pending } = useSimActions();
  const [choice, setChoice] = useState('24');
  const [custom, setCustom] = useState('');
  const [unit, setUnit] = useState<'hours' | 'days'>('days');
  const id = useId();
  const customHours = custom ? Math.round(Number(custom) * (unit === 'days' ? 24 : 1)) : NaN;
  const hours = choice === 'custom' ? customHours : Number(choice);
  const valid = Number.isFinite(hours) && hours >= 1 && hours <= 744;
  const busy = s?.state && s.state !== 'idle';
  const reason =
    s?.state === 'streaming' ? 'Stop the stream to run a batch.' : s?.state === 'building' ? 'Available when the build finishes.' : s?.state === 'running_batch' ? 'A batch is running.' : !s?.sim_clock ? 'Build the dataset first.' : null;
  const target = s?.sim_clock && valid ? fmtUtc(new Date(Date.parse(s.sim_clock) + hours * HOUR).toISOString(), 'MMM d, HH:mm') : null;

  return (
    <Panel title="Generate batch" meta="Inserts, updates and deletes in one go" className="col-span-5 flex flex-col max-[1100px]:col-span-12" bodyClassName="flex flex-1 flex-col">
      <div className="flex flex-1 flex-col gap-4">
        <div className="flex flex-wrap items-center gap-2">
          <Segmented
            ariaLabel="Batch length"
            value={choice}
            onValueChange={setChoice}
            options={[...BATCH_PRESETS, { value: 'custom', label: 'Custom' }]}
          />
        </div>
        {choice === 'custom' ? (
          <div className="grid grid-cols-[1fr_120px] items-end gap-2">
            <div>
              <Label htmlFor={id} hint="1 hour to 31 days">
                Length
              </Label>
              <Input
                id={id}
                inputMode="decimal"
                value={custom}
                autoFocus
                onChange={(e) => setCustom(e.target.value.replace(/[^\d.]/g, ''))}
                aria-invalid={!!custom && !valid}
                className="num"
                placeholder={unit === 'days' ? '2' : '36'}
              />
            </div>
            <Select
              ariaLabel="Unit"
              value={unit}
              onValueChange={(v) => setUnit(v as 'hours' | 'days')}
              options={[
                { value: 'hours', label: 'hours' },
                { value: 'days', label: 'days' },
              ]}
            />
          </div>
        ) : null}
        <p className="num min-h-5 text-base text-muted">
          {reason ? reason : target ? <>Sim clock moves to <span className="font-medium text-ink">{target} UTC</span></> : 'Enter a length between 1 hour and 31 days.'}
        </p>
        <div className="mt-auto">
          <Button variant="primary" size="lg" disabled={!!busy || !valid || !s?.sim_clock} loading={pending.batch} onClick={() => void runBatch(hours)}>
            Generate {valid ? fmtSimHours(hours) : 'batch'}
          </Button>
        </div>
      </div>
    </Panel>
  );
}

// ---------------------------------------------------------------- stream

const INTERVALS = ['1', '2', '5', '10', '30'];

function StreamPanel({ s }: { s: SimStatus | undefined }) {
  const { startStream, stopStream, pending } = useSimActions();
  const running = s?.state === 'streaming';
  const [interval, setInterval_] = useState<string | null>(null);
  const [speed, setSpeed] = useState<string | null>(null);
  const iv = interval ?? String(s?.stream.interval_s ?? 5);
  const sp = speed ?? String(s?.stream.speed ?? 60);
  const simPerTick = Number(iv) * Number(sp);
  const blocked = !running && s?.state !== 'idle';
  return (
    <Panel
      title="Stream"
      meta={running ? undefined : 'Small changes on a timer, for CDC'}
      actions={running ? <Pill tone="accent" icon={<Dot tone="accent" pulse className="size-1.5" />}>Running</Pill> : null}
      className="col-span-4 flex flex-col max-[1100px]:col-span-7 max-sm:col-span-12"
      bodyClassName="flex flex-1 flex-col"
    >
      <div className="flex flex-1 flex-col gap-4">
        <div className="flex flex-wrap items-end gap-3">
          <div>
            <Label>Every</Label>
            <Select
              ariaLabel="Tick interval"
              value={iv}
              onValueChange={setInterval_}
              disabled={running}
              className="w-24"
              options={INTERVALS.map((v) => ({ value: v, label: `${v} s` }))}
            />
          </div>
          <div>
            <Label>Speed</Label>
            <Segmented
              ariaLabel="Stream speed"
              value={sp}
              onValueChange={(v) => !running && setSpeed(v)}
              options={[
                { value: '1', label: '1×' },
                { value: '10', label: '10×' },
                { value: '60', label: '60×' },
              ]}
            />
          </div>
        </div>
        <p className="num min-h-5 text-base text-muted">
          {running ? (
            <>
              <span className="figure text-xl font-semibold text-ink">{fmtInt(s?.stream.ticks ?? 0)}</span> ticks · each adds{' '}
              {fmtDuration(simPerTick)} of sim time
            </>
          ) : blocked ? (
            s?.state === 'building' ? 'Available when the build finishes.' : 'Wait for the batch to finish.'
          ) : (
            <>Each tick adds {fmtDuration(simPerTick)} of sim time</>
          )}
        </p>
        <div className="mt-auto">
          {running ? (
            <Button size="lg" variant="danger" icon={<Square aria-hidden />} loading={pending.stream} onClick={() => void stopStream()}>
              Stop stream
            </Button>
          ) : (
            <Button size="lg" icon={<Play aria-hidden />} disabled={blocked || !s?.sim_clock} loading={pending.stream} onClick={() => void startStream(Number(iv), Number(sp))}>
              Start stream
            </Button>
          )}
        </div>
      </div>
    </Panel>
  );
}

// ---------------------------------------------------------------- data quality summary

function QualitySummary({ s }: { s: SimStatus | undefined }) {
  const cats = Object.values(s?.dq.categories ?? {});
  const on = cats.filter(Boolean).length;
  return (
    <Panel
      title="Bad data"
      className="col-span-3 max-[1100px]:col-span-5 max-sm:col-span-12"
      actions={
        <Link to="/control/quality" className="text-sm font-medium text-accent-strong hover:underline">
          Adjust
        </Link>
      }
    >
      {s ? (
        <div className="flex flex-col gap-3">
          <div>
            <div className="text-sm text-muted">Rate per category</div>
            <div className="figure num text-xl font-semibold text-ink">1 in {fmtInt(s.dq.denominator)}</div>
          </div>
          <div>
            <div className="text-sm text-muted">Categories on</div>
            <div className="num text-md text-ink">
              {on} <span className="text-muted">of {cats.length}</span>
            </div>
          </div>
          <p className="text-sm text-muted">Every defect is logged to answer_key/anomalies.csv for scoring.</p>
        </div>
      ) : (
        <Skeleton className="h-24 w-full" />
      )}
    </Panel>
  );
}

// ---------------------------------------------------------------- totals by table

function Change({ n, kind }: { n: number; kind: 'ins' | 'upd' | 'del' }) {
  if (!n) return <span className="text-faint">0</span>;
  const sign = kind === 'ins' ? '+' : kind === 'del' ? '−' : '';
  return (
    <span className="text-ink">
      {sign}
      {fmtInt(n)}
    </span>
  );
}

function TotalsPanel({ s }: { s: SimStatus }) {
  const [system, setSystem] = useState('wms');
  const changes = useMemo(() => {
    const m = new Map<string, { ins: number; upd: number; del: number }>();
    for (const c of s.last_run?.changes ?? []) m.set(`${c.system}.${c.table}`, c);
    return m;
  }, [s.last_run]);
  const bySystem = useMemo(() => {
    const m = new Map<string, number>();
    for (const t of s.totals) m.set(t.system, (m.get(t.system) ?? 0) + t.rows);
    return m;
  }, [s.totals]);
  const rows = s.totals.filter((t) => t.system === system);
  const sum = summarise(rows.map((r) => ({ ...r, ins: 0, upd: 0, del: 0, ...changes.get(`${r.system}.${r.table}`) })));
  const unit = system === 'edi' || system === 'files' ? 'Files' : system === 'salesforce' ? 'Records' : 'Rows';
  const where = SYSTEMS.find((x) => x.id === system)?.where;
  const present = SYSTEMS.filter((x) => bySystem.has(x.id));
  const lastLabel = s.last_run ? s.last_run.label ?? KIND_LABEL[s.last_run.kind] ?? s.last_run.kind : null;

  return (
    <Panel
      id="totals"
      title="Rows by table"
      meta={lastLabel ? `Change columns: ${lastLabel}` : undefined}
      className="col-span-8 max-[1100px]:col-span-12"
      bodyClassName="px-0 pb-2 pt-1"
    >
      <div className="px-4">
        <Tabs
          ariaLabel="Source system"
          value={system}
          onValueChange={setSystem}
          items={present.map((x) => ({ value: x.id, label: x.label }))}
        />
      </div>
      <div className="flex items-baseline justify-between px-4 pb-1 pt-3 text-sm text-muted">
        <span>
          {where} · {plural(rows.length, 'table')} · <span className="num">{fmtInt(bySystem.get(system) ?? 0)}</span> {unit.toLowerCase()}
        </span>
        {s.last_run ? (
          <span className="num">
            Last run: +{fmtInt(sum.ins)} · {fmtInt(sum.upd)} updated · {fmtInt(sum.del)} deleted
          </span>
        ) : null}
      </div>
      <table className="w-full text-base">
        <thead>
          <tr className="text-left text-sm text-muted">
            <th scope="col" className="h-9 pl-4 font-normal">
              Table
            </th>
            <th scope="col" className="num h-9 pr-4 text-right font-normal">
              {unit}
            </th>
            <th scope="col" className="num h-9 w-28 pr-4 text-right font-normal">
              Inserted
            </th>
            <th scope="col" className="num h-9 w-28 pr-4 text-right font-normal">
              Updated
            </th>
            <th scope="col" className="num h-9 w-28 pr-4 text-right font-normal">
              Deleted
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((t) => {
            const c = changes.get(`${t.system}.${t.table}`) ?? { ins: 0, upd: 0, del: 0 };
            return (
              <tr key={t.table} className="border-t border-rule hover:bg-hover/60">
                <td className="h-9 pl-4">
                  <span className={clsx(system === 'edi' || system === 'files' ? 'font-sans' : 'font-mono text-[13px]', 'text-ink')}>{t.table}</span>
                </td>
                <td className="num h-9 pr-4 text-right text-ink">{fmtInt(t.rows)}</td>
                <td className="num h-9 pr-4 text-right">
                  <Change n={c.ins} kind="ins" />
                </td>
                <td className="num h-9 pr-4 text-right">
                  <Change n={c.upd} kind="upd" />
                </td>
                <td className="num h-9 pr-4 text-right">
                  <Change n={c.del} kind="del" />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </Panel>
  );
}

// ---------------------------------------------------------------- activity feed

function RunRow({ r }: { r: RunSummary }) {
  const t = summarise(r.changes);
  const kind = KIND_LABEL[r.kind] ?? r.kind;
  return (
    <motion.li
      layout="position"
      initial={{ opacity: 0, transform: 'translateY(-6px)' }}
      animate={{ opacity: 1, transform: 'translateY(0px)' }}
      transition={{ duration: 0.22, ease: [0.23, 1, 0.32, 1] }}
      className="border-t border-rule px-4 py-2.5 first:border-t-0"
    >
      <div className="flex items-baseline justify-between gap-3">
        <span className="min-w-0 truncate text-base text-ink">
          {r.status === 'error' ? <Dot tone="bad" className="mr-1.5 align-middle" /> : null}
          {r.kind === 'stream_tick' ? r.label : (r.label ?? kind)}
        </span>
        <span className="num shrink-0 text-sm text-muted">{fmtRelative(r.finished_at ?? r.started_at)}</span>
      </div>
      <div className="num mt-0.5 truncate text-sm text-muted">
        {r.status === 'error' ? (
          <span className="text-bad">{r.error}</span>
        ) : r.kind === 'build' ? (
          <>{fmtInt(t.ins)} rows loaded</>
        ) : (
          <>
            +{fmtInt(t.ins)} · {fmtInt(t.upd)} upd · {fmtInt(t.del)} del
            {r.sim_from && r.sim_to ? <> · {fmtSimHours(Math.max(1, Math.round((Date.parse(r.sim_to) - Date.parse(r.sim_from)) / HOUR)))} sim</> : null}
          </>
        )}
      </div>
    </motion.li>
  );
}

function ActivityPanel({ streaming }: { streaming: boolean }) {
  const runs = useRuns();
  const feed = useLive((s) => s.feed);
  const list = useMemo(() => {
    const merged = [...feed, ...(runs.data ?? [])];
    const seen = new Set<string>();
    return merged
      .filter((r) => (seen.has(String(r.run_id)) ? false : (seen.add(String(r.run_id)), true)))
      .sort((a, b) => (a.started_at < b.started_at ? 1 : -1))
      .slice(0, 9);
  }, [feed, runs.data]);
  return (
    <Panel
      title="Recent runs"
      meta={streaming ? 'live' : undefined}
      className="col-span-4 self-start max-[1100px]:col-span-12"
      bodyClassName="px-0 pb-2 pt-1"
      actions={
        <Link to="/control/runs" className="flex items-center gap-1 text-sm font-medium text-accent-strong hover:underline">
          Run history <ArrowRight className="size-3.5" aria-hidden />
        </Link>
      }
    >
      {runs.isError ? (
        <div className="px-4">
          <ErrorNote error={runs.error} onRetry={() => void runs.refetch()} what="runs" />
        </div>
      ) : runs.isLoading ? (
        <div className="flex flex-col gap-3 px-4 py-2">
          {Array.from({ length: 5 }, (_, i) => (
            <Skeleton key={i} className="h-9 w-full" />
          ))}
        </div>
      ) : list.length === 0 ? (
        <div className="px-4">
          <Empty>No runs yet. Build the dataset, then generate a batch.</Empty>
        </div>
      ) : (
        <ul aria-live="polite" aria-relevant="additions">
          <AnimatePresence initial={false}>
            {list.map((r) => (
              <RunRow key={r.run_id} r={r} />
            ))}
          </AnimatePresence>
        </ul>
      )}
    </Panel>
  );
}

// ---------------------------------------------------------------- build progress

function BuildProgress({ s }: { s: SimStatus }) {
  const progress = useLive((l) => l.progress);
  const logs = useLive((l) => l.logs);
  const pct = progress?.pct ?? s.build?.progress ?? 0;
  const phase = progress?.phase ?? s.build?.phase ?? 'Starting';
  const eta = progress?.eta_s ?? s.build?.eta_s ?? null;
  const cancel = useCancelBuild();
  const [asking, setAsking] = useState(false);
  const cancelling = phase === 'cancelling' || s.build?.phase === 'cancelling';
  const confirmCancel = async () => {
    try {
      await cancel.mutateAsync();
      setAsking(false);
    } catch (e) {
      toast.error('Could not cancel the build', { description: e instanceof Error ? e.message : String(e) });
    }
  };
  return (
    <section aria-labelledby="buildprog" className="mt-4 rounded-l border border-line p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 id="buildprog" className="text-md font-semibold">
          Building the {s.preset ? PRESET_INFO[s.preset].label : ''} dataset
        </h2>
        <span className="num text-base text-muted">
          <span className="figure text-xl font-semibold text-ink">{Math.floor(pct * 100)}%</span>
          {eta != null && !cancelling ? <> · about {fmtDuration(eta).replace(/ \d+ s$/, '')} left</> : null}
        </span>
      </div>
      <div
        className="mt-3 h-2 overflow-hidden rounded-full bg-active"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(pct * 100)}
        aria-valuetext={`${Math.round(pct * 100)}%, ${phase}`}
      >
        <div className="h-full origin-left rounded-full bg-accent transition-transform duration-300 ease-out" style={{ transform: `scaleX(${Math.max(0.005, pct)})` }} />
      </div>
      <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
        <p className="text-base text-ink">{cancelling ? 'Cancelling: stopping at the end of the current step' : phase}</p>
        <Button variant="secondary" size="sm" disabled={cancelling} onClick={() => setAsking(true)}>
          {cancelling ? 'Cancelling…' : 'Cancel build'}
        </Button>
      </div>
      {logs.length ? (
        <ol className="mt-3 max-h-36 overflow-y-auto rounded-m bg-raised px-3 py-2 font-mono text-[12px] leading-5 text-muted" aria-label="Build log">
          {logs.slice(-8).map((l, i) => (
            <li key={`${l.ts}-${i}`} className={clsx(l.level === 'error' && 'text-bad', l.level === 'warning' && 'text-warn')}>
              <span className="text-faint">{fmtUtc(l.ts, 'HH:mm:ss')}</span> {l.msg}
            </li>
          ))}
        </ol>
      ) : null}
      <Dialog
        open={asking}
        onOpenChange={setAsking}
        tone="danger"
        title="Cancel this build?"
        description="It stops at the end of the current step and leaves incomplete data, which is not counted as built. The data it replaced went when it started, so you will need to build again. Your tracker is kept."
        footer={
          <>
            <Button variant="ghost" onClick={() => setAsking(false)}>
              Keep building
            </Button>
            <Button variant="danger-solid" loading={cancel.isPending} onClick={() => void confirmCancel()}>
              Cancel build
            </Button>
          </>
        }
      />
    </section>
  );
}

// ---------------------------------------------------------------- page

function finding(s: SimStatus | undefined): string {
  if (!s) return 'Reading the simulator status…';
  if (!s.sim_clock) return 'No data yet. Build the dataset to create the operating history across every source system.';
  if (s.state === 'building') return 'A build is running. Batches, the stream and scenarios come back when it finishes.';
  if (!s.built_at && s.last_run?.status === 'cancelled') return 'Build cancelled. The data is incomplete: build the dataset to start again.';
  if (!s.built_at) return s.error ?? 'The last build did not finish, so this data is incomplete. Build the dataset again.';
  if (s.state === 'streaming')
    return `Streaming: every ${s.stream.interval_s} s the simulator applies ${fmtDuration(s.stream.interval_s * s.stream.speed)} of operations. Point your CDC at it now.`;
  const lr = s.last_run;
  if (lr && lr.kind !== 'build') {
    const t = summarise(lr.changes);
    return `${lr.label ?? KIND_LABEL[lr.kind]} changed ${fmtInt(t.ins + t.upd + t.del)} rows across ${t.tables} tables ${fmtRelative(lr.finished_at ?? lr.started_at)}.`;
  }
  return 'The dataset is built and the clock is waiting. Generate a batch or start the stream.';
}

export default function ControlOverview() {
  const status = useStatus();
  const runs = useRuns();
  const buildOpen = ui.use((u) => u.buildDialog);
  const s = status.data;
  const offline = status.isError && !s;

  return (
    <>
      <PageHeader
        title="Control Center"
        subtitle={offline ? 'The simulator API is not answering.' : finding(s)}
        actions={
          <>
            {import.meta.env.VITE_DEMO === '1' ? null : (
              <a href="/docs" target="_blank" rel="noreferrer" className={buttonClasses('ghost')}>
                API docs
              </a>
            )}
            <Button icon={<Hammer aria-hidden />} onClick={() => ui.set({ buildDialog: true })} disabled={s?.state === 'building'}>
              Build dataset
            </Button>
          </>
        }
      />

      {offline ? (
        <ErrorNote error={status.error} onRetry={() => void status.refetch()} what="the simulator status" />
      ) : (
        <>
          <StatusStrip s={s} />
          {s?.state === 'building' ? <BuildProgress s={s} /> : s ? <SimTimeline s={s} runs={runs.data ?? []} /> : null}
          {s && !s.built_at && s.state !== 'building' ? (
            <div className="mt-4">
              <Empty action={<Button variant="primary" onClick={() => ui.set({ buildDialog: true })}>Build dataset</Button>}>
                {s.sim_clock ? 'The last build did not finish, so this data is incomplete.' : 'Nothing simulated yet.'} Medium has everything the projects need and takes about an hour on a laptop.
              </Empty>
            </div>
          ) : null}

          <div className="mt-4 grid grid-cols-12 gap-4">
            <BatchPanel s={s} />
            <StreamPanel s={s} />
            <QualitySummary s={s} />
          </div>

          <div className="mt-4 grid grid-cols-12 items-start gap-4">
            {s ? (
              <TotalsPanel s={s} />
            ) : (
              <Panel title="Rows by table" className="col-span-8 max-[1100px]:col-span-12">
                <div className="flex flex-col gap-2">
                  {Array.from({ length: 8 }, (_, i) => (
                    <Skeleton key={i} className="h-7 w-full" />
                  ))}
                </div>
              </Panel>
            )}
            <ActivityPanel streaming={s?.state === 'streaming'} />
          </div>
        </>
      )}

      <BuildDialog open={buildOpen} onOpenChange={(v) => ui.set({ buildDialog: v })} />
    </>
  );
}

