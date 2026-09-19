import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { toDateParamInTimeZone } from "@/lib/club-date";
import { formatDateRu } from "@/lib/locale";
import { getApiError } from "@/lib/utils";
import { useAuthStore } from "@/features/auth/auth-store";
import { useBrandingStore } from "@/features/branding/use-branding";
import type { PersonalBooking, TrainerPersonalAvailabilitySlot } from "../types";
import { listTrainerAvailability } from "../lib/personal-availability";
import {
  personalBookingClubWallDateTime,
  personalRescheduleCandidates,
} from "../lib/personal-booking-reschedule";
import {
  personalCommercialContextQueryKey,
  useCommercialCacheScope,
} from "./personal-commercial-context-api";
import apiClient from "@/api/custom-fetch";

const DEFAULT_REASON = "Перенос по согласованию с клиентом";
const RESCHEDULE_LOOKAHEAD_DAYS = 89;

let idempotencySequence = 0;

function createIdempotencyKey() {
  const uuid = globalThis.crypto?.randomUUID?.();
  if (uuid) return `trainer-personal-reschedule:${uuid}`;
  idempotencySequence += 1;
  return `trainer-personal-reschedule:${Date.now().toString(36)}-${idempotencySequence}`;
}

function formatTimeRange(startsAt: string, endsAt: string, timeZone: string) {
  const startsAtWall = personalBookingClubWallDateTime(startsAt, timeZone);
  const endsAtWall = personalBookingClubWallDateTime(endsAt, timeZone);
  return `${startsAtWall.slice(11, 16)}–${endsAtWall.slice(11, 16)}`;
}

interface PersonalBookingRescheduleSheetProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  booking: PersonalBooking;
  studentId: number;
  studentName: string;
}

