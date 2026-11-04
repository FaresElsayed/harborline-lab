import { useId, useMemo, useState } from 'react';
import { Play } from 'lucide-react';
import { useRuns, useScenarios, useStatus } from '@/api/queries';
import type { RunSummary, Scenario, ScenarioParam } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Input, Label, Select, Switch } from '@/components/ui/field';
import { ErrorNote, PageHeader, Skeleton } from '@/components/ui/misc';
import { fmtRelative } from '@/lib/dates';
import { fmtInt } from '@/lib/format';
import { summarise, useSimActions } from './actions';

type Value = string | number | boolean;

const isBool = (p: ScenarioParam) => p.type === 'bool' || p.type === 'boolean';
const isNum = (p: ScenarioParam) => ['int', 'integer', 'float', 'number'].includes(p.type);
const isInt = (p: ScenarioParam) => p.type === 'int' || p.type === 'integer';

function humanise(name: string) {
  const s = name.replace(/_/g, ' ');
  return s.charAt(0).toUpperCase() + s.slice(1);
}

const UNIT: Record<string, string> = { pct: '%', days: 'days', hours: 'hours', hours_late: 'hours', wind_down_days: 'days', go_live_days: 'days', multiplier: '×' };
const OPTION_LABEL: Record<string, string> = {
  edi_940: 'EDI 940 (orders)',
  edi_856: 'EDI 856 (ASNs)',
  item_master: 'Item-master CSV',
  rate_card: 'Rate-card workbook',
  iot_readings: 'IoT sensor readings',
  carrier_events: 'Carrier events',
  food_bev: 'Food and beverage',
  health_beauty: 'Health and beauty',
  home_goods: 'Home goods',
  auto_parts: 'Auto parts',
  b2b_general: 'General B2B',
  all: 'All systems',
  wms: 'WMS',
  salesforce: 'Salesforce',
  files: 'Files',
  ALL: 'All warehouses',
};
const optionLabel = (o: string | number) => OPTION_LABEL[String(o)] ?? (/^[a-z_]+$/.test(String(o)) ? humanise(String(o)) : String(o));

const LABEL: Record<string, string> = {
  pct: 'Increase',
  scac: 'Carrier (SCAC)',
  keyed_in_wms: 'Keyed in the WMS',
  go_live_days: 'Go live in',
  wind_down_days: 'Wind down over',
  hours_late: 'Arrives late by',
  count: 'How many',
};

function initialValues(s: Scenario): Record<string, Value> {
  return Object.fromEntries(s.params.map((p) => [p.name, p.default ?? (isBool(p) ? false : isNum(p) ? 0 : p.options?.[0] ?? '')]));
}

function ParamField({ p, value, onChange, scenarioId }: { p: ScenarioParam; value: Value; onChange: (v: Value) => void; scenarioId: string }) {
  const id = useId();
  const label = p.label ?? LABEL[p.name] ?? humanise(p.name);
  if (isBool(p))
    return (
      <div className="col-span-2">
        <Switch id={id} checked={!!value} onCheckedChange={onChange} label={label} description={p.help} />
      </div>
    );
  if (p.options?.length)
    return (
      <div>
        <Label htmlFor={id}>{label}</Label>
        <Select
          id={id}
          value={String(value)}
          onValueChange={(v) => onChange(typeof p.options![0] === 'number' ? Number(v) : v)}
          options={p.options.map((o) => ({ value: String(o), label: optionLabel(o) }))}
        />
      </div>
    );
  const unit = UNIT[p.name];
  const invalid = isNum(p) && (value === '' || Number.isNaN(Number(value)) || (p.min != null && Number(value) < p.min) || (p.max != null && Number(value) > p.max));
  return (
    <div>
      <Label htmlFor={id} hint={p.min != null && p.max != null ? `${p.min}–${p.max}` : undefined}>
        {label}
      </Label>
      <div className="relative">
        <Input
          id={id}
          name={`${scenarioId}.${p.name}`}
          inputMode={isNum(p) ? (isInt(p) ? 'numeric' : 'decimal') : undefined}
          type={p.type === 'date' ? 'date' : 'text'}
          value={String(value)}
          aria-invalid={invalid}
          onChange={(e) => onChange(isNum(p) ? e.target.value.replace(isInt(p) ? /[^\d]/g : /[^\d.]/g, '') : e.target.value)}
          className={unit ? 'num pr-14' : 'num'}
        />
        {unit ? <span className="pointer-events-none absolute inset-y-0 right-2.5 flex items-center text-sm text-muted">{unit}</span> : null}
      </div>
    </div>
  );
}

