/** The current product accepts source videos of at most ten minutes. */
export const MAX_VIDEO_MS = 600_000;

/** Editable mm:ss.d value; round the whole duration so 59.95s carries a minute. */
export function formatMs(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0 || ms > MAX_VIDEO_MS) return "";
  const tenths = Math.round(ms / 100);
  return `${Math.floor(tenths / 600)
    .toString()
    .padStart(2, "0")}:${((tenths % 600) / 10).toFixed(1).padStart(4, "0")}`;
}

/** Exact editable boundaries must not round beyond the source duration. */
export function formatMsExact(ms: number): string {
  if (!Number.isInteger(ms) || ms < 0 || ms > MAX_VIDEO_MS) return "";
  const minutes = Math.floor(ms / 60_000)
    .toString()
    .padStart(2, "0");
  const seconds = Math.floor((ms % 60_000) / 1000)
    .toString()
    .padStart(2, "0");
  const milliseconds = (ms % 1000).toString().padStart(3, "0");
  return `${minutes}:${seconds}.${milliseconds}`;
}

/** Invalid or out-of-limit input stays invalid; it is never silently clamped. */
export function parseMs(value: string): number {
  const match = /^(\d{1,3}):([0-5]\d)(?:[.,](\d{1,3}))?$/.exec(value.trim());
  if (!match) return NaN;
  const ms =
    Number(match[1]) * 60_000 +
    Number(match[2]) * 1_000 +
    Number((match[3] || "").padEnd(3, "0"));
  return ms <= MAX_VIDEO_MS ? ms : NaN;
}

/** Russian count form: 1 шаг, 2 шага, 5 шагов; 11–14 always use many. */
export function countNoun(
  count: number,
  forms: readonly [one: string, few: string, many: string],
): string {
  if (!Number.isInteger(count)) return forms[2];
  const absolute = Math.abs(count);
  const lastTwo = absolute % 100;
  if (lastTwo >= 11 && lastTwo <= 14) return forms[2];
  const last = absolute % 10;
  if (last === 1) return forms[0];
  if (last >= 2 && last <= 4) return forms[1];
  return forms[2];
}
