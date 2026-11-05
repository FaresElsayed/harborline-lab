/* In-browser /api/uat for VITE_MOCK=1. File lists and row counts come from a real Small build scaled to the Large
   preset (fixtures/extract-files.json), then to the current preset's volume; the downloadable zip holds the manifest
   only, marked as demo data. */

import type { ExtractGap, ExtractOptions, ExtractProgress, ExtractProject, ExtractResult } from '@/api/types';
import fixture from './fixtures/extract-files.json';
import { status, volumeOf } from './sim';
import { HttpError } from './tracker';

type Mode = 'two' | 'latest' | 'iot' | 'none';
interface Def {
  key: string;
  name: string;
  dashboards: Array<[string, string]>;
  mode: Mode;
  note?: string;
  gaps: Array<[string, string, string]>;
}

const RECENT_DAYS = 8;
const MAX_PERIODS = 3;
const KEEP = 5;
const IOT_DAYS = 90;
const HOUR = 3_600_000;
const WESTMOST_OFFSET_H = 8; // the last site to close a month is on Pacific time

const DEFS: Def[] = [
  {
    key: 'P1',
    name: 'Data Platform Foundation',
    dashboards: [['P1-D1', 'Data Platform Health']],
    mode: 'two',
    gaps: [
      ['P1-D1', 'Run success rate and run duration p95', 'Come from the engineer’s orchestrator logs.'],
      ['P1-D1', 'DQ pass rate and rows in quarantine', 'Come from the engineer’s own data quality checks.'],
      ['P1-D1', 'Quota used', 'Comes from the platform’s usage or billing report.'],
      ['P1-D1', 'Deletes in volume anomalies', 'The sources delete rows physically and keep no trace; daily_activity.csv shows created and changed rows only.'],
    ],
  },
  {
    key: 'P2',
    name: 'Warehouse Performance Scorecard',
    dashboards: [
      ['P2-D1', 'Warehouse Ops Scorecard'],
      ['P2-D2', 'Site Operations Detail'],
    ],
    mode: 'two',
    gaps: [
      ['P2-D1', 'Space utilization (past days)', 'The WMS keeps current inventory only, so end-of-day occupancy for a past date cannot be rebuilt from the sources. Today’s utilization can: inventory.csv, location.csv and the warehouse master workbook.'],
      ['P2-D1', 'Weather and holiday annotations', 'Public reference data, not a Harborline source.'],
      ['P2-D2', 'Productivity vs standard', 'The engineered standards are agreed values in the metric catalog; the actual picks per productive hour can be re-performed.'],
    ],
  },
  {
    key: 'P3',
    name: 'Billing Assurance & AR',
    dashboards: [
      ['P3-D1', 'Revenue Assurance'],
      ['P3-D2', 'AR & Cash'],
      ['P3-D3', 'Warehouse P&L vs Budget'],
    ],
    mode: 'two',
    gaps: [
      ['P3-D1', 'FX rate gap (market side)', 'ECB reference rates are public market data, not a Harborline source. The AX accounting rates are in exchangerate.csv (stored x100).'],
      ['P3-D1', 'Storage charges for past months (STOR-PLT, STOR-BIN, COLD-SUR)', 'The WMS keeps current inventory only, so past storage activity cannot be re-derived; tie storage to billing_record instead.'],
      ['P3-D3', 'Year to date and 24-month trends', 'Only the chosen months of GL entries are included. Add the months you need as periods (up to three per extract).'],
    ],
  },
  {
    key: 'P4',
    name: 'Freight Audit & Carrier Performance',
    dashboards: [
      ['P4-D1', 'Freight Audit'],
      ['P4-D2', 'Carrier Scorecard'],
    ],
    mode: 'two',
    gaps: [
      ['P4-D1', 'Fuel surcharge variance (expected %)', 'Uses the weekly diesel file in landing/files/reference. The carriers’ fuel tables are in the rate card workbooks; the applied % is on the bill lines.'],
      ['P4-D1', 'Disputable now', 'Each carrier’s dispute window is an agreed value in the metric catalog; the overbilled lines themselves can be re-performed.'],
      ['P4-D2', 'Composite score', 'Weights and the minimum-volume rule are agreed values in the metric catalog.'],
    ],
  },
  {
    key: 'P5',
    name: 'Client 360',
    dashboards: [
      ['P5-D1', 'Sales Pipeline'],
      ['P5-D2', 'Client Health 360'],
    ],
    mode: 'two',
    gaps: [
      ['P5-D1', 'Pipeline coverage (target)', 'The new-business target is versioned seed data agreed with Hannah Price, not a source system; open pipeline itself can be re-performed.'],
      ['P5-D2', 'Health score history and drivers', 'Only the current Health_Score__c and Churn_Risk__c are in Salesforce; weekly history and weights come from the engineer’s model and metric catalog.'],
    ],
  },
  {
    key: 'P6',
    name: 'Real-Time Operations',
    dashboards: [['P6-D1', 'Live Operations Board']],
    mode: 'none',
    note: `Project 6 is a live board: the extract holds the state at the sim clock and the last ${RECENT_DAYS} days (today and the same weekday last week). Months are not used.`,
    gaps: [['P6-D1', 'Board freshness', 'Measured by the engineer’s CDC monitor (commit time of the newest applied change), not by the sources.']],
  },
  {
    key: 'P7',
    name: 'Cold Chain & Lot Traceability',
    dashboards: [
      ['P7-D1', 'Cold Chain Monitor'],
      ['P7-D2', 'Recall Trace'],
    ],
    mode: 'iot',
    note: 'Traced lots are lots of COOLER and FREEZER materials received in a period. For any other lot, ask for an extract of the period it was received in.',
    gaps: [
      ['P7-D1', 'Effective-dated thresholds and the SOP duration rule', 'zone.csv holds today’s temperature range only; earlier threshold versions and the excursion duration rule are Quality’s SOP, recorded in the metric catalog.'],
      ['P7-D1', 'Mean kinetic temperature', 'Uses the activation energy documented in the metric catalog; the readings are included.'],
      ['P7-D2', 'Time to trace', 'Measured from the engineer’s own recall request log.'],
    ],
  },
  {
    key: 'P8',
    name: 'Client Portal & Executive Scorecard',
    dashboards: [
      ['P8-D1', 'Client Portal'],
      ['P8-D2', 'Executive Scorecard'],
    ],
    mode: 'latest',
    note: 'Project 8 combines the core of Projects 2, 3 and 5. By default it covers the latest closed month only; add months as periods when a trend point needs them.',
    gaps: [
      ['P8-D2', 'Data platform cost per month', 'Comes from the engineer’s cloud cost report, not a source system.'],
      ['P8-D2', 'Cold chain time in range', 'Re-perform it from a Project 7 extract of the same month (sensor readings are large).'],
      ['P8-D2', 'Pipeline coverage (target)', 'The new-business target is seed data agreed with Hannah Price.'],
    ],
  },
];

