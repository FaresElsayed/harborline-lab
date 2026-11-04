-- Harborline Tracker (database: tracker, schema: tracker). Idempotent: safe to run at every app start.
-- Catalog tickets come from tracker/tickets/*.yaml; his own work (status, comments, work log, history,
-- ranks, acceptance-criteria checks, his own tickets) lives only here and survives catalog re-loads.

CREATE SCHEMA IF NOT EXISTS tracker;

CREATE TABLE IF NOT EXISTS tracker.settings (
  key         varchar(40) PRIMARY KEY,           -- start_date, catalog
  value       jsonb       NOT NULL,
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tracker.sprint (
  id            smallint    PRIMARY KEY,         -- 1..16; project p owns sprints 2p-1 and 2p
  name          varchar(40) NOT NULL,
  project       smallint    NOT NULL,
  goal          text,
  start_date    date,                            -- planned, from the configured start date (working days)
  end_date      date,
  state         varchar(10) NOT NULL DEFAULT 'future' CHECK (state IN ('future', 'active', 'closed')),
  started_at    timestamptz,
  completed_at  timestamptz
);

CREATE TABLE IF NOT EXISTS tracker.issue (
  key                varchar(20)  PRIMARY KEY,   -- DATA-<num>
  num                integer      NOT NULL UNIQUE,
  source_id          varchar(40)  UNIQUE,        -- catalog id (P2.S1.T3); NULL for his own tickets
  origin             varchar(8)   NOT NULL CHECK (origin IN ('catalog', 'user')),
  removed            boolean      NOT NULL DEFAULT false,  -- dropped from the catalog but kept (has his work)
  project            smallint,
  type               varchar(10)  NOT NULL CHECK (type IN ('Epic', 'Story', 'Task', 'Bug', 'Spike')),
  summary            varchar(255) NOT NULL,
  description        text         NOT NULL DEFAULT '',
  status             varchar(12)  NOT NULL DEFAULT 'To Do'
                     CHECK (status IN ('To Do', 'In Progress', 'In Review', 'Done')),
  priority           varchar(8)   NOT NULL DEFAULT 'Medium'
                     CHECK (priority IN ('Highest', 'High', 'Medium', 'Low', 'Lowest')),
  reporter           varchar(40),
  assignee           varchar(40),
  department         varchar(40),
  epic_key           varchar(20),
  parent_key         varchar(20),
  sprint_id          smallint     REFERENCES tracker.sprint (id),
  story_points       numeric(5,1),
  estimate_hours     numeric(6,1),
  due_day            smallint,                   -- catalog: working day within the project (1..20)
  start_day          smallint,
  due_date           date,
  start_date         date,
  labels             text[]       NOT NULL DEFAULT '{}',
  components         text[]       NOT NULL DEFAULT '{}',
  attachments        text[]       NOT NULL DEFAULT '{}',
  dashboards         text[]       NOT NULL DEFAULT '{}',
  definition_of_done jsonb        NOT NULL DEFAULT '[]'::jsonb,
  pr_url             varchar(400),
  created_at         timestamptz  NOT NULL DEFAULT now(),
  updated_at         timestamptz  NOT NULL DEFAULT now(),
  resolved_at        timestamptz
);
CREATE INDEX IF NOT EXISTS issue_sprint ON tracker.issue (sprint_id);
CREATE INDEX IF NOT EXISTS issue_epic   ON tracker.issue (epic_key);
CREATE INDEX IF NOT EXISTS issue_parent ON tracker.issue (parent_key);

CREATE TABLE IF NOT EXISTS tracker.rank (
  issue_key  varchar(20) PRIMARY KEY REFERENCES tracker.issue (key) ON DELETE CASCADE,
  rank       numeric     NOT NULL
);

CREATE TABLE IF NOT EXISTS tracker.ac_check (
  issue_key   varchar(20) NOT NULL REFERENCES tracker.issue (key) ON DELETE CASCADE,
  idx         smallint    NOT NULL,
  text        text        NOT NULL,
  checked     boolean     NOT NULL DEFAULT false,
  checked_at  timestamptz,
  PRIMARY KEY (issue_key, idx)
);

CREATE TABLE IF NOT EXISTS tracker.comment (
  id          bigserial   PRIMARY KEY,
  issue_key   varchar(20) NOT NULL REFERENCES tracker.issue (key) ON DELETE CASCADE,
  author      varchar(40) NOT NULL,
  body        text        NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS comment_issue ON tracker.comment (issue_key);

CREATE TABLE IF NOT EXISTS tracker.worklog (
  id          bigserial    PRIMARY KEY,
  issue_key   varchar(20)  NOT NULL REFERENCES tracker.issue (key) ON DELETE CASCADE,
  author      varchar(40)  NOT NULL,
  hours       numeric(5,2) NOT NULL CHECK (hours > 0),
  work_date   date         NOT NULL,
  note        text,
  created_at  timestamptz  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS worklog_issue ON tracker.worklog (issue_key);

-- One row per field change he makes (status, sprint, assignee, ...). Burndown, velocity and CFD replay it.
CREATE TABLE IF NOT EXISTS tracker.history (
  id          bigserial   PRIMARY KEY,
  issue_key   varchar(20) NOT NULL REFERENCES tracker.issue (key) ON DELETE CASCADE,
  field       varchar(30) NOT NULL,
  from_value  text,
  to_value    text,
  author      varchar(40) NOT NULL DEFAULT 'me',
  changed_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS history_issue ON tracker.history (issue_key, changed_at);

-- from_key blocks to_key (catalog depends_on: the dependency blocks the dependent ticket).
CREATE TABLE IF NOT EXISTS tracker.link (
  id         bigserial   PRIMARY KEY,
  from_key   varchar(20) NOT NULL REFERENCES tracker.issue (key) ON DELETE CASCADE,
  to_key     varchar(20) NOT NULL REFERENCES tracker.issue (key) ON DELETE CASCADE,
  link_type  varchar(20) NOT NULL DEFAULT 'Blocks',
  origin     varchar(8)  NOT NULL DEFAULT 'catalog',
  UNIQUE (from_key, to_key, link_type)
);
