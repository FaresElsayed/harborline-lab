import { Link } from 'react-router';
import clsx from 'clsx';
import { Keyboard, Menu as MenuIcon, Monitor, Moon, PanelLeft, Search, Sun } from 'lucide-react';
import { useStatus } from '@/api/queries';
import { useLive } from '@/api/live';
import type { SimStatus } from '@/api/types';
import { IconButton } from '@/components/ui/button';
import { Dot, type Tone } from '@/components/ui/misc';
import { Kbd, Menu, MenuContent, MenuLabel, MenuRadioGroup, MenuRadioItem, MenuTrigger, Tooltip } from '@/components/ui/overlay';
import { fmtUtc } from '@/lib/dates';
import { modKey } from '@/lib/hotkeys';
import { setTheme, useTheme, type ThemePref } from '@/lib/theme';
import { toggleNavCollapsed, ui } from '@/lib/store';

export function stateView(s: SimStatus | undefined, offline: boolean): { tone: Tone; label: string; pulse?: boolean } {
  if (offline) return { tone: 'bad', label: 'Simulator offline' };
  if (!s) return { tone: 'neutral', label: 'Connecting…' };
  switch (s.state) {
    case 'streaming':
      return { tone: 'accent', label: `Streaming ${s.stream.speed}×`, pulse: true };
    case 'building':
      return { tone: 'warn', label: `Building ${Math.round((s.build?.progress ?? 0) * 100)}%`, pulse: true };
    case 'running_batch':
      return { tone: 'info', label: 'Batch running', pulse: true };
    case 'error':
      return { tone: 'bad', label: 'Simulator error' };
    default:
      return { tone: 'good', label: s.sim_clock ? 'Idle' : 'Not built' };
  }
}

function SimChip() {
  const status = useStatus();
  const link = useLive((s) => s.link);
  const offline = status.isError && !status.data;
  const v = stateView(status.data, offline);
  return (
    <Tooltip content={link === 'open' ? 'Live: updates arrive as they happen' : 'Reconnecting to live updates; polling every 5 s'}>
      <Link
        to="/control"
        className="flex h-8 items-center gap-2 whitespace-nowrap rounded-m px-2.5 text-sm text-muted transition-colors hover:bg-hover hover:text-ink"
      >
        <Dot tone={v.tone} pulse={v.pulse} />
        <span className="font-medium text-ink">{v.label}</span>
        {status.data?.sim_clock ? (
          <span className="num hidden whitespace-nowrap min-[1400px]:inline">
            Sim {fmtUtc(status.data.sim_clock, 'MMM d, HH:mm')} UTC
          </span>
        ) : null}
      </Link>
    </Tooltip>
  );
}

const THEME_ICON: Record<ThemePref, typeof Sun> = { light: Sun, dark: Moon, system: Monitor };

function ThemeMenu() {
  const { pref } = useTheme();
  const Icon = THEME_ICON[pref];
  return (
    <Menu>
      <MenuTrigger asChild>
        <IconButton label="Theme">
          <Icon aria-hidden />
        </IconButton>
      </MenuTrigger>
      <MenuContent className="min-w-40">
        <MenuLabel>Theme</MenuLabel>
        <MenuRadioGroup value={pref} onValueChange={(v) => setTheme(v as ThemePref)}>
          <MenuRadioItem value="light">Light</MenuRadioItem>
          <MenuRadioItem value="dark">Dark</MenuRadioItem>
          <MenuRadioItem value="system">Match system</MenuRadioItem>
        </MenuRadioGroup>
      </MenuContent>
    </Menu>
  );
}

export function TopBar({ sidebarHidden }: { sidebarHidden: boolean }) {
  return (
    <header className="grid h-12 shrink-0 grid-cols-[1fr_minmax(0,560px)_1fr] items-center gap-3 pr-3">
      <div className="flex items-center gap-1 pl-2">
        {sidebarHidden ? (
          <>
            <IconButton label="Open navigation" className="min-[1180px]:hidden" onClick={() => ui.set({ navOpen: true })}>
              <MenuIcon aria-hidden />
            </IconButton>
            <IconButton label="Show sidebar" tip="Show sidebar  [" className="max-[1179px]:hidden" onClick={toggleNavCollapsed}>
              <PanelLeft aria-hidden />
            </IconButton>
          </>
        ) : null}
      </div>

      <button
        type="button"
        onClick={() => ui.set({ palette: true })}
        className={clsx(
          'flex h-8 w-full items-center gap-2 rounded-m border border-line bg-sheet px-2.5 text-base text-faint',
          'transition-colors duration-150 hover:border-control hover:text-muted',
        )}
      >
        <Search className="size-4 shrink-0" aria-hidden />
        <span className="flex-1 truncate text-left">Search issues, pages and actions</span>
        <Kbd>{modKey}</Kbd>
        <Kbd className="-ml-1">K</Kbd>
      </button>

      <div className="flex items-center justify-end gap-1">
        <SimChip />
        <ThemeMenu />
        <IconButton label="Keyboard shortcuts" tip="Keyboard shortcuts  ?" onClick={() => ui.set({ shortcuts: true })} className="max-md:hidden">
          <Keyboard aria-hidden />
        </IconButton>
      </div>
    </header>
  );
}
