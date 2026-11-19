import type { Run, Status } from "./api";

/** The API stores and sends UTC. Everything shown to people is in Philippine time. */
export const DISPLAY_TZ = "Asia/Manila";

const dateTime = new Intl.DateTimeFormat("en-PH", {
  timeZone: DISPLAY_TZ,
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

const timeOnly = new Intl.DateTimeFormat("en-PH", {
  timeZone: DISPLAY_TZ,
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

const dayOnly = new Intl.DateTimeFormat("en-PH", { timeZone: DISPLAY_TZ, month: "short", day: "numeric" });

export const formatDateTime = (iso: string) => dateTime.format(new Date(iso));

/** "14:32" today, "Oct 28, 14:32" on another day (both in Philippine time). */
export function formatWhen(iso: string, now: Date = new Date()): string {
  const d = new Date(iso);
  const sameDay = dayOnly.format(d) === dayOnly.format(now);
  return sameDay ? timeOnly.format(d) : `${dayOnly.format(d)}, ${timeOnly.format(d)}`;
}

export function formatDuration(ms: number | null): string {
  if (ms === null) return "running";
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.floor(ms / 60_000)} min ${Math.round((ms % 60_000) / 1000)} s`;
}

/** "in 3 min", "in 2 h", or "now" for something already due. */
export function formatUntil(iso: string, now: Date = new Date()): string {
  const seconds = Math.round((new Date(iso).getTime() - now.getTime()) / 1000);
  if (seconds <= 5) return "now";
  if (seconds < 90) return `in ${seconds} s`;
  if (seconds < 90 * 60) return `in ${Math.round(seconds / 60)} min`;
  return `in ${Math.round(seconds / 3600)} h`;
}

/** Plain words for the internal states, used everywhere so each state has one name. */
export const STATUS_LABEL: Record<Status, string> = {
  queued: "Waiting",
  running: "Running",
  failed: "Retrying",
  succeeded: "Done",
  dead: "Gave up",
};

export const STATUS_ORDER: Status[] = ["queued", "running", "failed", "succeeded", "dead"];

/** What the status plate says underneath, when there is something worth saying. */
export function statusNote(run: Pick<Run, "status" | "run_at">, now: Date = new Date()): string | null {
  if (run.status === "failed") return `retry ${formatUntil(run.run_at, now)}`;
  if (run.status === "queued") return new Date(run.run_at).getTime() - now.getTime() > 5000 ? `starts ${formatUntil(run.run_at, now)}` : null;
  return null;
}

export const RANGES = [
  { value: "all", label: "All time", ms: 0 },
  { value: "1h", label: "Last hour", ms: 3_600_000 },
  { value: "24h", label: "Last 24 hours", ms: 86_400_000 },
  { value: "7d", label: "Last 7 days", ms: 7 * 86_400_000 },
] as const;

export function rangeToSince(range: string | null, now: Date = new Date()): string | undefined {
  const found = RANGES.find((r) => r.value === range);
  return found && found.ms ? new Date(now.getTime() - found.ms).toISOString() : undefined;
}
