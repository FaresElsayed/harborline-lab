export const SYSTEMS: Array<{ id: string; label: string; where: string }> = [
  { id: 'wms', label: 'WMS', where: 'Postgres · db wms' },
  { id: 'ax2012', label: 'AX 2012', where: 'Postgres · db ax2012' },
  { id: 'iot', label: 'IoT', where: 'Postgres · db iot' },
  { id: 'salesforce', label: 'Salesforce', where: 'Mock REST API' },
  { id: 'carrier', label: 'Carrier', where: 'Mock tracking API' },
  { id: 'edi', label: 'EDI', where: 'landing/edi' },
  { id: 'files', label: 'Files', where: 'landing/files' },
];

export const systemLabel = (id: string) => SYSTEMS.find((s) => s.id === id)?.label ?? id;

export const KIND_LABEL: Record<string, string> = {
  build: 'Build',
  batch: 'Batch',
  stream_tick: 'Stream tick',
  scenario: 'Scenario',
};

export const DQ_INFO: Record<string, { label: string; detail: string; group: 'Records' | 'Keys and text' | 'Time' | 'Files and EDI' | 'Sensors' | 'Salesforce' }> = {
  orphan_fk: { label: 'Orphan keys', detail: 'A child row points at a parent that does not exist', group: 'Records' },
  duplicate_row: { label: 'Duplicate rows', detail: 'The same record lands twice under a new key', group: 'Records' },
  null_required: { label: 'Missing required values', detail: 'NULL in a column the business treats as mandatory', group: 'Records' },
  negative_qty: { label: 'Negative quantities', detail: 'On order lines, receipts and inventory', group: 'Records' },
  shipped_gt_ordered: { label: 'Shipped more than ordered', detail: 'Shipped quantity above the ordered quantity', group: 'Records' },
  header_line_mismatch: { label: 'Header and lines disagree', detail: 'Header totals that do not match their lines', group: 'Records' },
  invalid_currency: { label: 'Invalid currency codes', detail: '“US$”, “usd”, “MXP” where an ISO code belongs', group: 'Records' },
  unit_mixup: { label: 'Pound and kilogram mix-ups', detail: 'Weights recorded in the other warehouse’s unit', group: 'Records' },
  key_whitespace_case: { label: 'Key whitespace and case', detail: '“ NBOUT”, “nbout ” in join keys', group: 'Keys and text' },
  mojibake: { label: 'Mojibake', detail: 'cp1252 text read as UTF-8, such as “LumiÃ¨re”', group: 'Keys and text' },
  sentinel_date: { label: 'Sentinel dates', detail: '1900-01-01 and 9999-12-31 where a real date belongs', group: 'Time' },
  future_date: { label: 'Future dates', detail: 'Timestamps after the sim clock', group: 'Time' },
  late_arriving: { label: 'Late-arriving facts', detail: 'Rows land days after their event time', group: 'Time' },
  dst_duplicate: { label: 'Daylight-saving duplicates', detail: 'The repeated local hour at the autumn clock change', group: 'Time' },
  malformed_edi: { label: 'Malformed EDI', detail: 'Wrong SE segment counts, missing GE trailers', group: 'Files and EDI' },
  schema_drift: { label: 'Schema drift', detail: 'New or renamed columns in delivered files', group: 'Files and EDI' },
  sensor_spike: { label: 'Sensor spikes', detail: 'A single reading jumps by 20 °C and back', group: 'Sensors' },
  sensor_stuck: { label: 'Stuck sensors', detail: 'The same value repeated for hours', group: 'Sensors' },
  duplicate_account: { label: 'Duplicate accounts', detail: 'The same client entered twice in Salesforce', group: 'Salesforce' },
};

export const DQ_GROUPS = ['Records', 'Keys and text', 'Time', 'Files and EDI', 'Sensors', 'Salesforce'] as const;
