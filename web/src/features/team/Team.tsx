import { useMemo, useState, type ReactNode } from 'react';
import { Link, NavLink, useParams } from 'react-router';
import clsx from 'clsx';
import { motion } from 'motion/react';
import { ArrowLeft, ArrowUpRight, ClipboardCopy, Download, Hash, Search } from 'lucide-react';
import { useTeamLinks } from '@/api/queries';
import { Button, buttonClasses } from '@/components/ui/button';
import { Input } from '@/components/ui/field';
import { copyText, CopyButton, Empty } from '@/components/ui/misc';
import team from './people.generated.json';

/** Written by simulator/harborline/tools/build_team_page.py from team/personas.yaml, so this page and the prompts agree. */
interface Handoff {
  topic: string;
  id: string;
  name: string;
}
interface Person {
  id: string;
  name: string;
  first: string;
  title: string;
  dept: string;
  location: string;
  tenure: string;
  photo: string;
  does: string;
  owns: string[];
  ask: string[];
  openers: string[];
  notFor: string[];
  handoffs: Handoff[];
  personality: string;
  life: string;
  prompt: string;
  needsZip: boolean;
}

const groups = team as { title: string; people: Person[] }[];
const everyone = groups.flatMap((g) => g.people);
const byId = new Map(everyone.map((p) => [p.id, p]));

// The demo has no backend, so its build puts the zip next to the page.
const CHAT_ZIP = import.meta.env.VITE_MOCK === '1' ? `${import.meta.env.BASE_URL}harborline-chat.zip` : '/api/handbook/chat-zip';

const sentence = (s: string) => (s ? `${s[0].toUpperCase()}${s.slice(1)}.` : '');

function Face({ person, size, className }: { person: Pick<Person, 'photo'>; size: number; className?: string }) {
  const box = { width: size, height: size };
  if (person.photo) {
    return <img src={person.photo} alt="" width={size} height={size} className={clsx('shrink-0 rounded-full', className)} style={box} />;
  }
  return (
    <span aria-hidden className={clsx('grid shrink-0 place-items-center rounded-full bg-navy text-white', className)} style={box}>
      <Hash style={{ width: size * 0.45, height: size * 0.45 }} />
    </span>
  );
}

/** What made this person match, shown instead of their title when the search found them by topic. */
function matchedTopic(person: Person, words: string[]) {
  const about = [...person.ask, ...person.owns, ...person.openers];
  return about.find((line) => words.every((w) => line.toLowerCase().includes(w)));
}

function matches(person: Person, words: string[]) {
  const text = [person.name, person.title, person.dept, person.does, ...person.owns, ...person.ask, ...person.openers].join(' ').toLowerCase();
  return words.every((w) => text.includes(w));
}

