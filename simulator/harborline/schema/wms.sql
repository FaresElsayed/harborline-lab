-- Harborline WMS (database: wms, schema: wms)
-- Legacy on-prem warehouse management system. All timestamps are LOCAL warehouse time
-- (timestamp without time zone); see warehouse.time_zone. Weights/dimensions use the unit
-- recorded on the row (LB/IN in US sites, KG/CM in TOR1/MTY1).
-- Audit columns: create_ts / mod_ts / create_user / mod_user (mod_ts is set by the application,
-- not by a trigger). Some tables have no mod_ts on purpose (see order_line).

CREATE SCHEMA IF NOT EXISTS wms;
SET search_path = wms;

CREATE TABLE warehouse (
  warehouse_id     smallint PRIMARY KEY,
  warehouse_code   varchar(8)  NOT NULL UNIQUE,
  warehouse_name   varchar(80) NOT NULL,
  address1         varchar(100),
  city             varchar(60),
  state_prov       varchar(10),
  postal_code      varchar(12),
  country_code     char(2)     NOT NULL,
  time_zone        varchar(40) NOT NULL,
  weight_uom       varchar(2)  NOT NULL,
  dim_uom          varchar(2)  NOT NULL,
  is_active        boolean     NOT NULL DEFAULT true,
  opened_date      date,
  create_ts        timestamp   NOT NULL,
  mod_ts           timestamp   NOT NULL
);
COMMENT ON TABLE warehouse IS 'Physical distribution centers operated by Harborline.';
COMMENT ON COLUMN warehouse.time_zone IS 'IANA zone. Every timestamp in this database is local to the row''s warehouse.';

CREATE TABLE zone (
  zone_id          integer PRIMARY KEY,
  warehouse_id     smallint    NOT NULL REFERENCES warehouse,
  zone_code        varchar(20) NOT NULL,
  zone_type        varchar(12) NOT NULL,   -- AMBIENT, COOLER, FREEZER, HAZMAT, CAGE, BULK, RETURNS, DOCK, STAGING
  temp_min_c       numeric(5,1),
  temp_max_c       numeric(5,1),
  is_active        boolean NOT NULL DEFAULT true,
  create_ts        timestamp NOT NULL,
  mod_ts           timestamp NOT NULL,
  UNIQUE (warehouse_id, zone_code)
);
COMMENT ON TABLE zone IS 'Storage zones inside a warehouse. Temperature range applies to COOLER/FREEZER zones.';

CREATE TABLE location (
  location_id      integer PRIMARY KEY,
  warehouse_id     smallint    NOT NULL REFERENCES warehouse,
  zone_id          integer     NOT NULL REFERENCES zone,
  location_code    varchar(20) NOT NULL,   -- aisle-bay-level-position, e.g. A12-03-B-2
  aisle            varchar(4),
  bay              varchar(4),
  level            varchar(2),
  position         varchar(2),
  location_type    varchar(10) NOT NULL,   -- PICK, RESERVE, DOCK, STAGE, QC, RETURN
  max_weight       numeric(9,2),
  max_volume       numeric(9,2),
  is_active        boolean NOT NULL DEFAULT true,
  create_ts        timestamp NOT NULL,
  mod_ts           timestamp NOT NULL,
  UNIQUE (warehouse_id, location_code)
);
COMMENT ON TABLE location IS 'Bin / slot master. Re-slotting updates zone_id and location_type in place.';

CREATE TABLE owner (
  owner_id             integer PRIMARY KEY,
  owner_code           varchar(10) NOT NULL UNIQUE,
  owner_name           varchar(100) NOT NULL,
  owner_status         varchar(12) NOT NULL,   -- ONBOARDING, ACTIVE, OFFBOARDING, INACTIVE
  vertical             varchar(20),
  erp_account_nbr      varchar(20),            -- AX custtable.accountnum, keyed manually; can be blank
  billing_currency     char(3),
  primary_warehouse_id smallint REFERENCES warehouse,
  contact_name         varchar(100),
  contact_email        varchar(120),
  go_live_date         date,
  create_ts            timestamp NOT NULL,
  mod_ts               timestamp NOT NULL,
  create_user          varchar(30),
  mod_user             varchar(30)
);
COMMENT ON TABLE owner IS 'Clients (depositors) whose inventory Harborline stores. One row per client.';

