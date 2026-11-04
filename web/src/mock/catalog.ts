/* Development-only ticket catalog, shaped like R/tracker/tickets/pNN_*.yaml (DESIGN_SPEC §6).
   When those YAML files exist, `npm run fixtures` writes fixtures/tickets.generated.json and the mock uses it instead. */

export interface CatalogTask {
  id: string;
  type: 'Task' | 'Bug' | 'Spike';
  summary: string;
  estimate_hours: number;
  due_day: number;
  priority: string;
  components: string[];
  labels: string[];
  description?: string;
  acceptance_criteria?: string[];
  definition_of_done?: string[];
  depends_on?: string[];
}

export interface CatalogStory {
  id: string;
  type?: 'Story';
  summary: string;
  reporter: string;
  priority: string;
  story_points: number;
  sprint: number;
  due_day: number;
  components: string[];
  labels: string[];
  description?: string;
  acceptance_criteria?: string[];
  definition_of_done?: string[];
  depends_on?: string[];
  attachments?: string[];
  tasks: CatalogTask[];
}

export interface CatalogProject {
  project: number;
  epic: {
    id: string;
    summary: string;
    department: string;
    reporter: string;
    priority: string;
    labels: string[];
    description?: string;
    dashboards?: string[];
  };
  stories: CatalogStory[];
}

type TaskSpec = [summary: string, hours: number, kind?: 'Bug' | 'Spike', priority?: string];
interface StorySpec {
  summary: string;
  points: number;
  due: number;
  component: string;
  labels: string[];
  reporter?: string;
  priority?: string;
  description?: string;
  ac?: string[];
  dashboard?: boolean;
  tasks: TaskSpec[];
}
interface ProjectSpec {
  epic: string;
  department: string;
  reporter: string;
  labels: string[];
  description: string;
  stories: StorySpec[];
}

