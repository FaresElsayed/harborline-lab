import { forwardRef, useId, type InputHTMLAttributes, type ReactNode, type TextareaHTMLAttributes } from 'react';
import clsx from 'clsx';
import { Checkbox as RCheckbox, Select as RSelect, Switch as RSwitch, ToggleGroup } from 'radix-ui';
import { Check, ChevronDown } from 'lucide-react';

export const controlClass =
  'h-8 rounded-m border border-control bg-sheet px-2.5 text-base text-ink ' +
  'transition-[border-color,box-shadow] duration-150 hover:border-faint/70 ' +
  'focus-visible:border-focus focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus/25 ' +
  'aria-[invalid=true]:border-bad disabled:bg-raised disabled:text-muted';

export function Label({ htmlFor, children, hint, className }: { htmlFor?: string; children: ReactNode; hint?: ReactNode; className?: string }) {
  return (
    <label htmlFor={htmlFor} className={clsx('mb-1.5 flex items-baseline justify-between gap-2 text-sm font-medium text-ink', className)}>
      <span>{children}</span>
      {hint ? <span className="text-xs font-normal text-muted">{hint}</span> : null}
    </label>
  );
}

export function FieldNote({ id, children, tone = 'muted' }: { id?: string; children: ReactNode; tone?: 'muted' | 'bad' }) {
  return (
    <p id={id} className={clsx('mt-1.5 text-xs', tone === 'bad' ? 'text-bad' : 'text-muted')}>
      {children}
    </p>
  );
}

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement> & { leading?: ReactNode }>(function Input(
  { className, leading, ...rest },
  ref,
) {
  const width = /(^|\s)w-/.test(className ?? '') ? '' : 'w-full';
  if (!leading) return <input ref={ref} className={clsx(controlClass, width, className)} {...rest} />;
  return (
    <div className="relative">
      <span className="pointer-events-none absolute inset-y-0 left-2.5 flex items-center text-muted [&_svg]:size-3.5">{leading}</span>
      <input ref={ref} className={clsx(controlClass, 'w-full pl-8', className)} {...rest} />
    </div>
  );
});

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(function Textarea(
  { className, ...rest },
  ref,
) {
  return <textarea ref={ref} className={clsx(controlClass, 'h-auto min-h-20 w-full py-2 leading-relaxed', className)} {...rest} />;
});

export interface SelectOption {
  value: string;
  label: ReactNode;
  hint?: ReactNode;
}

export function Select({
  value,
  onValueChange,
  options,
  placeholder,
  id,
  className,
  size = 'md',
  ariaLabel,
  disabled,
}: {
  value: string;
  onValueChange: (v: string) => void;
  options: SelectOption[];
  placeholder?: string;
  id?: string;
  className?: string;
  size?: 'sm' | 'md';
  ariaLabel?: string;
  disabled?: boolean;
}) {
  return (
    <RSelect.Root value={value} onValueChange={onValueChange} disabled={disabled}>
      <RSelect.Trigger
        id={id}
        aria-label={ariaLabel}
        className={clsx(
          controlClass,
          /(^|\s)w-/.test(className ?? '') ? '' : 'w-full',
          'inline-flex items-center justify-between gap-2 text-left data-[placeholder]:text-faint',
          size === 'sm' && 'h-7 text-sm',
          className,
        )}
      >
        <span className="truncate">
          <RSelect.Value placeholder={placeholder} />
        </span>
        <RSelect.Icon>
          <ChevronDown className="size-3.5 text-muted" aria-hidden />
        </RSelect.Icon>
      </RSelect.Trigger>
      <RSelect.Portal>
        <RSelect.Content
          position="popper"
          sideOffset={4}
          className="pop z-50 max-h-[min(360px,var(--radix-select-content-available-height))] min-w-[var(--radix-select-trigger-width)] overflow-hidden rounded-m border border-line bg-sheet p-1 shadow-float"
        >
          <RSelect.Viewport>
            {options.map((o) => (
              <RSelect.Item
                key={o.value}
                value={o.value}
                className="relative flex h-8 cursor-default select-none items-center gap-2 rounded-s pl-7 pr-2.5 text-base text-ink outline-none data-[highlighted]:bg-hover data-[disabled]:opacity-50"
              >
                <RSelect.ItemIndicator className="absolute left-2 inline-flex">
                  <Check className="size-3.5 text-accent" aria-hidden />
                </RSelect.ItemIndicator>
                <RSelect.ItemText>{o.label}</RSelect.ItemText>
                {o.hint ? <span className="ml-auto pl-3 text-xs text-muted">{o.hint}</span> : null}
              </RSelect.Item>
            ))}
          </RSelect.Viewport>
        </RSelect.Content>
      </RSelect.Portal>
    </RSelect.Root>
  );
}

