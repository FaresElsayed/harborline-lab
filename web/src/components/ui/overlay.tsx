import type { ReactNode } from 'react';
import clsx from 'clsx';
import { Dialog as RDialog, DropdownMenu, Popover as RPopover, Tooltip as RTooltip } from 'radix-ui';
import { X } from 'lucide-react';
import { IconButton } from './button';

// ---------------------------------------------------------------- dialog

export function Dialog({
  open,
  onOpenChange,
  title,
  description,
  children,
  footer,
  width = 520,
  tone,
  initialFocus,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  title: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  width?: number;
  tone?: 'danger';
  /** Element to focus when the dialog opens (defaults to the first focusable one). */
  initialFocus?: React.RefObject<HTMLElement | null>;
}) {
  return (
    <RDialog.Root open={open} onOpenChange={onOpenChange}>
      <RDialog.Portal>
        <RDialog.Overlay className="fixed inset-0 z-40 bg-[rgb(8_19_31/0.42)] data-[state=open]:animate-[fade-in_160ms_ease-out] data-[state=closed]:animate-[fade-in_120ms_ease-out_reverse]" />
        <RDialog.Content
          onOpenAutoFocus={(e) => {
            if (initialFocus?.current) {
              e.preventDefault();
              initialFocus.current.focus();
            }
          }}
          className={clsx(
            'dialog fixed left-1/2 top-[12vh] z-50 flex max-h-[80vh] w-[calc(100vw-32px)] -translate-x-1/2 flex-col overflow-hidden rounded-l border border-line bg-sheet shadow-float',
          )}
          style={{ maxWidth: width }}
        >
          <div className="flex items-start justify-between gap-4 px-5 pb-1 pt-4">
            <div className="min-w-0">
              <RDialog.Title className={clsx('text-lg font-semibold', tone === 'danger' && 'text-bad')}>{title}</RDialog.Title>
              {description ? <RDialog.Description className="mt-1 text-base text-muted">{description}</RDialog.Description> : null}
            </div>
            <RDialog.Close asChild>
              <IconButton label="Close" tip={false} size="sm" className="-mr-1.5 mt-0.5">
                <X aria-hidden />
              </IconButton>
            </RDialog.Close>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
          {footer ? <div className="flex items-center justify-end gap-2 border-t border-rule bg-raised/60 px-5 py-3">{footer}</div> : null}
        </RDialog.Content>
      </RDialog.Portal>
    </RDialog.Root>
  );
}

// ---------------------------------------------------------------- menus

export const Menu = DropdownMenu.Root;
export const MenuTrigger = DropdownMenu.Trigger;

export function MenuContent({ children, align = 'end', className }: { children: ReactNode; align?: 'start' | 'end' | 'center'; className?: string }) {
  return (
    <DropdownMenu.Portal>
      <DropdownMenu.Content
        align={align}
        sideOffset={4}
        className={clsx('pop z-50 min-w-48 rounded-m border border-line bg-sheet p-1 shadow-float', className)}
      >
        {children}
      </DropdownMenu.Content>
    </DropdownMenu.Portal>
  );
}

export function MenuItem({
  children,
  onSelect,
  icon,
  shortcut,
  tone,
  disabled,
}: {
  children: ReactNode;
  onSelect?: (e: Event) => void;
  icon?: ReactNode;
  shortcut?: string;
  tone?: 'danger';
  disabled?: boolean;
}) {
  return (
    <DropdownMenu.Item
      onSelect={onSelect}
      disabled={disabled}
      className={clsx(
        'flex h-8 cursor-default select-none items-center gap-2 rounded-s px-2 text-base outline-none data-[highlighted]:bg-hover data-[disabled]:opacity-50 [&_svg]:size-4 [&_svg]:text-muted',
        tone === 'danger' ? 'text-bad [&_svg]:!text-bad' : 'text-ink',
      )}
    >
      {icon}
      <span className="flex-1">{children}</span>
      {shortcut ? <Kbd>{shortcut}</Kbd> : null}
    </DropdownMenu.Item>
  );
}

export function MenuLabel({ children }: { children: ReactNode }) {
  return <DropdownMenu.Label className="px-2 pb-1 pt-2 text-xs text-muted">{children}</DropdownMenu.Label>;
}
export function MenuSeparator() {
  return <DropdownMenu.Separator className="my-1 h-px bg-rule" />;
}
export const MenuRadioGroup = DropdownMenu.RadioGroup;
export function MenuRadioItem({ value, children }: { value: string; children: ReactNode }) {
  return (
    <DropdownMenu.RadioItem
      value={value}
      className="relative flex h-8 cursor-default select-none items-center rounded-s pl-7 pr-2 text-base text-ink outline-none data-[highlighted]:bg-hover"
    >
      <DropdownMenu.ItemIndicator className="absolute left-2 inline-flex">
        <span className="size-1.5 rounded-full bg-accent" />
      </DropdownMenu.ItemIndicator>
      {children}
    </DropdownMenu.RadioItem>
  );
}

// ---------------------------------------------------------------- popover

export const Popover = RPopover.Root;
export const PopoverTrigger = RPopover.Trigger;
export const PopoverAnchor = RPopover.Anchor;
export function PopoverContent({
  children,
  align = 'start',
  className,
  width,
}: {
  children: ReactNode;
  align?: 'start' | 'end' | 'center';
  className?: string;
  width?: number;
}) {
  return (
    <RPopover.Portal>
      <RPopover.Content
        align={align}
        sideOffset={6}
        style={width ? { width } : undefined}
        className={clsx('pop z-50 rounded-m border border-line bg-sheet p-3 shadow-float', className)}
      >
        {children}
      </RPopover.Content>
    </RPopover.Portal>
  );
}

// ---------------------------------------------------------------- tooltip

export const TooltipProvider = RTooltip.Provider;

export function Tooltip({ content, children, side = 'top' }: { content: ReactNode; children: ReactNode; side?: 'top' | 'bottom' | 'left' | 'right' }) {
  if (!content) return <>{children}</>;
  return (
    <RTooltip.Root>
      <RTooltip.Trigger asChild>{children}</RTooltip.Trigger>
      <RTooltip.Portal>
        <RTooltip.Content
          side={side}
          sideOffset={6}
          className="tip z-[60] max-w-72 rounded-s bg-ink px-2 py-1 text-sm text-sheet shadow-float"
        >
          {content}
        </RTooltip.Content>
      </RTooltip.Portal>
    </RTooltip.Root>
  );
}

export function Kbd({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <kbd
      className={clsx(
        'inline-flex h-5 min-w-5 items-center justify-center rounded-[5px] border border-line bg-raised px-1 font-sans text-xs font-medium text-muted',
        className,
      )}
    >
      {children}
    </kbd>
  );
}
