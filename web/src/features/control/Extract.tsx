import { useEffect, useId, useMemo, useState, type ReactNode } from 'react';
import { Link } from 'react-router';
import clsx from 'clsx';
import { AnimatePresence, motion } from 'motion/react';
import { AlertTriangle, ArrowRight, Download, PackageCheck, Plus, Square, X } from 'lucide-react';
import { toast } from 'sonner';
import { uat } from '@/api/client';
import { useBuildExtract, useExtractOptions } from '@/api/queries';
import type { ExtractOptions, ExtractProject, ExtractResult } from '@/api/types';
import { Button, IconButton, buttonClasses } from '@/components/ui/button';
import { FieldNote, Label, Select } from '@/components/ui/field';
import { Dot, Empty, ErrorNote, PageHeader, Panel, Pill, Skeleton, download, type Tone } from '@/components/ui/misc';
import { fmtRelative, fmtUtc } from '@/lib/dates';
import { bytesText, fmtDuration, fmtInt, plural } from '@/lib/format';
import { useSimActions } from './actions';
import { systemLabel } from './systems';

const PROJECT_KEY = 'hl.extract.project';
const DEMO_ZIP_NOTE = 'In this preview the zip holds the file list only. The rows are available when you install it.';
const SYSTEM_ORDER = ['wms', 'ax2012', 'iot', 'salesforce', 'carrier', 'edi', 'files', 'platform'];

function monthLabel(m: string, long = false) {
  const [y, mm] = m.split('-').map(Number);
  return new Date(Date.UTC(y, mm - 1, 1)).toLocaleString('en-US', { month: long ? 'long' : 'short', year: 'numeric', timeZone: 'UTC' });
}

function monthsText(months: string[]) {
  return months.length ? months.map((m) => monthLabel(m)).join(', ') : 'Live state, last 8 days';
}

function readStoredProject(): string | null {
  try {
    return localStorage.getItem(PROJECT_KEY);
  } catch {
    return null;
  }
}

function storeProject(key: string) {
  try {
    localStorage.setItem(PROJECT_KEY, key);
  } catch {
    /* private window: the choice just isn't remembered */
  }
}

const sameMonths = (a: string[], b: string[]) => a.length === b.length && a.every((m, i) => m === b[i]);

// ---------------------------------------------------------------- readiness

function stillView(o: ExtractOptions | undefined): { tone: Tone; label: string; pulse?: boolean; sub: string } {
  if (!o) return { tone: 'neutral', label: 'Checking…', sub: 'Reading the simulator state' };
  switch (o.reason_code) {
    case null:
      return { tone: 'good', label: 'Still', sub: 'Nothing is changing. Safe to extract.' };
    case 'streaming':
      return { tone: 'accent', label: 'Stream running', pulse: true, sub: 'The sources change every tick' };
    case 'building':
      return { tone: 'warn', label: 'Building', pulse: true, sub: 'A build is replacing the data' };
    case 'busy':
      return { tone: 'info', label: 'Batch running', pulse: true, sub: 'Rows are being written' };
    case 'extracting':
      return { tone: 'info', label: 'Extracting', pulse: true, sub: 'Another extract is being built' };
    case 'no_months':
      return { tone: 'warn', label: 'No closed month', sub: 'The history has no whole month yet' };
    default:
      return { tone: 'neutral', label: 'Not built', sub: 'There is no data yet' };
  }
}

function Cell({ label, children, sub, className }: { label: string; children: ReactNode; sub?: ReactNode; className?: string }) {
  return (
    <div className={clsx('min-w-0 px-4 py-3.5', className)}>
      <div className="text-sm text-muted">{label}</div>
      <div className="mt-1 truncate">{children}</div>
      {sub ? <div className="mt-1 line-clamp-2 text-sm text-muted">{sub}</div> : null}
    </div>
  );
}

