import { useEffect, useId, useRef, useState } from 'react';
import clsx from 'clsx';
import { Dices } from 'lucide-react';
import { toast } from 'sonner';
import { useBuild, useStatus } from '@/api/queries';
import type { Preset } from '@/api/types';
import { Button, IconButton } from '@/components/ui/button';
import { FieldNote, Input, Label } from '@/components/ui/field';
import { Dialog } from '@/components/ui/overlay';

/* Disk is measured. Times are rough, for a typical laptop: a fast desktop is several times quicker. */
export const PRESET_INFO: Record<Preset, { label: string; months: number; orders: string; disk: string; time: string; note: string }> = {
  small: { label: 'Small', months: 6, orders: '3 warehouses', disk: '≈ 3 GB', time: '≈ 10 min', note: 'A quick try only: several project stories are missing' },
  medium: { label: 'Medium', months: 24, orders: 'All 8 warehouses', disk: '≈ 12 GB', time: '≈ 1 hour', note: 'Everything the eight projects need' },
  large: { label: 'Large', months: 36, orders: 'All 8 warehouses', disk: '≈ 30 GB', time: '≈ 3 hours', note: 'The same stories with more volume' },
};

const CONFIRM = 'REBUILD';

export function BuildDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (v: boolean) => void }) {
  const status = useStatus();
  const build = useBuild();
  const [preset, setPreset] = useState<Preset>('medium');
  const [seed, setSeed] = useState('20261031');
  const [typed, setTyped] = useState('');
  const seedId = useId();
  const confirmId = useId();
  const confirmRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    setPreset(status.data?.preset ?? 'medium');
    setSeed(String(status.data?.seed ?? 20261031));
    setTyped('');
  }, [open, status.data?.preset, status.data?.seed]);

  const seedOk = /^\d{1,10}$/.test(seed);
  const ready = typed === CONFIRM && seedOk;
  const streaming = status.data?.state === 'streaming';

  const submit = async () => {
    if (!ready) return;
    try {
      await build.mutateAsync({ preset, seed: Number(seed) });
      onOpenChange(false);
      toast.message(`Building the ${PRESET_INFO[preset].label} dataset`, { description: 'Progress shows on the Control Center. The tracker is not touched.' });
    } catch (e) {
      toast.error('Build did not start', { description: e instanceof Error ? e.message : String(e) });
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      width={600}
      initialFocus={confirmRef}
      title="Build the dataset"
      description="Wipes every source database and the landing folder, then simulates the full history again. Your tracker issues, comments and work log are kept."
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="danger-solid" disabled={!ready} loading={build.isPending} onClick={submit}>
            Wipe and build {PRESET_INFO[preset].label}
          </Button>
        </>
      }
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
        className="flex flex-col gap-5"
      >
        <fieldset>
          <legend className="mb-2 text-sm font-medium text-ink">Size</legend>
          <div role="radiogroup" className="grid grid-cols-3 gap-2 max-sm:grid-cols-1">
            {(Object.keys(PRESET_INFO) as Preset[]).map((p) => {
              const info = PRESET_INFO[p];
              const on = preset === p;
              return (
                <button
                  key={p}
                  type="button"
                  role="radio"
                  aria-checked={on}
                  onClick={() => setPreset(p)}
                  className={clsx(
                    'press flex flex-col items-start rounded-m border p-3 text-left transition-colors duration-150',
                    on ? 'border-accent bg-accent-tint ring-1 ring-accent' : 'border-line hover:border-control hover:bg-hover',
                  )}
                >
                  <span className="flex w-full items-baseline justify-between">
                    <span className="text-md font-semibold text-ink">{info.label}</span>
                    <span className="num text-sm text-muted">{info.months} months</span>
                  </span>
                  <span className="num mt-2 text-sm text-ink">{info.orders}</span>
                  <span className="num text-sm text-muted">
                    {info.disk} · {info.time}
                  </span>
                  <span className="mt-2 text-xs text-muted">{info.note}</span>
                </button>
              );
            })}
          </div>
        </fieldset>

        <div className="grid grid-cols-[1fr_auto] items-end gap-2">
          <div>
            <Label htmlFor={seedId} hint="Same seed and size, same data">
              Seed
            </Label>
            <Input
              id={seedId}
              inputMode="numeric"
              value={seed}
              onChange={(e) => setSeed(e.target.value.trim())}
              aria-invalid={!seedOk}
              aria-describedby={!seedOk ? `${seedId}-err` : undefined}
              className="num"
            />
          </div>
          <IconButton label="Random seed" variant="secondary" onClick={() => setSeed(String(Math.floor(Math.random() * 99_999_999)))}>
            <Dices aria-hidden />
          </IconButton>
        </div>
        {!seedOk ? (
          <FieldNote id={`${seedId}-err`} tone="bad">
            Use digits only, up to 10.
          </FieldNote>
        ) : null}

        <div>
          <Label htmlFor={confirmId}>
            Type <span className="font-mono font-semibold text-bad">{CONFIRM}</span> to confirm
          </Label>
          <Input
            ref={confirmRef}
            id={confirmId}
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            autoComplete="off"
            spellCheck={false}
            aria-describedby={`${confirmId}-note`}
            className="font-mono tracking-wide"
          />
          <FieldNote id={`${confirmId}-note`}>
            {streaming ? 'The running stream stops first. ' : ''}Your pipelines will see every table truncated and reloaded.
          </FieldNote>
        </div>
      </form>
    </Dialog>
  );
}