const PROJECTS: ProjectSpec[] = [
  {
    epic: 'Data Platform Foundation',
    department: 'IT',
    reporter: 'sam.okafor',
    labels: ['platform', 'ingestion'],
    description: `Harborline has no analytics platform today. Reports are pulled by hand from the WMS, AX 2012 and Salesforce, and nobody trusts the numbers because each export is taken at a different time.

**Why now.** Operations and Finance both asked for dashboards this quarter. Without a governed landing zone every project after this one would rebuild ingestion from scratch.

**Success looks like**
- Every source system lands in the platform on a schedule, with deletes captured.
- A *Data Platform Health* dashboard shows freshness, volumes, data-quality failures and cost.
- The repository, CI and environments are the ones every later project uses.`,
    stories: [
      {
        summary: 'Repository, environments and CI baseline',
        points: 5,
        due: 4,
        component: 'Platform',
        labels: ['git', 'ci'],
        description: `Set up the repository and delivery workflow that every later ticket goes through. Sam wants to see that nothing reaches \`main\` without review and a green build.`,
        ac: [
          '`main` is protected: PR required, one approval (self-review allowed), status checks required',
          'Pre-commit runs gitleaks, ruff and sqlfluff locally and in CI',
          'CI runs lint, unit tests and `dbt build` against the dev target on every PR',
          'Dev and prod are separate schemas or workspaces, documented in an ADR',
        ],
        tasks: [
          ['Create the repository with branch protection and a PR template', 4],
          ['Pre-commit hooks: gitleaks, ruff and sqlfluff', 3],
          ['CI workflow running lint, tests and dbt build on every PR', 6, undefined, 'High'],
          ['ADR: dev / prod separation and naming conventions', 4],
          ['Infrastructure as code for warehouse objects', 6],
          ['Store source credentials outside the repository', 3, undefined, 'High'],
        ],
      },
      {
        summary: 'Land the WMS tables with snapshot and CDC',
        points: 8,
        due: 9,
        component: 'Ingestion',
        labels: ['wms', 'cdc'],
        reporter: 'marcus.bell',
        description: `Marcus confirmed the WMS deletes rows physically and that \`order_line\` has no \`updated_at\`. A daily full extract is not acceptable for the tables Operations watches during the day.`,
        ac: [
          'All 24 WMS tables land in bronze with load timestamps',
          'Inserts, updates and hard deletes are captured for every table',
          '`order_line` changes are detected even though it has no `updated_at`',
          'A freshness table records rows and the latest change per table and load',
        ],
        tasks: [
          ['Connect as etl_reader and profile the WMS schema', 4],
          ['Consume the hl_wms_cdc publication through a logical replication slot', 8, undefined, 'High'],
          ['Bronze landing for the 24 WMS tables', 8],
          ['Snapshot-diff fallback for order_line', 6],
          ['Capture hard deletes as tombstones', 5],
          ['Row-count and freshness audit table', 4],
        ],
      },
      {
        summary: 'Ingest the AX 2012 finance tables',
        points: 5,
        due: 12,
        component: 'Ingestion',
        labels: ['ax2012'],
        reporter: 'priya.raman',
        description: `Priya can share table and enum documentation but cannot give database owner rights. Posted documents in AX never change; settlements and unposted journals do.`,
        ac: [
          'Every AX table listed in the source guide lands incrementally on MODIFIEDDATETIME',
          'Integer enums are decoded through SRSANALYSISENUMS',
          '1900-01-01 is treated as an empty date downstream',
          'Row counts reconcile per DATAAREAID (us01, ca01, mx01)',
        ],
        tasks: [
          ['Map AX system columns and DATAAREAID partitions', 4],
          ['Incremental load on MODIFIEDDATETIME with a late-row window', 6],
          ['Decode integer enums through SRSANALYSISENUMS', 4],
          ['Treat 1900-01-01 as an empty date', 2],
          ['Reconcile row counts per legal entity', 3],
        ],
      },
      {
        summary: 'Salesforce and carrier API ingestion',
        points: 8,
        due: 15,
        component: 'Ingestion',
        labels: ['salesforce', 'carrier', 'api'],
        reporter: 'dana.whitfield',
        description: `Dana warned that Salesforce keeps deleted records for only 15 days and that the daily API limit is shared with the sales team's tools. The carrier aggregator throttles hard and sends events late.`,
        ac: [
          'Salesforce objects load incrementally and stay inside the daily API limit',
          'Deleted Salesforce records are captured before the 15-day recycle bin empties',
          'Carrier events load with cursor paging and survive 429 and 503 responses',
          'Duplicate and out-of-order carrier events are resolved in silver',
        ],
        tasks: [
          ['OAuth client-credentials token handling', 3],
          ['Incremental SOQL pulls following nextRecordsUrl', 6],
          ['Capture deletes through the /deleted endpoint', 4, undefined, 'High'],
          ['Backfill OpportunityHistory with Bulk API 2.0', 5],
          ['Carrier events: cursor paging with Retry-After on 429', 6],
          ['De-duplicate out-of-order carrier events', 4],
        ],
      },
      {
        summary: 'Landing-folder files and EDI',
        points: 5,
        due: 17,
        component: 'Ingestion',
        labels: ['edi', 'files'],
        reporter: 'tomas.ortega',
        tasks: [
          ['Parse X12 940 and 856 envelopes and check SE counts', 8],
          ['Item-master CSVs with mixed delimiters and cp1252', 5],
          ['Budget workbook with merged headers', 4],
          ['File manifest and duplicate-resend detection', 4],
        ],
      },
      {
        summary: 'Data Platform Health dashboard',
        points: 8,
        due: 20,
        component: 'Dashboards',
        labels: ['dashboards'],
        dashboard: true,
        description: `Sam wants one page he can open on Monday morning to see whether the platform is healthy before anyone asks about a number.`,
        ac: [
          'KPI definitions for freshness, volume, DQ failures and cost are written and signed off',
          'Filters: source system, table, date range',
          'Refresh SLA: hourly; the freshness stamp is visible on the page',
          'Access: IT team only',
          'Every visual loads in under 3 seconds',
          'Uses the Harborline brand kit (theme, background template, number formats)',
          'Demo and readout given to Sam and Kevin',
        ],
        tasks: [
          ['Dashboard spec and wireframe sign-off with Sam', 3],
          ['Model freshness, volume and DQ metrics', 6],
          ['Track platform cost from system tables', 4],
          ['Build the Data Platform Health dashboard', 8, undefined, 'High'],
          ['Readout to Sam and Kevin', 2],
        ],
      },
    ],
  },
  {
    epic: 'Warehouse Performance Scorecard',
    department: 'Operations',
    reporter: 'luis.moreno',
    labels: ['operations', 'dashboards'],
    description: `Luis runs eight warehouses on spreadsheets that each GM builds differently. He wants one scorecard that ranks sites on the same definitions.

**Why now.** Peak season starts in six weeks and he has to decide where to add temporary labour.

**Success looks like**
- OTIF, order cycle time, dock-to-stock, pick productivity, inventory accuracy and space utilization per warehouse, on one page.
- Definitions agreed with Operations, Client Success and Finance, and written down.`,
    stories: [
      {
        summary: 'Conformed warehouse, client and calendar dimensions',
        points: 5,
        due: 5,
        component: 'Modeling',
        labels: ['dimensions'],
        tasks: [
          ['Warehouse and zone dimension with local time zones', 4],
          ['Client dimension with history on the active flag', 6],
          ['Calendar with public holidays from Nager.Date', 5],
          ['Unit conversion between lb and kg', 3],
          ['Location dimension with zone and temperature class', 4],
          ['Material dimension with dimension and weight fixes', 5],
        ],
      },
      {
        summary: 'Order fulfilment facts',
        points: 8,
        due: 10,
        component: 'Modeling',
        labels: ['wms', 'facts'],
        tasks: [
          ['Order and order-line facts with status timestamps', 8, undefined, 'High'],
          ['Shipment facts with carrier and service', 6],
          ['Order cycle-time model', 5],
          ['What does "on time" mean? Agree it with Ops, CS and Finance', 4, 'Spike', 'High'],
          ['Late and partial shipment flags', 5],
          ['Data tests for the order facts', 4],
          ['Channel and order-type attributes', 3],
        ],
      },
      {
        summary: 'Inbound and dock-to-stock',
        points: 5,
        due: 13,
        component: 'Modeling',
        labels: ['wms', 'inbound'],
        reporter: 'shanice.carter',
        tasks: [
          ['ASN and receipt facts', 5],
          ['Dock-to-stock duration by warehouse', 5],
          ['Receiving discrepancies, ASN against received', 4],
          ['Returns facts and reason codes', 5],
        ],
      },
      {
        summary: 'Labour productivity',
        points: 5,
        due: 16,
        component: 'Modeling',
        labels: ['labor'],
        reporter: 'shanice.carter',
        tasks: [
          ['Task facts and picker productivity', 6],
          ['Labour hours by role and shift', 4],
          ['Find the picker outliers', 4, 'Spike'],
          ['Pick tasks disappear after the 180-day purge', 3, 'Bug', 'High'],
          ['Shift calendar per warehouse', 3],
        ],
      },
      {
        summary: 'Inventory accuracy and space utilization',
        points: 5,
        due: 18,
        component: 'Modeling',
        labels: ['inventory'],
        reporter: 'marcus.bell',
        tasks: [
          ['Cycle-count accuracy by zone', 5],
          ['Pallet-position utilization against capacity', 5],
          ['Reconcile the inventory ledger to on-hand', 6, undefined, 'High'],
          ['Lot and expiry attributes', 4],
          ['Holds and releases timeline', 4],
        ],
      },
      {
        summary: 'Warehouse Ops Scorecard',
        points: 8,
        due: 20,
        component: 'Dashboards',
        labels: ['dashboards'],
        dashboard: true,
        tasks: [
          ['Scorecard spec: OTIF, cycle time, dock-to-stock, productivity', 3],
          ['KPI definitions log signed off by Luis', 3],
          ['Build the Warehouse Ops Scorecard', 10, undefined, 'High'],
          ['Drill-through from the scorecard to the order list', 5],
          ['Tune every visual to load in under 3 s', 4],
          ['Readout to Luis and Shanice', 2],
        ],
      },
    ],
  },
  {
    epic: 'Billing Assurance and AR',
    department: 'Finance',
    reporter: 'rebecca.lindqvist',
    labels: ['finance', 'billing'],
    description: `Rebecca suspects Harborline bills less than its contracts allow, and Alan cannot explain why DSO moved from 41 to 52 days. Both want answers they can defend to the board.`,
    stories: [
      {
        summary: 'Contracted rates with history',
        points: 5,
        due: 4,
        component: 'Modeling',
        labels: ['salesforce', 'scd2'],
        reporter: 'alan.brooks',
        tasks: [
          ['Rate card lines from Rate_Card__c', 5],
          ['Keep history for billing_rate overwrites (SCD2)', 6, undefined, 'High'],
          ['Map charge codes to GL accounts', 3],
          ['Contract currency against billing currency', 4],
          ['Reconcile rate versions to Salesforce amendments', 4],
        ],
      },
      {
        summary: 'Expected against billed revenue',
        points: 8,
        due: 9,
        component: 'Modeling',
        labels: ['billing'],
        tasks: [
          ['Expected charges from warehouse activity', 8],
          ['Billing records to invoice lines', 6],
          ['Under-billing variance by client and charge code', 5],
          ['Why are some invoices still on the old rate?', 4, 'Spike', 'High'],
        ],
      },
      {
        summary: 'Accounts receivable and DSO',
        points: 8,
        due: 13,
        component: 'Modeling',
        labels: ['ax2012', 'ar'],
        reporter: 'alan.brooks',
        tasks: [
          ['Open items and settlements from custtrans', 6],
          ['Aging buckets per legal entity', 4],
          ['DSO by client with payment terms', 5],
          ['Settlement reversals double-count the paid amount', 3, 'Bug', 'High'],
          ['FX to USD: ECB against AX rates', 5],
        ],
      },
      {
        summary: 'Warehouse P&L against budget',
        points: 5,
        due: 16,
        component: 'Modeling',
        labels: ['budget'],
        tasks: [
          ['Budget workbook to long format', 5],
          ['GL actuals by site and department', 6],
          ['Allocate shared costs', 4],
          ['Variance model', 4],
        ],
      },
      {
        summary: 'Revenue Assurance and AR dashboards',
        points: 8,
        due: 20,
        component: 'Dashboards',
        labels: ['dashboards'],
        dashboard: true,
        tasks: [
          ['Dashboard specs for Revenue Assurance and AR', 3],
          ['Build the Revenue Assurance dashboard', 10, undefined, 'High'],
          ['Build the AR and DSO dashboard', 8],
          ['Warehouse P&L page', 6],
          ['Row-level security for controllers', 3],
          ['Readout to Rebecca and Alan', 2],
        ],
      },
    ],
  },
  {
    epic: 'Freight Audit and Carrier Performance',
    department: 'Transportation',
    reporter: 'beth.kowalczyk',
    labels: ['transportation', 'carrier'],
    description: `Beth pays every carrier bill as it arrives. She thinks one carrier overbills by a few percent but has never had the data to dispute it.`,
    stories: [
      {
        summary: 'Carrier invoices and rate cards',
        points: 5,
        due: 5,
        component: 'Ingestion',
        labels: ['ax2012', 'files'],
        tasks: [
          ['Weekly carrier bills from vendinvoice', 6],
          ['Parse freight-bill lines for tracking numbers', 6],
          ['Carrier rate-card workbook versions', 5],
        ],
      },
      {
        summary: 'Tracking events to shipment milestones',
        points: 8,
        due: 10,
        component: 'Modeling',
        labels: ['carrier'],
        tasks: [
          ['Event stream to shipment milestones', 8],
          ['Late, duplicate and out-of-order events', 5],
          ['Delivered against promised date', 4],
          ['Exception-code taxonomy', 3],
        ],
      },
      {
        summary: 'Freight audit',
        points: 8,
        due: 14,
        component: 'Modeling',
        labels: ['audit'],
        tasks: [
          ['Expected charge from rate card, zone and billed weight', 8, undefined, 'High'],
          ['Dimensional-weight check', 4],
          ['Overbilling by carrier', 5],
          ['Is the overbilling one carrier or one lane?', 4, 'Spike'],
          ['Reprinted labels create a new tracking number', 4, 'Bug'],
        ],
      },
      {
        summary: 'Carrier scorecard',
        points: 5,
        due: 17,
        component: 'Modeling',
        labels: ['carrier'],
        tasks: [
          ['On-time rate by carrier and service', 5],
          ['Transit-time distributions', 4],
          ['Storm days from Open-Meteo', 6],
          ['Fuel surcharge against the EIA diesel index', 5],
        ],
      },
      {
        summary: 'Freight Audit and Carrier Scorecard dashboards',
        points: 8,
        due: 20,
        component: 'Dashboards',
        labels: ['dashboards'],
        dashboard: true,
        tasks: [
          ['Dashboard specs with Beth', 3],
          ['Build the Freight Audit dashboard', 10, undefined, 'High'],
          ['Build the Carrier Scorecard', 8],
          ['Dispute export for Beth', 4],
          ['Readout to Beth', 2],
        ],
      },
    ],
  },
  {
    epic: 'Client 360',
    department: 'Sales',
    reporter: 'hannah.price',
    labels: ['sales', 'client-success'],
    description: `Sales and Client Success look at the same clients through different tools. Omar wants to see churn risk before a client gives notice; Hannah wants the pipeline she reports to Daniel to match Salesforce.`,
    stories: [
      {
        summary: 'Salesforce account model',
        points: 5,
        due: 4,
        component: 'Modeling',
        labels: ['salesforce'],
        reporter: 'dana.whitfield',
        tasks: [
          ['Accounts, contacts and opportunities', 6],
          ['Opportunity history and stage durations', 5],
          ['Duplicate accounts and MasterRecordId merges', 5],
          ['Client key crosswalk across WMS, AX and Salesforce', 6, undefined, 'High'],
        ],
      },
      {
        summary: 'Pipeline',
        points: 5,
        due: 8,
        component: 'Modeling',
        labels: ['pipeline'],
        tasks: [
          ['Pipeline by stage and owner', 5],
          ['Win rate and close-date slips', 5],
          ['Forecast against closed', 4],
        ],
      },
      {
        summary: 'Client health score',
        points: 8,
        due: 13,
        component: 'Modeling',
        labels: ['health-score'],
        reporter: 'omar.haddad',
        tasks: [
          ['Which signals predict churn? Workshop with Omar', 4, 'Spike'],
          ['Case volume and severity trends', 5],
          ['Order volume trend', 6],
          ['Payment behaviour from AR', 4],
          ['Health score model', 6, undefined, 'High'],
        ],
      },
      {
        summary: 'Reverse ETL to Salesforce',
        points: 5,
        due: 16,
        component: 'Reverse ETL',
        labels: ['salesforce'],
        reporter: 'dana.whitfield',
        tasks: [
          ['PATCH Health_Score__c and Churn_Risk__c', 6],
          ['Writer that respects the API limit', 4],
          ['Audit log of every write', 3],
          ['PATCH fails for merged accounts', 3, 'Bug'],
        ],
      },
      {
        summary: 'Pipeline and Client Health 360 dashboards',
        points: 8,
        due: 20,
        component: 'Dashboards',
        labels: ['dashboards'],
        dashboard: true,
        tasks: [
          ['Dashboard specs with Hannah and Omar', 3],
          ['Build the Pipeline dashboard', 8],
          ['Build Client Health 360', 10, undefined, 'High'],
          ['Readout to Hannah and Omar', 2],
        ],
      },
    ],
  },
  {
    epic: 'Real-Time Operations',
    department: 'Operations',
    reporter: 'luis.moreno',
    labels: ['operations', 'streaming'],
    description: `Supervisors find out about missed carrier cut-offs the next morning. Luis wants the floor to see open work against cut-off while there is still time to act.`,
    stories: [
      {
        summary: 'CDC pipeline into silver',
        points: 8,
        due: 6,
        component: 'Streaming',
        labels: ['cdc'],
        reporter: 'kevin.ohara',
        tasks: [
          ['Logical decoding consumer with pgoutput', 10, undefined, 'High'],
          ['Exactly-once merge into silver', 8],
          ['Handle schema changes in the stream', 5],
          ['ADR: replay and backfill', 3],
        ],
      },
      {
        summary: 'Streaming transforms',
        points: 5,
        due: 11,
        component: 'Streaming',
        labels: ['cdc'],
        tasks: [
          ['Open orders and wave status', 6],
          ['Dock-door events from IoT', 5],
          ['Watermarks for late events', 6],
        ],
      },
      {
        summary: 'Freshness SLO',
        points: 5,
        due: 14,
        component: 'Data Quality',
        labels: ['slo'],
        reporter: 'kevin.ohara',
        tasks: [
          ['Measure end-to-end latency', 5],
          ['Alert when lag passes 5 minutes', 4],
          ['Replication slot keeps WAL growing', 4, 'Bug', 'Highest'],
        ],
      },
      {
        summary: 'Live Operations Board',
        points: 8,
        due: 20,
        component: 'Dashboards',
        labels: ['dashboards'],
        dashboard: true,
        tasks: [
          ['Live board spec with Luis', 3],
          ['Build the Live Operations Board', 10, undefined, 'High'],
          ['Auto-refresh and freshness stamp', 3],
          ['Soak test at 60× stream speed', 5],
          ['Readout to Luis', 2],
        ],
      },
    ],
  },
  {
    epic: 'Cold Chain and Lot Traceability',
    department: 'Quality',
    reporter: 'ayesha.khan',
    labels: ['quality', 'iot'],
    description: `CHI1 holds food and pharmaceutical lots. Dr. Khan must prove every lot stayed in range and trace a recall to every shipment within hours, not days.`,
    stories: [
      {
        summary: 'Sensor data model',
        points: 5,
        due: 5,
        component: 'Modeling',
        labels: ['iot'],
        reporter: 'grace.liu',
        tasks: [
          ['Sensor and reading facts', 6],
          ['Detect stuck sensors', 5],
          ['Spike detection', 5],
          ['Duplicate readings at the DST change', 3, 'Bug'],
        ],
      },
      {
        summary: 'Temperature excursions',
        points: 8,
        due: 10,
        component: 'Modeling',
        labels: ['cold-chain'],
        tasks: [
          ['Excursion rules: range and duration per zone', 8, undefined, 'High'],
          ['Link holds and releases to lots', 6],
          ['Excursion impact by client', 4],
        ],
      },
      {
        summary: 'Lot genealogy and recall trace',
        points: 8,
        due: 15,
        component: 'Modeling',
        labels: ['traceability'],
        tasks: [
          ['Lot genealogy: receipt to inventory to shipment', 10],
          ['Recall trace query', 6, undefined, 'High'],
          ['Expiry and FEFO compliance', 4],
        ],
      },
      {
        summary: 'Cold Chain Monitor and Recall Trace',
        points: 8,
        due: 20,
        component: 'Dashboards',
        labels: ['dashboards'],
        dashboard: true,
        tasks: [
          ['Dashboard specs with Dr. Khan', 3],
          ['Build the Cold Chain Monitor', 10, undefined, 'High'],
          ['Build Recall Trace', 8],
          ['Audit evidence export', 5],
          ['Readout to Dr. Khan', 2],
        ],
      },
    ],
  },
  {
    epic: 'Client Portal and Executive Scorecard',
    department: 'Executive',
    reporter: 'daniel.reyes',
    labels: ['executive', 'security'],
    description: `Clients ask for their own numbers every week, and Daniel wants one scorecard for the board. Mei will not allow either without row-level security and masked PII.`,
    stories: [
      {
        summary: 'Row-level security and PII masking',
        points: 8,
        due: 6,
        component: 'Security',
        labels: ['security', 'pii'],
        reporter: 'mei.tanaka',
        tasks: [
          ['PII inventory and masking policy with Mei', 5],
          ['Row-level security per client', 8, undefined, 'High'],
          ['Access review process', 3],
        ],
      },
      {
        summary: 'Client portal',
        points: 8,
        due: 12,
        component: 'Dashboards',
        labels: ['portal'],
        reporter: 'omar.haddad',
        dashboard: true,
        tasks: [
          ['Portal data mart per client', 8],
          ['Portal pages: orders, inventory, billing', 10],
          ['Client-facing metric definitions', 4],
          ['Drill-through leaks rows across clients', 4, 'Bug', 'Highest'],
        ],
      },
      {
        summary: 'Executive Scorecard',
        points: 8,
        due: 17,
        component: 'Dashboards',
        labels: ['dashboards'],
        dashboard: true,
        tasks: [
          ['KPI tree across all eight projects', 6],
          ['Build the Executive Scorecard', 10, undefined, 'High'],
          ['Narrative annotations', 3],
        ],
      },
      {
        summary: 'Hardening and final readout',
        points: 5,
        due: 20,
        component: 'Platform',
        labels: ['release'],
        tasks: [
          ['Release tags and CHANGELOG for every project', 3],
          ['Cost optimisation pass', 5],
          ['Disaster-recovery test', 5],
          ['Portfolio write-up and demo video', 8],
          ['Final readout to Daniel', 3],
        ],
      },
    ],
  },
];

