/* In-browser simulator backend for VITE_MOCK=1 (§5.1). Row counts follow the Large preset in the plan:
   36 months, ~2.5 M orders, ~7 M lines, ~20 M ledger rows. Daily rates follow the change matrix (plan §2).
   The sample opens as a Medium build, whose database size and landing file count are measured from a real one. */

import type {
  Connections,
  DqSettings,
  LogEvent,
  Preset,
  ProgressEvent,
  RunSummary,
  Scenario,
  SimStatus,
  TableChange,
  TableTotal,
} from '@/api/types';
import { HttpError } from './tracker';

type Rate = [ins: number, upd: number, del: number];
interface TableDef {
  system: string;
  table: string;
  rows: number; // Large preset
  rate: Rate; // per simulated day
  transactional: boolean;
}

const T = (system: string, table: string, rows: number, rate: Rate = [0, 0, 0], transactional = true): TableDef => ({
  system,
  table,
  rows,
  rate,
  transactional,
});

const TABLES: TableDef[] = [
  // WMS (db wms)
  T('wms', 'warehouse', 8, [0, 0, 0], false),
  T('wms', 'zone', 29, [0, 0, 0], false),
  T('wms', 'location', 212_400, [4, 12, 0], false),
  T('wms', 'owner', 60, [0, 0.2, 0], false),
  T('wms', 'owner_warehouse', 84, [0, 0, 0], false),
  T('wms', 'material', 48_200, [35, 12, 0.4]),
  T('wms', 'material_uom', 96_400, [70, 6, 0.8]),
  T('wms', 'carrier', 13, [0, 0, 0], false),
  T('wms', 'carrier_service', 24, [0, 0, 0], false),
  T('wms', 'asn_header', 612_300, [560, 910, 6]),
  T('wms', 'asn_line', 4_880_600, [4_480, 5_210, 41]),
  T('wms', 'receipt_header', 598_100, [545, 545, 0]),
  T('wms', 'receipt_line', 4_712_900, [4_310, 1_220, 0]),
  T('wms', 'license_plate', 9_641_000, [8_820, 7_390, 0]),
  T('wms', 'inventory', 1_284_300, [6_120, 38_400, 5_640]),
  T('wms', 'inventory_txn', 20_352_800, [18_620, 0, 0]),
  T('wms', 'order_header', 2_512_400, [2_296, 11_420, 14]),
  T('wms', 'order_line', 7_034_100, [6_441, 19_790, 48]),
  T('wms', 'task', 3_918_700, [21_060, 42_310, 20_380]),
  T('wms', 'shipment', 2_476_200, [2_268, 3_910, 9]),
  T('wms', 'shipment_carton', 3_310_500, [3_029, 604, 11]),
  T('wms', 'cycle_count', 418_200, [381, 377, 0]),
  T('wms', 'return_header', 286_400, [262, 418, 0]),
  T('wms', 'return_line', 492_100, [449, 301, 0]),
  T('wms', 'app_user', 2_140, [0.6, 1.8, 0], false),
  T('wms', 'billing_rate', 3_840, [0.4, 1.1, 0], false),
  T('wms', 'billing_record', 9_884_600, [9_052, 141, 28]),
  T('wms', 'sys_release', 3, [0, 0, 0], false),
  // AX 2012 (db ax2012)
  T('ax2012', 'dataarea', 3, [0, 0, 0], false),
  T('ax2012', 'dirpartytable', 4_182, [1.2, 0.8, 0], false),
  T('ax2012', 'logisticslocation', 5_964, [1.6, 0, 0], false),
  T('ax2012', 'dirpartylocation', 5_964, [1.6, 0.4, 0], false),
  T('ax2012', 'logisticspostaladdress', 6_412, [1.8, 0.5, 0], false),
  T('ax2012', 'dirpersonname', 2_951, [0.6, 0.2, 0], false),
  T('ax2012', 'hcmworker', 2_140, [0.6, 0.3, 0], false),
  T('ax2012', 'hcmemployment', 2_463, [0.6, 0.5, 0], false),
  T('ax2012', 'paymterm', 4, [0, 0, 0], false),
  T('ax2012', 'custtable', 186, [0.05, 0.4, 0], false),
  T('ax2012', 'vendtable', 212, [0.05, 0.1, 0], false),
  T('ax2012', 'currency', 168, [0, 0, 0], false),
  T('ax2012', 'exchangeratecurrencypair', 12, [0, 0, 0], false),
  T('ax2012', 'exchangerate', 13_140, [12, 0, 0]),
  T('ax2012', 'mainaccount', 19, [0, 0, 0], false),
  T('ax2012', 'dimensionattributevaluecombination', 1_244, [0.3, 0, 0], false),
  T('ax2012', 'custinvoicejour', 6_482, [6, 0, 0]),
  T('ax2012', 'custinvoicetrans', 118_340, [108, 0, 0]),
  T('ax2012', 'custtrans', 12_964, [12, 9, 0.02]),
  T('ax2012', 'custsettlement', 11_852, [9, 0, 0.02]),
  T('ax2012', 'vendinvoicejour', 8_941, [8, 0, 0]),
  T('ax2012', 'vendinvoicetrans', 1_240_700, [1_133, 0, 0]),
  T('ax2012', 'vendtrans', 9_120, [8, 6, 0]),
  T('ax2012', 'ledgerjournaltable', 3_722, [3.4, 2, 0.3]),
  T('ax2012', 'ledgerjournaltrans', 41_806, [38, 24, 6]),
  T('ax2012', 'generaljournalentry', 412_090, [376, 0, 0]),
  T('ax2012', 'generaljournalaccountentry', 2_890_400, [2_640, 0, 0]),
  T('ax2012', 'srsanalysisenums', 236, [0, 0, 0], false),
  // IoT (db iot)
  T('iot', 'sensor', 1_184, [0, 0.2, 0], false),
  T('iot', 'sensor_reading', 31_104_000, [345_600, 0, 0]),
  T('iot', 'dock_door_event', 2_941_200, [32_680, 0, 0]),
  // Salesforce (mock API)
  T('salesforce', 'Account', 4_412, [1.4, 9, 0.1]),
  T('salesforce', 'Contact', 11_926, [4, 6, 0.2]),
  T('salesforce', 'Opportunity', 2_861, [2.6, 7, 0.05]),
  T('salesforce', 'OpportunityHistory', 19_410, [17, 0, 0]),
  T('salesforce', 'OpportunityLineItem', 8_124, [7, 2, 0.2]),
  T('salesforce', 'Contract', 214, [0.2, 0.4, 0]),
  T('salesforce', 'Case', 26_805, [24, 41, 0.6]),
  T('salesforce', 'Task', 74_310, [68, 33, 1.1]),
  T('salesforce', 'Rate_Card__c', 318, [0.3, 0.1, 0]),
  T('salesforce', 'Rate_Card_Line__c', 4_982, [4, 1.2, 0]),
  T('salesforce', 'User', 38, [0, 0.05, 0], false),
  // Carrier aggregator (mock API)
  T('carrier', 'tracking_event', 9_874_300, [9_020, 41, 0]),
  // Files in the landing folder. Build history older than a week sits in monthly zips, so the totals are far below
  // months x rate. Scaled to Medium, these rows add up to the 11,594 files a real Medium build leaves.
  T('edi', '940 inbound', 6_663, [29, 0, 0]),
  T('edi', '856 inbound', 3_860, [17, 0, 0]),
  T('edi', '945 outbound', 6_535, [28, 0, 0]),
  T('edi', '997 outbound', 10_508, [46, 0, 0]),
  T('files', 'item master CSV', 1_314, [1.2, 0, 0]),
  T('files', 'rate card workbook', 92, [0.08, 0, 0]),
  T('files', 'budget workbook', 4, [0, 0, 0]),
  T('files', 'warehouse master workbook', 7, [0, 0, 0]),
];

