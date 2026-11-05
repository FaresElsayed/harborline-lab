import { useEffect, type ReactNode } from 'react';
import { Link, useNavigate } from 'react-router';
import clsx from 'clsx';
import { ArrowUpRight } from 'lucide-react';
import { useStatus } from '@/api/queries';
import { buttonClasses } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/field';
import { Pill } from '@/components/ui/misc';
import { Tooltip } from '@/components/ui/overlay';
import { levelOf, nextTicket, useTracker } from '@/features/tracker/data';
import { NextTicketCard, YouAreHere } from '@/features/tracker/here';
import { handbookUrl, REPO_URL } from '@/lib/base';
import { fmtDay } from '@/lib/dates';
import { markFinished, setStepDone, STEP_IDS, useStartPath, type StepId } from './path';

const isDemo = import.meta.env.VITE_DEMO === '1';

type Dest = { label: string; to: string } | { label: string; href: string };

interface Step {
  title: string;
  time?: string;
  body: ReactNode;
  links: Dest[];
}

function DestLink({ dest, primary }: { dest: Dest; primary?: boolean }) {
  const cls = buttonClasses(primary ? 'primary' : 'secondary', 'sm');
  if ('to' in dest)
    return (
      <Link to={dest.to} className={cls}>
        {dest.label}
      </Link>
    );
  return (
    <a href={dest.href} target="_blank" rel="noreferrer" className={cls}>
      {dest.label}
      <ArrowUpRight aria-hidden />
      <span className="sr-only">(opens in a new tab)</span>
    </a>
  );
}

function useSteps(): Record<StepId, Step> {
  const t = useTracker();
  const status = useStatus();
  const s = status.data;
  const next = nextTicket(t.issues, t.me);
  const building = s?.state === 'building' ? Math.round((s.build?.progress ?? 0) * 100) : null;

  return {
    setup: isDemo
      ? {
          title: 'Install Harborline on your computer',
          time: 'About 40 min',
          body: 'This page is the demo: sample data, nothing saved, nothing to connect to. The real thing runs on your own computer in Docker, and one setup file does the installing, on Windows or Mac.',
          links: [{ label: 'Get it on GitHub', href: REPO_URL }],
        }
      : {
          title: 'Setup finished',
          body:
            building != null
              ? `The setup file has done its part. Your data is still building (${building}%), so carry on with the next steps while it runs.`
              : 'The setup file installed everything and built your data. If it stopped partway, run it again: it carries on from where it stopped.',
          links: [
            { label: 'Check the data', to: '/control' },
            { label: 'Known issues', href: handbookUrl('known-issues.html') },
          ],
        },
    brief: {
      title: 'Read the project brief',
      time: '30 min',
      body: 'The company, who’s who, the rules and the eight projects: the brief you’d get on day one.',
      links: [{ label: 'Open the brief', href: handbookUrl('project-brief.html') }],
    },
    data: {
      title: 'Connect to the data',
      time: '20 min',
      body: isDemo
        ? 'Every address and password is on the Connections page. In the demo nothing answers at them; once installed, you run a first query against each source.'
        : 'Every address and password is on the Connections page. Run a first query against each source so you know they all answer.',
      links: [
        { label: 'Open Connections', to: '/control/connections' },
        { label: 'Try each source system', href: handbookUrl('getting-started.html#connect') },
      ],
    },
    platform: {
      title: 'Sign up for a cloud platform',
      time: '10 min',
      body: 'Databricks Free Edition costs nothing and does not expire. Snowflake’s trial ends after 30 days. Compare them, pick one and sign up.',
      links: [{ label: 'Compare and sign up', href: handbookUrl('getting-started.html#cloud') }],
    },
    date: {
      title: 'Set your start date',
      body: isDemo
        ? 'Every due date counts from it. In this sample it’s already set, two sprints back; you’d pick your own.'
        : t.configured
          ? `Set to ${fmtDay(t.meta.data?.start_date, true)}. Every due date counts from it.${t.active ? '' : ` ${t.nextFuture?.name ?? 'The next sprint'} hasn’t started yet: start it from the backlog.`}`
          : 'Every due date counts from it. The setup file usually asks, but it hasn’t been set here yet.',
      links: !t.configured
        ? [{ label: 'Set the date', to: '/tracker' }]
        : t.active
          ? [{ label: 'See your 32 weeks', to: '/tracker/timeline' }]
          : [{ label: 'Open the backlog', to: '/tracker/backlog' }],
    },
    nadia: {
      title: 'Meet Nadia Petrov',
      time: '5 min',
      body: 'Your team lead, and the one colleague who knows data engineering. She coaches and reviews but won’t write it for you. A good first question: “What should I do first this sprint?”',
      links: [{ label: 'Open Nadia’s page', to: '/team/nadia-petrov' }],
    },
    ticket: {
      title: 'Start your first ticket',
      body: 'Open it, read the acceptance criteria, and move it to In Progress. That’s the routine from here on: one ticket at a time, earliest due first.',
      links: [
        next ? { label: `Open ${next.key}`, to: `/tracker/issue/${next.key}` } : { label: 'Open the board', to: '/tracker/board' },
        { label: 'A normal day', href: handbookUrl('project-brief.html#tracker-day') },
      ],
    },
  };
}

