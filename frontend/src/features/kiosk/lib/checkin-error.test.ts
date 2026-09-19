import { describe, expect, it } from "vitest";
import { getKioskCheckinErrorMessage } from "./checkin-error";

describe("getKioskCheckinErrorMessage", () => {
  it.each([
    ["enrollment_frozen", "Абонемент заморожен"],
    ["student_schedule_ineligible", "Вы не записаны на эту тренировку"],
    ["student_ineligible", "Сейчас нельзя отметиться. Обратитесь к тренеру"],
    ["schedule_occurrence_not_found", "Тренировка сейчас недоступна"],
    ["group_session_closed", "Тренировка уже закрыта тренером"],
    ["training_type_mismatch", "Тип тренировки не подходит для отметки"],
    [
      "schedule_training_type_required",
      "Для этой тренировки не указан тип. Обратитесь к тренеру",
    ],
    [
      "drop_in_price_required",
      "Разовое занятие не настроено. Обратитесь к тренеру",
    ],
    ["one_time_date_mismatch", "Разовое занятие доступно только на свою дату"],
    ["duplicate_checkin_conflict", "Вы уже отмечены на этой тренировке"],
    ["already_checked_in", "Вы уже отмечены на этой тренировке"],
    [
      "can_book_guest_visit",
      "Подойдите к тренеру, чтобы записаться на это занятие",
    ],
    [
      "subscription_component_limit_exceeded",
      "Лимит этого типа занятий исчерпан. Обратитесь к администратору",
    ],
    [
      "subscription_component_credits_exhausted",
      "Занятия этого типа закончились. Обратитесь к администратору",
    ],
    [
      "payroll_period_closed",
      "Посещение за этот день закрыто для расчёта. Обратитесь к администратору",
    ],
  ])("maps %s to kiosk-safe copy", (code, message) => {
    expect(
      getKioskCheckinErrorMessage({
        response: {
          data: {
            code,
            detail: "Private trainer note must not leak",
          },
        },
      }),
    ).toBe(message);
  });

  it("supports legacy checkin_blocked_reason responses", () => {
    expect(
      getKioskCheckinErrorMessage({
        response: {
          data: {
            checkin_blocked_reason: "enrollment_frozen",
            detail: "Абонемент заморожен",
          },
        },
      }),
    ).toBe("Абонемент заморожен");
  });

  it("uses safe unknown details and hides private-looking details", () => {
    expect(
      getKioskCheckinErrorMessage({
        response: { data: { code: "unknown", detail: "Чек-ин недоступен" } },
      }),
    ).toBe("Чек-ин недоступен");

    expect(
      getKioskCheckinErrorMessage({
        response: {
          data: {
            code: "unknown",
            detail: "Private trainer note: call +7 900 000-00-00",
          },
        },
      }),
    ).toBe("Не удалось отметиться. Обратитесь к тренеру");
  });
});