const FILES = fixture as unknown as Record<string, { files: Array<[string, number | null, number]>; zip_ratio: number }>;

// ---------------------------------------------------------------- months

const ym = (d: Date) => `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}`;
const monthStart = (m: string, add = 0) => {
  const [y, mm] = m.split('-').map(Number);
  return Date.UTC(y, mm - 1 + add, 1);
};

function closedMonths(historyStart: string | null, simClock: string | null): string[] {
  if (!historyStart || !simClock) return [];
  const hs = Date.parse(historyStart);
  const clock = Date.parse(simClock);
  const out: string[] = [];
  let m = ym(new Date(hs));
  for (;;) {
    if (monthStart(m, 1) + WESTMOST_OFFSET_H * HOUR > clock) break;
    if (monthStart(m) + 4 * HOUR >= hs) out.push(m);
    m = ym(new Date(monthStart(m, 1)));
  }
  return out;
}

function defaults(available: string[], mode: Mode, iotFrom: number | null): string[] {
  if (mode === 'iot') {
    const withReadings = available.filter((m) => iotFrom != null && monthStart(m, 1) > iotFrom);
    return defaults(withReadings.length ? withReadings : available, 'two', null);
  }
  if (!available.length || mode === 'none') return [];
  const latest = available[available.length - 1];
  if (mode === 'latest') return [latest];
  let earlier = ym(new Date(monthStart(latest, -3)));
  if (!available.includes(earlier)) earlier = available[0];
  return [...new Set([earlier, latest])].sort();
}

// ---------------------------------------------------------------- state

let progress: ExtractProgress | null = null;
const recent: ExtractResult[] = [];
const manifests = new Map<string, unknown>();

function refusal(): [string, string] | null {
  const s = status();
  if (s.state === 'building') return ['building', 'A build is running. When it has finished, refresh your dashboard and then build the extract.'];
  if (s.stream.running || s.state === 'streaming')
    return ['streaming', 'The stream is running, so the sources are still changing. Stop the stream, refresh your dashboard, then build the extract.'];
  if (s.state === 'running_batch') return ['busy', 'A batch or scenario is running. Wait for it to finish, refresh your dashboard, then build the extract.'];
  if (!s.built_at || !s.sim_clock) return ['not_built', 'There is no data yet. Build the dataset first.'];
  if (progress) return ['extracting', 'An extract is being built. Wait for it to finish.'];
  return null;
}