function StepRow({ id, n, step, done, auto, current, onChange }: { id: StepId; n: number; step: Step; done: boolean; auto: boolean; current: boolean; onChange: (v: boolean) => void }) {
  const box = <Checkbox id={`step-${id}`} checked={done} disabled={auto} onCheckedChange={onChange} label={step.title} className={clsx('mt-0.5', auto && 'cursor-default opacity-80')} />;
  return (
    <li className="flex gap-3 border-t border-rule py-3.5 first:border-t-0 first:pt-1">
      {auto ? (
        <Tooltip content="Ticked by itself: the app can see this is done">
          <span className="inline-flex h-fit">{box}</span>
        </Tooltip>
      ) : (
        box
      )}
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <label htmlFor={`step-${id}`} className={clsx('text-md font-semibold', auto ? 'cursor-default' : 'cursor-pointer', done ? 'text-muted' : 'text-ink')}>
            <span className="num font-normal text-muted">{n}.</span> {step.title}
          </label>
          {step.time ? <span className="num text-sm text-muted">{step.time}</span> : null}
          {current ? <Pill tone="accent">Next</Pill> : null}
        </div>
        <p className="mt-0.5 max-w-[62ch] text-base text-muted">{step.body}</p>
        <div className="mt-2 flex flex-wrap gap-2">
          {step.links.map((dest, i) => (
            <DestLink key={dest.label} dest={dest} primary={current && i === 0} />
          ))}
        </div>
      </div>
    </li>
  );
}

const inline = 'font-medium text-accent-strong hover:underline';

function Answer({ q, children }: { q: string; children: ReactNode }) {
  return (
    <div className="border-t border-rule py-3 first:border-t-0">
      <dt className="text-base font-semibold text-ink">{q}</dt>
      <dd className="mt-0.5 text-base text-muted">{children}</dd>
    </div>
  );
}

function Help({ href, children }: { href: string; children: ReactNode }) {
  return (
    <a href={handbookUrl(href)} target="_blank" rel="noreferrer" className={inline}>
      {children}
    </a>
  );
}

function Answers() {
  const t = useTracker();
  const hasLevels = t.issues.some((i) => levelOf(i) != null);
  return (
    <section aria-labelledby="answers" className="mt-2">
      <h2 id="answers" className="text-md font-semibold text-ink">
        When you’re wondering
      </h2>
      <dl className="mt-1">
        <Answer q="What do I do each day?">
          Open the{' '}
          <Link to="/tracker/board" className={inline}>
            board
          </Link>
          , take the earliest-due ticket and move it to In Progress. Branch, work, open a pull request and move it to In Review; merge when CI is green and move it to Done. Log your hours and leave a note on what you decided.
        </Answer>
        <Answer q="Who do I ask when I’m stuck?">
          Every ticket has a Stuck? line naming them. What the business wants: the person who asked for the ticket. How to build it:{' '}
          <Link to="/team/nadia-petrov" className={inline}>
            Nadia Petrov
          </Link>
          . Not sure:{' '}
          <Link to="/team/channel" className={inline}>
            #data-help
          </Link>{' '}
          tells you who.
        </Answer>
        <Answer q="How do I connect to the data?">
          <Link to="/control/connections" className={inline}>
            Connections
          </Link>{' '}
          has the addresses, logins and ready-to-paste snippets. <Help href="source-systems.html">Source systems</Help> says how each one behaves.
        </Answer>
        <Answer q="How do I know a project is done?">
          Every ticket under its epic is Done,{' '}
          <Link to="/team/iris-novak" className={inline}>
            Iris Novak
          </Link>{' '}
          has signed off its dashboards against a{' '}
          <Link to="/control/extract" className={inline}>
            source extract
          </Link>
          , you’ve tagged a release, and you’ve given the requester a short readout. <Help href="project-brief.html#evaluation">How the work is judged</Help> is the check to run at the end.
        </Answer>
        <Answer q="What do Build, Batch and Stream do?">
          <strong className="font-medium text-ink">Build</strong> makes the whole history from scratch and wipes what was there; setup already did it. <strong className="font-medium text-ink">Batch</strong> jumps the clock forward and applies those changes in one go, for testing scheduled loads.{' '}
          <strong className="font-medium text-ink">Stream</strong> applies small changes every few seconds until you stop it, for testing CDC. All three are on the{' '}
          <Link to="/control" className={inline}>
            Control Center
          </Link>
          .
        </Answer>
        <Answer q="Something broke. Now what?">
          Look it up in <Help href="known-issues.html">Known issues</Help>, or paste the error to{' '}
          <Link to="/team/kevin-ohara" className={inline}>
            Kevin O’Hara
          </Link>
          , who does IT help. Running the setup file again is always safe: it skips what’s fine and fixes what it can.
        </Answer>
        {isDemo ? null : (
          <Answer q="How do I update?">
            Run the setup file again. It updates the app and keeps your data, tickets and progress. A note appears at the bottom of the sidebar when a new version is out.
          </Answer>
        )}
        {hasLevels ? <Answer q="What does Level 2 on a ticket mean?">How hard it is. Tickets start easy and get harder: Level 1 is the easiest, Level 5 the hardest.</Answer> : null}
      </dl>
    </section>
  );
}