const GB = 1024 ** 3;
/* bytes + landingBytes add up to the measured disk in BuildDialog's PRESET_INFO. Medium's bytes are measured on their own. */
const PRESETS: Record<Preset, { months: number; volume: number; bytes: number; landingBytes: number; buildSeconds: number }> = {
  small: { months: 6, volume: 0.35, bytes: 2.75 * GB, landingBytes: 0.25 * GB, buildSeconds: 9 },
  medium: { months: 24, volume: 0.6, bytes: 11 * GB, landingBytes: 1 * GB, buildSeconds: 15 },
  large: { months: 36, volume: 1, bytes: 27.5 * GB, landingBytes: 2.5 * GB, buildSeconds: 24 },
};

export const volumeOf = (preset: Preset | null) => PRESETS[preset ?? 'large'].volume;

const DQ_CATEGORIES = [
  'orphan_fk',
  'duplicate_row',
  'null_required',
  'negative_qty',
  'shipped_gt_ordered',
  'sentinel_date',
  'future_date',
  'invalid_currency',
  'unit_mixup',
  'key_whitespace_case',
  'mojibake',
  'malformed_edi',
  'sensor_spike',
  'sensor_stuck',
  'late_arriving',
  'schema_drift',
  'header_line_mismatch',
  'duplicate_account',
  'dst_duplicate',
];