function ReadinessStrip({ o }: { o: ExtractOptions | undefined }) {
  const v = stillView(o);
  const months = o?.months ?? [];
  return (
    <div className="grid grid-cols-[1.6fr_1fr_1fr] divide-x divide-line rounded-l border border-line max-[900px]:grid-cols-1 max-[900px]:divide-x-0 max-[900px]:divide-y">
      <Cell label="Data as of (sim clock, UTC)" sub="Your dashboard’s data-as-of must match this time.">
        {o ? (
          <span className="figure num text-[1.875rem] font-semibold leading-9 text-ink">{o.sim_clock ? fmtUtc(o.sim_clock, 'EEE MMM d · HH:mm') : '—'}</span>
        ) : (
          <Skeleton className="h-9 w-56" />
        )}
      </Cell>
      <Cell label="Sources" sub={v.sub}>
        {o ? (
          <span className="flex items-center gap-2 text-xl font-semibold text-ink">
            <Dot tone={v.tone} pulse={v.pulse} />
            {v.label}
          </span>
        ) : (
          <Skeleton className="h-7 w-28" />
        )}
      </Cell>
      <Cell
        label="Closed months"
        sub={months.length ? `${monthLabel(months[0])} – ${monthLabel(months[months.length - 1])}, closed in every site’s local time` : 'None yet'}
      >
        {o ? <span className="figure num text-xl font-semibold text-ink">{fmtInt(months.length)}</span> : <Skeleton className="h-7 w-12" />}
      </Cell>
    </div>
  );
}

function NotStill({ o }: { o: ExtractOptions }) {
  const { stopStream, pending } = useSimActions();
  if (o.ready || !o.reason) return null;
  let action: ReactNode = null;
  if (o.reason_code === 'streaming') {
    action = (
      <Button variant="danger" icon={<Square aria-hidden />} loading={pending.stream} onClick={() => void stopStream()}>
        Stop stream
      </Button>
    );
  } else if (o.reason_code === 'not_built' || o.reason_code === 'no_months') {
    action = (
      <Link to="/control" className={buttonClasses('secondary')}>
        Control Center <ArrowRight className="size-4" aria-hidden />
      </Link>
    );
  }
  return (
    <div role="status" className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-2.5 rounded-m border border-warn/30 bg-warn-bg/60 px-4 py-3">
      <Dot tone="warn" className="shrink-0" />
      <p className="min-w-0 flex-1 text-base text-ink">{o.reason}</p>
      {action}
    </div>
  );
}

// ---------------------------------------------------------------- build form

function MonthPicker({
  project,
  o,
  months,
  onChange,
}: {
  project: ExtractProject;
  o: ExtractOptions;
  months: string[];
  onChange: (m: string[] | null) => void;
}) {
  const id = useId();
  const max = o.max_periods;
  const isDefault = sameMonths(months, project.default_periods);
  const unused = o.months.filter((m) => !months.includes(m));
  const set = (i: number, m: string) => onChange([...months.slice(0, i), m, ...months.slice(i + 1)].sort());
  const remove = (i: number) => onChange(months.filter((_, j) => j !== i));
  const add = () => {
    const pick = [...unused].reverse().find((m) => m < (months[0] ?? '9999')) ?? unused[unused.length - 1];
    if (pick) onChange([...months, pick].sort());
  };
  const readings = o.sensor_readings_from;
  const sensorNote =
    project.key === 'P7' && readings ? (
      <>
        Sensor readings start {fmtUtc(readings, 'MMM d, yyyy')}; months before that have none.
      </>
    ) : null;

  return (
    <fieldset aria-label="Months">
      <Label htmlFor={`${id}-0`} hint={`Up to ${max} · closed months only`}>
        Months
      </Label>
      <ul className="flex flex-col gap-2" aria-label="Chosen months">
        {months.map((m, i) => (
          <li key={m} className="flex items-center gap-1.5">
            <Select
              id={`${id}-${i}`}
              ariaLabel={`Month ${i + 1}`}
              value={m}
              onValueChange={(v) => set(i, v)}
              className="w-44"
              options={[m, ...unused]
                .sort()
                .reverse()
                .map((x) => ({ value: x, label: monthLabel(x, true) }))}
            />
            <IconButton label={`Remove ${monthLabel(m, true)}`} size="sm" disabled={months.length <= 1} onClick={() => remove(i)}>
              <X aria-hidden />
            </IconButton>
          </li>
        ))}
      </ul>
      <div className="mt-2 flex flex-wrap items-center gap-1">
        <Button size="sm" variant="ghost" icon={<Plus aria-hidden />} disabled={months.length >= max || unused.length === 0} onClick={add}>
          Add month
        </Button>
        {!isDefault ? (
          <Button size="sm" variant="quiet" onClick={() => onChange(null)}>
            Use defaults
          </Button>
        ) : null}
      </div>
      <FieldNote>
        {project.key === 'P8'
          ? 'Default: the latest closed month. Project 8 is large; add a month only when a trend point needs it.'
          : project.key === 'P7'
            ? 'Default: the latest closed month and the month three months before it, or the oldest month with sensor readings.'
            : 'Default: the latest closed month and the month three months before it, the two periods Iris samples.'}{' '}
        {sensorNote}
      </FieldNote>
    </fieldset>
  );
}

