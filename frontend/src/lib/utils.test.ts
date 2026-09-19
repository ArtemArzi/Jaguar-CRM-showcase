import { afterEach, describe, expect, it, vi } from "vitest";

import { cn, formatRub, getApiError, getInitials, isSameDay, toISODate } from "./utils";

describe("cn", () => {
  it("combines truthy classes and resolves Tailwind conflicts", () => {
    expect(cn("px-2", null, "px-4", ["text-sm"])).toBe("px-4 text-sm");
  });
});

describe("toISODate", () => {
  it("formats a Date as local YYYY-MM-DD", () => {
    expect(toISODate(new Date(2026, 3, 5, 23, 30))).toBe("2026-04-05");
  });

  it("defaults to the current local date", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 3, 5, 23, 30));

    expect(toISODate()).toBe("2026-04-05");
  });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("formatRub", () => {
  it("formats numeric strings and numbers with a non-breaking ruble suffix", () => {
    expect(formatRub("1234.50")).toBe("1\u00A0234,5\u00A0\u20BD");
    expect(formatRub(5000)).toBe("5\u00A0000\u00A0\u20BD");
  });
});

describe("getInitials", () => {
  it("returns uppercase initials from the first two non-empty words", () => {
    expect(getInitials("Иван Петров Сидоров")).toBe("ИП");
    expect(getInitials("  Анна   ")).toBe("А");
  });
});

describe("isSameDay", () => {
  it("compares day, month, and year, not time", () => {
    expect(isSameDay(new Date(2026, 3, 5, 0, 1), new Date(2026, 3, 5, 23, 59))).toBe(true);
    expect(isSameDay(new Date(2026, 3, 5), new Date(2026, 3, 6))).toBe(false);
    expect(isSameDay(new Date(2026, 3, 5), new Date(2026, 4, 5))).toBe(false);
    expect(isSameDay(new Date(2026, 3, 5), new Date(2025, 3, 5))).toBe(false);
  });
});

describe("getApiError", () => {
  it("uses API detail when present and falls back otherwise", () => {
    expect(getApiError({ response: { data: { detail: "Нельзя удалить" } } }, "Ошибка")).toBe("Нельзя удалить");
    expect(getApiError(new Error("boom"), "Ошибка")).toBe("Ошибка");
  });
});
