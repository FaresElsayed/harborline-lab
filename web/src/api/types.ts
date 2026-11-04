/* Typed contract for the FastAPI backend (DESIGN_SPEC §5.1 control API, §5.4 tracker API). */

// ---------------------------------------------------------------- control (§5.1)

export type SimState = 'idle' | 'building' | 'running_batch' | 'streaming' | 'error';
export type Preset = 'small' | 'medium' | 'large';
export type SystemName = 'wms' | 'ax2012' | 'iot' | 'salesforce' | 'carrier' | 'edi' | 'files' | (string & {});
export type RunKind = 'build' | 'batch' | 'stream_tick' | 'scenario' | (string & {});
export type RunStatus = 'running' | 'ok' | 'error' | 'cancelled' | (string & {});

export interface TableChange {
  system: SystemName;
  table: string;
  ins: number;
  upd: number;
  del: number;
}

export interface RunSummary {
  run_id: number | string;
  kind: RunKind;
  label: string | null;
  started_at: string;
  finished_at: string | null;
  sim_from: string | null;
  sim_to: string | null;
  status: RunStatus;
  error: string | null;
  changes: TableChange[];
}

export interface TableTotal {
  system: SystemName;
  table: string;
  rows: number;
}

export interface DqSettings {
  denominator: number;
  categories: Record<string, boolean>;
}

export interface StreamStatus {
  running: boolean;
  interval_s: number;
  speed: number;
  ticks: number;
}

export interface BuildProgress {
  progress: number; // 0-1
  eta_s: number | null;
  phase: string;
}

export interface SimStatus {
  state: SimState;
  preset: Preset | null;
  seed: number | null;
  sim_clock: string | null;
  real_clock: string;
  history_start: string | null;
  history_end: string | null;
  built_at: string | null;
  dq: DqSettings;
  stream: StreamStatus;
  build: BuildProgress | null;
  totals: TableTotal[];
  db_size_bytes: number;
  landing: { files: number; bytes: number };
  last_run: RunSummary | null;
  error?: string | null;
  presets?: Partial<Record<Preset, PresetInfo>>;
}

export interface Health {
  ok: boolean;
  db: boolean;
}

export type ScenarioParamType =
  | 'int'
  | 'integer'
  | 'float'
  | 'number'
  | 'str'
  | 'string'
  | 'bool'
  | 'boolean'
  | 'date'
  | 'choice'
  | 'select'
  | (string & {});

export interface ScenarioParam {
  name: string;
  type: ScenarioParamType;
  default: string | number | boolean | null;
  options?: Array<string | number> | null;
  label?: string;
  help?: string;
  min?: number;
  max?: number;
}

export interface Scenario {
  id: string;
  name: string;
  description: string;
  params: ScenarioParam[];
}

export interface Connections {
  postgres: { host: string; port: number; user: string; password: string; databases: string[] };
  salesforce: {
    instance_url: string;
    token_url: string;
    client_id: string;
    client_secret: string;
    api_version: string;
  };
  carrier: { base_url: string; api_key: string };
  landing_path_host_hint: string;
}

// ---------------------------------------------------------------- UAT source extract (/api/uat)

export type ExtractReason = 'not_built' | 'building' | 'streaming' | 'busy' | 'extracting' | 'no_months' | (string & {});

export interface ExtractGap {
  dashboard: string;
  kpi: string;
  why: string;
}

export interface ExtractProject {
  key: string;
  name: string;
  dashboards: Array<{ id: string; name: string }>;
  uses_periods: boolean;
  default_periods: string[];
  note: string | null;
  not_in_extract: ExtractGap[];
}

export interface ExtractProgress {
  project: string;
  file: string | null;
  done: number;
  total: number;
  started_at: string;
}

export interface ExtractFile {
  path: string;
  rows: number | null;
  bytes: number;
}

export interface ExtractResult {
  id: string;
  filename: string;
  size_bytes: number;
  project: string;
  periods: string[];
  sim_clock: string;
  generated_at: string;
  duration_s: number;
  files: ExtractFile[] | null;
  download_url: string;
}

export interface ExtractOptions {
  ready: boolean;
  reason_code: ExtractReason | null;
  reason: string | null;
  sim_clock: string | null;
  preset: Preset | null;
  seed: number | null;
  history_start: string | null;
  /** When the first sensor reading landed (builds keep only the last weeks of IoT history). */
  sensor_readings_from: string | null;
  months: string[];
  max_periods: number;
  projects: ExtractProject[];
  in_progress: ExtractProgress | null;
  recent: ExtractResult[];
}