const CLIENT_CODES = ['NBOUT', 'LUMEN', 'KESTREL', 'COBALT', 'VOLTWV', 'ORBITSH', 'CPRKTL', 'GRNFLD', 'HEARTH', 'APEXAUT', 'PAWSOME', 'PKPROT', 'BEACNHW', 'LAKETOY'];
const WAREHOUSES = ['ATL1', 'DFW1', 'RNO1', 'CHI1', 'NJ1', 'LAX1', 'TOR1', 'MTY1'];

export const SCENARIOS: Scenario[] = [
  {
    id: 'peak_surge',
    name: 'Peak surge',
    description: 'Order intake jumps for a few days, the way Cyber Week hits. Pick queues and carrier pickups back up.',
    params: [
      { name: 'multiplier', type: 'float', default: 2.5, min: 1.1, max: 6 },
      { name: 'days', type: 'int', default: 3, min: 1, max: 14 },
      { name: 'warehouse', type: 'choice', default: 'ALL', options: ['ALL', ...WAREHOUSES] },
    ],
  },
  {
    id: 'onboard_client',
    name: 'Onboard a client',
    description: 'A new client goes live: Salesforce account and contract, AX customer, WMS owner, item master file and opening inventory.',
    params: [
      { name: 'vertical', type: 'choice', default: 'apparel', options: ['apparel', 'electronics', 'food_bev', 'health_beauty', 'home_goods', 'auto_parts', 'pet', 'supplements', 'b2b_general'] },
      { name: 'tier', type: 'choice', default: 'M', options: ['S', 'M', 'L', 'XL'] },
      { name: 'warehouse', type: 'choice', default: 'ATL1', options: WAREHOUSES },
      { name: 'currency', type: 'choice', default: 'USD', options: ['USD', 'CAD', 'MXN', 'EUR', 'GBP'] },
      { name: 'go_live_days', type: 'int', default: 7, min: 0, max: 60 },
    ],
  },
  {
    id: 'offboard_client',
    name: 'Offboard a client',
    description: 'A client gives notice: orders taper, inventory ships out, the account is deactivated in every system on different days.',
    params: [
      { name: 'client', type: 'choice', default: 'LAKETOY', options: CLIENT_CODES },
      { name: 'wind_down_days', type: 'int', default: 30, min: 1, max: 90 },
    ],
  },
  {
    id: 'rate_increase',
    name: 'Rate increase',
    description: 'Sales amends a contract in Salesforce. Whether the WMS billing rate is updated, late or never, is up to you.',
    params: [
      { name: 'client', type: 'choice', default: 'HEARTH', options: CLIENT_CODES },
      { name: 'pct', type: 'float', default: 4, min: 0.5, max: 15 },
      { name: 'keyed_in_wms', type: 'bool', default: false },
    ],
  },
  {
    id: 'schema_drift_release',
    name: 'Schema-drift release',
    description: 'A vendor release adds columns: a new WMS column, a new Salesforce custom field and an extra column in the item-master CSV.',
    params: [{ name: 'target', type: 'choice', default: 'all', options: ['all', 'wms', 'salesforce', 'files'] }],
  },
  {
    id: 'duplicate_file_resend',
    name: 'Duplicate file resend',
    description: 'A trading partner resends yesterday’s files with new control numbers.',
    params: [
      { name: 'source', type: 'choice', default: 'edi_940', options: ['edi_940', 'edi_856', 'item_master', 'rate_card'] },
      { name: 'count', type: 'int', default: 1, min: 1, max: 20 },
    ],
  },
  {
    id: 'carrier_outage',
    name: 'Carrier outage',
    description: 'The carrier aggregator stops sending events for one carrier, then delivers the backlog at once.',
    params: [
      { name: 'scac', type: 'choice', default: 'UPSN', options: ['UPSN', 'FDEG', 'FDE', 'USPS', 'DHLC', 'VRPX', 'CNPC', 'PURO', 'ESTF', 'ODFL', 'MRLF', 'EXLA', 'TRMX'] },
      { name: 'hours', type: 'int', default: 12, min: 1, max: 72 },
    ],
  },
  {
    id: 'storm',
    name: 'Storm',
    description: 'Severe weather closes lanes: deliveries slip, exceptions spike and dock doors sit idle.',
    params: [
      { name: 'region', type: 'choice', default: 'Southeast', options: ['Southeast', 'Texas', 'Northeast', 'Great Lakes', 'West', 'Ontario', 'Nuevo León'] },
      { name: 'days', type: 'int', default: 2, min: 1, max: 7 },
      { name: 'severity', type: 'choice', default: 'severe', options: ['moderate', 'severe'] },
    ],
  },
  {
    id: 'late_data_dump',
    name: 'Late data dump',
    description: 'A buffer flushes: carrier events or sensor readings arrive hours after they happened.',
    params: [
      { name: 'source', type: 'choice', default: 'iot_readings', options: ['iot_readings', 'carrier_events'] },
      { name: 'hours_late', type: 'int', default: 36, min: 1, max: 240 },
    ],
  },
  {
    id: 'sf_account_merge',
    name: 'Salesforce account merge',
    description: 'Sales ops merges duplicate accounts. Losers are deleted with MasterRecordId set; children move to the winner.',
    params: [{ name: 'count', type: 'int', default: 2, min: 1, max: 10 }],
  },
];

