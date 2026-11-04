import type { ReactNode } from 'react';
import { NavLink } from 'react-router';
import clsx from 'clsx';
import {
  ArrowUpRight,
  BookOpen,
  Braces,
  ChartGantt,
  ChartLine,
  Gauge,
  History,
  ListFilter,
  ListTodo,
  PackageCheck,
  PanelLeftClose,
  Plug,
  ShieldCheck,
  Shuffle,
  SquareKanban,
  UserRound,
  UsersRound,
} from 'lucide-react';
import lockupLight from '@brand/logo/harborline-lockup-light.svg';
import lockupDark from '@brand/logo/harborline-lockup-dark.svg';
import { useAllIssues, useMeta } from '@/api/queries';
import { IconButton } from '@/components/ui/button';
import { withBase } from '@/lib/base';
import { dueState } from '@/lib/dates';
import { toggleNavCollapsed, ui } from '@/lib/store';

function Item({ to, icon, children, end, count, countLabel }: { to: string; icon: ReactNode; children: ReactNode; end?: boolean; count?: number; countLabel?: string }) {
  return (
    <NavLink
      to={to}
      end={end}
      onClick={() => ui.set({ navOpen: false })}
      className={({ isActive }) =>
        clsx(
          'group flex h-8 items-center gap-2.5 rounded-m px-2.5 text-base transition-colors duration-150 [&_svg]:size-4 [&_svg]:shrink-0',
          isActive ? 'bg-active font-medium text-ink [&_svg]:text-ink' : 'text-muted hover:bg-hover hover:text-ink [&_svg]:text-muted',
        )
      }
    >
      {icon}
      <span className="flex-1 truncate">{children}</span>
      {count ? (
        <span className="num rounded-full bg-bad-bg px-1.5 text-xs font-semibold leading-5 text-bad" aria-label={countLabel}>
          {count}
        </span>
      ) : null}
    </NavLink>
  );
}

function External({ href, icon, children }: { href: string; icon: ReactNode; children: ReactNode }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className="group flex h-8 items-center gap-2.5 rounded-m px-2.5 text-base text-muted transition-colors duration-150 hover:bg-hover hover:text-ink [&_svg]:size-4 [&_svg]:shrink-0 [&_svg]:text-muted"
    >
      {icon}
      <span className="flex-1 truncate">{children}</span>
      <ArrowUpRight className="!size-3.5 opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100" aria-hidden />
      <span className="sr-only">(opens in a new tab)</span>
    </a>
  );
}

function Group({ label, children }: { label?: ReactNode; children: ReactNode }) {
  return (
    <div className="mt-5 first:mt-2">
      {label ? <div className="mb-1 px-2.5 text-sm text-muted">{label}</div> : null}
      <div className="flex flex-col gap-0.5">{children}</div>
    </div>
  );
}

export function Logo() {
  return (
    <NavLink to="/control" className="flex h-12 items-center rounded-m px-2" aria-label="Harborline home">
      <img src={lockupLight} alt="Harborline Logistics" className="logo-light h-[30px] w-auto" />
      <img src={lockupDark} alt="" aria-hidden className="logo-dark h-[30px] w-auto" />
    </NavLink>
  );
}

export function Sidebar({ inDrawer = false }: { inDrawer?: boolean }) {
  const meta = useMeta();
  const issues = useAllIssues(!!meta.data?.configured);
  const me = meta.data?.me ?? 'me';
  const overdue = (issues.data ?? []).filter((i) => i.assignee === me && i.type !== 'Epic' && dueState(i.due_date, i.status === 'Done') === 'overdue').length;

  return (
    <nav aria-label="Main" className="flex h-full flex-col px-2 pb-3">
      <div className="flex items-center justify-between">
        <Logo />
        {!inDrawer ? (
          <IconButton label="Hide sidebar" tip="Hide sidebar  [" size="sm" onClick={toggleNavCollapsed} className="mr-0.5">
            <PanelLeftClose aria-hidden />
          </IconButton>
        ) : null}
      </div>

      <div className="scroll-thin -mx-2 min-h-0 flex-1 overflow-y-auto px-2">
        <Group label="Simulator">
          <Item to="/control" end icon={<Gauge aria-hidden />}>
            Control Center
          </Item>
          <Item to="/control/scenarios" icon={<Shuffle aria-hidden />}>
            Scenarios
          </Item>
          <Item to="/control/quality" icon={<ShieldCheck aria-hidden />}>
            Data quality
          </Item>
          <Item to="/control/connections" icon={<Plug aria-hidden />}>
            Connections
          </Item>
          <Item to="/control/runs" icon={<History aria-hidden />}>
            Run history
          </Item>
          <Item to="/control/extract" icon={<PackageCheck aria-hidden />}>
            Source extract
          </Item>
        </Group>

        <Group
          label={
            <span className="flex items-center gap-2 text-ink">
              <span className="grid size-5 place-items-center rounded-[5px] bg-navy font-display text-[11px] font-semibold text-white" aria-hidden>
                D
              </span>
              <span className="flex min-w-0 flex-col leading-tight">
                <span className="truncate text-sm font-semibold">Harborline Tracker</span>
                <span className="truncate text-xs text-muted">Project DATA</span>
              </span>
            </span>
          }
        >
          <Item to="/tracker/timeline" icon={<ChartGantt aria-hidden />}>
            Timeline
          </Item>
          <Item to="/tracker/backlog" icon={<ListTodo aria-hidden />}>
            Backlog
          </Item>
          <Item to="/tracker/board" icon={<SquareKanban aria-hidden />}>
            Board
          </Item>
          <Item to="/tracker/reports" icon={<ChartLine aria-hidden />}>
            Reports
          </Item>
          <Item to="/tracker/issues" icon={<ListFilter aria-hidden />}>
            Issues
          </Item>
          <Item to="/tracker/my-work" icon={<UserRound aria-hidden />} count={overdue} countLabel={`${overdue} overdue`}>
            My work
          </Item>
        </Group>

        <Group label="Company">
          <Item to="/team" icon={<UsersRound aria-hidden />}>
            Team
          </Item>
        </Group>

        <Group label="Reference">
          <External href={withBase('/handbook/')} icon={<BookOpen aria-hidden />}>
            Handbook
          </External>
          {import.meta.env.VITE_DEMO === '1' ? null : (
            <External href="/docs" icon={<Braces aria-hidden />}>
              API docs
            </External>
          )}
        </Group>
      </div>
    </nav>
  );
}