function BuildProgress({ o, started }: { o: ExtractOptions | undefined; started: number }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(t);
  }, []);
  const p = o?.in_progress;
  const pct = p && p.total ? Math.min(1, p.done / p.total) : 0;
  return (
    <div role="status" aria-live="polite" className="mt-4 rounded-m bg-raised px-3.5 py-3">
      <div className="flex items-baseline justify-between gap-3 text-sm">
        <span className="min-w-0 truncate text-ink">
          {p?.file ? (
            <>
              Writing <span className="font-mono text-[12.5px]">{p.file}</span>
            </>
          ) : (
            'Reading the sources…'
          )}
        </span>
        <span className="num shrink-0 text-muted">
          {p ? `${fmtInt(p.done)} of ${fmtInt(p.total)} · ` : ''}
          {fmtDuration((now - started) / 1000)}
        </span>
      </div>
      <div
        className="mt-2 h-1.5 overflow-hidden rounded-full bg-active"
        role="progressbar"
        aria-label="Extract progress"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(pct * 100)}
      >
        <div className="h-full origin-left rounded-full bg-accent transition-transform duration-300 ease-out" style={{ transform: `scaleX(${Math.max(0.02, pct)})` }} />
      </div>
    </div>
  );
}

function BuildError({ error }: { error: unknown }) {
  const msg = error instanceof Error ? error.message : String(error ?? 'Unknown error');
  return (
    <div role="alert" className="mt-3 flex items-start gap-2.5 rounded-m border border-bad/25 bg-bad-bg/60 px-3.5 py-2.5 text-base">
      <AlertTriangle className="mt-0.5 size-4 shrink-0 text-bad" aria-hidden />
      <span className="min-w-0 text-ink">
        The extract was not built. <span className="text-muted">{msg}</span>
      </span>
    </div>
  );
}

function BuildPanel({
  o,
  project,
  onProject,
  months,
  onMonths,
  onBuild,
  building,
  started,
  error,
}: {
  o: ExtractOptions;
  project: ExtractProject;
  onProject: (key: string) => void;
  months: string[];
  onMonths: (m: string[] | null) => void;
  onBuild: () => void;
  building: boolean;
  started: number;
  error: unknown;
}) {
  const id = useId();
  const noMonths = project.uses_periods && months.length === 0;
  const blocked = !o.ready || noMonths;
  return (
    <Panel title="What to extract" id="extract-form">
      <div className="flex flex-col gap-5">
        <div>
          <Label htmlFor={`${id}-project`}>Project</Label>
          <Select
            id={`${id}-project`}
            ariaLabel="Project"
            value={project.key}
            onValueChange={onProject}
            disabled={building}
            options={o.projects.map((p) => ({ value: p.key, label: `${p.key} · ${p.name}` }))}
          />
          <ul className="mt-2 flex flex-col gap-0.5" aria-label="Dashboards Iris reviews with this extract">
            {project.dashboards.map((d) => (
              <li key={d.id} className="flex items-baseline gap-2 text-sm">
                <span className="num w-12 shrink-0 font-medium text-muted">{d.id}</span>
                <span className="text-ink">{d.name}</span>
              </li>
            ))}
          </ul>
        </div>

        {project.uses_periods ? (
          o.months.length ? (
            <MonthPicker project={project} o={o} months={months} onChange={onMonths} />
          ) : (
            <Empty>No whole month has closed yet.</Empty>
          )
        ) : (
          <div>
            <div className="mb-1.5 text-sm font-medium text-ink">Time frame</div>
            <p className="rounded-m bg-raised px-3 py-2.5 text-base text-muted">{project.note}</p>
          </div>
        )}

        <div>
          <Button variant="primary" size="lg" icon={<PackageCheck aria-hidden />} loading={building} disabled={blocked} onClick={onBuild}>
            {building ? 'Building extract…' : 'Build extract'}
          </Button>
          {building ? <BuildProgress o={o} started={started} /> : null}
          {!building && !o.ready ? <FieldNote>Available when the sources are still.</FieldNote> : null}
          {!building && error ? <BuildError error={error} /> : null}
        </div>
      </div>
    </Panel>
  );
}