// ---------------------------------------------------------------- state

const HOUR = 3_600_000;
const iso = (ms: number) => new Date(ms).toISOString();
const dayStartUtc = (ms: number) => Math.floor(ms / 86_400_000) * 86_400_000;

interface SimMock {
  status: SimStatus;
  runs: RunSummary[];
  nextRun: number;
  streamTimer: ReturnType<typeof setInterval> | null;
  buildTimer: ReturnType<typeof setInterval> | null;
  rows: Map<string, number>;
}

const keyOf = (s: string, t: string) => `${s}.${t}`;

function presetRows(preset: Preset): Map<string, number> {
  const p = PRESETS[preset];
  const m = new Map<string, number>();
  for (const t of TABLES) {
    const scale = t.transactional ? (p.months / 36) * p.volume : 1;
    m.set(keyOf(t.system, t.table), Math.max(t.rows > 0 ? 1 : 0, Math.round(t.rows * scale)));
  }
  return m;
}

const landingFiles = (rows: Map<string, number>) =>
  TABLES.filter((t) => t.system === 'edi' || t.system === 'files').reduce((n, t) => n + (rows.get(keyOf(t.system, t.table)) ?? 0), 0);

function totals(rows: Map<string, number>): TableTotal[] {
  return TABLES.map((t) => ({ system: t.system, table: t.table, rows: rows.get(keyOf(t.system, t.table)) ?? 0 }));
}

function poisson(x: number) {
  return Math.max(0, Math.floor(x + Math.random()));
}

function changesFor(hours: number, preset: Preset, boost: (t: TableDef) => number = () => 1): TableChange[] {
  const vol = PRESETS[preset].volume;
  return TABLES.map((t) => {
    const k = (hours / 24) * vol * boost(t) * (0.86 + Math.random() * 0.28);
    return { system: t.system, table: t.table, ins: poisson(t.rate[0] * k), upd: poisson(t.rate[1] * k), del: poisson(t.rate[2] * k) };
  }).filter((c) => c.ins + c.upd + c.del > 0);
}

