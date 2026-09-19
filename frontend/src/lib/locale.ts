export const DAY_LABELS = [
  "Понедельник",
  "Вторник",
  "Среда",
  "Четверг",
  "Пятница",
  "Суббота",
  "Воскресенье",
] as const;

export const MONTH_NAMES = [
  "Январь",
  "Февраль",
  "Март",
  "Апрель",
  "Май",
  "Июнь",
  "Июль",
  "Август",
  "Сентябрь",
  "Октябрь",
  "Ноябрь",
  "Декабрь",
] as const;

export const APP_TIME_ZONE = "Asia/Yekaterinburg";

const ruDateFormatter = new Intl.DateTimeFormat("ru-RU");

export function formatDateRu(date: string | Date): string {
  if (typeof date === "string") {
    const localDateMatch = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date);
    if (localDateMatch) {
      const [, year, month, day] = localDateMatch;
      return ruDateFormatter.format(
        new Date(Number(year), Number(month) - 1, Number(day)),
      );
    }
  }
  return ruDateFormatter.format(
    typeof date === "string" ? new Date(date) : date,
  );
}
