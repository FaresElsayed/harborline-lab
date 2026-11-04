-- Harborline sensor gateway (database: iot, schema: telemetry)
-- Readings are pushed by gateways in each warehouse. device_ts_ms is the device clock
-- (epoch milliseconds, UTC, can drift); ingested_at is when the gateway landed the row.
-- Gateways buffer when the network drops and flush late, so ingested_at can be hours after device_ts_ms.

CREATE SCHEMA IF NOT EXISTS telemetry;
SET search_path = telemetry;

CREATE TABLE sensor (
  sensor_id        varchar(24) PRIMARY KEY,     -- e.g. CHI1-CLR-03-T
  warehouse_code   varchar(8)  NOT NULL,
  zone_code        varchar(20) NOT NULL,
  sensor_type      varchar(8)  NOT NULL,        -- TEMP, HUMIDITY, DOOR
  unit             varchar(4),                  -- C, %RH
  model            varchar(40),
  installed_date   date,
  calibration_due  date,
  is_active        boolean NOT NULL DEFAULT true
);

CREATE TABLE sensor_reading (
  reading_id       bigint PRIMARY KEY,
  sensor_id        varchar(24) NOT NULL,
  device_ts_ms     bigint      NOT NULL,
  value            double precision,
  battery_pct      smallint,
  ingested_at      timestamptz NOT NULL
);
COMMENT ON TABLE sensor_reading IS 'Append-only. Nominal cadence 5 minutes. Late flushes and duplicate sends happen.';

CREATE TABLE dock_door_event (
  event_id         bigint PRIMARY KEY,
  warehouse_code   varchar(8)  NOT NULL,
  door_nbr         smallint    NOT NULL,
  event_type       varchar(6)  NOT NULL,        -- OPEN, CLOSE
  trailer_nbr      varchar(20),
  device_ts_ms     bigint      NOT NULL,
  ingested_at      timestamptz NOT NULL
);
