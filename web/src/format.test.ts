import { describe, expect, it } from "vitest";
import { STATUS_LABEL, STATUS_ORDER, formatDateTime, formatDuration, formatUntil, formatWhen, rangeToSince, statusNote } from "./format";

describe("times are shown in Philippine time", () => {
  it("converts UTC to UTC+8", () => {
    expect(formatDateTime("2026-10-29T06:00:00Z")).toMatch(/14:00:00/);
  });

  it("rolls over to the next day when UTC is still the evening before", () => {
    const text = formatDateTime("2026-10-29T17:30:00Z");
    expect(text).toMatch(/01:30:00/);
    expect(text).toMatch(/30/);
  });

  it("shows just the time for today and adds the day for other days", () => {
    const now = new Date("2026-10-29T08:00:00Z");
    expect(formatWhen("2026-10-29T06:05:00Z", now)).toBe("14:05");
    expect(formatWhen("2026-10-27T06:05:00Z", now)).toMatch(/Oct.*27.*14:05|27.*Oct.*14:05/);
  });

  it("decides 'today' by the Manila date, not the UTC date", () => {
    const now = new Date("2026-10-29T17:00:00Z"); // already Oct 30 in Manila
    expect(formatWhen("2026-10-29T10:00:00Z", now)).not.toBe("18:00");
  });
});

describe("durations and countdowns", () => {
  it("formats durations at each scale", () => {
    expect(formatDuration(null)).toBe("running");
    expect(formatDuration(42)).toBe("42 ms");
    expect(formatDuration(1500)).toBe("1.5 s");
    expect(formatDuration(125_000)).toBe("2 min 5 s");
  });

  it("formats a countdown, treating the past as now", () => {
    const now = new Date("2026-10-29T06:00:00Z");
    expect(formatUntil("2026-10-29T05:59:00Z", now)).toBe("now");
    expect(formatUntil("2026-10-29T06:00:30Z", now)).toBe("in 30 s");
    expect(formatUntil("2026-10-29T06:10:00Z", now)).toBe("in 10 min");
    expect(formatUntil("2026-10-29T09:00:00Z", now)).toBe("in 3 h");
  });
});

describe("state names", () => {
  it("gives every state one distinct plain-language name", () => {
    expect(STATUS_ORDER).toHaveLength(5);
    const labels = STATUS_ORDER.map((s) => STATUS_LABEL[s]);
    expect(new Set(labels).size).toBe(5);
    expect(labels).toEqual(["Waiting", "Running", "Retrying", "Done", "Gave up"]);
  });

  it("only adds a note where there is something to say", () => {
    const now = new Date("2026-10-29T06:00:00Z");
    expect(statusNote({ status: "failed", run_at: "2026-10-29T06:05:00Z" }, now)).toBe("retry in 5 min");
    expect(statusNote({ status: "queued", run_at: "2026-10-29T06:00:00Z" }, now)).toBeNull();
    expect(statusNote({ status: "succeeded", run_at: "2026-10-29T06:00:00Z" }, now)).toBeNull();
    expect(statusNote({ status: "dead", run_at: "2026-10-29T06:00:00Z" }, now)).toBeNull();
  });
});

describe("time range presets", () => {
  it("returns nothing for all time or an unknown value", () => {
    expect(rangeToSince("all")).toBeUndefined();
    expect(rangeToSince("nonsense")).toBeUndefined();
    expect(rangeToSince(null)).toBeUndefined();
  });

  it("subtracts the preset from now", () => {
    const now = new Date("2026-10-29T06:00:00Z");
    expect(rangeToSince("1h", now)).toBe("2026-10-29T05:00:00.000Z");
    expect(rangeToSince("7d", now)).toBe("2026-10-22T06:00:00.000Z");
  });
});
