import { describe, expect, it } from "vitest";
import {
  findNextTrainingOccurrence,
  findNextTrainingStreams,
  formatUpcomingTrainingDayLabel,
} from "./student-home-utils";
import type { StudentScheduleOccurrence } from "./student-schedule-utils";

const mondayTraining: StudentScheduleOccurrence = {
  schedule_id: 1,
  group_name: "Karate",
  effective_date: "2026-04-13",
  effective_start_time: "18:00:00",
  effective_end_time: "19:00:00",
  trainer_name: "Ivan Coach",
  location_name: "Main Hall",
};

describe("student-home-utils", () => {
  it("picks the nearest upcoming effective occurrence", () => {
    const result = findNextTrainingOccurrence(
      [
        {
          ...mondayTraining,
          schedule_id: 2,
          effective_date: "2026-04-14",
          effective_start_time: "17:00:00",
        },
        mondayTraining,
      ],
      new Date("2026-04-13T17:30:00"),
    );

    expect(result?.schedule_id).toBe(1);
  });

  it("skips already finished occurrences even if their template slot is today", () => {
    const result = findNextTrainingOccurrence(
      [
        mondayTraining,
        {
          ...mondayTraining,
          schedule_id: 3,
          effective_date: "2026-04-20",
          effective_start_time: "09:00:00",
        },
      ],
      new Date("2026-04-13T20:00:00"),
    );

    expect(result?.schedule_id).toBe(3);
  });

  it("returns the nearest group and personal streams separately", () => {
    const result = findNextTrainingStreams(
      [
        {
          ...mondayTraining,
          schedule_id: 4,
          group_name: "Personal",
          effective_date: "2026-04-14",
          effective_start_time: "11:00:00",
          training_type_kind: "personal",
          created_from: "personal_booking",
        },
        {
          ...mondayTraining,
          schedule_id: 5,
          group_name: "Group",
          effective_date: "2026-04-14",
          effective_start_time: "18:00:00",
          training_type_kind: "group",
        },
        {
          ...mondayTraining,
          schedule_id: 6,
          group_name: "Past Group",
          effective_date: "2026-04-13",
          effective_start_time: "10:00:00",
          training_type_kind: "group",
        },
      ],
      new Date("2026-04-13T17:30:00"),
    );

    expect(result.group?.schedule_id).toBe(5);
    expect(result.personal?.schedule_id).toBe(4);
  });

  it("formats upcoming labels as today, tomorrow, or weekday", () => {
    const now = new Date("2026-04-13T10:00:00");

    expect(formatUpcomingTrainingDayLabel("2026-04-13", now)).toBe("Сегодня");
    expect(formatUpcomingTrainingDayLabel("2026-04-14", now)).toBe("Завтра");
    expect(formatUpcomingTrainingDayLabel("2026-04-16", now)).toBe(
      "Четверг",
    );
  });

  it("parses date-only labels without timezone drift", () => {
    const now = new Date(2026, 3, 13, 23, 30, 0);

    expect(formatUpcomingTrainingDayLabel("2026-04-14", now)).toBe("Завтра");
    expect(formatUpcomingTrainingDayLabel("2026-04-15", now)).toBe("Среда");
  });
});
