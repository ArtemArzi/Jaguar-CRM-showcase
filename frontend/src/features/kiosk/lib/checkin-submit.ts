import {
  fetchKioskOptions,
  kioskCheckin,
  kioskGuestBookAndCheckin,
} from "./kiosk-api";
import type {
  CheckinResult,
  KioskScheduleOption,
} from "./kiosk-api";
import { addPendingCheckin } from "./kiosk-db";

interface SubmitKioskCheckinInput {
  studentId: number;
  scheduleId: number;
  trainingTypeId: number;
  checkinDate?: string;
  online?: boolean;
}

const NETWORK_ERROR_CODES = new Set([
  "ERR_NETWORK",
  "ECONNABORTED",
  "ETIMEDOUT",
]);

export function isNetworkCheckinFailure(error: unknown): boolean {
  if (error instanceof TypeError) return true;
  if (!error || typeof error !== "object") return false;

  const maybeAxiosError = error as {
    code?: unknown;
    request?: unknown;
    response?: unknown;
  };

  if (
    typeof maybeAxiosError.code === "string" &&
    NETWORK_ERROR_CODES.has(maybeAxiosError.code)
  ) {
    return true;
  }

  return !!maybeAxiosError.request && maybeAxiosError.response == null;
}

export function buildQueuedCheckinResult(): CheckinResult {
  return {
    checkin_id: -1,
    is_debt: false,
    subscription_id: null,
    alerts: [
      {
        type: "offline",
        icon: "wifi-off",
        message: "Чек-ин сохранен. Синхронизируется при подключении",
      },
    ],
  };
}

function preflightError(code: string, detail: string) {
  return {
    data: { code, detail },
  };
}

function assertCanCheckin(option: KioskScheduleOption | undefined): void {
  if (!option) {
    throw preflightError(
      "schedule_occurrence_not_found",
      "Тренировка сейчас недоступна",
    );
  }

  if (option.self_checkin_status === "can_checkin") return;

  const code =
    option.reason_code ||
    (option.self_checkin_status === "can_book_guest_visit"
      ? "can_book_guest_visit"
      : "student_schedule_ineligible");
  throw preflightError(code, "Подойдите к тренеру");
}

async function fetchSelectedOnlineOption({
  studentId,
  scheduleId,
  checkinDate,
}: Pick<
  SubmitKioskCheckinInput,
  "studentId" | "scheduleId" | "checkinDate"
>): Promise<KioskScheduleOption | undefined> {
  const options = await fetchKioskOptions(studentId, checkinDate);
  return options.options.find((option) => option.schedule_id === scheduleId);
}

export async function submitKioskCheckinWithOfflineQueue({
  studentId,
  scheduleId,
  trainingTypeId,
  checkinDate,
  online = typeof navigator === "undefined" ? true : navigator.onLine,
}: SubmitKioskCheckinInput): Promise<CheckinResult> {
  if (online) {
    let preflightOption: KioskScheduleOption | undefined;
    let preflightFailedOnNetwork = false;
    try {
      preflightOption = await fetchSelectedOnlineOption({
        studentId,
        scheduleId,
        checkinDate,
      });
    } catch (error) {
      if (!isNetworkCheckinFailure(error)) {
        throw error;
      }
      preflightFailedOnNetwork = true;
    }

    if (preflightFailedOnNetwork) {
      await addPendingCheckin({
        student_id: studentId,
        schedule_id: scheduleId,
        training_type_id: trainingTypeId,
        checkin_date: checkinDate,
      });
      return buildQueuedCheckinResult();
    }

    if (preflightOption?.self_checkin_status === "can_book_guest_visit") {
      return kioskGuestBookAndCheckin(
        studentId,
        scheduleId,
        trainingTypeId,
        checkinDate,
      );
    }

    assertCanCheckin(preflightOption);

    try {
      return await kioskCheckin(
        studentId,
        scheduleId,
        trainingTypeId,
        checkinDate,
      );
    } catch (error) {
      if (!isNetworkCheckinFailure(error)) {
        throw error;
      }
    }
  }

  await addPendingCheckin({
    student_id: studentId,
    schedule_id: scheduleId,
    training_type_id: trainingTypeId,
    checkin_date: checkinDate,
  });
  return buildQueuedCheckinResult();
}
