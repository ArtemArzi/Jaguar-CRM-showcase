import { describe, expect, it } from "vitest";
import { toDateParamInTimeZone, toDateTimeParamInTimeZone } from "./club-date";

describe("club-date", () => {
  it("formats date params in the requested club time zone", () => {
    const utcBoundary = new Date("2026-06-28T20:30:00.000Z");

    expect(toDateParamInTimeZone(utcBoundary, "Asia/Yekaterinburg")).toBe("2026-06-29");
    expect(toDateParamInTimeZone(utcBoundary, "America/New_York")).toBe("2026-06-28");
  });

  it("formats wall-clock date and time in the requested club time zone", () => {
    const utcTime = new Date("2026-07-14T04:55:30.000Z");

    expect(toDateTimeParamInTimeZone(utcTime, "Europe/Moscow")).toBe(
      "2026-07-14T07:55:30",
    );
    expect(toDateTimeParamInTimeZone(utcTime, "Asia/Yekaterinburg")).toBe(
      "2026-07-14T09:55:30",
    );
  });
});