CREATE TABLE owner_warehouse (
  owner_id         integer  NOT NULL REFERENCES owner,
  warehouse_id     smallint NOT NULL REFERENCES warehouse,
  active_from      date     NOT NULL,
  active_to        date,
  PRIMARY KEY (owner_id, warehouse_id)
);

CREATE TABLE material (
  material_id          integer PRIMARY KEY,
  owner_id             integer      NOT NULL REFERENCES owner,
  sku                  varchar(40)  NOT NULL,          -- the client's own item number
  description          varchar(200) NOT NULL,
  upc                  varchar(14),
  base_uom             varchar(4)   NOT NULL DEFAULT 'EA',
  unit_weight          numeric(10,3),
  weight_uom           varchar(2),
  unit_length          numeric(8,2),
  unit_width           numeric(8,2),
  unit_height          numeric(8,2),
  dim_uom              varchar(2),
  case_qty             integer,
  cases_per_pallet     integer,
  is_lot_controlled    boolean NOT NULL DEFAULT false,
  is_serial_controlled boolean NOT NULL DEFAULT false,
  shelf_life_days      integer,
  temp_class           varchar(8) NOT NULL DEFAULT 'AMBIENT',
  hazmat_class         varchar(6),
  unit_value           numeric(12,2),
  country_of_origin    char(2),
  hs_code              varchar(12),
  material_status      varchar(12) NOT NULL DEFAULT 'ACTIVE',  -- ACTIVE, INACTIVE, DISCONTINUED
  create_ts            timestamp NOT NULL,
  mod_ts               timestamp NOT NULL,
  create_user          varchar(30),
  mod_user             varchar(30),
  UNIQUE (owner_id, sku)
);
COMMENT ON TABLE material IS 'Item master per client. Dimension/weight corrections overwrite in place.';

CREATE TABLE material_uom (
  material_id      integer    NOT NULL REFERENCES material,
  uom_code         varchar(4) NOT NULL,     -- EA, CS, PL
  qty_per_uom      integer    NOT NULL,
  weight           numeric(10,3),
  length           numeric(8,2),
  width            numeric(8,2),
  height           numeric(8,2),
  mod_ts           timestamp  NOT NULL,
  PRIMARY KEY (material_id, uom_code)
);

CREATE TABLE carrier (
  carrier_id       smallint PRIMARY KEY,
  scac             varchar(4)  NOT NULL UNIQUE,
  carrier_name     varchar(60) NOT NULL,
  mode             varchar(6)  NOT NULL,    -- PARCEL, LTL
  is_active        boolean NOT NULL DEFAULT true,
  mod_ts           timestamp NOT NULL
);

CREATE TABLE carrier_service (
  service_id       smallint PRIMARY KEY,
  carrier_id       smallint    NOT NULL REFERENCES carrier,
  service_code     varchar(6)  NOT NULL,
  service_name     varchar(60) NOT NULL,
  transit_days_min smallint,
  transit_days_max smallint,
  is_active        boolean NOT NULL DEFAULT true,
  mod_ts           timestamp NOT NULL
);

CREATE TABLE asn_header (
  asn_id           bigint PRIMARY KEY,
  asn_nbr          varchar(30) NOT NULL,
  owner_id         integer  NOT NULL,
  warehouse_id     smallint NOT NULL,
  supplier_name    varchar(100),
  source           varchar(10) NOT NULL,     -- EDI856, PORTAL, EMAIL
  expected_date    date,
  asn_status       varchar(14) NOT NULL,     -- OPEN, IN_RECEIVING, RECEIVED, CANCELLED
  create_ts        timestamp NOT NULL,
  mod_ts           timestamp NOT NULL
);

CREATE TABLE asn_line (
  asn_id           bigint  NOT NULL REFERENCES asn_header ON DELETE CASCADE,
  line_nbr         integer NOT NULL,
  material_id      integer NOT NULL,
  expected_qty     integer NOT NULL,
  uom              varchar(4) NOT NULL,
  lot_nbr          varchar(30),
  expiry_date      date,
  PRIMARY KEY (asn_id, line_nbr)
);

CREATE TABLE receipt_header (
  receipt_id       bigint PRIMARY KEY,
  receipt_nbr      varchar(30) NOT NULL,
  asn_id           bigint,
  owner_id         integer  NOT NULL,
  warehouse_id     smallint NOT NULL,
  dock_door        varchar(6),
  trailer_nbr      varchar(20),
  arrived_ts       timestamp,
  started_ts       timestamp,
  completed_ts     timestamp,
  receipt_status   varchar(12) NOT NULL,     -- ARRIVED, RECEIVING, COMPLETE
  create_ts        timestamp NOT NULL,
  mod_ts           timestamp NOT NULL
);
COMMENT ON COLUMN receipt_header.completed_ts IS 'All lines received. Putaway completion is on the PUTAWAY task / inventory_txn.';