function ScenarioCard({ s, last, disabled, reason }: { s: Scenario; last?: RunSummary; disabled: boolean; reason: string | null }) {
  const { runScenario, pending } = useSimActions();
  const [values, setValues] = useState<Record<string, Value>>(() => initialValues(s));
  const titleId = useId();
  const valid = s.params.every((p) => {
    if (!isNum(p)) return true;
    const v = values[p.name];
    return v !== '' && !Number.isNaN(Number(v)) && (p.min == null || Number(v) >= p.min) && (p.max == null || Number(v) <= p.max);
  });
  const busy = pending.scenario === s.id;
  const t = last ? summarise(last.changes) : null;

  return (
    <article aria-labelledby={titleId} className="flex flex-col rounded-l border border-line bg-sheet p-4">
      <h2 id={titleId} className="text-md font-semibold text-ink">
        {s.name}
      </h2>
      <p className="mt-1 text-base text-muted">{s.description}</p>
      <form
        className="mt-4 flex flex-1 flex-col"
        onSubmit={(e) => {
          e.preventDefault();
          if (!valid || disabled) return;
          const params = Object.fromEntries(s.params.map((p) => [p.name, isNum(p) ? Number(values[p.name]) : values[p.name]]));
          void runScenario(s.id, s.name, params);
        }}
      >
        <div className="grid grid-cols-2 content-start gap-3">
          {s.params.map((p) => (
            <ParamField key={p.name} p={p} scenarioId={s.id} value={values[p.name]} onChange={(v) => setValues((prev) => ({ ...prev, [p.name]: v }))} />
          ))}
        </div>
        <div className="min-h-4 flex-1" aria-hidden />
        <div className="flex items-center justify-between gap-3 border-t border-rule pt-3">
          <span className="num min-w-0 truncate text-sm text-muted">
            {reason ?? (last && t ? <>Last run {fmtRelative(last.finished_at ?? last.started_at)} · +{fmtInt(t.ins)} · {fmtInt(t.upd)} upd · {fmtInt(t.del)} del</> : 'Not run yet')}
          </span>
          <Button type="submit" size="md" icon={<Play aria-hidden />} disabled={disabled || !valid} loading={busy}>
            Run
          </Button>
        </div>
      </form>
    </article>
  );
}

export default function ScenariosPage() {
  const scenarios = useScenarios();
  const status = useStatus();
  const runs = useRuns();
  const st = status.data?.state;
  const reason =
    st === 'streaming' ? 'Stop the stream to run a scenario' : st === 'building' ? 'Available after the build' : st === 'running_batch' ? 'Another run is in progress' : status.data && !status.data.sim_clock ? 'Build the dataset first' : null;
  const lastBy = useMemo(() => {
    const m = new Map<string, RunSummary>();
    for (const r of runs.data ?? []) {
      if (r.kind !== 'scenario') continue;
      const s = scenarios.data?.find((x) => r.label?.includes(x.name));
      if (s && !m.has(s.id)) m.set(s.id, r);
    }
    return m;
  }, [runs.data, scenarios.data]);

  return (
    <>
      <PageHeader
        title="Scenarios"
        subtitle="Business events on demand. Each one changes the source systems the way the real event would, then runs one sim hour so the effects flow through."
      />
      {scenarios.isError ? (
        <ErrorNote error={scenarios.error} onRetry={() => void scenarios.refetch()} what="the scenario list" />
      ) : (
        <div className="grid grid-cols-3 gap-4 max-[1240px]:grid-cols-2 max-[720px]:grid-cols-1">
          {scenarios.isLoading
            ? Array.from({ length: 6 }, (_, i) => <Skeleton key={i} className="h-64 rounded-l" />)
            : scenarios.data?.map((s) => <ScenarioCard key={s.id} s={s} last={lastBy.get(s.id)} disabled={!!reason || st !== 'idle'} reason={reason} />)}
        </div>
      )}
    </>
  );
}
