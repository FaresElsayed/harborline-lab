import { useId, useMemo, useState } from 'react';
import { useNavigate } from 'react-router';
import { toast } from 'sonner';
import { useCreateIssue } from '@/api/queries';
import type { IssueType, Priority } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Checkbox, FieldNote, Input, Label, Select, Textarea } from '@/components/ui/field';
import { Dialog } from '@/components/ui/overlay';
import { ui } from '@/lib/store';
import { TypeIcon } from './atoms';
import { epicShort, useTracker } from './data';

const TYPES: IssueType[] = ['Task', 'Story', 'Bug', 'Spike'];

export default function CreateIssueDialog() {
  const preset = ui.use((s) => s.createIssue);
  const t = useTracker();
  const create = useCreateIssue();
  const navigate = useNavigate();
  const [type, setType] = useState<IssueType>(preset?.type ?? 'Task');
  const [summary, setSummary] = useState('');
  const [description, setDescription] = useState('');
  const [epic, setEpic] = useState<string>(preset?.epic_key ?? 'none');
  const [parent, setParent] = useState<string>(preset?.parent_key ?? 'none');
  const [sprint, setSprint] = useState<string>(preset?.sprint_id === null ? 'backlog' : preset?.sprint_id != null ? String(preset.sprint_id) : t.active ? String(t.active.id) : 'backlog');
  const [priority, setPriority] = useState<Priority>('Medium');
  const [estimate, setEstimate] = useState('');
  const [due, setDue] = useState('');
  const [another, setAnother] = useState(false);
  const [touched, setTouched] = useState(false);
  const ids = { type: useId(), summary: useId(), desc: useId(), epic: useId(), parent: useId(), sprint: useId(), prio: useId(), est: useId(), due: useId(), more: useId() };

  const stories = useMemo(() => t.issues.filter((i) => i.type === 'Story' && (epic === 'none' || i.epic_key === epic)), [t.issues, epic]);
  const close = () => ui.set({ createIssue: null });
  const configured = t.configured;
  const summaryOk = summary.trim().length > 0;

  const submit = () => {
    setTouched(true);
    if (!summaryOk) return;
    const est = estimate ? Number(estimate) : null;
    create.mutate(
      {
        type,
        summary: summary.trim(),
        description: description.trim() || undefined,
        priority,
        assignee: t.me,
        epic_key: epic === 'none' ? (parent !== 'none' ? t.byKey.get(parent)?.epic_key ?? null : null) : epic,
        parent_key: type !== 'Story' && parent !== 'none' ? parent : null,
        sprint_id: sprint === 'backlog' ? null : Number(sprint),
        story_points: type === 'Story' ? est : null,
        estimate_hours: type !== 'Story' ? est : null,
        due_date: due || null,
      },
      {
        onSuccess: (issue) => {
          toast.success(`${issue.key} created`, { description: issue.summary, action: { label: 'Open', onClick: () => navigate(`/tracker/issue/${issue.key}`) } });
          if (another) {
            setSummary('');
            setDescription('');
            setTouched(false);
          } else close();
        },
        onError: (e) => toast.error('Issue not created', { description: e.message }),
      },
    );
  };

  return (
    <Dialog
      open
      onOpenChange={(v) => !v && close()}
      title="Create issue"
      description={configured ? 'Your own tickets sit beside the pre-written ones and export to Jira the same way.' : 'Set the program start date first.'}
      width={640}
      footer={
        <>
          <label htmlFor={ids.more} className="mr-auto flex cursor-pointer items-center gap-2 text-base text-ink">
            <Checkbox id={ids.more} checked={another} onCheckedChange={setAnother} />
            Create another
          </label>
          <Button variant="ghost" onClick={close}>
            Cancel
          </Button>
          <Button variant="primary" onClick={submit} loading={create.isPending} disabled={!configured}>
            Create
          </Button>
        </>
      }
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
        className="grid grid-cols-2 gap-4"
      >
        <div>
          <Label htmlFor={ids.type}>Type</Label>
          <Select
            id={ids.type}
            value={type}
            onValueChange={(v) => setType(v as IssueType)}
            options={TYPES.map((x) => ({
              value: x,
              label: (
                <span className="flex items-center gap-2">
                  <TypeIcon type={x} label={false} /> {x}
                </span>
              ),
            }))}
          />
        </div>
        <div>
          <Label htmlFor={ids.prio}>Priority</Label>
          <Select id={ids.prio} value={priority} onValueChange={(v) => setPriority(v as Priority)} options={(['Highest', 'High', 'Medium', 'Low', 'Lowest'] as const).map((p) => ({ value: p, label: p }))} />
        </div>
        <div className="col-span-2">
          <Label htmlFor={ids.summary}>Summary</Label>
          <Input
            id={ids.summary}
            value={summary}
            autoFocus
            onChange={(e) => setSummary(e.target.value)}
            aria-invalid={touched && !summaryOk}
            aria-describedby={touched && !summaryOk ? `${ids.summary}-err` : undefined}
            placeholder="Backfill carrier events older than the API's 90-day window"
          />
          {touched && !summaryOk ? (
            <FieldNote id={`${ids.summary}-err`} tone="bad">
              Give the issue a summary.
            </FieldNote>
          ) : null}
        </div>
        <div className="col-span-2">
          <Label htmlFor={ids.desc} hint="Markdown">
            Description
          </Label>
          <Textarea id={ids.desc} value={description} onChange={(e) => setDescription(e.target.value)} placeholder="What and why. Leave the how to the PR." />
        </div>
        <div>
          <Label htmlFor={ids.epic}>Epic</Label>
          <Select
            id={ids.epic}
            value={epic}
            onValueChange={(v) => {
              setEpic(v);
              setParent('none');
            }}
            options={[{ value: 'none', label: 'None' }, ...t.epics.map((e) => ({ value: e.key, label: epicShort(e.summary) }))]}
          />
        </div>
        {type !== 'Story' ? (
          <div>
            <Label htmlFor={ids.parent}>Parent story</Label>
            <Select id={ids.parent} value={parent} onValueChange={setParent} options={[{ value: 'none', label: 'None' }, ...stories.map((s) => ({ value: s.key, label: `${s.key} ${s.summary}` }))]} />
          </div>
        ) : (
          <div />
        )}
        <div>
          <Label htmlFor={ids.sprint}>Sprint</Label>
          <Select
            id={ids.sprint}
            value={sprint}
            onValueChange={setSprint}
            options={[...t.sprints.filter((s) => s.state !== 'closed').map((s) => ({ value: String(s.id), label: s.name, hint: s.state === 'active' ? 'active' : undefined })), { value: 'backlog', label: 'Backlog' }]}
          />
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <Label htmlFor={ids.est}>{type === 'Story' ? 'Points' : 'Estimate (h)'}</Label>
            <Input id={ids.est} inputMode="decimal" value={estimate} onChange={(e) => setEstimate(e.target.value.replace(/[^\d.]/g, ''))} className="num" placeholder={type === 'Story' ? '5' : '4'} />
          </div>
          <div>
            <Label htmlFor={ids.due}>Due</Label>
            <Input id={ids.due} type="date" value={due} onChange={(e) => setDue(e.target.value)} className="num px-2" />
          </div>
        </div>
        <button type="submit" className="hidden" aria-hidden tabIndex={-1} />
      </form>
    </Dialog>
  );
}
