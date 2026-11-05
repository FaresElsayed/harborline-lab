import { useMemo, useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router';
import { Command } from 'cmdk';
import { Dialog as RDialog } from 'radix-ui';
import {
  BookOpen,
  Braces,
  ChartGantt,
  ChartLine,
  CornerDownLeft,
  Flag,
  Gauge,
  Hammer,
  Hash,
  History,
  LifeBuoy,
  ListFilter,
  ListTodo,
  Monitor,
  PackageCheck,
  Moon,
  Play,
  Plug,
  Plus,
  RotateCcw,
  ShieldCheck,
  Shuffle,
  Square,
  SquareKanban,
  Sun,
  Timer,
  UserRound,
  UsersRound,
} from 'lucide-react';
import { useAllIssues, useMeta, useStatus } from '@/api/queries';
import { useSimActions } from '@/features/control/actions';
import { TypeIcon } from '@/features/tracker/atoms';
import { Kbd } from '@/components/ui/overlay';
import { handbookUrl, withBase } from '@/lib/base';
import { setTheme } from '@/lib/theme';
import { ui } from '@/lib/store';

const itemClass =
  'flex h-9 cursor-default select-none items-center gap-2.5 rounded-m px-2.5 text-base text-ink outline-none ' +
  'data-[selected=true]:bg-active [&>svg]:size-4 [&>svg]:shrink-0 [&>svg]:text-muted';

function Item({ children, onSelect, icon, hint, value, keywords }: { children: ReactNode; onSelect: () => void; icon?: ReactNode; hint?: ReactNode; value?: string; keywords?: string[] }) {
  return (
    <Command.Item value={value} keywords={keywords} onSelect={onSelect} className={itemClass}>
      {icon}
      <span className="min-w-0 flex-1 truncate">{children}</span>
      {hint ? <span className="shrink-0 text-sm text-muted">{hint}</span> : null}
    </Command.Item>
  );
}

const groupClass = '[&_[cmdk-group-heading]]:px-2.5 [&_[cmdk-group-heading]]:pb-1 [&_[cmdk-group-heading]]:pt-3 [&_[cmdk-group-heading]]:text-xs [&_[cmdk-group-heading]]:text-muted';

const KEY_RE = /^\s*(data-?\d+|\d+)\s*$/i;

export function CommandPalette() {
  const open = ui.use((s) => s.palette);
  const [search, setSearch] = useState('');
  const navigate = useNavigate();
  const meta = useMeta();
  const issues = useAllIssues(open && !!meta.data?.configured);
  const status = useStatus();
  const sim = useSimActions();

  const close = () => {
    ui.set({ palette: false });
    setSearch('');
  };
  const go = (to: string) => {
    close();
    navigate(to);
  };
  const run = (fn: () => unknown) => {
    close();
    void fn();
  };

  const keyGuess = useMemo(() => {
    const m = KEY_RE.exec(search);
    if (!m) return null;
    const n = m[1].replace(/\D/g, '');
    return `DATA-${n}`;
  }, [search]);

  const streaming = status.data?.state === 'streaming';
  const isDemo = import.meta.env.VITE_DEMO === '1';
  const isMock = import.meta.env.VITE_MOCK === '1' && !isDemo;

  return (
    <Command.Dialog
      open={open}
      onOpenChange={(v) => (v ? ui.set({ palette: true }) : close())}
      label="Command palette"
      shouldFilter
      loop
      overlayClassName="fixed inset-0 z-40 bg-[rgb(8_19_31/0.32)]"
      contentClassName="fixed left-1/2 top-[14vh] z-50 w-[calc(100vw-32px)] max-w-[620px] -translate-x-1/2 overflow-hidden rounded-l border border-line bg-sheet shadow-float"
    >
      <RDialog.Title className="sr-only">Command palette</RDialog.Title>
      <RDialog.Description className="sr-only">Go to a page, run a simulator action or open an issue by key.</RDialog.Description>
      <div className="flex items-center gap-2 border-b border-rule px-4">
        <Command.Input
          autoFocus
          value={search}
          onValueChange={setSearch}
          placeholder="Type a command, a page, or an issue key like DATA-42"
          className="h-12 flex-1 bg-transparent text-md text-ink outline-none placeholder:text-faint"
        />
        <Kbd>Esc</Kbd>
      </div>
      <Command.List className="scroll-thin max-h-[min(440px,60vh)] overflow-y-auto p-1.5">
        <Command.Empty className="px-3 py-6 text-center text-base text-muted">No command, page or issue matches “{search}”.</Command.Empty>

        {keyGuess ? (
          <Command.Group heading="Jump to issue" className={groupClass}>
            <Item value={`open ${keyGuess} ${search}`} onSelect={() => go(`/tracker/issue/${keyGuess}`)} icon={<CornerDownLeft aria-hidden />} hint="Open">
              {keyGuess}
            </Item>
          </Command.Group>
        ) : null}

        <Command.Group heading="Go to" className={groupClass}>
          <Item onSelect={() => go('/start')} icon={<Flag aria-hidden />} keywords={['begin', 'first', 'next', 'what do i do', 'onboarding']}>
            Start here
          </Item>
          <Item onSelect={() => go('/control')} icon={<Gauge aria-hidden />} keywords={['status', 'home', 'dashboard']}>
            Control Center
          </Item>
          <Item onSelect={() => go('/control/scenarios')} icon={<Shuffle aria-hidden />}>
            Scenarios
          </Item>
          <Item onSelect={() => go('/control/quality')} icon={<ShieldCheck aria-hidden />} keywords={['dq', 'anomalies', 'bad data']}>
            Data quality
          </Item>
          <Item onSelect={() => go('/control/connections')} icon={<Plug aria-hidden />} keywords={['postgres', 'salesforce', 'credentials', 'password']}>
            Connections
          </Item>
          <Item onSelect={() => go('/control/runs')} icon={<History aria-hidden />}>
            Run history
          </Item>
          <Item onSelect={() => go('/control/extract')} icon={<PackageCheck aria-hidden />} keywords={['iris', 'uat', 'review', 'zip', 'sign off']}>
            Source extract
          </Item>
          <Item onSelect={() => go('/team')} icon={<UsersRound aria-hidden />} keywords={['people', 'colleagues', 'who', 'ask']}>
            Team
          </Item>
          <Item onSelect={() => go('/tracker/board')} icon={<SquareKanban aria-hidden />} keywords={['sprint', 'kanban']}>
            Board
          </Item>
          <Item onSelect={() => go('/tracker/backlog')} icon={<ListTodo aria-hidden />}>
            Backlog
          </Item>
          <Item onSelect={() => go('/tracker/timeline')} icon={<ChartGantt aria-hidden />} keywords={['roadmap', 'epics']}>
            Timeline
          </Item>
          <Item onSelect={() => go('/tracker/reports')} icon={<ChartLine aria-hidden />} keywords={['burndown', 'velocity', 'cfd']}>
            Reports
          </Item>
          <Item onSelect={() => go('/tracker/issues')} icon={<ListFilter aria-hidden />} keywords={['search', 'jql', 'filter']}>
            Issues
          </Item>
          <Item onSelect={() => go('/tracker/my-work')} icon={<UserRound aria-hidden />} keywords={['overdue', 'due']}>
            My work
          </Item>
          <Item onSelect={() => run(() => window.open(withBase('/handbook/'), '_blank'))} icon={<BookOpen aria-hidden />}>
            Handbook
          </Item>
          {isDemo ? null : (
            <Item onSelect={() => run(() => window.open('/docs', '_blank'))} icon={<Braces aria-hidden />} keywords={['openapi', 'swagger']}>
              API docs
            </Item>
          )}
        </Command.Group>

        <Command.Group heading="Lost?" className={groupClass}>
          <Item value="lost start here" onSelect={() => go('/start')} icon={<Flag aria-hidden />} keywords={['help', 'stuck', 'what do i do', 'next']}>
            Start here
          </Item>
          <Item value="lost ask data-help" onSelect={() => go('/team/channel')} icon={<Hash aria-hidden />} keywords={['help', 'stuck', 'who', 'ask', 'channel']}>
            Ask #data-help who can help
          </Item>
          <Item value="lost known issues" onSelect={() => run(() => window.open(handbookUrl('known-issues.html'), '_blank'))} icon={<LifeBuoy aria-hidden />} keywords={['help', 'error', 'broken', 'setup', 'troubleshooting']}>
            Known issues
          </Item>
          <Item value="lost it help kevin" onSelect={() => go('/team/kevin-ohara')} icon={<LifeBuoy aria-hidden />} keywords={['help', 'error', 'broken', 'setup', 'docker', 'connect']}>
            IT help: Kevin O’Hara
          </Item>
          <Item value="lost team lead nadia" onSelect={() => go('/team/nadia-petrov')} icon={<UsersRound aria-hidden />} keywords={['help', 'stuck', 'coach', 'review', 'manager']}>
            Your team lead: Nadia Petrov
          </Item>
        </Command.Group>

        <Command.Group heading="Simulator" className={groupClass}>
          {[1, 6, 24, 168].map((h) => (
            <Item key={h} onSelect={() => run(() => sim.runBatch(h))} icon={<Timer aria-hidden />} keywords={['batch', 'generate', 'run']}>
              Generate batch · {h < 24 ? `${h} h` : h === 24 ? '1 day' : '7 days'}
            </Item>
          ))}
          {streaming ? (
            <Item onSelect={() => run(sim.stopStream)} icon={<Square aria-hidden />} keywords={['stream', 'stop']}>
              Stop stream
            </Item>
          ) : (
            <Item onSelect={() => run(() => sim.startStream())} icon={<Play aria-hidden />} keywords={['stream', 'start', 'cdc']}>
              Start stream
            </Item>
          )}
          <Item onSelect={() => run(() => (navigate('/control'), ui.set({ buildDialog: true })))} icon={<Hammer aria-hidden />} keywords={['rebuild', 'reset', 'preset']}>
            Build dataset…
          </Item>
        </Command.Group>

        <Command.Group heading="Tracker" className={groupClass}>
          <Item onSelect={() => run(() => ui.set({ createIssue: {} }))} icon={<Plus aria-hidden />} hint={<Kbd>C</Kbd>}>
            Create issue
          </Item>
        </Command.Group>

        {search.trim().length >= 2 && issues.data ? (
          <Command.Group heading="Issues" className={groupClass}>
            {issues.data.map((i) => (
              <Command.Item
                key={i.key}
                value={`${i.key} ${i.summary}`}
                onSelect={() => go(`/tracker/issue/${i.key}`)}
                className={itemClass}
              >
                <TypeIcon type={i.type} label={false} />
                <span className="w-16 shrink-0 font-mono text-sm text-muted">{i.key}</span>
                <span className="min-w-0 flex-1 truncate">{i.summary}</span>
              </Command.Item>
            ))}
          </Command.Group>
        ) : null}

        <Command.Group heading="Theme" className={groupClass}>
          <Item onSelect={() => run(() => setTheme('light'))} icon={<Sun aria-hidden />} keywords={['theme', 'appearance']}>
            Light theme
          </Item>
          <Item onSelect={() => run(() => setTheme('dark'))} icon={<Moon aria-hidden />} keywords={['theme', 'appearance']}>
            Dark theme
          </Item>
          <Item onSelect={() => run(() => setTheme('system'))} icon={<Monitor aria-hidden />} keywords={['theme', 'appearance']}>
            Match system theme
          </Item>
        </Command.Group>

        {isMock ? (
          <Command.Group heading="Mock data (dev only)" className={groupClass}>
            {(['demo', 'fresh'] as const).map((mode) => (
              <Item
                key={mode}
                onSelect={() =>
                  run(() => {
                    (window as unknown as { __harborlineMock: { resetTracker: (m: string) => void } }).__harborlineMock.resetTracker(mode);
                    location.reload();
                  })
                }
                icon={<RotateCcw aria-hidden />}
              >
                {mode === 'demo' ? 'Reset tracker to the two-sprints-in demo' : 'Reset tracker to first run'}
              </Item>
            ))}
          </Command.Group>
        ) : null}
      </Command.List>
    </Command.Dialog>
  );
}