function apply(s: SimMock, changes: TableChange[]) {
  for (const c of changes) {
    const k = keyOf(c.system, c.table);
    s.rows.set(k, (s.rows.get(k) ?? 0) + c.ins - c.del);
  }
  s.status.totals = totals(s.rows);
  const ins = changes.reduce((t, c) => t + c.ins, 0);
  s.status.db_size_bytes += ins * 182;
  const files = changes.filter((c) => c.system === 'edi' || c.system === 'files').reduce((t, c) => t + c.ins, 0);
  s.status.landing = { files: s.status.landing.files + files, bytes: s.status.landing.bytes + files * 41_000 };
}

function initial(): SimMock {
  const now = Date.now();
  const preset: Preset = 'medium';
  const p = PRESETS[preset];
  const historyEnd = dayStartUtc(now) - 86_400_000; // built yesterday
  const historyStart = Date.UTC(new Date(historyEnd).getUTCFullYear(), new Date(historyEnd).getUTCMonth() - p.months, new Date(historyEnd).getUTCDate());
  const builtAt = historyEnd + 9.5 * HOUR;
  const rows = presetRows(preset);
  // History: the build, then a day of batches and a scenario so every screen has something to show. These runs end
  // on the preset's figures, so the build itself starts that much lower.
  const steps: Array<[string, number, string]> = [
    ['batch', 24, 'Batch · 1 day'],
    ['batch', 6, 'Batch · 6 h'],
    ['scenario', 1, 'Scenario · Rate increase (HEARTH +4%, not keyed in WMS)'],
    ['batch', 12, 'Batch · 12 h'],
  ];
  const seeded = steps.map(([, hours]) => changesFor(hours, preset));
  for (const c of seeded.flat()) rows.set(keyOf(c.system, c.table), (rows.get(keyOf(c.system, c.table)) ?? 0) - c.ins + c.del);
  const mock: SimMock = {
    status: {
      state: 'idle',
      preset,
      seed: 20261031,
      sim_clock: iso(historyEnd),
      real_clock: iso(now),
      history_start: iso(historyStart),
      history_end: iso(historyEnd),
      built_at: iso(builtAt),
      dq: { denominator: 10_000, categories: Object.fromEntries(DQ_CATEGORIES.map((c) => [c, true])) },
      stream: { running: false, interval_s: 5, speed: 60, ticks: 0 },
      build: null,
      totals: totals(rows),
      db_size_bytes: p.bytes,
      landing: { files: landingFiles(rows), bytes: p.landingBytes },
      last_run: null,
    },
    runs: [],
    nextRun: 1,
    streamTimer: null,
    buildTimer: null,
    rows,
  };
  const build: RunSummary = {
    run_id: mock.nextRun++,
    kind: 'build',
    label: 'Build · Medium · seed 20261031',
    started_at: iso(builtAt - 58 * 60_000 - 41_000),
    finished_at: iso(builtAt),
    sim_from: iso(historyStart),
    sim_to: iso(historyEnd),
    status: 'ok',
    error: null,
    changes: totals(rows).map((t) => ({ system: t.system, table: t.table, ins: t.rows, upd: 0, del: 0 })),
  };
  mock.runs.unshift(build);
  let clock = historyEnd;
  let real = builtAt + 40 * 60_000;
  for (const [i, [kind, hours, label]] of steps.entries()) {
    const changes = seeded[i];
    apply(mock, changes);
    mock.runs.unshift({
      run_id: mock.nextRun++,
      kind,
      label,
      started_at: iso(real),
      finished_at: iso(real + (hours / 24) * 41_000 + 3_000),
      sim_from: iso(clock),
      sim_to: iso(clock + hours * HOUR),
      status: 'ok',
      error: null,
      changes,
    });
    clock += hours * HOUR;
    real += 2.6 * HOUR;
  }
  mock.status.sim_clock = iso(clock);
  mock.status.last_run = mock.runs[0];
  mock.status.db_size_bytes = p.bytes;
  mock.status.landing = { files: landingFiles(mock.rows), bytes: p.landingBytes };
  return mock;
}

const S: SimMock = initial();

// ---------------------------------------------------------------- events

