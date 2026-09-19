import { toDateTimeParamInTimeZone } from "@/lib/club-date";
import type { PersonalBooking, TrainerPersonalAvailabilitySlot } from "../types";

const EXPLICIT_TIME_ZONE_SUFFIX = /(?:Z|[+-]\d{2}:\d{2})$/i;

export function personalBookingClubWallDateTime(value: string, timeZone: string) {
  if (!EXPLICIT_TIME_ZONE_SUFFIX.test(value)) return value.slice(0, 19);
  const instant = new Date(value);
  return Number.isNaN(instant.getTime())
    ? value.slice(0, 19)
    : toDateTimeParamInTimeZone(instant, timeZone);
}

function isSameExactTime(
  slot: TrainerPersonalAvailabilitySlot,
  booking: PersonalBooking,
  timeZone: string,
) {
  return (
    personalBookingClubWallDateTime(slot.starts_at, timeZone) ===
      personalBookingClubWallDateTime(booking.starts_at, timeZone) &&
    personalBookingClubWallDateTime(slot.ends_at, timeZone) ===
      personalBookingClubWallDateTime(booking.ends_at, timeZone)
  );
}

export function personalRescheduleCandidates({
  booking,
  slots,
  timeZone,
  now = new Date(),
}: {
  booking: PersonalBooking;
  slots: readonly TrainerPersonalAvailabilitySlot[];
  timeZone: string;
  now?: Date;
}) {
  return slots.filter(
    (slot) =>
      slot.status === "published" &&
      slot.trainer_id === booking.trainer_id &&
      slot.location_id === booking.location_id &&
      slot.training_type_id === booking.training_type_id &&
      !isSameExactTime(slot, booking, timeZone) &&
      new Date(slot.starts_at).getTime() > now.getTime(),
  );
}
