const intFmt = new Intl.NumberFormat('en-US');
const oneDec = new Intl.NumberFormat('en-US', { maximumFractionDigits: 1, minimumFractionDigits: 1 });

export const fmtInt = (n: number | null | undefined) => (n == null || Number.isNaN(n) ? '—' : intFmt.format(Math.round(n)));

/** Signed count for change columns: "+1,204", "−12", "0". Uses a true minus sign. */
export function fmtSigned(n: number, sign: '+' | '−' | '~' = n < 0 ? '−' : '+'): string {
  if (n === 0) return '0';
  return `${sign}${intFmt.format(Math.abs(Math.round(n)))}`;
}

export function fmtBytes(bytes: number | null | undefined): { value: string; unit: string } {
  if (bytes == null || bytes < 0) return { value: '—', unit: '' };
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let v = bytes;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return { value: i === 0 ? String(v) : oneDec.format(v), unit: units[i] };
}

export const bytesText = (b: number | null | undefined) => {
  const { value, unit } = fmtBytes(b);
  return unit ? `${value} ${unit}` : value;
};

/** Seconds to "42 s", "4 min 12 s", "1 h 05 min". */
export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return '—';
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return s % 60 ? `${m} min ${s % 60} s` : `${m} min`;
  const h = Math.floor(m / 60);
  return `${h} h ${String(m % 60).padStart(2, '0')} min`;
}

/** Sim hours to "6 h", "1 day", "3 days", "1 day 6 h". */
export function fmtSimHours(h: number): string {
  if (h < 24) return `${h} h`;
  const d = Math.floor(h / 24);
  const r = h % 24;
  return `${intFmt.format(d)} ${d === 1 ? 'day' : 'days'}${r ? ` ${r} h` : ''}`;
}

export function fmtHours(h: number | null | undefined): string {
  if (h == null) return '—';
  return Number.isInteger(h) ? `${h}h` : `${oneDec.format(h)}h`;
}

export const plural = (n: number, one: string, many = `${one}s`) => `${fmtInt(n)} ${n === 1 ? one : many}`;