type Listener = (event: string, data: unknown) => void;
const listeners = new Set<Listener>();
export function subscribe(l: Listener) {
  listeners.add(l);
  return () => listeners.delete(l);
}
function emit(event: string, data: unknown) {
  listeners.forEach((l) => l(event, data));
}
function log(msg: string, level: LogEvent['level'] = 'info') {
  emit('log', { ts: new Date().toISOString(), level, msg } satisfies LogEvent);
}

function snapshot(): SimStatus {
  return { ...S.status, real_clock: new Date().toISOString(), stream: { ...S.status.stream } };
}
function pushState() {
  emit('state', snapshot());
}

function finishRun(run: RunSummary) {
  S.runs.unshift(run);
  S.runs = S.runs.slice(0, 400);
  S.status.last_run = run;
  emit('run', run);
  pushState();
}

// ---------------------------------------------------------------- API

export const status = (): SimStatus => snapshot();
export const runs = (limit: number) => S.runs.slice(0, limit);
export const scenarios = () => SCENARIOS;

function requireIdle(action: string) {
  const st = S.status.state;
  if (st === 'building') throw new HttpError(409, `A build is running. ${action} is available when it finishes.`);
  if (st === 'streaming') throw new HttpError(409, `The stream is running. Stop it before you ${action.toLowerCase()}.`);
  if (st === 'running_batch') throw new HttpError(409, 'A batch is already running.');
}

export async function batch(hours: number): Promise<RunSummary> {
  if (!(hours > 0) || hours > 24 * 31) throw new HttpError(422, 'hours must be between 1 and 744');
  requireIdle('Generate batch');
  if (!S.status.sim_clock) throw new HttpError(409, 'Build the dataset first');
  S.status.state = 'running_batch';
  pushState();
  const started = Date.now();
  await new Promise((r) => setTimeout(r, Math.min(3200, 500 + hours * 30)));
  const from = Date.parse(S.status.sim_clock);
  const changes = changesFor(hours, S.status.preset ?? 'large');
  apply(S, changes);
  S.status.sim_clock = iso(from + hours * HOUR);
  S.status.state = 'idle';
  const run: RunSummary = {
    run_id: S.nextRun++,
    kind: 'batch',
    label: `Batch · ${hours % 24 === 0 ? `${hours / 24} ${hours === 24 ? 'day' : 'days'}` : `${hours} h`}`,
    started_at: iso(started),
    finished_at: iso(Date.now()),
    sim_from: iso(from),
    sim_to: S.status.sim_clock,
    status: 'ok',
    error: null,
    changes,
  };
  finishRun(run);
  return run;
}

function tick() {
  const { interval_s, speed } = S.status.stream;
  const simSeconds = interval_s * speed;
  const from = Date.parse(S.status.sim_clock ?? new Date().toISOString());
  const changes = changesFor(simSeconds / 3600, S.status.preset ?? 'large');
  apply(S, changes);
  S.status.sim_clock = iso(from + simSeconds * 1000);
  S.status.stream.ticks += 1;
  finishRun({
    run_id: S.nextRun++,
    kind: 'stream_tick',
    label: `Tick ${S.status.stream.ticks}`,
    started_at: iso(Date.now() - 140),
    finished_at: iso(Date.now()),
    sim_from: iso(from),
    sim_to: S.status.sim_clock,
    status: 'ok',
    error: null,
    changes,
  });
}

export function streamStart(interval_s: number, speed: number): SimStatus {
  if (![1, 10, 60].includes(speed)) throw new HttpError(422, 'speed must be 1, 10 or 60');
  if (!(interval_s >= 1 && interval_s <= 60)) throw new HttpError(422, 'interval_s must be between 1 and 60');
  if (S.status.state === 'streaming') return snapshot();
  requireIdle('Start the stream');
  S.status.state = 'streaming';
  S.status.stream = { running: true, interval_s, speed, ticks: 0 };
  S.streamTimer = setInterval(tick, interval_s * 1000);
  log(`Stream started: every ${interval_s} s at ${speed}×`);
  pushState();
  return snapshot();
}