/** Server-sent events on GET /api/sim/events. */
export interface ProgressEvent {
  run_id: number | string;
  phase: string;
  pct: number; // 0-1 (a value > 1 is read as a percentage)
  eta_s: number | null;
}
export interface LogEvent {
  ts: string;
  level: 'debug' | 'info' | 'warning' | 'error' | (string & {});
  msg: string;
}

// ---------------------------------------------------------------- tracker (§5.4)

export type StatusName = 'To Do' | 'In Progress' | 'In Review' | 'Done';
export type IssueType = 'Epic' | 'Story' | 'Task' | 'Bug' | 'Spike';
export type Priority = 'Highest' | 'High' | 'Medium' | 'Low' | 'Lowest';

export interface Person {
  key: string;
  name: string;
  title?: string | null;
  dept?: string | null;
}

export type SprintState = 'future' | 'active' | 'closed';

export interface Sprint {
  id: number;
  name: string;
  goal?: string | null;
  state: SprintState;
  /** Planned window (from the start date) until started; actual dates once started/closed. */
  start_date: string | null;
  end_date: string | null;
  completed_at?: string | null;
  project?: number | null;
}

export interface PresetInfo {
  months: number;
  volume_scale?: number;
}

export interface TrackerMeta {
  project: { key: string; name: string };
  statuses: StatusName[];
  issue_types: IssueType[];
  priorities: Priority[];
  people: Person[];
  components: string[];
  labels: string[];
  sprints: Sprint[];
  start_date: string | null;
  configured: boolean;
  /** People key of the engineer using the tracker (assignee of "my" issues). Defaults to "me". */
  me?: string | null;
}

export interface ChecklistItem {
  id?: string;
  /** Position in the server's list; PATCH addresses criteria by idx. */
  idx?: number;
  text: string;
  done: boolean;
}

export interface Comment {
  id: number | string;
  author: string;
  body: string;
  created: string;
}

export interface Worklog {
  id: number | string;
  hours: number;
  date: string;
  note: string | null;
  author?: string | null;
  created?: string | null;
}

export interface Attachment {
  name: string;
  path: string;
  url?: string | null;
}

export interface IssueLink {
  type: 'blocks' | 'is blocked by' | 'relates to' | (string & {});
  key: string;
}

export interface HistoryEntry {
  ts: string;
  field: string;
  from: string | null;
  to: string | null;
  actor?: string | null;
}

export interface Issue {
  key: string;
  type: IssueType;
  summary: string;
  status: StatusName;
  priority: Priority;
  assignee: string | null;
  reporter: string | null;
  epic_key: string | null;
  parent_key: string | null;
  sprint_id: number | null;
  rank: number | string;
  story_points: number | null;
  estimate_hours: number | null;
  time_spent_hours?: number | null;
  due_date: string | null;
  labels: string[];
  components: string[];
  description?: string | null;
  acceptance_criteria?: ChecklistItem[] | string[];
  definition_of_done?: ChecklistItem[] | string[];
  depends_on?: string[];
  links?: IssueLink[];
  attachments?: Array<Attachment | string>;
  comments?: Comment[];
  worklog?: Worklog[];
  pr_url?: string | null;
  created?: string | null;
  updated?: string | null;
  resolved?: string | null;
  project_no?: number | null;
  source_id?: string | null;
  department?: string | null;
  /** Server-computed helpers (backend may send them; the UI falls back to its own). */
  branch_name?: string | null;
  overdue?: boolean;
  comment_count?: number;
}

export type IssuePatch = Partial<
  Pick<
    Issue,
    | 'summary'
    | 'status'
    | 'priority'
    | 'assignee'
    | 'sprint_id'
    | 'story_points'
    | 'estimate_hours'
    | 'due_date'
    | 'labels'
    | 'components'
    | 'pr_url'
    | 'description'
  >
> & { acceptance_criteria?: ChecklistItem[] };

export interface NewIssue {
  type: IssueType;
  summary: string;
  description?: string;
  priority?: Priority;
  assignee?: string | null;
  epic_key?: string | null;
  parent_key?: string | null;
  sprint_id?: number | null;
  estimate_hours?: number | null;
  story_points?: number | null;
  due_date?: string | null;
  labels?: string[];
}

export interface BurndownPoint {
  date: string;
  remaining: number | null; // null for days after today
  ideal: number;
}
export interface BurndownReport {
  sprint_id: number;
  unit: 'hours' | 'points' | (string & {});
  committed: number;
  days: BurndownPoint[];
}

export interface VelocityRow {
  sprint_id: number;
  name: string;
  committed: number;
  completed: number;
}
export interface VelocityReport {
  unit: 'hours' | 'points' | (string & {});
  sprints: VelocityRow[];
}

export interface CfdRow {
  date: string;
  'To Do': number;
  'In Progress': number;
  'In Review': number;
  Done: number;
}
export interface CfdReport {
  rows: CfdRow[];
}