const TARGET_HOURS = 152;

function taskDescription(summary: string, story: StorySpec, project: ProjectSpec): string {
  return `Part of **${story.summary}** in *${project.epic}*.

${summary}. Agree the expected result with the reporter before you start, and record anything you learn about the source in the story's comments so the next ticket can use it.`;
}

function storyDescription(story: StorySpec, project: ProjectSpec): string {
  return (
    story.description ??
    `Milestone of *${project.epic}*. The ${project.department} team needs **${story.summary.toLowerCase()}** before the next milestone can start.`
  );
}

function defaultAc(story: StorySpec): string[] {
  return (
    story.ac ?? [
      `${story.summary} is modeled, tested and documented in the repository`,
      'Data tests cover keys, nulls and accepted values for every new model',
      'Numbers reconcile to the source system for one sample day',
      'The reporter has reviewed the result and the review is recorded in a comment',
    ]
  );
}

export const HAND_CATALOG: CatalogProject[] = PROJECTS.map((p, pi) => {
  const project = pi + 1;
  const raw = p.stories.reduce((s, st) => s + st.tasks.reduce((t, [, h]) => t + h, 0), 0);
  const scale = TARGET_HOURS / raw;
  let prevDue = 0;
  const stories = p.stories.map((st, si): CatalogStory => {
    const id = `P${project}.S${si + 1}`;
    const span = Math.max(1, st.due - prevDue);
    const tasks = st.tasks.map(([summary, hours, kind, priority], ti): CatalogTask => {
      const due = Math.min(st.due, prevDue + Math.max(1, Math.round(((ti + 1) / st.tasks.length) * span)));
      return {
        id: `${id}.T${ti + 1}`,
        type: kind ?? 'Task',
        summary,
        estimate_hours: Math.max(1, Math.round(hours * scale)),
        due_day: due,
        priority: priority ?? (kind === 'Bug' ? 'High' : 'Medium'),
        components: [st.component],
        labels: st.labels,
        description: taskDescription(summary, st, p),
        acceptance_criteria: [`${summary} is merged to main through a reviewed PR`, 'Tests cover the change and CI is green'],
        depends_on: ti > 0 ? [`${id}.T${ti}`] : si > 0 ? [`P${project}.S${si}`] : [],
      };
    });
    const story: CatalogStory = {
      id,
      type: 'Story',
      summary: st.summary,
      reporter: st.reporter ?? p.reporter,
      priority: st.priority ?? (st.dashboard ? 'High' : 'Medium'),
      story_points: st.points,
      sprint: st.due <= 10 ? 1 : 2,
      due_day: st.due,
      components: [st.component],
      labels: st.labels,
      description: storyDescription(st, p),
      acceptance_criteria: defaultAc(st),
      depends_on: si > 0 ? [`P${project}.S${si}`] : project > 1 ? [`P${project - 1}.S1`] : [],
      attachments: st.dashboard ? [`wireframes/P${project}-D1.svg`] : [],
      tasks,
    };
    prevDue = st.due;
    return story;
  });
  return {
    project,
    epic: {
      id: `P${project}`,
      summary: p.epic,
      department: p.department,
      reporter: p.reporter,
      priority: 'High',
      labels: p.labels,
      description: p.description,
      dashboards: [`P${project}-D1`],
    },
    stories,
  };
});
