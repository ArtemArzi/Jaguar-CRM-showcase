import { DAY_LABELS } from "@/lib/locale";
import {
  isPersonalScheduleOccurrence,
  parseDateOnly,
  parseOccurrenceDateTime,
  type StudentScheduleOccurrence,
} from "./student-schedule-utils";

function toOccurrenceDateTime(occurrence: StudentScheduleOccurrence): Date {
  return parseOccurrenceDateTime(
    occurrence.effective_date,
    occurrence.effective_start_time,
  );
}

export function findNextTrainingOccurrence(
  occurrences: StudentScheduleOccurrence[],
  now: Date = new Date(),
): StudentScheduleOccurrence | null {
  const upcoming = occurrences
    .filter((occurrence) => toOccurrenceDateTime(occurrence) >= now)
    .sort(
      (left, right) =>
        toOccurrenceDateTime(left).getTime() -
        toOccurrenceDateTime(right).getTime(),
    );

  return upcoming[0] ?? null;
}

export interface NextTrainingStreams {
  group: StudentScheduleOccurrence | null;
  personal: StudentScheduleOccurrence | null;
}

export function findNextTrainingStreams(
  occurrences: StudentScheduleOccurrence[],
  now: Date = new Date(),
): NextTrainingStreams {
  const upcoming = occurrences
    .filter((occurrence) => toOccurrenceDateTime(occurrence) >= now)
    .sort(
      (left, right) =>
        toOccurrenceDateTime(left).getTime() -
        toOccurrenceDateTime(right).getTime(),
    );

  return {
    group: upcoming.find((occurrence) => !isPersonalScheduleOccurrence(occurrence)) ?? null,
    personal: upcoming.find(isPersonalScheduleOccurrence) ?? null,
  };
}

export function formatUpcomingTrainingDayLabel(
  effectiveDate: string,
  now: Date = new Date(),
): string {
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());

  const tomorrow = new Date(today);
  tomorrow.setDate(today.getDate() + 1);

  const target = parseDateOnly(effectiveDate);

  if (target.getTime() === today.getTime()) {
    return "Сегодня";
  }

  if (target.getTime() === tomorrow.getTime()) {
    return "Завтра";
  }

  return DAY_LABELS[(target.getDay() + 6) % 7] ?? "";
}