export function streamStop(): SimStatus {
  if (S.streamTimer) clearInterval(S.streamTimer);
  S.streamTimer = null;
  if (S.status.state === 'streaming') S.status.state = 'idle';
  S.status.stream = { ...S.status.stream, running: false };
  log(`Stream stopped after ${S.status.stream.ticks} ticks`);
  pushState();
  return snapshot();
}

export function setDq(dq: DqSettings): DqSettings {
  if (!(dq.denominator >= 100 && dq.denominator <= 100_000)) throw new HttpError(422, 'denominator must be between 100 and 100000');
  S.status.dq = { denominator: Math.round(dq.denominator), categories: { ...S.status.dq.categories, ...dq.categories } };
  pushState();
  return S.status.dq;
}

const PHASES: Array<[string, number]> = [
  ['Dropping source schemas', 0.03],
  ['Reference data: warehouses, clients, carriers, people', 0.05],
  ['Simulating history day by day', 0.7],
  ['Building secondary indexes', 0.1],
  ['Grants and CDC publications', 0.04],
  ['Writing the world checkpoint', 0.08],
];

export function cancelBuild() {
  if (S.status.state !== 'building') throw new HttpError(409, 'No build is running.');
  if (S.buildTimer) clearInterval(S.buildTimer);
  S.status.state = 'idle';
  S.status.build = null;
  S.status.built_at = null;
  log('Build cancelled. The data is incomplete.');
  pushState();
  return S.status;
}

export function build(preset: Preset, seed: number, confirm: boolean): { run_id: number } {
  if (!confirm) throw new HttpError(422, 'confirm must be true');
  if (!(preset in PRESETS)) throw new HttpError(422, 'Unknown preset');
  if (S.status.state === 'streaming') streamStop();
  requireIdle('Build');
  const runId = S.nextRun++;
  const p = PRESETS[preset];
  const started = Date.now();
  const total = p.buildSeconds * 1000;
  const realEstimate = preset === 'large' ? 3 * 3600 : preset === 'medium' ? 3600 : 10 * 60;
  S.status.state = 'building';
  S.status.preset = preset;
  S.status.seed = seed;
  S.status.build = { progress: 0, eta_s: realEstimate, phase: PHASES[0][0] };
  log(`Build started: ${preset}, seed ${seed}`);
  pushState();
  const historyEnd = dayStartUtc(Date.now());
  const historyStart = Date.UTC(new Date(historyEnd).getUTCFullYear(), new Date(historyEnd).getUTCMonth() - p.months, new Date(historyEnd).getUTCDate());
  let lastPhase = '';
  let lastMonth = -1;
  S.buildTimer = setInterval(() => {
    const pct = Math.min(1, (Date.now() - started) / total);
    let acc = 0;
    let phase = PHASES[PHASES.length - 1][0];
    let within = 1;
    for (const [name, w] of PHASES) {
      if (pct <= acc + w) {
        phase = name;
        within = (pct - acc) / w;
        break;
      }
      acc += w;
    }
    if (phase.startsWith('Simulating')) {
      const month = Math.min(p.months - 1, Math.floor(within * p.months));
      const d = new Date(historyStart);
      d.setUTCMonth(d.getUTCMonth() + month);
      phase = `Simulating history · ${d.toISOString().slice(0, 7)} (month ${month + 1} of ${p.months})`;
      if (month !== lastMonth) {
        lastMonth = month;
        const orders = Math.round((62_000 + Math.random() * 14_000) * p.volume);
        log(`${d.toISOString().slice(0, 7)} · ${orders.toLocaleString('en-US')} orders · ${(orders * 2.8).toLocaleString('en-US', { maximumFractionDigits: 0 })} lines copied`);
      }
    } else if (phase !== lastPhase) log(phase);
    lastPhase = phase;
    const eta = Math.round(realEstimate * (1 - pct));
    S.status.build = { progress: pct, eta_s: eta, phase };
    emit('progress', { run_id: runId, phase, pct, eta_s: eta } satisfies ProgressEvent);
    if (pct >= 1) {
      clearInterval(S.buildTimer!);
      S.buildTimer = null;
      S.rows = presetRows(preset);
      S.status = {
        ...S.status,
        state: 'idle',
        build: null,
        built_at: iso(Date.now()),
        history_start: iso(historyStart),
        history_end: iso(historyEnd),
        sim_clock: iso(historyEnd),
        totals: totals(S.rows),
        db_size_bytes: p.bytes,
        landing: { files: landingFiles(S.rows), bytes: p.landingBytes },
        stream: { ...S.status.stream, ticks: 0, running: false },
      };
      log(`Build finished in ${Math.round(realEstimate / 60)} min (simulated)`);
      finishRun({
        run_id: runId,
        kind: 'build',
        label: `Build · ${preset[0].toUpperCase() + preset.slice(1)} · seed ${seed}`,
        started_at: iso(started),
        finished_at: iso(Date.now()),
        sim_from: iso(historyStart),
        sim_to: iso(historyEnd),
        status: 'ok',
        error: null,
        changes: totals(S.rows).map((t) => ({ system: t.system, table: t.table, ins: t.rows, upd: 0, del: 0 })),
      });
    }
  }, 250);
  return { run_id: runId };
}

