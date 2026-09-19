const GENERIC_CHECKIN_ERROR =
  "Не удалось отметиться. Обратитесь к тренеру";

export const KIOSK_CHECKIN_ERROR_MESSAGES: Record<string, string> = {
  enrollment_frozen: "Абонемент заморожен",
  student_schedule_ineligible: "Вы не записаны на эту тренировку",
  student_ineligible: "Сейчас нельзя отметиться. Обратитесь к тренеру",
  schedule_occurrence_not_found: "Тренировка сейчас недоступна",
  group_session_closed: "Тренировка уже закрыта тренером",
  training_type_mismatch: "Тип тренировки не подходит для отметки",
  schedule_training_type_required:
    "Для этой тренировки не указан тип. Обратитесь к тренеру",
  drop_in_price_required:
    "Разовое занятие не настроено. Обратитесь к тренеру",
  one_time_date_mismatch: "Разовое занятие доступно только на свою дату",
  duplicate_checkin_conflict: "Вы уже отмечены на этой тренировке",
  already_checked_in: "Вы уже отмечены на этой тренировке",
  can_book_guest_visit: "Подойдите к тренеру, чтобы записаться на это занятие",
  subscription_component_limit_exceeded:
    "Лимит этого типа занятий исчерпан. Обратитесь к администратору",
  subscription_component_credits_exhausted:
    "Занятия этого типа закончились. Обратитесь к администратору",
  payroll_period_closed:
    "Посещение за этот день закрыто для расчёта. Обратитесь к администратору",
};

export const KIOSK_CHECKIN_ERROR_CODES = Object.freeze(
  Object.keys(KIOSK_CHECKIN_ERROR_MESSAGES),
);

interface ErrorPayload {
  code?: unknown;
  detail?: unknown;
  checkin_blocked_reason?: unknown;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object";
}

function extractPayload(error: unknown): ErrorPayload | null {
  if (!isObject(error)) return null;

  const response = error.response;
  if (isObject(response) && isObject(response.data)) {
    return response.data;
  }

  if (isObject(error.data)) {
    return error.data;
  }

  return error;
}

function stringValue(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : null;
}

function extractCode(payload: ErrorPayload | null): string | null {
  if (!payload) return null;
  return (
    stringValue(payload.code) ??
    stringValue(payload.checkin_blocked_reason) ??
    null
  );
}

function isSafeDetail(detail: string): boolean {
  if (detail.length > 120) return false;
  if (/[\r\n]/.test(detail)) return false;
  if (/@/.test(detail)) return false;
  if (/\+?\d[\d\s().-]{6,}\d/.test(detail)) return false;
  if (/private|note|замет|комментар|телефон|email|почт/i.test(detail)) {
    return false;
  }
  return true;
}

export function getKioskCheckinErrorMessage(error: unknown): string {
  const payload = extractPayload(error);
  const code = extractCode(payload);

  if (code && KIOSK_CHECKIN_ERROR_MESSAGES[code]) {
    return KIOSK_CHECKIN_ERROR_MESSAGES[code];
  }

  const detail = stringValue(payload?.detail);
  if (detail && isSafeDetail(detail)) {
    return detail;
  }

  return GENERIC_CHECKIN_ERROR;
}