export function PersonalBookingRescheduleSheet({
  open,
  onOpenChange,
  booking,
  studentId,
  studentName,
}: PersonalBookingRescheduleSheetProps) {
  const queryClient = useQueryClient();
  const commercialCacheScope = useCommercialCacheScope();
  const clubId = useAuthStore((state) => state.clubId);
  const timeZone = useBrandingStore((state) => state.timeZone);
  const timeZoneStatus = useBrandingStore((state) => state.timeZoneStatus);
  const timeZoneClubId = useBrandingStore((state) => state.timeZoneClubId);
  const isTimeZoneAuthoritative = useBrandingStore(
    (state) => state.isTimeZoneAuthoritative,
  );
  const hasAuthoritativeTimeZone =
    clubId !== null &&
    timeZoneStatus === "ready" &&
    isTimeZoneAuthoritative &&
    timeZoneClubId === clubId;
  const [selectedSlotId, setSelectedSlotId] = useState<number | null>(null);
  const [reason, setReason] = useState(DEFAULT_REASON);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [availabilityStartedAt] = useState(() => new Date());
  const commandRef = useRef<{ fingerprint: string; key: string } | null>(null);

  const dateFrom = toDateParamInTimeZone(availabilityStartedAt, timeZone);
  const dateTo = toDateParamInTimeZone(
    new Date(availabilityStartedAt.getTime() + RESCHEDULE_LOOKAHEAD_DAYS * 24 * 60 * 60 * 1000),
    timeZone,
  );
  const availabilityQuery = useQuery<TrainerPersonalAvailabilitySlot[]>({
    queryKey: ["trainer", "availability", "personal-reschedule", dateFrom, dateTo],
    queryFn: () => listTrainerAvailability({
      dateFrom,
      dateTo,
      trainerId: booking.trainer_id,
    }),
    enabled: open && hasAuthoritativeTimeZone,
    staleTime: 0,
  });

  const candidates = personalRescheduleCandidates({
    booking,
    slots: availabilityQuery.data ?? [],
    timeZone,
  });
  const selectedSlot = candidates.find((slot) => slot.id === selectedSlotId) ?? null;
  const normalizedReason = reason.trim();

  function idempotencyKeyFor(slot: TrainerPersonalAvailabilitySlot, commandReason: string) {
    const fingerprint = [
      booking.booking_kind ?? "entitlement",
      booking.booking_id ?? booking.enrollment_id,
      booking.enrollment_id,
      slot.id,
      commandReason,
    ].join("|");
    if (commandRef.current?.fingerprint === fingerprint) return commandRef.current.key;
    const key = createIdempotencyKey();
    commandRef.current = { fingerprint, key };
    return key;
  }

  const rescheduleMutation = useMutation({
    mutationFn: () => {
      if (!hasAuthoritativeTimeZone) throw new Error("Не подтверждён часовой пояс клуба");
      if (!selectedSlot) throw new Error("Выберите новое время");
      if (!normalizedReason) throw new Error("Укажите причину переноса");
      const payload = {
        destination_slot_id: selectedSlot.id,
        reason: normalizedReason,
        idempotency_key: idempotencyKeyFor(selectedSlot, normalizedReason),
      };
      if (booking.booking_kind === "drop_in") {
        if (!booking.booking_id) throw new Error("Не найден идентификатор персоналки");
        return apiClient.post(
          `/personal-drop-in-bookings/${booking.booking_id}/reschedule/`,
          payload,
        );
      }
      return apiClient.post(
        `/personal-bookings/${booking.enrollment_id}/reschedule/`,
        payload,
      );
    },
    onSuccess: async () => {
      commandRef.current = null;
      setErrorMessage(null);
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["student", String(studentId), "personal-bookings"],
        }),
        queryClient.invalidateQueries({
          queryKey: personalCommercialContextQueryKey(studentId, commercialCacheScope),
        }),
        queryClient.invalidateQueries({ queryKey: ["trainer", "availability"] }),
        queryClient.invalidateQueries({ queryKey: ["schedules"] }),
      ]);
      onOpenChange(false);
    },
    onError: (error: unknown) => {
      setErrorMessage(
        `${getApiError(error, "Не удалось перенести персоналку")}. Время могло уже стать недоступно — обновите варианты и повторите попытку.`,
      );
    },
  });

  function handleOpenChange(nextOpen: boolean) {
    if (!nextOpen) {
      setSelectedSlotId(null);
      setReason(DEFAULT_REASON);
      setErrorMessage(null);
      commandRef.current = null;
    }
    onOpenChange(nextOpen);
  }

  if (!hasAuthoritativeTimeZone) {
    const isTimeZoneError =
      timeZoneStatus === "failed" ||
      (timeZoneStatus === "ready" &&
        (!isTimeZoneAuthoritative || timeZoneClubId !== clubId));
    return (
      <Sheet open={open} onOpenChange={handleOpenChange}>
        <SheetContent side="bottom">
          <SheetHeader>
            <SheetTitle>Перенести персоналку</SheetTitle>
            <SheetDescription>{studentName}</SheetDescription>
          </SheetHeader>
          <p
            role={isTimeZoneError ? "alert" : "status"}
            className={isTimeZoneError ? "ui-warning" : "ui-muted-status"}
          >
            {isTimeZoneError
              ? "Не удалось подтвердить часовой пояс клуба. Обновите страницу перед переносом."
              : "Проверяем часовой пояс клуба перед переносом..."}
          </p>
        </SheetContent>
      </Sheet>
    );
  }

  return (
    <Sheet open={open} onOpenChange={handleOpenChange}>
      <SheetContent side="bottom">
        <SheetHeader>
          <SheetTitle>Перенести персоналку</SheetTitle>
          <SheetDescription>{studentName}</SheetDescription>
        </SheetHeader>
        <div className="ui-sheet-body">
          <div className="rounded-lg bg-muted p-3 text-[13px] text-foreground">
            <p className="font-semibold">Текущая запись</p>
            <p className="mt-1">{formatDateRu(personalBookingClubWallDateTime(booking.starts_at, timeZone).slice(0, 10))}, {formatTimeRange(booking.starts_at, booking.ends_at, timeZone)}</p>
            <p className="mt-1 text-muted-foreground">{booking.trainer_name} · {booking.location_name}</p>
            <p className="text-muted-foreground">{booking.training_type_name}</p>
          </div>

          <div className="space-y-2">
            <p className="ui-field-label">Новое точное время</p>
            {availabilityQuery.isLoading ? (
              <p role="status" className="ui-muted-status">Загружаем свободное время…</p>
            ) : availabilityQuery.isError ? (
              <div role="alert" className="ui-warning">
                <p>Не удалось загрузить свободное время.</p>
                <Button
                  type="button"
                  variant="outline"
                  className="mt-2 min-h-[44px]"
                  onClick={() => void availabilityQuery.refetch()}
                >
                  Обновить варианты
                </Button>
              </div>
            ) : candidates.length === 0 ? (
              <p className="ui-muted-status">Совместимых свободных слотов пока нет.</p>
            ) : (
              <div className="space-y-2">
                {candidates.map((slot) => (
                  <Button
                    key={slot.id}
                    type="button"
                    variant={selectedSlotId === slot.id ? "default" : "outline"}
                    className="min-h-[44px] w-full justify-between text-left"
                    onClick={() => {
                      setSelectedSlotId(slot.id);
                      setErrorMessage(null);
                    }}
                  >
                    <span>{formatDateRu(slot.date)}, {formatTimeRange(slot.starts_at, slot.ends_at, timeZone)}</span>
                    <span className="text-xs font-normal">Выбрать</span>
                  </Button>
                ))}
              </div>
            )}
          </div>

          <label className="block space-y-1">
            <span className="ui-field-label">Причина переноса *</span>
            <textarea
              value={reason}
              onChange={(event) => {
                setReason(event.target.value);
                setErrorMessage(null);
              }}
              required
              rows={2}
              className="flex min-h-[72px] w-full rounded-md border border-input bg-background px-3 py-2 text-base ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 md:text-sm"
            />
          </label>

          {errorMessage ? (
            <div role="alert" className="ui-warning">
              <p>{errorMessage}</p>
              <Button
                type="button"
                variant="outline"
                className="mt-2 min-h-[44px]"
                onClick={() => void availabilityQuery.refetch()}
              >
                Обновить варианты
              </Button>
            </div>
          ) : null}

          <Button
            type="button"
            className="ui-brand-button min-h-[44px]"
            disabled={!selectedSlot || !normalizedReason || rescheduleMutation.isPending}
            onClick={() => rescheduleMutation.mutate()}
          >
            {rescheduleMutation.isPending ? "Переносим…" : "Подтвердить перенос"}
          </Button>
          {errorMessage && selectedSlot ? (
            <Button
              type="button"
              variant="outline"
              className="min-h-[44px]"
              disabled={rescheduleMutation.isPending}
              onClick={() => rescheduleMutation.mutate()}
            >
              Повторить перенос
            </Button>
          ) : null}
        </div>
      </SheetContent>
    </Sheet>
  );
}
