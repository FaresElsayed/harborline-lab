-- Simulator-private storage (database: sim_internal). The etl_reader role has NO access here.
-- Salesforce and the carrier aggregator are SaaS: he must use their APIs, not this database.

CREATE SCHEMA IF NOT EXISTS sfdc;
CREATE SCHEMA IF NOT EXISTS carrier;
CREATE SCHEMA IF NOT EXISTS sim;

-- One row per Salesforce record. `data` holds field API name -> value (JSON types: string, number, boolean, null).
-- Date fields are 'YYYY-MM-DD'; datetime fields are ISO-8601 UTC 'YYYY-MM-DDTHH:MM:SS.000+0000'.
CREATE TABLE sfdc.record (
  id                 char(18)    PRIMARY KEY,
  object             varchar(40) NOT NULL,
  data               jsonb       NOT NULL,
  created_date       timestamptz NOT NULL,
  last_modified_date timestamptz NOT NULL,
  system_modstamp    timestamptz NOT NULL,
  is_deleted         boolean     NOT NULL DEFAULT false,
  deleted_date       timestamptz,
  master_record_id   char(18)
);
CREATE INDEX record_obj_modstamp ON sfdc.record (object, system_modstamp);
CREATE INDEX record_obj_deleted  ON sfdc.record (object, is_deleted, deleted_date);

-- Per-key-prefix counters for ID generation.
CREATE TABLE sfdc.id_counter (prefix char(3) PRIMARY KEY, next_value bigint NOT NULL);

-- Schema-drift fields added at runtime (merged into describe output).
CREATE TABLE sfdc.custom_field (
  object varchar(40) NOT NULL, api_name varchar(80) NOT NULL, definition jsonb NOT NULL,
  added_at timestamptz NOT NULL, PRIMARY KEY (object, api_name)
);

-- Bulk API 2.0 query jobs.
CREATE TABLE sfdc.bulk_job (
  id char(18) PRIMARY KEY, operation varchar(10) NOT NULL, query text NOT NULL, state varchar(16) NOT NULL,
  created_date timestamptz NOT NULL, system_modstamp timestamptz NOT NULL, result_path text, number_records bigint
);

-- Query locators (REST nextRecordsUrl and Bulk 2.0 result sets): the matching record ids are snapshotted when the
-- query runs. REST locators expire after 15 minutes without a fetch, as in Salesforce. (added by apis-tracker)
CREATE TABLE sfdc.query_cursor (
  id char(18) PRIMARY KEY, kind varchar(4) NOT NULL, soql text NOT NULL, include_deleted boolean NOT NULL,
  record_ids text[] NOT NULL, options jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL, last_access timestamptz NOT NULL
);

-- Rolling API usage for the daily limit.
CREATE TABLE sfdc.api_usage (day date PRIMARY KEY, calls integer NOT NULL DEFAULT 0);

-- Carrier aggregator events. received_at = when the event became visible through the API (late events have
-- received_at well after event_ts). Cursor order is (received_at, event_id).
CREATE TABLE carrier.tracking_event (
  event_id         bigint      PRIMARY KEY,
  tracking_nbr     varchar(40) NOT NULL,
  scac             varchar(4)  NOT NULL,
  event_code       varchar(20) NOT NULL,   -- LABEL_CREATED, PICKED_UP, IN_TRANSIT, OUT_FOR_DELIVERY, DELIVERED, EXCEPTION, RETURN_TO_SENDER
  event_ts         timestamptz NOT NULL,
  event_local_ts   varchar(32) NOT NULL,   -- ISO-8601 with the local offset, as carriers send it
  city             varchar(60),
  state_prov       varchar(10),
  country_code     char(2),
  description      varchar(200),
  exception_code   varchar(20),            -- WEATHER, ADDRESS, DAMAGED, REFUSED, CARRIER_DELAY
  received_at      timestamptz NOT NULL,
  is_correction    boolean NOT NULL DEFAULT false
);
CREATE INDEX tracking_event_cursor ON carrier.tracking_event (received_at, event_id);
CREATE INDEX tracking_event_trk    ON carrier.tracking_event (tracking_nbr);

-- Simulator state and run history.
CREATE TABLE sim.state (
  id               smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  preset           varchar(10),
  seed             bigint,
  history_start    timestamptz,
  sim_clock        timestamptz,             -- end of the last simulated window (UTC)
  built_at         timestamptz,
  dq_denominator   integer NOT NULL DEFAULT 10000,
  dq_categories    jsonb   NOT NULL DEFAULT '{}'::jsonb,
  world            bytea,                   -- pickled world checkpoint (open orders, inventory index, ...)
  updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sim.run_log (
  run_id           bigserial PRIMARY KEY,
  kind             varchar(12) NOT NULL,    -- build, batch, stream_tick, scenario
  label            varchar(80),
  started_at       timestamptz NOT NULL,
  finished_at      timestamptz,
  sim_from         timestamptz,
  sim_to           timestamptz,
  status           varchar(10) NOT NULL,    -- running, ok, error
  error            text,
  changes          jsonb NOT NULL DEFAULT '[]'::jsonb   -- [{system, table, ins, upd, del}]
);

CREATE TABLE sim.anomaly_log (
  anomaly_id       bigserial PRIMARY KEY,
  injected_at      timestamptz NOT NULL DEFAULT now(),
  sim_ts           timestamptz,
  category         varchar(30) NOT NULL,
  system           varchar(12) NOT NULL,    -- wms, ax2012, iot, salesforce, carrier, edi, files
  object           varchar(240) NOT NULL,   -- table, Salesforce object, file path or <zip>!<member>
  record_key       varchar(200),
  column_name      varchar(80),
  before_value     text,
  after_value      text,
  note             text
);

CREATE TABLE sim.handbook_progress (
  page             varchar(60) PRIMARY KEY,
  state            jsonb NOT NULL,
  updated_at       timestamptz NOT NULL DEFAULT now()
);
