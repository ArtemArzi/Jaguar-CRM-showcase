import { describe, expect, it } from "vitest";
import type { KioskScheduleOption } from "./kiosk-api";
import {
  formatKioskCheckinOpening,
  getKioskOptionDecision,
} from "./kiosk-option-decision";

function option(
  overrides: Partial<KioskScheduleOption> = {},
): KioskScheduleOption {
  return {
    schedule_id: 17,
    effective_date: "2026-07-24",
    start_time: "21:00:00",
    end_time: "22:00:00",
    group_name: "Персональная тренировка",
    trainer_name: "Тренер",
    location_name: "Зал",
    training_type_id: 3,
    training_type_name: "Персональная",
    self_checkin_status: "can_checkin",
    reason_code: "",
    financial_status: "drop_in_debt",
    subscription_id: null,
    drop_in_price: "2000.00",
    existing_checkin_id: null,
    checkin_window_status: "too_early",
    checkin_opens_at: "2026-07-24T20:30:00+05:00",
    checkin_closes_at: "2026-07-24T22:00:00+05:00",
    ...overrides,
  };
}

describe("getKioskOptionDecision", () => {
  it("waits for an upcoming own booking instead of choosing an unrelated open guest session", () => {
    const decision = getKioskOptionDecision([
      option({
        schedule_id: 5,
        group_name: "Текущая группа",
        self_checkin_status: "can_book_guest_visit",
        financial_status: "subscription",
        checkin_window_status: "open",
        checkin_opens_at: "2026-07-24T19:30:00+05:00",
        checkin_closes_at: "2026-07-24T21:00:00+05:00",
      }),
      option(),
    ]);

    expect(decision).toMatchObject({
      kind: "wait",
      options: [{ schedule_id: 17 }],
    });
  });

  it("auto-checks an open exact drop-in without exposing or branching on its financial metadata", () => {
    const decision = getKioskOptionDecision([
      option({ checkin_window_status: "open" }),
    ]);

    expect(decision).toMatchObject({
      kind: "auto",
      option: {
        schedule_id: 17,
        financial_status: "drop_in_debt",
      },
    });
  });

  it("requires selection when more than one own option is open", () => {
    const decision = getKioskOptionDecision([
      option({ schedule_id: 17, checkin_window_status: "open" }),
      option({ schedule_id: 18, checkin_window_status: "open" }),
    ]);

    expect(decision).toMatchObject({
      kind: "select",
      options: [{ schedule_id: 17 }, { schedule_id: 18 }],
    });
  });

  it.each(["already_checked_in", "enrollment_frozen"])(
    "returns direct feedback for a current own booking blocked by %s",
    (reasonCode) => {
      const decision = getKioskOptionDecision([
        option({
          self_checkin_status: "blocked",
          reason_code: reasonCode,
          existing_checkin_id:
            reasonCode === "already_checked_in" ? 301 : null,
          checkin_window_status: "open",
        }),
      ]);

      expect(decision).toEqual({ kind: "blocked", reasonCode });
    },
  );

  it("uses a safe generic block only when no open guest option is available", () => {
    const decision = getKioskOptionDecision([
      option({
        self_checkin_status: "blocked",
        reason_code: "guest_visit_requires_group_schedule",
        financial_status: "blocked",
        checkin_window_status: "open",
      }),
    ]);

    expect(decision).toEqual({
      kind: "blocked",
      reasonCode: "guest_visit_requires_group_schedule",
    });
  });

  it("uses the club-local opening timestamp for client copy", () => {
    expect(formatKioskCheckinOpening(option())).toBe("20:30");
  });
});
