import { useEffect, useRef, useState, type ReactNode } from 'react';
import clsx from 'clsx';
import { Tabs as RTabs } from 'radix-ui';
import { AlertTriangle, Check, Copy, RotateCw } from 'lucide-react';
import { toast } from 'sonner';
import { Button } from './button';
import { Tooltip } from './overlay';

// ---------------------------------------------------------------- panels

export function Panel({
  title,
  meta,
  actions,
  children,
  className,
  bodyClassName,
  id,
  as: Tag = 'section',
}: {
  title?: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
  id?: string;
  as?: 'section' | 'div';
}) {
  return (
    <Tag id={id} className={clsx('rounded-l border border-line bg-sheet', className)} aria-labelledby={title && id ? `${id}-title` : undefined}>
      {title || actions ? (
        <header className="flex min-h-12 items-center justify-between gap-3 px-4 pt-3">
          <div className="flex min-w-0 items-baseline gap-2">
            {title ? (
              <h2 id={id ? `${id}-title` : undefined} className="truncate text-md font-semibold text-ink">
                {title}
              </h2>
            ) : null}
            {meta ? <span className="truncate text-sm text-muted">{meta}</span> : null}
          </div>
          {actions ? <div className="flex shrink-0 items-center gap-1.5">{actions}</div> : null}
        </header>
      ) : null}
      <div className={clsx('px-4 pb-4 pt-3', bodyClassName)}>{children}</div>
    </Tag>
  );
}