CREATE TABLE receipt_line (
  receipt_id       bigint  NOT NULL REFERENCES receipt_header ON DELETE CASCADE,
  line_nbr         integer NOT NULL,
  material_id      integer NOT NULL,
  lpn_id           bigint,
  received_qty     integer NOT NULL,
  damaged_qty      integer NOT NULL DEFAULT 0,
  uom              varchar(4) NOT NULL,
  lot_nbr          varchar(30),
  expiry_date      date,
  received_ts      timestamp NOT NULL,
  received_by      integer,
  PRIMARY KEY (receipt_id, line_nbr)
);

CREATE TABLE license_plate (
  lpn_id           bigint PRIMARY KEY,
  lpn_nbr          varchar(20) NOT NULL UNIQUE,
  warehouse_id     smallint NOT NULL,
  location_id      integer,
  lpn_type         varchar(6) NOT NULL,      -- PALLET, CASE, TOTE
  lpn_status       varchar(10) NOT NULL,     -- RECEIVED, STORED, PICKED, SHIPPED, CONSUMED
  create_ts        timestamp NOT NULL,
  mod_ts           timestamp NOT NULL
);

CREATE TABLE inventory (
  inventory_id     bigint PRIMARY KEY,
  warehouse_id     smallint NOT NULL,
  location_id      integer  NOT NULL,
  lpn_id           bigint,
  owner_id         integer  NOT NULL,
  material_id      integer  NOT NULL,
  lot_nbr          varchar(30),
  expiry_date      date,
  qty_on_hand      integer  NOT NULL,
  qty_allocated    integer  NOT NULL DEFAULT 0,
  inv_status       varchar(10) NOT NULL,     -- AVAILABLE, HOLD, DAMAGED, QC
  hold_reason      varchar(30),
  received_date    date,
  create_ts        timestamp NOT NULL,
  mod_ts           timestamp NOT NULL
);
COMMENT ON TABLE inventory IS 'Current on-hand by location / LPN / lot. Rows are physically DELETED when qty_on_hand reaches 0.';

CREATE TABLE inventory_txn (
  txn_id           bigint PRIMARY KEY,
  txn_ts           timestamp NOT NULL,
  warehouse_id     smallint  NOT NULL,
  owner_id         integer   NOT NULL,
  material_id      integer   NOT NULL,
  txn_type         varchar(10) NOT NULL,     -- RECEIVE, PUTAWAY, MOVE, REPLEN, PICK, SHIP, ADJUST, COUNT, RETURN, HOLD, RELEASE, DESTROY
  qty              integer   NOT NULL,       -- signed change to on-hand (0 for moves/holds)
  from_location_id integer,
  to_location_id   integer,
  lpn_id           bigint,
  lot_nbr          varchar(30),
  reason_code      varchar(20),
  ref_type         varchar(10),              -- ORDER, RECEIPT, COUNT, RETURN, TASK, QA
  ref_id           bigint,
  user_id          integer,
  create_ts        timestamp NOT NULL
);
COMMENT ON TABLE inventory_txn IS 'Immutable inventory ledger. Corrections are new reversing rows, never updates.';

CREATE TABLE order_header (
  order_id             bigint PRIMARY KEY,
  order_nbr            varchar(20) NOT NULL UNIQUE,
  owner_id             integer  NOT NULL,
  warehouse_id         smallint NOT NULL,
  client_order_ref     varchar(40),          -- client PO / web order number (EDI 940 W05-03)
  order_type           varchar(10) NOT NULL, -- B2C, B2B, TRANSFER, RTV
  channel              varchar(8)  NOT NULL, -- EDI, API, PORTAL, CSV
  order_status         varchar(12) NOT NULL, -- NEW, ON_HOLD, ALLOCATED, BACKORDERED, RELEASED, PICKING, PACKED, SHIPPED, CANCELLED
  priority             smallint NOT NULL DEFAULT 5,
  ordered_ts           timestamp,            -- when the end customer / client placed it (client clock)
  received_ts          timestamp NOT NULL,   -- when the WMS received it
  requested_ship_date  date,
  carrier_service_id   smallint,
  ship_to_name         varchar(100),
  ship_to_company      varchar(100),
  ship_to_address1     varchar(120),
  ship_to_address2     varchar(120),
  ship_to_city         varchar(60),
  ship_to_state        varchar(10),
  ship_to_postal       varchar(12),
  ship_to_country      char(2),
  ship_to_phone        varchar(30),
  ship_to_email        varchar(120),
  is_residential       boolean,
  total_lines          integer,
  total_units          integer,
  cancel_reason        varchar(40),
  create_ts            timestamp NOT NULL,
  mod_ts               timestamp NOT NULL,
  create_user          varchar(30),
  mod_user             varchar(30)
);
COMMENT ON TABLE order_header IS 'Outbound orders. Contains consumer PII (ship_to_*).';

