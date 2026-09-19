import { describe, expect, it } from "vitest";
import {
  getScheduleOccurrenceKindLabel,
  getScheduleOccurrenceStatusLabel,
  groupOccurrencesByDate,
  type StudentScheduleOccurrence,
} from "./student-schedule-utils";

function makeOccurrence(
  overrides: Partial<StudentScheduleOccurrence> = {},
): StudentScheduleOccurrence {
  return {
    schedule_id: 1,
    group_name: "Evening Group",
    effective_date: "2026-04-13",
    effective_start_time: "18:00:00",
    effective_end_time: "19:00:00",
    trainer_name: "Coach",
    location_name: "Main Hall",
    training_type_id: 7,
    training_type_name: "Muay Thai",
    ...overrides,
  };
}

describe("groupOccurrencesByDate", () => {
  it("preserves schedule training type fields while grouping", () => {
    const grouped = groupOccurrencesByDate([makeOccurrence()]);

    expect(grouped[0].items[0]).toMatchObject({
      training_type_id: 7,
      training_type_name: "Muay Thai",
    });
  });
});

describe("student schedule occurrence presenters", () => {
  it("labels assigned group occurrences as already booked", () => {
    const occurrence = makeOccurrence({
      enrollment_id: 12,
      training_type_kind: "group",
    });

    expect(getScheduleOccurrenceKindLabel(occurrence)).toBe("Группа");
    expect(getScheduleOccurrenceStatusLabel(occurrence)).toBe("Вы уже записаны");
  });

  it("labels self-booked group occurrences without making them look bookable", () => {
    expect(
      getScheduleOccurrenceStatusLabel(
        makeOccurrence({
          created_from: "student_self_booking",
          training_type_kind: "group",
        }),
      ),
    ).toBe("Вы записались");
  });

  it("labels personal occurrences separately from group sessions", () => {
    const occurrence = makeOccurrence({
      created_from: "personal_booking",
      training_type_kind: "personal",
    });

    expect(getScheduleOccurrenceKindLabel(occurrence)).toBe("Персоналка");
    expect(getScheduleOccurrenceStatusLabel(occurrence)).toBe(
      "Персоналка забронирована",
    );
  });
});