export function PageHeader({
  title,
  subtitle,
  actions,
  breadcrumb,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  breadcrumb?: ReactNode;
}) {
  return (
    <div className="mb-5 flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
      <div className="min-w-0">
        {breadcrumb ? <div className="mb-1.5 text-sm text-muted">{breadcrumb}</div> : null}
        <h1 className="figure text-[1.75rem] font-semibold leading-8 tracking-[-0.01em] text-ink">{title}</h1>
        {subtitle ? <p className="mt-1 max-w-[80ch] text-md text-muted">{subtitle}</p> : null}
      </div>
      {actions ? <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
    </div>
  );
}

// ---------------------------------------------------------------- tabs (underline)

export function Tabs({
  value,
  onValueChange,
  items,
  className,
  ariaLabel,
}: {
  value: string;
  onValueChange: (v: string) => void;
  items: Array<{ value: string; label: ReactNode; count?: number }>;
  className?: string;
  ariaLabel: string;
}) {
  return (
    <RTabs.Root value={value} onValueChange={onValueChange}>
      <RTabs.List aria-label={ariaLabel} className={clsx('scroll-thin flex gap-5 overflow-x-auto border-b border-line', className)}>
        {items.map((it) => (
          <RTabs.Trigger
            key={it.value}
            value={it.value}
            className="relative -mb-px flex h-9 shrink-0 items-center gap-1.5 border-b-2 border-transparent text-base text-muted transition-colors duration-150 hover:text-ink data-[state=active]:border-accent data-[state=active]:font-medium data-[state=active]:text-ink"
          >
            {it.label}
            {it.count != null ? <span className="num text-sm text-muted">{it.count.toLocaleString('en-US')}</span> : null}
          </RTabs.Trigger>
        ))}
      </RTabs.List>
    </RTabs.Root>
  );
}

// ---------------------------------------------------------------- small marks

export type Tone = 'neutral' | 'good' | 'warn' | 'bad' | 'info' | 'accent';

const pillTones: Record<Tone, string> = {
  neutral: 'bg-active text-muted',
  good: 'bg-good-bg text-good',
  warn: 'bg-warn-bg text-warn',
  bad: 'bg-bad-bg text-bad',
  info: 'bg-info-bg text-info',
  accent: 'bg-accent-tint text-accent-strong',
};

export function Pill({ tone = 'neutral', children, className, icon }: { tone?: Tone; children: ReactNode; className?: string; icon?: ReactNode }) {
  return (
    <span
      className={clsx(
        'inline-flex h-5 flex-none items-center gap-1 whitespace-nowrap rounded-s px-1.5 text-xs font-semibold [&_svg]:size-3',
        pillTones[tone],
        className,
      )}
    >
      {icon}
      {children}
    </span>
  );
}

const dotTones: Record<Tone, string> = {
  neutral: 'bg-faint',
  good: 'bg-good',
  warn: 'bg-warn',
  bad: 'bg-bad',
  info: 'bg-info',
  accent: 'bg-accent',
};

export function Dot({ tone = 'neutral', pulse, className }: { tone?: Tone; pulse?: boolean; className?: string }) {
  return (
    <span
      aria-hidden
      className={clsx('inline-block size-2 shrink-0 rounded-full', dotTones[tone], pulse && 'motion-safe:animate-pulse-dot', className)}
    />
  );
}

export function Skeleton({ className, style }: { className?: string; style?: React.CSSProperties }) {
  return <span aria-hidden className={clsx('block rounded-s bg-active', className)} style={style} />;
}

/** A section with nothing in it keeps its frame and says so in one sentence. */
export function Empty({ children, action, className }: { children: ReactNode; action?: ReactNode; className?: string }) {
  return (
    <div
      className={clsx(
        'flex min-h-10 flex-wrap items-center justify-between gap-3 rounded-m border border-dashed border-control px-3.5 py-2.5 text-base text-muted',
        className,
      )}
    >
      <span>{children}</span>
      {action}
    </div>
  );
}

/** Error inside the block that failed: what failed, and a retry. */
export function ErrorNote({ error, onRetry, what = 'this' }: { error: unknown; onRetry?: () => void; what?: string }) {
  const msg = error instanceof Error ? error.message : String(error ?? 'Unknown error');
  return (
    <div role="alert" className="flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-m border border-bad/25 bg-bad-bg/60 px-3.5 py-2.5 text-base">
      <AlertTriangle className="size-4 shrink-0 text-bad" aria-hidden />
      <span className="min-w-0 flex-1 text-ink">
        Couldn’t load {what}. <span className="text-muted">{msg}</span>
      </span>
      {onRetry ? (
        <Button size="sm" variant="ghost" icon={<RotateCw aria-hidden />} onClick={onRetry}>
          Retry
        </Button>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------- copy

export async function copyText(text: string, label = 'Copied to clipboard') {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
  }
  toast.success(label, { duration: 1800 });
}

export function CopyButton({ text, label = 'Copy', toastLabel, className }: { text: string; label?: string; toastLabel?: string; className?: string }) {
  const [done, setDone] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  return (
    <Tooltip content={done ? 'Copied' : label}>
      <button
        type="button"
        aria-label={label}
        onClick={() => {
          void copyText(text, toastLabel ?? 'Copied to clipboard');
          setDone(true);
          clearTimeout(timer.current);
          timer.current = setTimeout(() => setDone(false), 1400);
        }}
        className={clsx(
          'press relative inline-grid size-7 shrink-0 place-items-center rounded-s text-muted transition-colors hover:bg-hover hover:text-ink',
          className,
        )}
      >
        <Copy
          aria-hidden
          className={clsx('col-start-1 row-start-1 size-3.5 transition-[opacity,transform] duration-200 ease-out', done && 'scale-50 opacity-0')}
        />
        <Check
          aria-hidden
          className={clsx('col-start-1 row-start-1 size-3.5 text-good transition-[opacity,transform] duration-200 ease-out', !done && 'scale-50 opacity-0')}
        />
      </button>
    </Tooltip>
  );
}

/** Download through fetch so it works with the mock API and with real endpoints alike. */
export async function download(url: string, filename: string) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Download failed (${res.status})`);
  const blob = await res.blob();
  const href = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = href;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(href), 1000);
}
