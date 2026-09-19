import type { KioskScheduleOption } from "./kiosk-api";

export type KioskOptionDecision =
  | { kind: "auto"; option: KioskScheduleOption }
  | { kind: "select"; options: KioskScheduleOption[] }
  | { kind: "wait"; options: KioskScheduleOption[] }
  | { kind: "blocked"; reasonCode: string }
  | { kind: "unavailable" };

const CURRENT_BOOKING_FEEDBACK_REASONS = new Set([
  "already_checked_in",
  "enrollment_frozen",
  "group_session_closed",
]);

function sortByOpeningTime(
  options: KioskScheduleOption[],
): KioskScheduleOption[] {
  return [...options].sort((left, right) =>
    left.checkin_opens_at.localeCompare(right.checkin_opens_at),
  );
}

function decideOpenOptions(
  options: KioskScheduleOption[],
): KioskOptionDecision | null {
  if (options.length === 1) {
    return { kind: "auto", option: options[0] };
  }
  if (options.length > 1) {
    return { kind: "select", options };
  }
  return null;
}

/**
 * Resolve the public kiosk action from server-owned, person-aware options.
 *
 * An existing booking/enrollment always wins over a walk-in guest option.
 * A future own booking becomes a waiting state rather than silently booking
 * the student into an unrelated session that happens to be open now.
 */
export function getKioskOptionDecision(
  options: KioskScheduleOption[],
): KioskOptionDecision {
  const ownOpen = options.filter(
    (option) =>
      option.self_checkin_status === "can_checkin" &&
      option.checkin_window_status === "open",
  );
  const ownOpenDecision = decideOpenOptions(ownOpen);
  if (ownOpenDecision) return ownOpenDecision;

  const ownUpcoming = sortByOpeningTime(
    options.filter(
      (option) =>
        option.self_checkin_status === "can_checkin" &&
        option.checkin_window_status === "too_early",
    ),
  );
  if (ownUpcoming.length > 0) {
    return { kind: "wait", options: ownUpcoming };
  }

  const currentBookingBlock = options.find(
    (option) =>
      option.self_checkin_status === "blocked" &&
      option.checkin_window_status === "open" &&
      CURRENT_BOOKING_FEEDBACK_REASONS.has(option.reason_code),
  );
  if (currentBookingBlock) {
    return {
      kind: "blocked",
      reasonCode: currentBookingBlock.reason_code,
    };
  }

  const guestOpen = options.filter(
    (option) =>
      option.self_checkin_status === "can_book_guest_visit" &&
      option.checkin_window_status === "open",
  );
  const guestOpenDecision = decideOpenOptions(guestOpen);
  if (guestOpenDecision) return guestOpenDecision;

  const currentGenericBlock = options.find(
    (option) =>
      option.self_checkin_status === "blocked" &&
      option.checkin_window_status === "open",
  );
  if (currentGenericBlock) {
    return {
      kind: "blocked",
      reasonCode:
        currentGenericBlock.reason_code || "student_schedule_ineligible",
    };
  }

  return { kind: "unavailable" };
}

export function formatKioskCheckinOpening(
  option: KioskScheduleOption,
): string {
  const timeMatch = option.checkin_opens_at.match(/T(\d{2}:\d{2})/);
  if (timeMatch) return timeMatch[1];

  const [hours = "00", minutes = "00"] = option.start_time.split(":");
  const startMinutes = Number(hours) * 60 + Number(minutes);
  const openingMinutes = (startMinutes - 30 + 24 * 60) % (24 * 60);
  return `${String(Math.floor(openingMinutes / 60)).padStart(2, "0")}:${String(
    openingMinutes % 60,
  ).padStart(2, "0")}`;
}