CREATE TABLE order_line (
  order_id         bigint  NOT NULL REFERENCES order_header ON DELETE CASCADE,
  line_nbr         integer NOT NULL,
  material_id      integer NOT NULL,
  ordered_qty      integer NOT NULL,
  allocated_qty    integer NOT NULL DEFAULT 0,
  picked_qty       integer NOT NULL DEFAULT 0,
  shipped_qty      integer NOT NULL DEFAULT 0,
  uom              varchar(4) NOT NULL DEFAULT 'EA',
  unit_price       numeric(12,2),
  line_status      varchar(12) NOT NULL,     -- OPEN, ALLOCATED, BACKORDERED, PICKED, SHIPPED
  PRIMARY KEY (order_id, line_nbr)
);
COMMENT ON TABLE order_line IS 'Order lines. No audit timestamp. Cancelled lines are DELETED.';

CREATE TABLE task (
  task_id          bigint PRIMARY KEY,
  warehouse_id     smallint NOT NULL,
  task_type        varchar(8)  NOT NULL,     -- PICK, PUTAWAY, REPLEN, COUNT, LOAD, MOVE
  task_status      varchar(12) NOT NULL,     -- CREATED, ASSIGNED, IN_PROGRESS, COMPLETE, CANCELLED
  priority         smallint NOT NULL DEFAULT 5,
  order_id         bigint,
  order_line_nbr   integer,
  receipt_id       bigint,
  material_id      integer,
  qty              integer,
  from_location_id integer,
  to_location_id   integer,
  assigned_user_id integer,
  created_ts       timestamp NOT NULL,
  assigned_ts      timestamp,
  started_ts       timestamp,
  completed_ts     timestamp,
  mod_ts           timestamp NOT NULL
);
COMMENT ON TABLE task IS 'Labor tasks. A nightly housekeeping job DELETES tasks older than 180 days.';

CREATE TABLE shipment (
  shipment_id        bigint PRIMARY KEY,
  shipment_nbr       varchar(20) NOT NULL UNIQUE,
  order_id           bigint   NOT NULL,
  warehouse_id       smallint NOT NULL,
  carrier_service_id smallint NOT NULL,
  scac               varchar(4) NOT NULL,
  tracking_nbr       varchar(40),            -- master tracking (parcel) or PRO number (LTL)
  bol_nbr            varchar(20),
  ship_ts            timestamp,
  shipment_status    varchar(10) NOT NULL,   -- CREATED, MANIFESTED, SHIPPED, DELIVERED, EXCEPTION
  total_weight       numeric(10,2),
  weight_uom         varchar(2),
  carton_count       integer,
  freight_charge_est numeric(10,2),
  currency           char(3),
  create_ts          timestamp NOT NULL,
  mod_ts             timestamp NOT NULL
);
COMMENT ON COLUMN shipment.tracking_nbr IS 'Changes when a label is re-printed (voided label -> new tracking number).';

CREATE TABLE shipment_carton (
  carton_id        bigint PRIMARY KEY,
  shipment_id      bigint NOT NULL REFERENCES shipment ON DELETE CASCADE,
  carton_nbr       varchar(20) NOT NULL,
  tracking_nbr     varchar(40),
  weight           numeric(10,2),
  length           numeric(8,2),
  width            numeric(8,2),
  height           numeric(8,2),
  dim_uom          varchar(2),
  create_ts        timestamp NOT NULL,
  mod_ts           timestamp NOT NULL
);
COMMENT ON TABLE shipment_carton IS 'Packages. Voided labels are DELETED.';

