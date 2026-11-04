import { useEffect, useMemo, useRef, useState } from 'react';
import { Slider } from 'radix-ui';
import { Check } from 'lucide-react';
import { useSetDq, useStatus } from '@/api/queries';
import type { DqSettings } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Switch } from '@/components/ui/field';
import { ErrorNote, PageHeader, Panel, Skeleton } from '@/components/ui/misc';
import { fmtInt } from '@/lib/format';
import { DQ_GROUPS, DQ_INFO } from './systems';

/* The rate runs on a log scale in a 1-2-5 sequence: 1 in 100 … 1 in 100,000. */
const STOPS = [100, 200, 500, 1_000, 2_000, 5_000, 10_000, 20_000, 50_000, 100_000];
const nearest = (d: number) => STOPS.reduce((best, s, i) => (Math.abs(Math.log10(s) - Math.log10(d)) < Math.abs(Math.log10(STOPS[best]) - Math.log10(d)) ? i : best), 0);

type SaveState = 'idle' | 'saving' | 'saved' | 'error';

export default function QualityPage() {
  const status = useStatus();
  const setDq = useSetDq();
  const [draft, setDraft] = useState<DqSettings | null>(null);
  const [save, setSave] = useState<SaveState>('idle');
  const [err, setErr] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const dq = draft ?? status.data?.dq ?? null;

  useEffect(() => () => clearTimeout(timer.current), []);

  /** One save model: every control saves itself (debounced) and shows a quiet stamp. */
  const commit = (next: DqSettings, delay = 350) => {
    setDraft(next);
    setSave('saving');
    clearTimeout(timer.current);
    timer.current = setTimeout(async () => {
      try {
        await setDq.mutateAsync(next);
        setSave('saved');
        setErr(null);
        setDraft(null);
      } catch (e) {
        setSave('error');
        setErr(e instanceof Error ? e.message : String(e));
      }
    }, delay);
  };

  const names = useMemo(() => Object.keys(dq?.categories ?? DQ_INFO), [dq]);
  const on = names.filter((n) => dq?.categories[n]).length;

  return (
    <>
      <PageHeader
        title="Data quality"
        subtitle={
          dq
            ? `One bad record in ${fmtInt(dq.denominator)} per category, with ${on} of ${names.length} categories on. Every injected defect is logged to answer_key/anomalies.csv so you can score your checks.`
            : 'Loading the current settings…'
        }
        actions={
          <span className="num flex h-8 items-center gap-1.5 text-sm text-muted" aria-live="polite">
            {save === 'saving' ? 'Saving…' : save === 'saved' ? (
              <>
                <Check className="size-3.5 text-good" aria-hidden /> Saved
              </>
            ) : save === 'error' ? (
              <span className="text-bad">Not saved</span>
            ) : null}
          </span>
        }
      />
      {status.isError && !status.data ? <ErrorNote error={status.error} onRetry={() => void status.refetch()} what="the data-quality settings" /> : null}
      {err ? (
        <div className="mb-4">
          <ErrorNote error={new Error(err)} onRetry={() => dq && commit(dq, 0)} what="your change" />
        </div>
      ) : null}

      <div className="flex flex-col gap-4">
        <Panel title="Rate" meta="per category, per eligible record">
          {dq ? (
            <RateControl denominator={dq.denominator} onChange={(d) => commit({ ...dq, denominator: d })} />
          ) : (
            <Skeleton className="h-28 w-full" />
          )}
        </Panel>

        <Panel
          title="Categories"
          meta={dq ? `${on} of ${names.length} on` : undefined}
          actions={
            dq ? (
              <>
                <Button size="sm" variant="ghost" disabled={on === names.length} onClick={() => commit({ ...dq, categories: Object.fromEntries(names.map((n) => [n, true])) }, 0)}>
                  All on
                </Button>
                <Button size="sm" variant="ghost" disabled={on === 0} onClick={() => commit({ ...dq, categories: Object.fromEntries(names.map((n) => [n, false])) }, 0)}>
                  All off
                </Button>
              </>
            ) : null
          }
        >
          {dq ? (
            <div className="columns-3 gap-10 max-[1280px]:columns-2 max-[760px]:columns-1">
              {DQ_GROUPS.map((g) => {
                const items = names.filter((n) => (DQ_INFO[n]?.group ?? 'Records') === g);
                if (!items.length) return null;
                return (
                  <fieldset key={g} className="mb-6 break-inside-avoid">
                    <legend className="mb-2 text-sm font-semibold text-ink">{g}</legend>
                    <div className="flex flex-col gap-3">
                      {items.map((n) => (
                        <Switch
                          key={n}
                          checked={!!dq.categories[n]}
                          onCheckedChange={(v) => commit({ ...dq, categories: { ...dq.categories, [n]: v } }, 0)}
                          label={DQ_INFO[n]?.label ?? n}
                          description={DQ_INFO[n]?.detail}
                        />
                      ))}
                    </div>
                  </fieldset>
                );
              })}
            </div>
          ) : (
            <div className="grid grid-cols-2 gap-4">
              {Array.from({ length: 8 }, (_, i) => (
                <Skeleton key={i} className="h-10" />
              ))}
            </div>
          )}
        </Panel>
      </div>
    </>
  );
}

function RateControl({ denominator, onChange }: { denominator: number; onChange: (d: number) => void }) {
  const [idx, setIdx] = useState(() => nearest(denominator));
  useEffect(() => setIdx(nearest(denominator)), [denominator]);
  const d = STOPS[idx];
  return (
    <div className="grid grid-cols-[minmax(0,5fr)_minmax(0,7fr)] items-center gap-10 max-[900px]:grid-cols-1 max-[900px]:gap-4">
      <div>
      <div className="flex items-baseline gap-2">
        <span className="figure num text-[1.875rem] font-semibold leading-9 text-ink">1 in {fmtInt(d)}</span>
        <span className="text-base text-muted">records</span>
      </div>
      <p className="mt-1 text-base text-muted">
        {d <= 500 ? 'Noisy: easy to see, unrealistic for production.' : d <= 5_000 ? 'Busy: plenty to catch in a day of data.' : d <= 20_000 ? 'Realistic: the default. Defects are rare and worth finding.' : 'Sparse: a needle-in-a-haystack test for your checks.'}
      </p>
      </div>
      <div>
      <Slider.Root
        className="relative flex h-5 w-full touch-none select-none items-center"
        min={0}
        max={STOPS.length - 1}
        step={1}
        value={[idx]}
        onValueChange={([v]) => setIdx(v)}
        onValueCommit={([v]) => onChange(STOPS[v])}
        aria-label="Bad record rate"
      >
        <Slider.Track className="relative h-1.5 grow overflow-hidden rounded-full bg-active">
          <Slider.Range className="absolute h-full rounded-full bg-accent" />
        </Slider.Track>
        <Slider.Thumb
          aria-valuetext={`1 in ${fmtInt(d)}`}
          className="press block size-5 rounded-full border-2 border-accent bg-sheet shadow-[0_1px_3px_rgb(15_35_56/0.25)] transition-transform duration-150 hover:scale-110 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
        />
      </Slider.Root>
      <div className="num relative mt-2 h-4 text-xs text-muted" aria-hidden>
        {[0, 3, 6, 9].map((i) => (
          <span key={i} className="absolute -translate-x-1/2 first:translate-x-0 last:-translate-x-full" style={{ left: `${(i / (STOPS.length - 1)) * 100}%` }}>
            1/{fmtInt(STOPS[i])}
          </span>
        ))}
      </div>
      </div>
    </div>
  );
}