function GapsPanel({ project }: { project: ExtractProject }) {
  if (!project.not_in_extract.length) return null;
  return (
    <Panel title="Not in the extract" meta="Iris will ask you for evidence">
      <ul className="flex flex-col">
        {project.not_in_extract.map((g) => (
          <li key={`${g.dashboard}-${g.kpi}`} className="border-t border-rule py-2.5 first:border-t-0 first:pt-0 last:pb-0">
            <div className="flex items-baseline gap-2">
              <span className="num shrink-0 text-sm text-muted">{g.dashboard}</span>
              <span className="text-base font-medium text-ink">{g.kpi}</span>
            </div>
            <p className="mt-0.5 text-sm text-muted">{g.why}</p>
          </li>
        ))}
      </ul>
    </Panel>
  );
}

// ---------------------------------------------------------------- result

function useDownload() {
  const [busy, setBusy] = useState<string | null>(null);
  const run = async (r: ExtractResult) => {
    setBusy(r.id);
    try {
      await download(r.download_url || uat.downloadUrl(r.id), r.filename);
      if (import.meta.env.VITE_DEMO === '1') toast.message('File list downloaded', { description: DEMO_ZIP_NOTE });
    } catch (e) {
      toast.error('Download failed', { description: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(null);
    }
  };
  return { busy, run };
}

function Fact({ label, children, title }: { label: string; children: ReactNode; title?: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-sm text-muted">{label}</dt>
      <dd className="num mt-0.5 truncate text-base text-ink" title={title}>
        {children}
      </dd>
    </div>
  );
}

function FileTable({ r }: { r: ExtractResult }) {
  const groups = useMemo(() => {
    const m = new Map<string, NonNullable<ExtractResult['files']>>();
    for (const f of r.files ?? []) {
      const sys = f.path.split('/')[0];
      m.set(sys, [...(m.get(sys) ?? []), f]);
    }
    return [...m.entries()].sort((a, b) => SYSTEM_ORDER.indexOf(a[0]) - SYSTEM_ORDER.indexOf(b[0]));
  }, [r.files]);
  return (
    <div className="flex flex-col gap-4">
      {groups.map(([sys, files]) => {
        const rows = files.reduce((t, f) => t + (f.rows ?? 0), 0);
        const label = sys === 'platform' ? 'Platform' : systemLabel(sys);
        return (
          <table key={sys} className="w-full text-sm">
            <caption className="mb-1 text-left text-sm font-semibold text-ink">
              {label}{' '}
              <span className="num font-normal text-muted">
                · {plural(files.length, 'file')}
                {sys !== 'files' ? ` · ${fmtInt(rows)} rows` : ''}
              </span>
            </caption>
            <thead className="sr-only">
              <tr>
                <th scope="col">File</th>
                <th scope="col">Rows</th>
                <th scope="col">Size</th>
              </tr>
            </thead>
            <tbody>
              {files.map((f) => (
                <tr key={f.path} className="border-t border-rule">
                  <td className="h-7 max-w-0 truncate pr-3 font-mono text-[12.5px] text-ink">{f.path.slice(sys.length + 1)}</td>
                  <td className="num h-7 w-28 text-right text-ink">{f.rows == null ? <span className="text-faint">—</span> : fmtInt(f.rows)}</td>
                  <td className="num h-7 w-24 text-right text-muted">{bytesText(f.bytes)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        );
      })}
    </div>
  );
}

function ResultPanel({ r, clock, projectName }: { r: ExtractResult | null; clock: string | null; projectName: (key: string) => string }) {
  const dl = useDownload();
  const rows = (r?.files ?? []).reduce((t, f) => t + (f.rows ?? 0), 0);
  const stale = !!(r && clock && Date.parse(clock) !== Date.parse(r.sim_clock));
  return (
    <Panel
      title="Extract"
      meta={r ? `Built ${fmtRelative(r.generated_at)} in ${fmtDuration(r.duration_s)}` : undefined}
      actions={r ? stale ? <Pill tone="warn">Out of date</Pill> : <Pill tone="good">Ready</Pill> : null}
      id="extract-result"
    >
      <AnimatePresence mode="wait" initial={false}>
        {r ? (
          <motion.div
            key={r.id}
            initial={{ opacity: 0, transform: 'translateY(4px)' }}
            animate={{ opacity: 1, transform: 'translateY(0px)' }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.2, ease: [0.23, 1, 0.32, 1] }}
            aria-live="polite"
          >
            <div className="flex items-center justify-between gap-4 max-sm:flex-wrap">
              <div className="min-w-0 flex-1">
                <div className="truncate font-mono text-[13px] text-ink" title={r.filename}>
                  {r.filename}
                </div>
                <div className="num mt-0.5 text-sm text-muted">
                  {bytesText(r.size_bytes)} zip · {plural(r.files?.length ?? 0, 'file')} · {fmtInt(rows)} rows
                </div>
              </div>
              <Button variant="primary" size="lg" icon={<Download aria-hidden />} loading={dl.busy === r.id} onClick={() => void dl.run(r)}>
                Download
              </Button>
            </div>
            <dl className="mt-4 grid grid-cols-[1.5fr_1fr_1fr] gap-4 border-y border-rule py-3 max-sm:grid-cols-1">
              <Fact label="Project" title={`${r.project} · ${projectName(r.project)}`}>
                {r.project} · {projectName(r.project)}
              </Fact>
              <Fact label="Months">{monthsText(r.periods)}</Fact>
              <Fact label="Data as of (UTC)">{fmtUtc(r.sim_clock, 'MMM d, yyyy HH:mm')}</Fact>
            </dl>
            {stale ? (
              <p role="status" className="mt-3 flex items-start gap-2 rounded-m bg-warn-bg/60 px-3 py-2 text-base text-ink">
                <Dot tone="warn" className="mt-1.5" />
                <span>
                  The sim clock has moved since this extract was built. Refresh your dashboard and build a new extract, so Iris
                  compares the same data.
                </span>
              </p>
            ) : null}
            <p className="mt-3 text-base text-muted">
              Attach this zip to your Iris chat with the package zip and your zipped submission (screenshots, KPI and
              end-table exports, bridge.csv, metric catalog, submission.yaml). Keep the stream stopped until she replies,
              so the sources stay as extracted.
            </p>
            {import.meta.env.VITE_DEMO === '1' ? <p className="mt-2 text-base text-muted">{DEMO_ZIP_NOTE}</p> : null}
            <div className="mt-4">
              <FileTable r={r} />
            </div>
          </motion.div>
        ) : (
          <motion.div key="empty" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.15 }}>
            <Empty>Choose a project and months, then build. The zip shows up here with every file and its row count.</Empty>
          </motion.div>
        )}
      </AnimatePresence>
    </Panel>
  );
}

function RecentPanel({ items, current }: { items: ExtractResult[]; current: string | null }) {
  const dl = useDownload();
  return (
    <Panel title="Recent extracts" meta="The last five are kept" bodyClassName="px-0 pb-2 pt-1">
      {items.length === 0 ? (
        <div className="px-4 pb-2">
          <Empty>No extracts yet.</Empty>
        </div>
      ) : (
        <div className="scroll-thin overflow-x-auto">
          <table className="w-full min-w-[560px] text-base">
            <thead>
              <tr className="text-left text-sm text-muted">
                <th scope="col" className="h-9 pl-4 font-normal">
                  Project
                </th>
                <th scope="col" className="h-9 font-normal">
                  Months
                </th>
                <th scope="col" className="h-9 font-normal">
                  Data as of (UTC)
                </th>
                <th scope="col" className="num h-9 pr-4 text-right font-normal">
                  Size
                </th>
                <th scope="col" className="h-9 w-12 pr-3">
                  <span className="sr-only">Download</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {items.map((r) => (
                <tr key={r.id} className={clsx('border-t border-rule', r.id === current ? 'bg-accent-tint/50' : 'hover:bg-hover/60')}>
                  <td className="h-10 pl-4 font-medium text-ink">{r.project}</td>
                  <td className="num h-10 text-ink">{monthsText(r.periods)}</td>
                  <td className="num h-10 whitespace-nowrap text-muted">
                    {fmtUtc(r.sim_clock, 'MMM d, HH:mm')} <span className="text-faint">· built {fmtRelative(r.generated_at)}</span>
                  </td>
                  <td className="num h-10 pr-4 text-right text-muted">{bytesText(r.size_bytes)}</td>
                  <td className="h-10 pr-3 text-right">
                    <IconButton label={`Download ${r.filename}`} size="sm" disabled={dl.busy === r.id} onClick={() => void dl.run(r)}>
                      <Download aria-hidden />
                    </IconButton>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

// ---------------------------------------------------------------- page

export default function ExtractPage() {
  const build = useBuildExtract();
  const opts = useExtractOptions(build.isPending);
  const o = opts.data;
  const [projectKey, setProjectKey] = useState<string | null>(readStoredProject);
  const [chosen, setChosen] = useState<string[] | null>(null);
  const [result, setResult] = useState<ExtractResult | null>(null);
  const [started, setStarted] = useState(0);

  const project = o?.projects.find((p) => p.key === projectKey) ?? o?.projects[0];
  const months = useMemo(() => {
    if (!project || !o) return [];
    const valid = (chosen ?? project.default_periods).filter((m) => o.months.includes(m));
    return valid.length ? valid : project.default_periods;
  }, [chosen, project, o]);

  const chooseProject = (key: string) => {
    setProjectKey(key);
    setChosen(null);
    storeProject(key);
  };

  const run = async () => {
    if (!project) return;
    setStarted(Date.now());
    build.reset();
    try {
      const r = await build.mutateAsync({ project: project.key, periods: project.uses_periods ? months : undefined });
      setResult(r);
      toast.success('Extract ready', { description: `${r.project} · ${bytesText(r.size_bytes)} · ${plural(r.files?.length ?? 0, 'file')}` });
    } catch (e) {
      toast.error('Extract was not built', { description: e instanceof Error ? e.message : String(e) });
      void opts.refetch();
    }
  };

  const projectName = (key: string) => o?.projects.find((p) => p.key === key)?.name ?? '';

  return (
    <>
      <PageHeader
        title="Source extract"
        subtitle="Iris checks your dashboard against the source systems. Stop the stream, refresh your dashboard, then build this extract and attach it to your Iris chat with your submission."
      />

      {opts.isError && !o ? (
        <ErrorNote error={opts.error} onRetry={() => void opts.refetch()} what="the extract options" />
      ) : (
        <>
          <ReadinessStrip o={o} />
          {o ? <NotStill o={o} /> : null}

          <div className="mt-4 grid grid-cols-12 items-start gap-4">
            <div className="col-span-5 flex flex-col gap-4 max-[1100px]:col-span-12">
              {o && project ? (
                <>
                  <BuildPanel
                    o={o}
                    project={project}
                    onProject={chooseProject}
                    months={months}
                    onMonths={setChosen}
                    onBuild={() => void run()}
                    building={build.isPending}
                    started={started}
                    error={build.error}
                  />
                  <GapsPanel project={project} />
                </>
              ) : (
                <Panel title="What to extract">
                  <div className="flex flex-col gap-3">
                    <Skeleton className="h-8 w-full" />
                    <Skeleton className="h-12 w-3/4" />
                    <Skeleton className="h-8 w-44" />
                    <Skeleton className="h-9 w-36" />
                  </div>
                </Panel>
              )}
            </div>
            <div className="col-span-7 flex flex-col gap-4 max-[1100px]:col-span-12">
              <ResultPanel r={result} clock={o?.sim_clock ?? null} projectName={projectName} />
              {o ? <RecentPanel items={o.recent} current={result?.id ?? null} /> : null}
            </div>
          </div>
        </>
      )}
    </>
  );
}
