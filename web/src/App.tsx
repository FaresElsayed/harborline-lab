import { lazy, Suspense, type CSSProperties } from 'react';
import { createBrowserRouter, Navigate, RouterProvider } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MotionConfig } from 'motion/react';
import { Toaster } from 'sonner';
import { AlertTriangle, CheckCircle2, Info, XCircle } from 'lucide-react';
import { ApiError } from '@/api/client';
import { TooltipProvider } from '@/components/ui/overlay';
import { useTheme } from '@/lib/theme';
import { AppShell } from '@/shell/AppShell';
import { RouteError, NotFound, PageFallback } from '@/shell/RouteStates';

const ControlLayout = lazy(() => import('@/features/control/ControlLayout'));
const ControlOverview = lazy(() => import('@/features/control/Overview'));
const ScenariosPage = lazy(() => import('@/features/control/Scenarios'));
const QualityPage = lazy(() => import('@/features/control/Quality'));
const ConnectionsPage = lazy(() => import('@/features/control/Connections'));
const RunsPage = lazy(() => import('@/features/control/Runs'));
const ExtractPage = lazy(() => import('@/features/control/Extract'));
const TeamPage = lazy(() => import('@/features/team/Team'));
const TrackerLayout = lazy(() => import('@/features/tracker/TrackerLayout'));
const BoardPage = lazy(() => import('@/features/tracker/Board'));
const BacklogPage = lazy(() => import('@/features/tracker/Backlog'));
const TimelinePage = lazy(() => import('@/features/tracker/Timeline'));
const ReportsPage = lazy(() => import('@/features/tracker/Reports'));
const IssuesPage = lazy(() => import('@/features/tracker/Issues'));
const MyWorkPage = lazy(() => import('@/features/tracker/MyWork'));
const IssuePage = lazy(() => import('@/features/tracker/IssuePage'));

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: (count, err) => !(err instanceof ApiError && err.status >= 400 && err.status < 500) && count < 2,
      refetchOnWindowFocus: true,
      staleTime: 5_000,
    },
  },
});

const page = (el: React.ReactNode) => <Suspense fallback={<PageFallback />}>{el}</Suspense>;

const router = createBrowserRouter([
  {
    element: <AppShell />,
    errorElement: <RouteError />,
    children: [
      { index: true, element: <Navigate to="/control" replace /> },
      {
        path: 'control',
        element: page(<ControlLayout />),
        children: [
          { index: true, element: page(<ControlOverview />) },
          { path: 'scenarios', element: page(<ScenariosPage />) },
          { path: 'quality', element: page(<QualityPage />) },
          { path: 'connections', element: page(<ConnectionsPage />) },
          { path: 'runs', element: page(<RunsPage />) },
          { path: 'extract', element: page(<ExtractPage />) },
        ],
      },
      {
        path: 'tracker',
        element: page(<TrackerLayout />),
        children: [
          { index: true, element: <Navigate to="board" replace /> },
          { path: 'board', element: page(<BoardPage />) },
          { path: 'backlog', element: page(<BacklogPage />) },
          { path: 'timeline', element: page(<TimelinePage />) },
          { path: 'reports', element: page(<ReportsPage />) },
          { path: 'issues', element: page(<IssuesPage />) },
          { path: 'my-work', element: page(<MyWorkPage />) },
          { path: 'issue/:key', element: page(<IssuePage />) },
        ],
      },
      { path: 'team/*', element: page(<TeamPage />) },
      { path: '*', element: <NotFound /> },
    ],
  },
], { basename: import.meta.env.BASE_URL.replace(/\/$/, '') || '/' });

const toastIcons = {
  success: <CheckCircle2 className="size-4 text-good" aria-hidden />,
  error: <XCircle className="size-4 text-bad" aria-hidden />,
  warning: <AlertTriangle className="size-4 text-warn" aria-hidden />,
  info: <Info className="size-4 text-info" aria-hidden />,
};

function Toasts() {
  const { resolved } = useTheme();
  return (
    <Toaster
      theme={resolved}
      position="bottom-right"
      icons={toastIcons}
      gap={8}
      visibleToasts={4}
      style={
        {
          '--normal-bg': 'var(--sheet)',
          '--normal-border': 'var(--line)',
          '--normal-text': 'var(--ink)',
          '--border-radius': '10px',
          fontFamily: 'var(--hl-font-sans)',
        } as CSSProperties
      }
      toastOptions={{
        classNames: {
          toast: '!shadow-float !text-base !gap-2.5',
          title: '!font-semibold',
          description: '!text-muted !text-sm num',
          actionButton: '!bg-accent !text-on-accent !font-medium !rounded-m',
        },
      }}
    />
  );
}

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <MotionConfig reducedMotion="user">
        <TooltipProvider delayDuration={450} skipDelayDuration={300}>
          <RouterProvider router={router} />
          <Toasts />
        </TooltipProvider>
      </MotionConfig>
    </QueryClientProvider>
  );
}
