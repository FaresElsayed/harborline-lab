import { lazy, Suspense, useEffect, useSyncExternalStore } from 'react';
import { Outlet, useLocation, useNavigate } from 'react-router';
import { Dialog as RDialog } from 'radix-ui';
import { useSimEvents } from '@/api/live';
import { useHotkeys } from '@/lib/hotkeys';
import { toggleNavCollapsed, ui } from '@/lib/store';
import { CommandPalette } from './CommandPalette';
import { ShortcutsDialog } from './ShortcutsDialog';
import { Sidebar } from './Sidebar';
import { TopBar } from './TopBar';

const CreateIssueDialog = lazy(() => import('@/features/tracker/CreateIssue'));
const DevDataToggle =
  import.meta.env.VITE_MOCK === '1' && import.meta.env.VITE_DEMO !== '1' ? lazy(() => import('./DevDataToggle').then((m) => ({ default: m.DevDataToggle }))) : null;
const DemoNotice = import.meta.env.VITE_DEMO === '1' ? lazy(() => import('./DemoNotice').then((m) => ({ default: m.DemoNotice }))) : null;

function useMedia(query: string) {
  return useSyncExternalStore(
    (l) => {
      const m = window.matchMedia(query);
      m.addEventListener('change', l);
      return () => m.removeEventListener('change', l);
    },
    () => window.matchMedia(query).matches,
  );
}

/**
 * The framed sheet: sidebar and top bar share the Fog frame with no rule between them;
 * the page is a white sheet inset 8px from the window's right and bottom edges, 1px edge, no shadow.
 */
export function AppShell() {
  useSimEvents();
  const navigate = useNavigate();
  const location = useLocation();
  const wide = useMedia('(min-width: 1180px)');
  const collapsed = ui.use((s) => s.navCollapsed);
  const drawer = ui.use((s) => s.navOpen);
  const createOpen = ui.use((s) => s.createIssue !== null);
  const showSidebar = wide && !collapsed;

  useHotkeys({
    'mod+k': () => ui.set((s) => ({ palette: !s.palette })),
    c: () => ui.set({ createIssue: {} }),
    '/': () => navigate('/tracker/issues', { state: { focusSearch: Date.now() } }),
    '[': () => (wide ? toggleNavCollapsed() : ui.set((s) => ({ navOpen: !s.navOpen }))),
    '?': () => ui.set({ shortcuts: true }),
  });

  // Close the drawer and reset the keyboard focus target on navigation.
  useEffect(() => {
    ui.set({ navOpen: false });
  }, [location.pathname]);

  return (
    <div className="flex h-dvh overflow-hidden bg-frame">
      <a
        href="#main"
        className="fixed left-3 top-2 z-[70] -translate-y-16 rounded-m bg-ink px-3 py-1.5 text-sm text-sheet focus-visible:translate-y-0"
      >
        Skip to content
      </a>
      {showSidebar ? (
        <div className="w-[232px] shrink-0">
          <Sidebar />
        </div>
      ) : null}

      <RDialog.Root open={!showSidebar && drawer} onOpenChange={(v) => ui.set({ navOpen: v })}>
        <RDialog.Portal>
          <RDialog.Overlay className="fixed inset-0 z-40 bg-[rgb(8_19_31/0.32)] data-[state=open]:animate-[fade-in_160ms_ease-out]" />
          <RDialog.Content className="drawer fixed inset-y-0 left-0 z-50 w-[264px] bg-frame shadow-float outline-none">
            <RDialog.Title className="sr-only">Navigation</RDialog.Title>
            <Sidebar inDrawer />
          </RDialog.Content>
        </RDialog.Portal>
      </RDialog.Root>

      <div className="relative flex min-w-0 flex-1 flex-col">
        <TopBar sidebarHidden={!showSidebar} />
        <main
          id="main"
          tabIndex={-1}
          className="relative mb-2 mr-2 min-h-0 flex-1 overflow-hidden rounded-sheet border border-line bg-sheet outline-none max-[640px]:mx-0 max-[640px]:mb-0 max-[640px]:rounded-none"
          style={showSidebar ? undefined : { marginLeft: 8 }}
        >
          <Outlet />
        </main>
        {DemoNotice ? (
          <Suspense fallback={null}>
            <DemoNotice />
          </Suspense>
        ) : null}
      </div>

      {DevDataToggle ? (
        <Suspense fallback={null}>
          <DevDataToggle />
        </Suspense>
      ) : null}
      <CommandPalette />
      <ShortcutsDialog />
      {createOpen ? (
        <Suspense fallback={null}>
          <CreateIssueDialog />
        </Suspense>
      ) : null}
    </div>
  );
}
