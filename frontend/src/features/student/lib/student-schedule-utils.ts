import { DAY_LABELS } from "@/lib/locale";

export interface StudentScheduleOccurrence {
  schedule_id: number;
  group_name: string;
  effective_date: string;
  effective_start_time: string;
  effective_end_time: string;
  trainer_name: string;
  location_name: string;
  one_time_date?: string | null;
  training_type_id?: number | null;
  training_type_name?: string | null;
  training_type_kind?: string | null;
  is_rescheduled?: boolean;
  is_substitute?: boolean;
  enrollment_id?: number | null;
  created_from?: string;
  can_cancel?: boolean;
}

export function parseDateOnly(value: string): Date {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) {
    return new Date(value);
  }

  const [, year, month, day] = match;
  return new Date(Number(year), Number(month) - 1, Number(day));
}

export function parseOccurrenceDateTime(date: string, time: string): Date {
  const parsed = parseDateOnly(date);
  const [hours = "0", minutes = "0", seconds = "0"] = time.split(":");
  parsed.setHours(Number(hours), Number(minutes), Number(seconds), 0);
  return parsed;
}

export function getMonday(date: Date): Date {
  const d = new Date(date);
  const day = d.getDay();
  const diff = day === 0 ? -6 : 1 - day;
  d.setDate(d.getDate() + diff);
  d.setHours(0, 0, 0, 0);
  return d;
}

export function toDateParam(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

export function formatWeekLabel(monday: Date): string {
  const sunday = new Date(monday);
  sunday.setDate(monday.getDate() + 6);

  const dayStart = monday.getDate();
  const dayEnd = sunday.getDate();
  const monthEnd = sunday.toLocaleDateString("ru-RU", { month: "long" });

  if (monday.getMonth() === sunday.getMonth()) {
    return `${dayStart}\u2013${dayEnd} ${monthEnd}`;
  }
  const monthStart = monday.toLocaleDateString("ru-RU", { month: "long" });
  return `${dayStart} ${monthStart} \u2013 ${dayEnd} ${monthEnd}`;
}

export function formatClockTime(time: string): string {
  return time.slice(0, 5);
}

export function isPersonalScheduleOccurrence(
  occurrence: StudentScheduleOccurrence,
): boolean {
  return (
    occurrence.training_type_kind === "personal" ||
    occurrence.training_type_kind === "mini_group" ||
    occurrence.created_from === "personal_booking"
  );
}

export function getScheduleOccurrenceStatusLabel(
  occurrence: StudentScheduleOccurrence,
): string {
  if (isPersonalScheduleOccurrence(occurrence)) {
    return "Персоналка забронирована";
  }
  if (occurrence.created_from === "student_self_booking") {
    return "Вы записались";
  }
  if (occurrence.enrollment_id) {
    return "Вы уже записаны";
  }
  return "В расписании";
}

export function getScheduleOccurrenceKindLabel(
  occurrence: StudentScheduleOccurrence,
): string {
  return isPersonalScheduleOccurrence(occurrence) ? "Персоналка" : "Группа";
}

export function groupOccurrencesByDate(
  schedule: StudentScheduleOccurrence[],
): Array<{
  date: string;
  label: string;
  items: StudentScheduleOccurrence[];
}> {
  const map = new Map<string, StudentScheduleOccurrence[]>();

  for (const item of schedule) {
    const existing = map.get(item.effective_date) ?? [];
    existing.push(item);
    map.set(item.effective_date, existing);
  }

  return Array.from(map.entries())
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([date, items]) => {
      const parsed = parseDateOnly(date);
      const label = `${DAY_LABELS[(parsed.getDay() + 6) % 7]}, ${parsed.toLocaleDateString("ru-RU", {
        day: "numeric",
        month: "long",
      })}`;
      return {
        date,
        label,
        items: items.sort((a, b) =>
          a.effective_start_time.localeCompare(b.effective_start_time),
        ),
      };
    });
}
