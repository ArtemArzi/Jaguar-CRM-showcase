import { afterEach, describe, expect, it, vi } from "vitest";

import {
  formatDateFull,
  formatDateLong,
  formatDateShort,
  formatDaysMissed,
  formatRelativeDate,
  getMonthRange,
  hasTimeStarted,
} from "./format";

describe("date display helpers", () => {
  it("formats short, long, and full Russian dates", () => {
    expect(formatDateShort("2026-04-15T12:30:00")).toBe("15 апр.");
    expect(formatDateLong("2026-04-15")).toBe("15 апреля");
    expect(formatDateLong("2026-04-15T12:30:00")).toBe("15 апреля");
    expect(formatDateFull("2026-04-15T12:30:00")).toBe("15 апр. 2026 г.");
  });
});

describe("formatDaysMissed", () => {
  it("uses the expected Russian labels around plural boundaries", () => {
    expect(formatDaysMissed(0)).toBe("Недавно был");
    expect(formatDaysMissed(1)).toBe("Не ходит 1 день");
    expect(formatDaysMissed(3)).toBe("Не ходит 3 дня");
    expect(formatDaysMissed(5)).toBe("Не ходит 5 дней");
  });
});

describe("formatRelativeDate", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("formats today, yesterday, and older dates from local midnight", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-04-15T18:30:00"));

    expect(formatRelativeDate("2026-04-15T08:00:00")).toBe("Сегодня");
    expect(formatRelativeDate("2026-04-15")).toBe("Сегодня");
    expect(formatRelativeDate("2026-04-14T23:59:00")).toBe("Вчера");
    expect(formatRelativeDate("2026-04-12T12:00:00")).toBe("3 дня назад");
    expect(formatRelativeDate("2026-04-10T12:00:00")).toBe("5 дней назад");
  });
});

describe("hasTimeStarted", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("compares HH:MM against the current local clock", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-04-15T18:30:00"));

    expect(hasTimeStarted("17:59")).toBe(true);
    expect(hasTimeStarted("18:29")).toBe(true);
    expect(hasTimeStarted("18:30")).toBe(true);
    expect(hasTimeStarted("18:31")).toBe(false);
    expect(hasTimeStarted("19:00")).toBe(false);
  });
});

describe("getMonthRange", () => {
  it("returns inclusive first and last day for the zero-based month", () => {
    expect(getMonthRange(2026, 1)).toEqual({
      dateFrom: "2026-02-01",
      dateTo: "2026-02-28",
    });
    expect(getMonthRange(2024, 1)).toEqual({
      dateFrom: "2024-02-01",
      dateTo: "2024-02-29",
    });
  });
});
