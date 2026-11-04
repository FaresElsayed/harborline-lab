import { toast } from 'sonner';
import type { RunSummary, TableChange } from '@/api/types';
import { useBatch, useRunScenario, useStatus, useStream } from '@/api/queries';
import { fmtInt, fmtSimHours } from '@/lib/format';

export function summarise(changes: TableChange[]) {
  const ins = changes.reduce((t, c) => t + c.ins, 0);
  const upd = changes.reduce((t, c) => t + c.upd, 0);
  const del = changes.reduce((t, c) => t + c.del, 0);
  const tables = changes.filter((c) => c.ins + c.upd + c.del > 0).length;
  return { ins, upd, del, tables };
}

export function changeLine(changes: TableChange[]) {
  const s = summarise(changes);
  return `+${fmtInt(s.ins)} inserted · ${fmtInt(s.upd)} updated · ${fmtInt(s.del)} deleted across ${s.tables} tables`;
}

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

/** Simulator actions with their toasts, shared by the Control Center and the command palette. */
export function useSimActions() {
  const batch = useBatch();
  const stream = useStream();
  const scenario = useRunScenario();
  const status = useStatus();

  const runBatch = async (hours: number): Promise<RunSummary | null> => {
    const id = toast.loading(`Generating ${fmtSimHours(hours)} of activity…`);
    try {
      const run = await batch.mutateAsync(hours);
      toast.success(`${fmtSimHours(hours)} generated`, { id, description: changeLine(run.changes) });
      return run;
    } catch (e) {
      toast.error('Batch did not run', { id, description: message(e) });
      return null;
    }
  };

  const startStream = async (interval_s?: number, speed?: number) => {
    const s = status.data?.stream;
    try {
      await stream.start.mutateAsync({ interval_s: interval_s ?? s?.interval_s ?? 5, speed: speed ?? s?.speed ?? 60 });
      toast.success('Stream started', {
        description: `A tick every ${interval_s ?? s?.interval_s ?? 5} s at ${speed ?? s?.speed ?? 60}× speed`,
      });
    } catch (e) {
      toast.error('Stream did not start', { description: message(e) });
    }
  };

  const stopStream = async () => {
    try {
      const st = await stream.stop.mutateAsync();
      toast.success('Stream stopped', { description: st ? `${fmtInt(st.stream.ticks)} ticks applied` : undefined });
    } catch (e) {
      toast.error('Stream did not stop', { description: message(e) });
    }
  };

  const runScenario = async (id: string, name: string, params: Record<string, unknown>) => {
    const t = toast.loading(`Running ${name}…`);
    try {
      const run = await scenario.mutateAsync({ id, params });
      toast.success(`${name} applied`, { id: t, description: changeLine(run.changes) });
      return run;
    } catch (e) {
      toast.error(`${name} did not run`, { id: t, description: message(e) });
      return null;
    }
  };

  return {
    runBatch,
    startStream,
    stopStream,
    runScenario,
    pending: {
      batch: batch.isPending,
      stream: stream.start.isPending || stream.stop.isPending,
      scenario: scenario.isPending ? scenario.variables?.id ?? null : null,
    },
  };
}
