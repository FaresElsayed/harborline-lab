import { Dialog, Kbd } from '@/components/ui/overlay';
import { modKey } from '@/lib/hotkeys';
import { ui } from '@/lib/store';

const GROUPS: Array<{ title: string; rows: Array<[keys: string[], what: string]> }> = [
  {
    title: 'Anywhere',
    rows: [
      [[modKey, 'K'], 'Command palette: pages, simulator actions, issues by key'],
      [['C'], 'Create an issue'],
      [['/'], 'Search issues'],
      [['['], 'Show or hide the sidebar'],
      [['?'], 'This list'],
    ],
  },
  {
    title: 'Board, backlog and issue lists',
    rows: [
      [['J'], 'Next issue'],
      [['K'], 'Previous issue'],
      [['Enter'], 'Open the focused issue'],
      [['E'], 'Edit the focused issue’s summary'],
      [['I'], 'Assign the focused issue to me'],
      [['Esc'], 'Close the issue panel'],
    ],
  },
];

export function ShortcutsDialog() {
  const open = ui.use((s) => s.shortcuts);
  return (
    <Dialog open={open} onOpenChange={(v) => ui.set({ shortcuts: v })} title="Keyboard shortcuts" width={560}>
      <div className="flex flex-col gap-6">
        {GROUPS.map((g) => (
          <section key={g.title}>
            <h3 className="mb-2 text-sm font-semibold text-ink">{g.title}</h3>
            <dl className="flex flex-col">
              {g.rows.map(([keys, what]) => (
                <div key={what} className="flex items-center justify-between gap-4 border-b border-rule py-2 last:border-0">
                  <dt className="text-base text-ink">{what}</dt>
                  <dd className="flex shrink-0 gap-1">
                    {keys.map((k) => (
                      <Kbd key={k}>{k}</Kbd>
                    ))}
                  </dd>
                </div>
              ))}
            </dl>
          </section>
        ))}
      </div>
    </Dialog>
  );
}