export default function StartPage() {
  const navigate = useNavigate();
  const path = useStartPath();
  const steps = useSteps();

  // Ticking the last step is what ends it: from then on the app opens on the Control Center.
  useEffect(() => {
    if (path.allDone && !path.finished) markFinished();
  }, [path.allDone, path.finished]);

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-[1160px] px-6 pb-14 pt-6 max-sm:px-4">
        <h1 className="figure text-[1.75rem] font-semibold leading-8 tracking-[-0.01em] text-ink">Start here</h1>
        <p className="mt-1 max-w-[70ch] text-md text-muted">
          {isDemo
            ? 'This is what a new data engineer at Harborline Logistics works through in week one. You’re looking at the demo, so step 1 is the one that makes it real.'
            : 'You’ve joined Harborline Logistics, a simulated 3PL, as its first data engineer. Work down this list once, in order. After that, your days run on the board.'}
        </p>

        <div className="mt-3 max-w-[420px]">
          <div className="num flex justify-between text-sm text-muted">
            <span>
              {path.doneCount} of {path.total} done
            </span>
          </div>
          <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-active" role="progressbar" aria-label="Start here progress" aria-valuemin={0} aria-valuemax={path.total} aria-valuenow={path.doneCount}>
            <div className="h-full origin-left rounded-full bg-good transition-transform duration-300 ease-out" style={{ transform: `scaleX(${path.doneCount / path.total})` }} />
          </div>
        </div>

        {path.allDone ? (
          <div role="status" className="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-m border border-good/30 bg-good-bg px-4 py-3">
            <p className="min-w-0 flex-1 basis-72 text-base text-ink">That’s the start done. The app now opens on the Control Center, and this page stays here if you want it.</p>
            <Link to="/tracker/board" className={buttonClasses('secondary')}>
              Open the board
            </Link>
          </div>
        ) : null}

        <div className="mt-6 grid grid-cols-[minmax(0,1fr)_340px] grid-rows-[auto_1fr] items-start gap-x-10 gap-y-6 max-[1000px]:grid-cols-1 max-[1000px]:grid-rows-none">
          <div className="min-w-0">
            <ol aria-label="Your first steps">
              {STEP_IDS.map((id, i) => (
                <StepRow key={id} id={id} n={i + 1} step={steps[id]} done={path.done(id)} auto={path.isAuto(id)} current={path.current === id} onChange={(v) => setStepDone(id, v)} />
              ))}
            </ol>
            {!path.allDone && !path.finished ? (
              <button
                type="button"
                className="mt-2 rounded-s text-left text-sm text-muted transition-colors hover:text-ink hover:underline"
                onClick={() => {
                  markFinished();
                  navigate('/control');
                }}
              >
                Know your way around? Open the Control Center from now on
              </button>
            ) : null}
          </div>
          <aside className="row-span-2 flex min-w-0 flex-col gap-4 min-[1001px]:sticky min-[1001px]:top-6 max-[1000px]:row-span-1" aria-label="Where you stand">
            <NextTicketCard />
            <YouAreHere />
          </aside>
          <div className="min-w-0">
            <Answers />
          </div>
        </div>
      </div>
    </div>
  );
}
