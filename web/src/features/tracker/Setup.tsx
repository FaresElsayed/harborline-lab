import { useId, useMemo, useState } from 'react';
import { useNavigate } from 'react-router';
import { toast } from 'sonner';
import { useSearch, useSetup } from '@/api/queries';
import { Button } from '@/components/ui/button';
import { FieldNote, Input, Label } from '@/components/ui/field';
import { format, isWeekend, nextMonday, parseDay, toDay, workingDay } from '@/lib/dates';
import { epicShort } from './data';

const PROGRAM = [
  ['Data Platform Foundation', 'Director of IT'],
  ['Warehouse Performance', 'VP Operations'],
  ['Billing Assurance and AR', 'CFO and Controller'],
  ['Freight Audit and Carrier Performance', 'Transportation'],
  ['Client 360', 'Sales Ops and Client Success'],
  ['Real-Time Operations', 'VP Operations'],
  ['Cold Chain and Lot Traceability', 'Quality'],
  ['Client Portal and Executive Scorecard', 'CEO, CS and InfoSec'],
] as const;

export function Setup() {
  const [date, setDate] = useState(toDay(nextMonday()));
  const setup = useSetup();
  const navigate = useNavigate();
  const epics = useSearch({ type: 'Epic' });
  const id = useId();
  const d = date ? parseDay(date) : null;
  const weekend = d ? isWeekend(d) : false;
  const valid = !!d && !Number.isNaN(d.getTime()) && !weekend;

  const projects = useMemo(() => {
    if (!valid || !d) return [];
    const names = epics.data?.length ? epics.data.slice(0, 8).map((e) => [epicShort(e.summary), '']) : PROGRAM;
    return names.map(([name, who], i) => ({
      n: i + 1,
      name,
      who: who || PROGRAM[i]?.[1] || '',
      start: workingDay(d, i * 20 + 1),
      end: workingDay(d, (i + 1) * 20),
    }));
  }, [valid, d, epics.data]);

  const programEnd = projects.at(-1)?.end ?? null;
  const start = projects[0]?.start ?? null;
  const span = start && programEnd ? programEnd.getTime() - start.getTime() : 1;

  const submit = () => {
    if (!valid) return;
    setup.mutate(date, {
      onSuccess: () => {
        toast.success('Program scheduled', { description: '16 sprints are planned. Start Sprint 1 from the backlog when you are ready.' });
        navigate('/tracker/backlog');
      },
      onError: (e) => toast.error('Start date not saved', { description: e.message }),
    });
  };

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-[920px] px-6 pb-16 pt-12 max-sm:px-4 max-sm:pt-6">
        <h1 className="figure text-[2rem] font-semibold leading-10 text-ink">When does your program start?</h1>
        <p className="mt-2 max-w-[62ch] text-md text-muted">
          Every ticket is due a set number of working days after this date. Eight projects of four weeks each, two-week sprints, 32 weeks in all. You can change dates on single
          issues later; this sets them all.
        </p>

        <form
          className="mt-8 flex flex-wrap items-end gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
        >
          <div>
            <Label htmlFor={id}>Start date</Label>
            <Input
              id={id}
              type="date"
              value={date}
              onChange={(e) => setDate(e.target.value)}
              aria-invalid={!valid}
              aria-describedby={`${id}-note`}
              className="num h-9 w-48"
            />
          </div>
          <Button type="submit" variant="primary" size="lg" disabled={!valid} loading={setup.isPending}>
            {valid && d ? `Start on ${format(d, 'EEE, MMM d')}` : 'Start the program'}
          </Button>
        </form>
        <FieldNote id={`${id}-note`} tone={weekend ? 'bad' : 'muted'}>
          {weekend ? 'Pick a weekday. Working days skip Saturdays and Sundays.' : d && d.getDay() !== 1 ? `Sprints will start on a ${format(d, 'EEEE')}. A Monday keeps them aligned to weeks.` : 'A Monday keeps sprints aligned to calendar weeks.'}
        </FieldNote>

        {projects.length ? (
          <section aria-labelledby="preview" className="mt-10">
            <div className="flex items-baseline justify-between gap-4">
              <h2 id="preview" className="text-md font-semibold text-ink">
                Your 32 weeks
              </h2>
              {programEnd ? <span className="num text-base text-muted">Final readout due {format(programEnd, 'EEE, MMM d, yyyy')}</span> : null}
            </div>
            <ol className="mt-3 flex flex-col">
              {projects.map((p) => {
                const left = ((p.start.getTime() - start!.getTime()) / span) * 100;
                const width = ((p.end.getTime() - p.start.getTime()) / span) * 100;
                return (
                  <li key={p.n} className="grid grid-cols-[minmax(0,300px)_minmax(0,1fr)_140px] items-center gap-4 border-t border-rule py-2.5 max-md:grid-cols-[1fr_auto]">
                    <div className="min-w-0">
                      <div className="truncate text-base font-medium text-ink">{p.name}</div>
                      <div className="truncate text-sm text-muted">{p.who}</div>
                    </div>
                    <div className="relative h-2.5 rounded-full bg-active max-md:hidden" aria-hidden>
                      <div className="absolute inset-y-0 rounded-full bg-accent" style={{ left: `${left}%`, width: `${Math.max(width, 2)}%` }} />
                    </div>
                    <div className="num text-right text-sm text-muted">
                      {format(p.start, 'MMM d')} – {format(p.end, 'MMM d')}
                    </div>
                  </li>
                );
              })}
            </ol>
          </section>
        ) : null}
      </div>
    </div>
  );
}