const SCENARIO_BOOST: Record<string, (t: TableDef) => number> = {
  peak_surge: (t) => (t.system === 'wms' || t.system === 'carrier' || t.system === 'edi' ? 2.6 : 1),
  onboard_client: (t) => (t.system === 'salesforce' || t.table === 'owner' || t.table === 'material' ? 6 : 1),
  offboard_client: (t) => (t.table === 'inventory' ? 2 : 1),
  storm: (t) => (t.system === 'carrier' ? 1.9 : t.table === 'dock_door_event' ? 0.4 : 1),
  carrier_outage: (t) => (t.system === 'carrier' ? 0.1 : 1),
  late_data_dump: (t) => (t.system === 'iot' || t.system === 'carrier' ? 3.4 : 1),
};

export async function runScenario(id: string, params: Record<string, unknown>): Promise<RunSummary> {
  const sc = SCENARIOS.find((s) => s.id === id);
  if (!sc) throw new HttpError(404, `Unknown scenario ${id}`);
  requireIdle('Run a scenario');
  S.status.state = 'running_batch';
  pushState();
  const started = Date.now();
  await new Promise((r) => setTimeout(r, 900));
  const from = Date.parse(S.status.sim_clock ?? new Date().toISOString());
  const changes = changesFor(1, S.status.preset ?? 'large', SCENARIO_BOOST[id]);
  if (id === 'schema_drift_release') changes.push({ system: 'wms', table: 'sys_release', ins: 1, upd: 0, del: 0 });
  if (id === 'sf_account_merge') changes.push({ system: 'salesforce', table: 'Account', ins: 0, upd: Number(params.count ?? 2) * 3, del: Number(params.count ?? 2) });
  apply(S, changes);
  S.status.sim_clock = iso(from + HOUR);
  S.status.state = 'idle';
  const detail = Object.entries(params)
    .map(([k, v]) => `${k} ${String(v)}`)
    .join(', ');
  const run: RunSummary = {
    run_id: S.nextRun++,
    kind: 'scenario',
    label: `Scenario · ${sc.name}${detail ? ` (${detail})` : ''}`,
    started_at: iso(started),
    finished_at: iso(Date.now()),
    sim_from: iso(from),
    sim_to: S.status.sim_clock,
    status: 'ok',
    error: null,
    changes,
  };
  finishRun(run);
  return run;
}

export const connections = (): Connections => ({
  postgres: { host: 'localhost', port: 5432, user: 'etl_reader', password: 'etl-reader-2026', databases: ['wms', 'ax2012', 'iot'] },
  salesforce: {
    instance_url: 'http://localhost:8080/sfdc',
    token_url: 'http://localhost:8080/sfdc/services/oauth2/token',
    client_id: '3MVG9harborline.etl.client',
    client_secret: 'hl-sf-secret-7c41',
    api_version: 'v67.0',
  },
  carrier: { base_url: 'http://localhost:8080/carrier/v1', api_key: 'hl-carrier-live-5e2a9d' },
  landing_path_host_hint: 'C:\\dev\\harborline-lab\\landing',
});

/** Keep the real clock ticking for open dashboards even when nothing else happens. */
setInterval(pushState, 15_000);
