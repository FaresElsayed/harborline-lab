import { useEffect, useId, useState } from 'react';
import { useNavigate } from 'react-router';
import { toast } from 'sonner';
import { useCompleteSprint, useStartSprint } from '@/api/queries';
import type { Sprint } from '@/api/types';
import { Button } from '@/components/ui/button';
import { FieldNote, Label, Select } from '@/components/ui/field';
import { Dialog } from '@/components/ui/overlay';
import { differenceInCalendarDays, format, parseDay, today } from '@/lib/dates';
import { fmtInt, plural } from '@/lib/format';
import { SPRINT_CAPACITY_HOURS, sprintHours, useTracker } from './data';

export function StartSprintDialog({ sprint, open, onOpenChange }: { sprint: Sprint; open: boolean; onOpenChange: (v: boolean) => void }) {
  const start = useStartSprint();
  const t = useTracker();
  const navigate = useNavigate();
  const items = t.issues.filter((i) => i.sprint_id === sprint.id && i.type !== 'Epic');
  const hours = sprintHours(items);
  const late = sprint.start_date ? differenceInCalendarDays(today(), parseDay(sprint.start_date)) : 0;

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={`Start ${sprint.name}`}
      description="Sprints follow the program calendar you set up, so every due date keeps its meaning."
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            variant="primary"
            loading={start.isPending}
            onClick={() =>
              start.mutate(sprint.id, {
                onSuccess: () => {
                  onOpenChange(false);
                  toast.success(`${sprint.name} started`, { action: { label: 'Open board', onClick: () => navigate('/tracker/board') } });
                },
                onError: (e) => toast.error('Sprint not started', { description: e.message }),
              })
            }
          >
            Start sprint
          </Button>
        </>
      }
    >
      <dl className="grid grid-cols-[140px_1fr] gap-y-2 text-base">
        <dt className="text-muted">Planned dates</dt>
        <dd className="num text-ink">
          {sprint.start_date ? format(parseDay(sprint.start_date), 'EEE, MMM d') : '—'} – {sprint.end_date ? format(parseDay(sprint.end_date), 'EEE, MMM d') : '—'}
        </dd>
        <dt className="text-muted">Issues</dt>
        <dd className="num text-ink">{plural(items.length, 'issue')}</dd>
        <dt className="text-muted">Estimate</dt>
        <dd className="num text-ink">
          {fmtInt(hours)} h of {SPRINT_CAPACITY_HOURS} h capacity
          {hours > SPRINT_CAPACITY_HOURS ? <span className="text-bad"> · {fmtInt(hours - SPRINT_CAPACITY_HOURS)} h over</span> : null}
        </dd>
        {sprint.goal ? (
          <>
            <dt className="text-muted">Goal</dt>
            <dd className="text-ink">{sprint.goal}</dd>
          </>
        ) : null}
      </dl>
      {late > 0 ? <FieldNote>This sprint was planned to start {plural(late, 'day')} ago. Its due dates stay as planned.</FieldNote> : null}
    </Dialog>
  );
}

export function CompleteSprintDialog({ sprint, open, onOpenChange }: { sprint: Sprint; open: boolean; onOpenChange: (v: boolean) => void }) {
  const t = useTracker();
  const complete = useCompleteSprint();
  const items = t.issues.filter((i) => i.sprint_id === sprint.id && i.type !== 'Epic');
  const done = items.filter((i) => i.status === 'Done').length;
  const openCount = items.length - done;
  const next = t.sprints.filter((s) => s.state === 'future').sort((a, b) => a.id - b.id);
  const nextId = next[0] ? String(next[0].id) : 'backlog';
  const [target, setTarget] = useState<string>(nextId);
  const id = useId();
  useEffect(() => {
    if (open) setTarget(nextId);
  }, [open, nextId]);

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={`Complete ${sprint.name}`}
      description={
        <span className="num">
          {plural(done, 'issue')} done, {plural(openCount, 'issue')} still open{sprint.end_date ? ` · planned to end ${format(parseDay(sprint.end_date), 'EEE, MMM d')}` : ''}.
        </span>
      }
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            variant="primary"
            loading={complete.isPending}
            onClick={() =>
              complete.mutate(
                { id: sprint.id, move_open_to: target === 'backlog' ? 'backlog' : Number(target) },
                {
                  onSuccess: () => {
                    onOpenChange(false);
                    toast.success(`${sprint.name} completed`, {
                      description: openCount ? `${plural(openCount, 'open issue')} moved to ${target === 'backlog' ? 'the backlog' : next.find((s) => String(s.id) === target)?.name}.` : 'Every issue was done.',
                    });
                  },
                  onError: (e) => toast.error('Sprint not completed', { description: e.message }),
                },
              )
            }
          >
            Complete sprint
          </Button>
        </>
      }
    >
      {openCount ? (
        <div>
          <Label htmlFor={id}>Move the {plural(openCount, 'open issue')} to</Label>
          <Select
            id={id}
            value={target}
            onValueChange={setTarget}
            options={[...next.map((s) => ({ value: String(s.id), label: s.name, hint: s.start_date ? format(parseDay(s.start_date), 'MMM d') : undefined })), { value: 'backlog', label: 'Backlog' }]}
          />
          <FieldNote>Their due dates stay as they are; overdue work shows red on the next board.</FieldNote>
        </div>
      ) : (
        <p className="text-base text-ink">Every issue in this sprint is done.</p>
      )}
    </Dialog>
  );
}