const iotFrom = () => {
  const s = status();
  return s.sim_clock ? Date.parse(s.sim_clock) - IOT_DAYS * 24 * HOUR : null;
};

const project = (d: Def, months: string[]): ExtractProject => ({
  key: d.key,
  name: d.name,
  dashboards: d.dashboards.map(([id, name]) => ({ id, name })),
  uses_periods: d.mode !== 'none',
  default_periods: defaults(months, d.mode, iotFrom()),
  note: d.note ?? null,
  not_in_extract: d.gaps.map(([dashboard, kpi, why]): ExtractGap => ({ dashboard, kpi, why })),
});

export function options(): ExtractOptions {
  const s = status();
  const months = closedMonths(s.history_start, s.sim_clock);
  let why = refusal();
  if (!why && !months.length) why = ['no_months', 'No whole month has closed in the simulated history yet. Run a batch past the end of the month, or build a larger preset.'];
  const iot = iotFrom();
  return {
    ready: !why,
    reason_code: why?.[0] ?? null,
    reason: why?.[1] ?? null,
    sim_clock: s.sim_clock,
    preset: s.preset,
    seed: s.seed,
    history_start: s.history_start,
    sensor_readings_from: iot != null ? new Date(iot).toISOString() : null,
    months,
    max_periods: MAX_PERIODS,
    projects: DEFS.map((d) => project(d, months)),
    in_progress: progress ? { ...progress } : null,
    recent: recent.map((r) => ({ ...r, files: null })),
  };
}

function validate(def: Def, requested: string[] | undefined, available: string[]): string[] {
  if (def.mode === 'none') return [];
  const months = [...new Set((requested ?? defaults(available, def.mode, iotFrom())).map((m) => String(m).trim()))].sort();
  if (!months.length) throw new HttpError(400, 'Choose at least one month.');
  if (months.length > MAX_PERIODS) throw new HttpError(400, `Choose at most ${MAX_PERIODS} months per extract.`);
  for (const m of months) {
    if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(m)) throw new HttpError(400, `'${m}' is not a month. Use YYYY-MM, for example 2026-09.`);
    if (!available.includes(m)) {
      if (available.length && m > available[available.length - 1])
        throw new HttpError(400, `${m} has not closed yet at the sim clock in every site's local time. The latest closed month is ${available[available.length - 1]}.`);
      throw new HttpError(400, `${m} is before the simulated history, which starts with ${available[0]}.`);
    }
  }
  return months;
}

const hex = (n: number) => Array.from({ length: n }, () => Math.floor(Math.random() * 16).toString(16)).join('');
const stamp = (iso: string) => iso.replace(/[-:]/g, '').slice(0, 13) + 'Z';
const isoZ = (ms: number) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z');