CREATE TABLE cycle_count (
  count_id         bigint PRIMARY KEY,
  warehouse_id     smallint NOT NULL,
  location_id      integer  NOT NULL,
  material_id      integer  NOT NULL,
  lot_nbr          varchar(30),
  system_qty       integer NOT NULL,
  counted_qty      integer NOT NULL,
  variance_qty     integer NOT NULL,
  count_ts         timestamp NOT NULL,
  counted_by       integer,
  count_status     varchar(10) NOT NULL,     -- COUNTED, RECOUNT, APPROVED
  approved_by      integer,
  create_ts        timestamp NOT NULL,
  mod_ts           timestamp NOT NULL
);

CREATE TABLE return_header (
  return_id          bigint PRIMARY KEY,
  rma_nbr            varchar(20) NOT NULL,
  owner_id           integer  NOT NULL,
  warehouse_id       smallint NOT NULL,
  original_order_id  bigint,
  carrier_tracking_nbr varchar(40),
  received_ts        timestamp,
  return_status      varchar(10) NOT NULL,   -- EXPECTED, RECEIVED, PROCESSED
  create_ts          timestamp NOT NULL,
  mod_ts             timestamp NOT NULL
);

CREATE TABLE return_line (
  return_id        bigint  NOT NULL REFERENCES return_header ON DELETE CASCADE,
  line_nbr         integer NOT NULL,
  material_id      integer NOT NULL,
  qty              integer NOT NULL,
  reason_code      varchar(20),              -- WRONG_SIZE, DAMAGED, NOT_AS_DESCRIBED, NO_LONGER_NEEDED, DEFECTIVE
  disposition      varchar(10),              -- RESTOCK, DAMAGED, DESTROY, RTV
  PRIMARY KEY (return_id, line_nbr)
);

CREATE TABLE app_user (
  user_id          integer PRIMARY KEY,
  username         varchar(30) NOT NULL UNIQUE,
  first_name       varchar(40),
  last_name        varchar(40),
  personnel_nbr    varchar(20),              -- AX hcmworker.personnelnumber
  warehouse_id     smallint,
  user_role        varchar(12) NOT NULL,     -- PICKER, PACKER, RECEIVER, FORKLIFT, INV_CONTROL, LEAD, SUPERVISOR, ADMIN
  shift            char(1),
  hire_date        date,
  term_date        date,
  is_active        boolean NOT NULL DEFAULT true,
  create_ts        timestamp NOT NULL,
  mod_ts           timestamp NOT NULL
);

CREATE TABLE billing_rate (
  rate_id          integer PRIMARY KEY,
  owner_id         integer     NOT NULL,
  warehouse_id     smallint,                 -- null = all warehouses
  charge_code      varchar(10) NOT NULL,
  rate             numeric(12,4) NOT NULL,
  rate_uom         varchar(14) NOT NULL,
  currency         char(3)     NOT NULL,
  effective_date   date        NOT NULL,
  mod_ts           timestamp   NOT NULL,
  mod_user         varchar(30)
);
COMMENT ON TABLE billing_rate IS 'Rates keyed by billing analysts from the signed contract. Updated IN PLACE - no history kept.';

CREATE TABLE billing_record (
  billing_id       bigint PRIMARY KEY,
  owner_id         integer     NOT NULL,
  warehouse_id     smallint    NOT NULL,
  charge_code      varchar(10) NOT NULL,
  activity_date    date        NOT NULL,
  qty              numeric(12,2) NOT NULL,
  rate             numeric(12,4) NOT NULL,
  amount           numeric(14,2) NOT NULL,
  currency         char(3)     NOT NULL,
  ref_type         varchar(10),
  ref_id           bigint,
  billing_status   varchar(10) NOT NULL,     -- ACCRUED, INVOICED, VOID
  invoice_ref      varchar(20),              -- AX custinvoicejour.invoiceid once invoiced
  create_ts        timestamp NOT NULL,
  mod_ts           timestamp NOT NULL
);
COMMENT ON TABLE billing_record IS 'Billable activity accrued daily. Exported to AX at month end. Corrections before invoicing are DELETED or re-rated.';

CREATE TABLE sys_release (
  release_id       integer PRIMARY KEY,
  version          varchar(12) NOT NULL,
  released_ts      timestamp   NOT NULL,
  notes            text
);
COMMENT ON TABLE sys_release IS 'WMS application releases. Schema changes ship with a release.';
