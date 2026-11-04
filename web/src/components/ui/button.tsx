import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from 'react';
import clsx from 'clsx';
import { Tooltip } from './overlay';

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger' | 'danger-solid' | 'quiet';
export type ButtonSize = 'sm' | 'md' | 'lg';

const base =
  'press inline-flex shrink-0 select-none items-center justify-center gap-1.5 whitespace-nowrap rounded-m font-medium ' +
  'transition-[background-color,border-color,color,opacity] duration-150 ease-out ' +
  'disabled:opacity-55 disabled:saturate-50 [&_svg]:shrink-0';

const variants: Record<ButtonVariant, string> = {
  primary: 'bg-accent text-on-accent hover:bg-accent-strong dark:hover:bg-accent-strong',
  secondary: 'border border-control bg-sheet text-ink hover:bg-hover hover:border-faint/60',
  ghost: 'text-ink hover:bg-hover',
  quiet: 'text-muted hover:bg-hover hover:text-ink',
  danger: 'border border-control bg-sheet text-bad hover:border-bad/50 hover:bg-bad-bg',
  'danger-solid': 'bg-bad text-white hover:brightness-110 dark:text-[#1a0606]',
};

const sizes: Record<ButtonSize, string> = {
  sm: 'h-7 px-2.5 text-sm [&_svg]:size-3.5',
  md: 'h-8 px-3 text-base [&_svg]:size-4',
  lg: 'h-9 px-3.5 text-base [&_svg]:size-4',
};

const iconSizes: Record<ButtonSize, string> = {
  sm: 'size-7 [&_svg]:size-3.5',
  md: 'size-8 [&_svg]:size-4',
  lg: 'size-9 [&_svg]:size-[18px]',
};

/** Button look for links (a link must not wrap a button). */
export function buttonClasses(variant: ButtonVariant = 'secondary', size: ButtonSize = 'md', className?: string) {
  return clsx(base, variants[variant], sizes[size], className);
}

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  icon?: ReactNode;
  trailing?: ReactNode;
  loading?: boolean;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = 'secondary', size = 'md', icon, trailing, loading, className, children, disabled, type = 'button', ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={clsx(base, variants[variant], sizes[size], className)}
      {...rest}
    >
      {loading ? <Spinner /> : icon}
      {children}
      {trailing}
    </button>
  );
});

export interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  label: string;
  /** Tooltip text; defaults to the label. Pass false for none. */
  tip?: ReactNode | false;
  variant?: ButtonVariant;
  size?: ButtonSize;
}

/** Icon-only button: the label is required and becomes its accessible name and tooltip text. */
export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { label, tip, variant = 'quiet', size = 'md', className, children, type = 'button', ...rest },
  ref,
) {
  const button = (
    <button
      ref={ref}
      type={type}
      aria-label={label}
      className={clsx(base, variants[variant], iconSizes[size], 'px-0', className)}
      {...rest}
    >
      {children}
    </button>
  );
  return tip === false ? button : <Tooltip content={tip ?? label}>{button}</Tooltip>;
});

/** A small rotating arc. Used only inside a pressed button while its request is in flight. */
export function Spinner({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 16 16" className={clsx('animate-spin [animation-duration:650ms]', className)} aria-hidden>
      <circle cx="8" cy="8" r="6.25" fill="none" stroke="currentColor" strokeOpacity="0.25" strokeWidth="1.75" />
      <path d="M14.25 8A6.25 6.25 0 0 0 8 1.75" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" />
    </svg>
  );
}
