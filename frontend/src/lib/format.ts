export function formatDateShort(dateStr: string): string {
  return new Date(dateStr).toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
}

export function formatDateLong(dateStr: string): string {
  const safe = dateStr.includes("T") ? dateStr : dateStr + "T00:00:00";
  return new Date(safe).toLocaleDateString("ru-RU", { day: "numeric", month: "long" });
}

export function formatDateFull(dateStr: string): string {
  return new Date(dateStr).toLocaleDateString("ru-RU", { day: "numeric", month: "short", year: "numeric" });
}

export function formatDaysMissed(days: number): string {
  if (days <= 0) return "Недавно был";
  if (days === 1) return "Не ходит 1 день";
  if (days < 5) return `Не ходит ${days} дня`;
  return `Не ходит ${days} дней`;
}

export function formatRelativeDate(dateStr: string): string {
  const d = new Date(dateStr);
  const now = new Date();
  const dMid = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const nMid = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const days = Math.floor((nMid.getTime() - dMid.getTime()) / 86_400_000);
  if (days === 0) return "Сегодня";
  if (days === 1) return "Вчера";
  if (days < 5) return `${days} дня назад`;
  return `${days} дней назад`;
}

/** Check if HH:MM time has passed (current time >= given time). */
export function hasTimeStarted(time: string): boolean {
  const now = new Date();
  const [h, m] = time.split(":").map(Number);
  return now.getHours() > h || (now.getHours() === h && now.getMinutes() >= m);
}

export function getMonthRange(year: number, month: number): { dateFrom: string; dateTo: string } {
  const dateFrom = `${year}-${String(month + 1).padStart(2, "0")}-01`;
  const lastDay = new Date(year, month + 1, 0).getDate();
  const dateTo = `${year}-${String(month + 1).padStart(2, "0")}-${String(lastDay).padStart(2, "0")}`;
  return { dateFrom, dateTo };
}