export function Switch({
  checked,
  onCheckedChange,
  label,
  description,
  id,
  disabled,
  ariaLabel,
}: {
  checked: boolean;
  onCheckedChange: (v: boolean) => void;
  label?: ReactNode;
  description?: ReactNode;
  id?: string;
  disabled?: boolean;
  ariaLabel?: string;
}) {
  const auto = useId();
  const sid = id ?? auto;
  const control = (
    <RSwitch.Root
      id={sid}
      checked={checked}
      onCheckedChange={onCheckedChange}
      disabled={disabled}
      aria-label={label ? undefined : ariaLabel}
      className="relative inline-flex h-[18px] w-[30px] shrink-0 items-center rounded-full bg-control transition-colors duration-150 data-[state=checked]:bg-good disabled:opacity-50"
    >
      <RSwitch.Thumb className="block size-3.5 translate-x-0.5 rounded-full bg-white shadow-[0_1px_2px_rgb(0_0_0/0.25)] transition-transform duration-200 ease-out data-[state=checked]:translate-x-[14px]" />
    </RSwitch.Root>
  );
  if (!label) return control;
  return (
    <div className="flex items-start justify-between gap-4">
      <label htmlFor={sid} className="min-w-0 cursor-pointer">
        <span className="block text-base text-ink">{label}</span>
        {description ? <span className="mt-0.5 block text-sm text-muted">{description}</span> : null}
      </label>
      <div className="pt-0.5">{control}</div>
    </div>
  );
}

export function Checkbox({
  checked,
  onCheckedChange,
  id,
  label,
  disabled,
  className,
}: {
  checked: boolean;
  onCheckedChange: (v: boolean) => void;
  id?: string;
  label?: string;
  disabled?: boolean;
  className?: string;
}) {
  return (
    <RCheckbox.Root
      id={id}
      checked={checked}
      onCheckedChange={(v) => onCheckedChange(v === true)}
      aria-label={label}
      disabled={disabled}
      className={clsx(
        'press inline-flex size-[18px] shrink-0 items-center justify-center rounded-[5px] border-[1.5px] border-control bg-sheet transition-colors duration-150',
        'hover:border-accent data-[state=checked]:border-accent data-[state=checked]:bg-accent',
        className,
      )}
    >
      <RCheckbox.Indicator className="text-on-accent">
        <Check className="size-3" strokeWidth={3} aria-hidden />
      </RCheckbox.Indicator>
    </RCheckbox.Root>
  );
}

export function Segmented<T extends string>({
  value,
  onValueChange,
  options,
  ariaLabel,
  size = 'md',
}: {
  value: T;
  onValueChange: (v: T) => void;
  options: Array<{ value: T; label: ReactNode; title?: string }>;
  ariaLabel: string;
  size?: 'sm' | 'md';
}) {
  return (
    <ToggleGroup.Root
      type="single"
      value={value}
      onValueChange={(v) => v && onValueChange(v as T)}
      aria-label={ariaLabel}
      className={clsx('inline-flex rounded-m bg-active p-0.5', size === 'sm' ? 'h-7' : 'h-8')}
    >
      {options.map((o) => (
        <ToggleGroup.Item
          key={o.value}
          value={o.value}
          title={o.title}
          className={clsx(
            'num inline-flex items-center justify-center whitespace-nowrap rounded-[6px] px-2.5 font-medium text-muted transition-colors duration-150',
            'hover:text-ink data-[state=on]:bg-sheet data-[state=on]:text-ink',
            size === 'sm' ? 'text-sm' : 'text-base',
          )}
        >
          {o.label}
        </ToggleGroup.Item>
      ))}
    </ToggleGroup.Root>
  );
}