export async function build(key: string, requested: string[] | undefined): Promise<ExtractResult> {
  const def = DEFS.find((d) => d.key === key.trim().toUpperCase());
  if (!def) throw new HttpError(400, `Unknown project '${key}'. Choose one of ${DEFS.map((d) => d.key).join(', ')}.`);
  const why = refusal();
  if (why) throw new HttpError(409, why[1]);
  const s = status();
  const periods = validate(def, requested, closedMonths(s.history_start, s.sim_clock));
  const plan = FILES[def.key]?.files ?? [];
  const started = Date.now();
  progress = { project: def.key, file: null, done: 0, total: plan.length, started_at: new Date(started).toISOString() };
  // Rows scale with the number of months (the fixture was built with the default two) and the preset (it holds Large).
  const k = (def.mode === 'none' ? 1 : periods.length / (def.mode === 'latest' ? 1 : 2)) * volumeOf(s.preset);
  const files: NonNullable<ExtractResult['files']> = [];
  try {
    for (const [path, rows, bytes] of plan) {
      progress = { ...progress, file: path, done: files.length };
      await new Promise((r) => setTimeout(r, 70 + Math.min(260, bytes / 900_000)));
      const scoped = rows != null && rows > 2_000;
      files.push({ path, rows: rows == null ? null : Math.round(scoped ? rows * k : rows), bytes: Math.round(scoped ? bytes * k : bytes) });
    }
  } finally {
    progress = null;
  }
  const raw = files.reduce((t, f) => t + f.bytes, 0);
  const sim = s.sim_clock ?? new Date().toISOString();
  const r: ExtractResult = {
    id: hex(12),
    filename: `harborline_${def.key}_source-extract_${periods.join('+') || 'live'}_simclock-${stamp(sim)}.zip`,
    size_bytes: Math.round(raw * (FILES[def.key]?.zip_ratio ?? 0.2)),
    project: def.key,
    periods,
    sim_clock: isoZ(Date.parse(sim)),
    generated_at: isoZ(Date.now()),
    duration_s: Math.round((Date.now() - started) / 100) / 10,
    files,
    download_url: '',
  };
  r.download_url = `/api/uat/extract/${r.id}`;
  manifests.set(r.id, {
    demo:
      import.meta.env.VITE_DEMO === '1'
        ? 'Live demo: this zip holds the manifest only. The rows are available when you install it.'
        : 'Mock mode: this zip holds the manifest only. Build against the simulator for the real rows.',
    extract_version: 1,
    generated_at_utc: r.generated_at,
    sim_clock: r.sim_clock,
    preset: s.preset,
    seed: s.seed,
    project: { key: def.key, name: def.name, dashboards: project(def, []).dashboards },
    periods: periods.map((m) => {
      const start = monthStart(m);
      const end = monthStart(m, 1);
      return { month: m, start: isoZ(start).slice(0, 10), end: isoZ(end - 24 * HOUR).slice(0, 10), end_exclusive: isoZ(end).slice(0, 10) };
    }),
    files: files.map((f) => ({ path: f.path, rows: f.rows, bytes: f.bytes })),
    not_in_extract: def.gaps.map(([dashboard, kpi, why]) => ({ dashboard, kpi, why })),
  });
  recent.unshift(r);
  for (const old of recent.splice(KEEP)) manifests.delete(old.id);
  return r;
}

// ---------------------------------------------------------------- a stored (uncompressed) zip with the manifest

const CRC = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();

function crc32(data: Uint8Array) {
  let c = 0xffffffff;
  for (const b of data) c = CRC[(c ^ b) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function storedZip(entries: Array<[string, string]>): Blob {
  const enc = new TextEncoder();
  const parts: Uint8Array[] = [];
  const central: Uint8Array[] = [];
  let offset = 0;
  const d = new Date();
  const time = (d.getHours() << 11) | (d.getMinutes() << 5) | Math.floor(d.getSeconds() / 2);
  const date = ((d.getFullYear() - 1980) << 9) | ((d.getMonth() + 1) << 5) | d.getDate();
  for (const [name, text] of entries) {
    const nameBytes = enc.encode(name);
    const data = enc.encode(text);
    const crc = crc32(data);
    const local = new DataView(new ArrayBuffer(30));
    local.setUint32(0, 0x04034b50, true);
    local.setUint16(4, 20, true);
    local.setUint16(6, 0x0800, true);
    local.setUint16(10, time, true);
    local.setUint16(12, date, true);
    local.setUint32(14, crc, true);
    local.setUint32(18, data.length, true);
    local.setUint32(22, data.length, true);
    local.setUint16(26, nameBytes.length, true);
    const cd = new DataView(new ArrayBuffer(46));
    cd.setUint32(0, 0x02014b50, true);
    cd.setUint16(4, 20, true);
    cd.setUint16(6, 20, true);
    cd.setUint16(8, 0x0800, true);
    cd.setUint16(12, time, true);
    cd.setUint16(14, date, true);
    cd.setUint32(16, crc, true);
    cd.setUint32(20, data.length, true);
    cd.setUint32(24, data.length, true);
    cd.setUint16(28, nameBytes.length, true);
    cd.setUint32(42, offset, true);
    parts.push(new Uint8Array(local.buffer), nameBytes, data);
    central.push(new Uint8Array(cd.buffer), nameBytes);
    offset += 30 + nameBytes.length + data.length;
  }
  const cdSize = central.reduce((t, p) => t + p.length, 0);
  const end = new DataView(new ArrayBuffer(22));
  end.setUint32(0, 0x06054b50, true);
  end.setUint16(8, entries.length, true);
  end.setUint16(10, entries.length, true);
  end.setUint32(12, cdSize, true);
  end.setUint32(16, offset, true);
  return new Blob([...parts, ...central, new Uint8Array(end.buffer)] as BlobPart[], { type: 'application/zip' });
}

export function zip(id: string): { blob: Blob; filename: string } | null {
  const r = recent.find((x) => x.id === id);
  const manifest = manifests.get(id);
  if (!r || !manifest) return null;
  return { blob: storedZip([['manifest.json', JSON.stringify(manifest, null, 2) + '\n']]), filename: r.filename };
}
