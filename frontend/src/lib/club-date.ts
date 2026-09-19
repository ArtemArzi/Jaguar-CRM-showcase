import { APP_TIME_ZONE } from "@/lib/locale";

const formatterCache = new Map<string, Intl.DateTimeFormat>();
const dateTimeFormatterCache = new Map<string, Intl.DateTimeFormat>();

function getDateFormatter(timeZone: string): Intl.DateTimeFormat {
  const safeTimeZone = timeZone || APP_TIME_ZONE;
  const cached = formatterCache.get(safeTimeZone);
  if (cached) return cached;

  try {
    const formatter = new Intl.DateTimeFormat("en-CA", {
      timeZone: safeTimeZone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    });
    formatterCache.set(safeTimeZone, formatter);
    return formatter;
  } catch {
    const formatter = new Intl.DateTimeFormat("en-CA", {
      timeZone: APP_TIME_ZONE,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    });
    formatterCache.set(safeTimeZone, formatter);
    return formatter;
  }
}

function getDateTimeFormatter(timeZone: string): Intl.DateTimeFormat {
  const safeTimeZone = timeZone || APP_TIME_ZONE;
  const cached = dateTimeFormatterCache.get(safeTimeZone);
  if (cached) return cached;

  try {
    const formatter = new Intl.DateTimeFormat("en-CA", {
      timeZone: safeTimeZone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
    });
    dateTimeFormatterCache.set(safeTimeZone, formatter);
    return formatter;
  } catch {
    const formatter = new Intl.DateTimeFormat("en-CA", {
      timeZone: APP_TIME_ZONE,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
    });
    dateTimeFormatterCache.set(safeTimeZone, formatter);
    return formatter;
  }
}

export function toDateParamInTimeZone(date: Date, timeZone = APP_TIME_ZONE): string {
  const parts = getDateFormatter(timeZone).formatToParts(date);
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}

export function toDateTimeParamInTimeZone(date: Date, timeZone = APP_TIME_ZONE): string {
  const parts = getDateTimeFormatter(timeZone).formatToParts(date);
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}T${values.hour}:${values.minute}:${values.second}`;
}

export function parseDateParam(value: string): Date {
  const [year = "1970", month = "01", day = "01"] = value.split("-");
  return new Date(Number(year), Number(month) - 1, Number(day));
}

export function todayInTimeZone(timeZone = APP_TIME_ZONE): Date {
  return parseDateParam(toDateParamInTimeZone(new Date(), timeZone));
}