function Roster({ selected, onlyOnWide }: { selected: Person; onlyOnWide: boolean }) {
  const [query, setQuery] = useState('');
  const words = useMemo(() => query.toLowerCase().split(/\s+/).filter(Boolean), [query]);
  const shown = groups.map((g) => ({ ...g, people: g.people.filter((p) => matches(p, words)) })).filter((g) => g.people.length);

  return (
    <div className={clsx('flex min-h-0 flex-col border-r border-line max-[900px]:border-r-0', onlyOnWide && 'max-[900px]:hidden')}>
      <div className="px-4 pb-3 pt-6">
        <h1 className="figure text-[1.75rem] font-semibold leading-8 tracking-[-0.01em] text-ink">Team</h1>
        <p className="mt-1 text-md text-muted">Who does what, and what to ask them.</p>
        <div className="mt-3">
          <Input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            leading={<Search aria-hidden />}
            placeholder="Find a person or topic"
            aria-label="Find a person or topic"
          />
        </div>
      </div>
      <nav aria-label="Colleagues" className="scroll-thin min-h-0 flex-1 overflow-y-auto px-2 pb-4">
        {shown.map((group) => (
          <div key={group.title} className="mt-4 first:mt-0">
            <h2 className="mb-1 px-2 text-sm text-muted">{group.title}</h2>
            <ul className="flex flex-col gap-0.5">
              {group.people.map((person) => {
                const topic = words.length ? matchedTopic(person, words) : undefined;
                return (
                  <li key={person.id}>
                    <NavLink
                      to={`/team/${person.id}`}
                      aria-current={person.id === selected.id ? 'true' : undefined}
                      className={clsx(
                        'flex items-center gap-2.5 rounded-m px-2 py-1.5 transition-colors duration-150',
                        // On a phone the list stands alone until someone is picked, so nothing is selected yet.
                        person.id === selected.id ? (onlyOnWide ? 'bg-active' : 'hover:bg-hover min-[901px]:bg-active') : 'hover:bg-hover',
                      )}
                    >
                      <Face person={person} size={36} />
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-base font-medium text-ink">{person.name}</span>
                        <span className="block truncate text-sm text-muted">{topic ?? person.title}</span>
                      </span>
                    </NavLink>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
        {!shown.length ? (
          <Empty className="mt-2">
            Nobody matches that. Try another word, or ask <Link to="/team/channel" className="underline">#data-help</Link>.
          </Empty>
        ) : null}
      </nav>
    </div>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section>
      <h3 className="mb-2 text-base font-semibold text-ink">{title}</h3>
      {children}
    </section>
  );
}

function Lines({ items }: { items: string[] }) {
  return (
    <ul className="divide-y divide-line border-y border-line">
      {items.map((item) => (
        <li key={item} className="py-2 text-base text-ink">
          {item}
        </li>
      ))}
    </ul>
  );
}

function Profile({ person, savedChat, hiddenOnNarrow }: { person: Person; savedChat?: string; hiddenOnNarrow: boolean }) {
  const place = [person.dept, person.location, person.tenure].filter(Boolean).join(' · ');
  return (
    <div key={person.id} className={clsx('scroll-thin min-h-0 overflow-y-auto', hiddenOnNarrow && 'max-[900px]:hidden')}>
      <motion.article
        initial={{ opacity: 0, y: 6 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.18, ease: [0.2, 0, 0, 1] }}
        className="mx-auto max-w-[1040px] px-8 pb-12 pt-6 max-sm:px-4"
      >
        <Link to="/team" className="mb-4 inline-flex items-center gap-1.5 text-base text-muted hover:text-ink min-[901px]:hidden">
          <ArrowLeft aria-hidden className="size-4" />
          Team
        </Link>

        <header className="flex flex-wrap items-center gap-x-5 gap-y-3">
          <Face person={person} size={88} />
          <div className="min-w-0 flex-1 basis-64">
            <h2 className="figure text-[1.75rem] font-semibold leading-8 tracking-[-0.01em] text-ink">{person.name}</h2>
            <p className="mt-1 text-md text-ink">{person.title}</p>
            {place ? <p className="mt-0.5 text-base text-muted">{place}</p> : null}
          </div>
          <div className="flex shrink-0 flex-wrap items-center gap-2">
            {savedChat ? (
              <a href={savedChat} target="_blank" rel="noreferrer" className={buttonClasses('secondary', 'md')}>
                Your chat
                <ArrowUpRight aria-hidden />
              </a>
            ) : null}
            <Button
              variant="secondary"
              icon={<ClipboardCopy aria-hidden />}
              onClick={() =>
                void copyText(
                  person.prompt,
                  person.needsZip
                    ? `${person.first}'s prompt copied. Attach harborline-chat.zip in the same message.`
                    : `${person.first}'s prompt copied. Paste it into a new AI chat.`,
                )
              }
            >
              Copy prompt
            </Button>
          </div>
        </header>

        {person.needsZip ? (
          <div role="note" className="mt-5 flex flex-wrap items-center gap-x-4 gap-y-2 rounded-m border border-warn/30 bg-warn-bg px-4 py-3">
            <p className="min-w-0 flex-1 basis-72 text-base text-ink">
              <strong className="font-semibold">Send the zip with the prompt.</strong> Start a new chat, attach{' '}
              <code className="font-mono text-sm">harborline-chat.zip</code>, paste {person.first}’s prompt and send them together. Without the
              zip, {person.first} can’t see the handbook, tickets or brand kit, and will guess.
            </p>
            <a href={CHAT_ZIP} download className={buttonClasses('secondary', 'md')}>
              <Download aria-hidden />
              Download zip
            </a>
          </div>
        ) : null}

        <p className="mt-5 max-w-[65ch] text-lg leading-7 text-ink">{sentence(person.does)}</p>
        {person.personality ? <p className="mt-2 max-w-[65ch] text-md text-muted">{person.personality}</p> : null}

        <div className="mt-8 grid grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)] gap-x-10 gap-y-8 max-[1180px]:grid-cols-1">
          <div className="flex flex-col gap-8">
            <Section title="What to ask">
              <Lines items={person.ask} />
            </Section>
            <Section title="Good first questions">
              <ul className="divide-y divide-line border-y border-line">
                {person.openers.map((opener) => (
                  <li key={opener} className="flex items-center justify-between gap-3 py-1.5 text-base text-ink">
                    <span>{opener}</span>
                    <CopyButton text={opener} label="Copy question" toastLabel="Question copied" />
                  </li>
                ))}
              </ul>
            </Section>
            {person.owns.length ? (
              <Section title={`What ${person.first} owns`}>
                <Lines items={person.owns} />
              </Section>
            ) : null}
          </div>

          <div className="flex flex-col gap-8">
            <Section title="Not their area">
              <Lines items={person.notFor} />
            </Section>
            {person.handoffs.length ? (
              <Section title={`Who ${person.first} would send you to`}>
                <ul className="divide-y divide-line border-y border-line">
                  {person.handoffs.map((handoff) => {
                    const target = byId.get(handoff.id);
                    return (
                      <li key={handoff.topic}>
                        <Link to={`/team/${handoff.id}`} className="group flex items-center gap-2.5 py-1.5">
                          {target ? <Face person={target} size={28} /> : null}
                          <span className="min-w-0 flex-1">
                            <span className="block text-base font-medium text-ink group-hover:underline">{handoff.name}</span>
                            <span className="block text-sm text-muted">{sentence(handoff.topic).slice(0, -1)}</span>
                          </span>
                        </Link>
                      </li>
                    );
                  })}
                </ul>
              </Section>
            ) : null}
            {person.life ? (
              <Section title="As a person">
                <p className="max-w-[65ch] text-base text-muted">{person.life}</p>
              </Section>
            ) : null}
          </div>
        </div>
      </motion.article>
    </div>
  );
}

export default function TeamPage() {
  const id = useParams()['*'];
  const person = (id && byId.get(id)) || everyone[0];
  const links = useTeamLinks();

  return (
    <div className="grid h-full min-h-0 grid-cols-[320px_minmax(0,1fr)] max-[900px]:grid-cols-[minmax(0,1fr)]">
      <Roster selected={person} onlyOnWide={!!id} />
      <Profile key={person.id} person={person} savedChat={links.data?.links[person.id]} hiddenOnNarrow={!id} />
    </div>
  );
}
