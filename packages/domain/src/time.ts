/**
 * Timezone arithmetic without a dependency.
 *
 * Two jobs in this system are wall-clock jobs, not interval jobs: the 03:00
 * dormancy sweep and the 21:00–08:00 quiet window. Both are anchored to the
 * user's timezone from `users.tz` — a setting, not the device — because a
 * device-derived zone would move the dormancy job every time you travel.
 * decisions.md D4.
 */

export interface LocalParts {
  year: number;
  month: number; // 1-12
  day: number;
  hour: number;
  minute: number;
  second: number;
}

const PART_FORMAT = new Map<string, Intl.DateTimeFormat>();

function formatter(tz: string): Intl.DateTimeFormat {
  let f = PART_FORMAT.get(tz);
  if (!f) {
    f = new Intl.DateTimeFormat('en-US', {
      timeZone: tz,
      hour12: false,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    });
    PART_FORMAT.set(tz, f);
  }
  return f;
}

export function localParts(date: Date, tz: string): LocalParts {
  const parts = formatter(tz).formatToParts(date);
  const get = (t: string): number => Number(parts.find((p) => p.type === t)?.value ?? '0');
  // Intl renders midnight as hour 24 in some engines; normalise it.
  const hour = get('hour') % 24;
  return {
    year: get('year'),
    month: get('month'),
    day: get('day'),
    hour,
    minute: get('minute'),
    second: get('second'),
  };
}

function offsetAt(date: Date, tz: string): number {
  const p = localParts(date, tz);
  const asUtc = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second);
  return asUtc - date.getTime();
}

/** Wall-clock time in `tz` → the instant it denotes. DST-correct. */
export function zonedToUtc(
  tz: string,
  year: number,
  month: number,
  day: number,
  hour: number,
  minute = 0,
): Date {
  const guess = Date.UTC(year, month - 1, day, hour, minute);
  const off1 = offsetAt(new Date(guess), tz);
  let ts = guess - off1;
  const off2 = offsetAt(new Date(ts), tz);
  // A spring-forward hour that does not exist locally resolves to the instant
  // just after the jump, which is the behaviour a scheduled job wants.
  if (off2 !== off1) ts = guess - off2;
  return new Date(ts);
}

/** Today's wall-clock `HH:MM` in `tz`, as an instant. */
export function atLocalTime(now: Date, tz: string, hhmm: string, dayOffset = 0): Date {
  const [h, m] = parseHHMM(hhmm);
  const p = localParts(now, tz);
  return zonedToUtc(tz, p.year, p.month, p.day + dayOffset, h, m);
}

export function parseHHMM(hhmm: string): [number, number] {
  const m = /^(\d{1,2}):(\d{2})$/.exec(hhmm.trim());
  if (!m) throw new Error(`invalid time of day: ${hhmm}`);
  return [Number(m[1]), Number(m[2])];
}

export interface QuietHours {
  from: string; // "21:00"
  to: string; // "08:00"
}

export function parseQuietHours(spec: string): QuietHours {
  const m = /^(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})$/.exec(spec.trim());
  if (!m) throw new Error(`invalid quiet hours: ${spec}`);
  return { from: m[1]!, to: m[2]! };
}

/** True inside the window, which wraps past midnight. */
export function isQuietHour(now: Date, tz: string, q: QuietHours): boolean {
  const p = localParts(now, tz);
  const mins = p.hour * 60 + p.minute;
  const [fh, fm] = parseHHMM(q.from);
  const [th, tm] = parseHHMM(q.to);
  const from = fh * 60 + fm;
  const to = th * 60 + tm;
  return from <= to ? mins >= from && mins < to : mins >= from || mins < to;
}

/** The first instant at or after `now` when a notification may be delivered. */
export function nextDeliverableAt(now: Date, tz: string, q: QuietHours): Date {
  if (!isQuietHour(now, tz, q)) return now;
  const p = localParts(now, tz);
  const [th, tm] = parseHHMM(q.to);
  const sameDay = zonedToUtc(tz, p.year, p.month, p.day, th, tm);
  return sameDay.getTime() > now.getTime()
    ? sameDay
    : zonedToUtc(tz, p.year, p.month, p.day + 1, th, tm);
}

export const DAY_MS = 86_400_000;

export function daysBetween(a: Date, b: Date): number {
  return Math.floor((b.getTime() - a.getTime()) / DAY_MS);
}

export function hoursBetween(a: Date, b: Date): number {
  return (b.getTime() - a.getTime()) / 3_600_000;
}

/** "in 3 days", "in 4 hours", "today" — the meta line on a suggestion card. */
export function relativeFuture(now: Date, then: Date): string {
  const h = hoursBetween(now, then);
  if (h < 0) return 'overdue';
  if (h < 1) return 'within the hour';
  if (h < 24) return `in ${Math.round(h)} hours`;
  const d = Math.round(h / 24);
  return d === 1 ? 'tomorrow' : `in ${d} days`;
}

/** "6 days quiet" — the meta line on a follow-up card. */
export function relativePast(now: Date, then: Date): string {
  const d = daysBetween(then, now);
  if (d <= 0) return 'today';
  return d === 1 ? '1 day quiet' : `${d} days quiet`;
}
