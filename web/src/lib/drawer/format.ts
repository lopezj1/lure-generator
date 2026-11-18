// Pure display helpers for the builds/jobs drawer (W9). No imports, so
// `node --test tests/*.test.ts` can load this file directly.

/** `1.234` → `"1.23"`; null/undefined → `"–"`. */
export function num(v: number | null | undefined, digits = 2): string {
  return v == null || !Number.isFinite(v) ? '–' : v.toFixed(digits);
}

/** Signed delta: `+0.50`, `−1.25`, `±0.00`; null → `"–"`. */
export function signed(v: number | null | undefined, digits = 2): string {
  if (v == null || !Number.isFinite(v)) return '–';
  const s = Math.abs(v).toFixed(digits);
  if (Number(s) === 0) return `±${s}`;
  return v > 0 ? `+${s}` : `−${s}`;
}

/** Bytes → `"812 B"`, `"14.2 kB"`, `"3.1 MB"`. */
export function bytes(n: number | null | undefined): string {
  if (n == null) return '';
  if (n < 1000) return `${n} B`;
  if (n < 1e6) return `${(n / 1e3).toFixed(1)} kB`;
  return `${(n / 1e6).toFixed(1)} MB`;
}

/** Build stamp `YYYYMMDD-HHMMSS-<sha>[-dirty]` → `"2026-11-18 14:03"` (else the stamp). */
export function stampTime(stamp: string): string {
  const m = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})/.exec(stamp);
  return m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}` : stamp;
}

/** The git part of a stamp (`abc1234` or `abc1234-dirty`), or `""`. */
export function stampSha(stamp: string): string {
  const m = /^\d{8}-\d{6}-(.+)$/.exec(stamp);
  return m ? m[1] : '';
}

/** A job's argv (`python -m luregen --progress json build …`) → `lure build …`. */
export function cliCommand(argv: readonly string[]): string {
  let i = argv.indexOf('luregen');
  i = i < 0 ? 0 : i + 1;
  if (argv[i] === '--progress') i += 2;
  return ['lure', ...argv.slice(i).map(shellQuote)].join(' ');
}

function shellQuote(arg: string): string {
  return /^[A-Za-z0-9_./=:,+-]+$/.test(arg) ? arg : `'${arg.replace(/'/g, `'\\''`)}'`;
}

/** Short human target of a job request: the variant, build or image it works on. */
export function jobTarget(req: { op: string; args: Record<string, unknown> }): string {
  const a = req.args;
  const pick = (k: string) => (typeof a[k] === 'string' ? (a[k] as string) : '');
  if (req.op === 'build') return pick('variant').replace(/^spec\/designs\//, '');
  if (req.op === 'profile') return pick('image');
  return pick('build').replace(/^builds\//, '');
}

/** Elapsed `"12 s"`, `"3 min 5 s"` between two ISO times (end defaults to now). */
export function elapsed(start?: string | null, end?: string | null, now = Date.now()): string {
  if (!start) return '';
  const ms = (end ? Date.parse(end) : now) - Date.parse(start);
  if (!Number.isFinite(ms) || ms < 0) return '';
  const s = Math.round(ms / 1000);
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${s % 60} s`;
}

/**
 * Toggle `key` in a two-slot compare selection: a third pick drops the oldest. Returns the new
 * selection (oldest first).
 */
export function togglePick(picked: readonly string[], key: string): string[] {
  if (picked.includes(key)) return picked.filter((k) => k !== key);
  return [...picked, key].slice(-2);
}
