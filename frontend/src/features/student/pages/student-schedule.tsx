import { useMemo, useState } from "react";
import { CalendarPlus, ChevronLeft, ChevronRight, MapPin, UserRound, XCircle } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuthStore } from "@/features/auth/auth-store";
import apiClient from "@/api/custom-fetch";
import {
  hasOnlinePaymentsCapability,
  usePaymentCapabilities,
} from "@/api/payment-capabilities";
import {
  getPersonalAvailabilityCapabilityMode,
  usePersonalAvailabilityCapabilityQuery,
} from "@/api/unified-client-journey";
import {
  SelfServicePersonalBookingSection,
  SelfServicePersonalCommandCards,
  SelfServicePersonalUnavailableNotice,
} from "@/components/portal/self-service-personal";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CardContent } from "@/components/ui/card";
import {
  GuestSelfBookingSheet,
  PersonalPaymentReservationLinkPanel,
  type GuestBookingDateOption,
  type GuestBookingOption,
  type GuestVisitOut,
  type PersonalAvailabilityOption,
  type PersonalBookingPaymentReservation,
  type PersonalBookingOut,
} from "@/components/portal/guest-self-booking-section";
import { isVisiblePersonalPaymentReservation } from "@/components/portal/personal-payment-reservation-state";
import { Skeleton } from "@/components/ui/skeleton";
import { useBrandingStore } from "@/features/branding/use-branding";
import { toDateParamInTimeZone, todayInTimeZone } from "@/lib/club-date";
import { getApiError } from "@/lib/utils";
import { StudentPageIntro } from "../components/student-page-intro";
import { StudentSectionTitle } from "../components/student-section-title";
import { StudentSurfaceCard } from "../components/student-surface-card";
import {
  formatClockTime,
  formatWeekLabel,
  getScheduleOccurrenceKindLabel,
  getScheduleOccurrenceStatusLabel,
  getMonday,
  groupOccurrencesByDate,
  toDateParam,
  type StudentScheduleOccurrence,
} from "../lib/student-schedule-utils";

function formatLessonCount(count: number): string {
  const remainder10 = count % 10;
  const remainder100 = count % 100;

  if (remainder10 === 1 && remainder100 !== 11) return `${count} занятие`;
  if (remainder10 >= 2 && remainder10 <= 4 && (remainder100 < 10 || remainder100 >= 20)) {
    return `${count} занятия`;
  }
  return `${count} занятий`;
}

function getPersonalBookingErrorMessage(error: unknown, fallback: string) {
  const code =
    error && typeof error === "object"
      ? (error as { response?: { data?: { code?: unknown } } }).response?.data?.code
      : undefined;
  if (code === "personal_offer_changed") {
    return "Цена или условия персоналки изменились. Мы обновили доступные слоты — проверьте сумму и повторите действие.";
  }
  if (
    code === "personal_booking_tariff_not_configured" ||
    code === "personal_booking_tariff_ambiguous"
  ) {
    return "Цена персональной тренировки пока не настроена. Обратитесь в клуб.";
  }
  return getApiError(error, fallback);
}

function formatWeekScopeLabel(weekOffset: number): string {
  if (weekOffset === 0) return "Текущая неделя";
  const direction = weekOffset > 0 ? "вперёд" : "назад";
  const absoluteOffset = Math.abs(weekOffset);
  return `${direction} на ${absoluteOffset} нед.`;
}

function buildWeekBookingDates(monday: Date): GuestBookingDateOption[] {
  return Array.from({ length: 7 }, (_, index) => {
    const date = new Date(monday);
    date.setDate(monday.getDate() + index);
    return {
      value: toDateParam(date),
      shortLabel: date.toLocaleDateString("ru-RU", { weekday: "short" }),
      label: date.toLocaleDateString("ru-RU", { day: "numeric", month: "short" }),
    };
  });
}

function defaultBookingDate(dates: readonly GuestBookingDateOption[], timeZone: string) {
  const today = toDateParamInTimeZone(new Date(), timeZone);
  return dates.some((date) => date.value === today) ? today : (dates[0]?.value ?? today);
}

function getCancelBookingPath(item: StudentScheduleOccurrence): string | null {
  if (!item.can_cancel || !item.enrollment_id) return null;
  if (item.created_from === "personal_booking") {
    return `/personal-bookings/${item.enrollment_id}/cancel/`;
  }
  if (item.created_from === "student_self_booking") {
    return `/guest-bookings/${item.enrollment_id}/cancel/`;
  }
  return null;
}

function getCancelBookingErrorMessage(error: unknown): string {
  const e = error as { response?: { data?: { code?: string } } };
  if (e?.response?.data?.code === "booking_past_date") {
    return "Эту запись уже нельзя отменить, занятие прошло";
  }
  return getApiError(error, "Не удалось отменить запись");
}

function ScheduleSkeleton() {
  return (
    <div className="min-h-full bg-[radial-gradient(circle_at_top,_rgba(0,0,0,0.025),_transparent_36%)] px-5 pb-24 pt-5">
      <div className="space-y-4">
        <div className="space-y-2">
          <Skeleton className="h-3 w-28" />
          <Skeleton className="h-8 w-56" />
          <Skeleton className="h-12 w-full max-w-[30rem]" />
        </div>
        <Skeleton className="h-36 w-full rounded-[24px]" />
        <Skeleton className="h-24 w-full rounded-[24px]" />
        <Skeleton className="h-24 w-full rounded-[24px]" />
      </div>
    </div>
  );
}

export default function StudentSchedule() {
  const queryClient = useQueryClient();
  const paymentCapabilitiesQuery = usePaymentCapabilities();
  const personalAvailabilityCapability = usePersonalAvailabilityCapabilityQuery();
  const personalAvailabilityCapabilityMode = getPersonalAvailabilityCapabilityMode(
    personalAvailabilityCapability,
  );
  const unifiedClientJourneyEnabled = personalAvailabilityCapabilityMode === "unified";
  const legacyPersonalAvailabilityEnabled = personalAvailabilityCapabilityMode === "legacy";
  const studentId = useAuthStore((s) => s.studentId);
  const bootstrapStatus = useAuthStore((s) => s.studentBootstrapStatus);
  const timeZone = useBrandingStore((s) => s.timeZone);
  const [weekOffset, setWeekOffset] = useState(0);
  const currentMonday = useMemo(() => {
    const monday = getMonday(todayInTimeZone(timeZone));
    monday.setDate(monday.getDate() + weekOffset * 7);
    return monday;
  }, [timeZone, weekOffset]);
  const bookingDates = useMemo(
    () => buildWeekBookingDates(currentMonday),
    [currentMonday],
  );
  const [requestedBookingDate, setRequestedBookingDate] = useState(() =>
    defaultBookingDate(buildWeekBookingDates(getMonday(todayInTimeZone(timeZone))), timeZone),
  );
  const bookingDate = useMemo(() => {
    return bookingDates.some((date) => date.value === requestedBookingDate)
      ? requestedBookingDate
      : defaultBookingDate(bookingDates, timeZone);
  }, [bookingDates, requestedBookingDate, timeZone]);
  const [bookingSheetOpen, setBookingSheetOpen] = useState(false);
  const [bookedScheduleId, setBookedScheduleId] = useState<number | null>(null);
  const [bookedPersonalSlotId, setBookedPersonalSlotId] = useState<number | null>(null);
  const [createdPersonalPaymentReservation, setCreatedPersonalPaymentReservation] =
    useState<PersonalBookingPaymentReservation | null>(null);
  const [createdPersonalPaymentReservationAt, setCreatedPersonalPaymentReservationAt] =
    useState<number | null>(null);
  const [hiddenPersonalPaymentReservationIds, setHiddenPersonalPaymentReservationIds] = useState<
    number[]
  >([]);
  const [bookingErrorMessage, setBookingErrorMessage] = useState<string | null>(null);
  const [personalBookingErrorMessage, setPersonalBookingErrorMessage] = useState<string | null>(
    null,
  );
  const [cancelBookingErrorMessage, setCancelBookingErrorMessage] = useState<string | null>(
    null,
  );

  const {
    data: schedule = [],
    isLoading,
    isError,
  } = useQuery({
    queryKey: ["student", "schedule-week", studentId, toDateParam(currentMonday)],
    queryFn: () =>
      apiClient
        .get<StudentScheduleOccurrence[]>("/students/me/schedule-week/", {
          params: { week_start: toDateParam(currentMonday) },
        })
        .then((r) => r.data),
    enabled: bootstrapStatus === "resolved" && !!studentId,
    staleTime: 5 * 60_000,
  });

  const {
    data: knownSchedule = [],
    isLoading: knownScheduleLoading,
  } = useQuery({
    queryKey: ["student", "schedule-known", studentId],
    queryFn: () =>
      apiClient
        .get<StudentScheduleOccurrence[] | Array<unknown>>("/students/me/schedule/")
        .then((r) => r.data),
    enabled: bootstrapStatus === "resolved" && !!studentId,
    staleTime: 5 * 60_000,
  });

  const {
    data: bookingOptions = [],
    isLoading: bookingOptionsLoading,
    isError: bookingOptionsError,
    refetch: refetchBookingOptions,
  } = useQuery<GuestBookingOption[]>({
    queryKey: ["student", "guest-booking-options", studentId, bookingDate],
    queryFn: () =>
      apiClient
        .get<GuestBookingOption[]>("/schedules/guest-booking-options/", {
          params: { date: bookingDate },
        })
        .then((r) => r.data),
    enabled:
      legacyPersonalAvailabilityEnabled &&
      bookingSheetOpen &&
      bootstrapStatus === "resolved" &&
      !!studentId &&
      !!bookingDate,
    staleTime: 60_000,
  });

  const {
    data: personalOptions = [],
    isLoading: personalOptionsLoading,
    isError: personalOptionsError,
    refetch: refetchPersonalOptions,
  } = useQuery<PersonalAvailabilityOption[]>({
    queryKey: ["student", "personal-availability-options", studentId, bookingDate],
    queryFn: () =>
      apiClient
        .get<PersonalAvailabilityOption[]>("/personal-availability/options/", {
          params: { date: bookingDate },
        })
        .then((r) => r.data),
    enabled:
      legacyPersonalAvailabilityEnabled &&
      bookingSheetOpen &&
      bootstrapStatus === "resolved" &&
      !!studentId &&
      !!bookingDate,
    staleTime: 60_000,
  });

  const {
    data: pendingPersonalPaymentReservations = [],
    isError: pendingPersonalPaymentReservationsError,
    isFetching: pendingPersonalPaymentReservationsFetching,
    dataUpdatedAt: pendingPersonalPaymentReservationsUpdatedAt,
  } = useQuery<
    PersonalBookingPaymentReservation[]
  >({
    queryKey: ["student", "personal-payment-reservations", studentId, "open_actionable"],
    queryFn: () =>
      apiClient
        .get<PersonalBookingPaymentReservation[]>(
          "/personal-availability/payment-reservations/",
          {
            params: { status: "open_actionable" },
          },
        )
        .then((r) => r.data),
    enabled:
      legacyPersonalAvailabilityEnabled && bootstrapStatus === "resolved" && !!studentId,
    staleTime: 30_000,
  });

  const bookingMutation = useMutation({
    mutationFn: (option: GuestBookingOption) =>
      apiClient
        .post<GuestVisitOut>(`/schedules/${option.schedule_id}/guest-bookings/`, {
          date: option.date,
          idempotency_key: `student-self-booking-${studentId}-${option.schedule_id}-${option.date}`,
        })
        .then((r) => r.data),
    onSuccess: (booking, option) => {
      setBookedScheduleId(booking.schedule_id);
      setBookingErrorMessage(null);
      void queryClient.invalidateQueries({
        queryKey: ["student", "guest-booking-options", studentId, option.date],
      });
      void queryClient.invalidateQueries({
        queryKey: ["student", "schedule-week", studentId],
      });
      void queryClient.invalidateQueries({
        queryKey: ["student", "schedule-known", studentId],
      });
    },
    onError: (error) => {
      setBookingErrorMessage(getApiError(error, "Не удалось записаться"));
    },
  });

  const personalBookingMutation = useMutation({
    mutationFn: (option: PersonalAvailabilityOption) =>
      apiClient
        .post<PersonalBookingOut>(`/personal-availability/${option.slot_id}/book/`, {
          subscription_id: option.subscription_id,
          idempotency_key: `student-personal-self-booking-${studentId}-${option.slot_id}`,
        })
        .then((r) => r.data),
    onSuccess: (booking, option) => {
      setBookedPersonalSlotId(booking.availability_slot_id ?? option.slot_id);
      setPersonalBookingErrorMessage(null);
      void queryClient.invalidateQueries({
        queryKey: ["student", "personal-availability-options", studentId, option.date],
      });
      void queryClient.invalidateQueries({
        queryKey: ["student", "schedule-week", studentId],
      });
      void queryClient.invalidateQueries({
        queryKey: ["student", "schedule-known", studentId],
      });
    },
    onError: (error) => {
      if (
        (error as { response?: { data?: { code?: unknown } } })?.response?.data?.code ===
        "personal_offer_changed"
      ) {
        void queryClient.invalidateQueries({
          queryKey: ["student", "personal-availability-options", studentId],
        });
      }
      setPersonalBookingErrorMessage(getPersonalBookingErrorMessage(error, "Не удалось записаться"));
    },
  });

  const personalPaymentReservationMutation = useMutation({
    mutationFn: (option: PersonalAvailabilityOption) => {
      if (!hasOnlinePaymentsCapability(paymentCapabilitiesQuery)) {
        throw new Error("Online payment capability is unavailable");
      }
      const tariffId = unifiedClientJourneyEnabled
        ? option.offer_tariff_id || null
        : option.payment_tariff_id;
      if (!tariffId) {
        throw new Error("Personal payment tariff is not available");
      }
      return apiClient
        .post<PersonalBookingPaymentReservation>(
          `/personal-availability/${option.slot_id}/payment-reservations/`,
          {
            tariff_id: tariffId,
            ...(unifiedClientJourneyEnabled ? { offer_digest: option.offer_digest } : {}),
            idempotency_key: `student-personal-payment-reservation-${studentId}-${option.slot_id}-${tariffId}`,
          },
        )
        .then((r) => r.data);
    },
    onSuccess: (reservation, option) => {
      setCreatedPersonalPaymentReservation(reservation);
      setCreatedPersonalPaymentReservationAt(Date.now());
      setHiddenPersonalPaymentReservationIds((current) =>
        current.filter((id) => id !== reservation.id),
      );
      setPersonalBookingErrorMessage(null);
      void queryClient.invalidateQueries({
        queryKey: ["student", "personal-availability-options", studentId, option.date],
      });
      void queryClient.invalidateQueries({
        queryKey: ["student", "personal-payment-reservations", studentId, "open_actionable"],
      });
    },
    onError: (error) => {
      if (
        (error as { response?: { data?: { code?: unknown } } })?.response?.data?.code ===
        "personal_offer_changed"
      ) {
        void queryClient.invalidateQueries({
          queryKey: ["student", "personal-availability-options", studentId],
        });
      }
      setPersonalBookingErrorMessage(
        getPersonalBookingErrorMessage(error, "Не удалось создать ссылку на оплату"),
      );
    },
  });

  const cancelPersonalPaymentReservationMutation = useMutation({
    mutationFn: (reservation: PersonalBookingPaymentReservation) =>
      apiClient
        .post<PersonalBookingPaymentReservation>(
          `/personal-availability/payment-reservations/${reservation.id}/cancel/`,
          {},
        )
        .then((r) => r.data),
    onSuccess: (reservation) => {
      setHiddenPersonalPaymentReservationIds((current) =>
        current.includes(reservation.id) ? current : [...current, reservation.id],
      );
      setCreatedPersonalPaymentReservation((current) =>
        current?.id === reservation.id ? null : current,
      );
      setCreatedPersonalPaymentReservationAt((current) =>
        createdPersonalPaymentReservation?.id === reservation.id ? null : current,
      );
      setPersonalBookingErrorMessage(null);
      void queryClient.invalidateQueries({
        queryKey: ["student", "personal-payment-reservations", studentId, "open_actionable"],
      });
      void queryClient.invalidateQueries({
        queryKey: ["student", "personal-availability-options", studentId],
      });
    },
    onError: (error) => {
      setPersonalBookingErrorMessage(getApiError(error, "Не удалось отменить ссылку"));
    },
  });

  const cancelBookingMutation = useMutation({
    mutationFn: (item: StudentScheduleOccurrence) => {
      const path = getCancelBookingPath(item);
      if (!path) {
        throw new Error("Booking cannot be cancelled from this screen");
      }
      return apiClient.post(path, { reason: "" }).then((r) => r.data);
    },
    onSuccess: () => {
      setCancelBookingErrorMessage(null);
      void queryClient.invalidateQueries({
        queryKey: ["student", "schedule-week", studentId],
      });
      void queryClient.invalidateQueries({
        queryKey: ["student", "schedule-known", studentId],
      });
      void queryClient.invalidateQueries({
        queryKey: ["student", "guest-booking-options", studentId],
      });
      void queryClient.invalidateQueries({
        queryKey: ["student", "personal-availability-options", studentId],
      });
    },
    onError: (error) => {
      setCancelBookingErrorMessage(getCancelBookingErrorMessage(error));
      void queryClient.invalidateQueries({
        queryKey: ["student", "schedule-week", studentId],
      });
      void queryClient.invalidateQueries({
        queryKey: ["student", "schedule-known", studentId],
      });
    },
  });

  const grouped = useMemo(() => {
    return groupOccurrencesByDate(schedule);
  }, [schedule]);
  const visiblePersonalPaymentReservations = useMemo(() => {
    const hidden = new Set(hiddenPersonalPaymentReservationIds);
    const reservations = new Map<number, PersonalBookingPaymentReservation>();
    const serverVisibleReservationIds = new Set<number>();
    const append = (reservation: PersonalBookingPaymentReservation | null) => {
      if (!reservation || hidden.has(reservation.id)) return;
      if (!isVisiblePersonalPaymentReservation(reservation)) return;
      reservations.set(reservation.id, reservation);
    };

    pendingPersonalPaymentReservations.forEach((reservation) => {
      if (isVisiblePersonalPaymentReservation(reservation)) {
        serverVisibleReservationIds.add(reservation.id);
      }
      append(reservation);
    });

    const createdStillAwaitingServerSnapshot =
      createdPersonalPaymentReservationAt !== null &&
      (pendingPersonalPaymentReservationsFetching ||
        pendingPersonalPaymentReservationsUpdatedAt < createdPersonalPaymentReservationAt);
    if (
      pendingPersonalPaymentReservationsError ||
      createdStillAwaitingServerSnapshot ||
      (createdPersonalPaymentReservation &&
        serverVisibleReservationIds.has(createdPersonalPaymentReservation.id))
    ) {
      append(createdPersonalPaymentReservation);
    }
    return Array.from(reservations.values());
  }, [
    createdPersonalPaymentReservation,
    createdPersonalPaymentReservationAt,
    hiddenPersonalPaymentReservationIds,
    pendingPersonalPaymentReservations,
    pendingPersonalPaymentReservationsError,
    pendingPersonalPaymentReservationsFetching,
    pendingPersonalPaymentReservationsUpdatedAt,
  ]);

  const totalSessions = grouped.reduce((count, day) => count + day.items.length, 0);
  const hasSpecialStates = schedule.some((item) => item.is_rescheduled || item.is_substitute);

  if (bootstrapStatus === "idle" || bootstrapStatus === "loading") {
    return <ScheduleSkeleton />;
  }

  if (!studentId || isLoading || knownScheduleLoading) return <ScheduleSkeleton />;

  if (isError) {
    return (
      <div className="min-h-full bg-[radial-gradient(circle_at_top,_rgba(0,0,0,0.025),_transparent_36%)] px-5 pb-24 pt-5">
        <StudentSurfaceCard>
          <CardContent className="space-y-2 py-5 text-center">
            <p className="ui-overline">
              Расписание
            </p>
            <p className="text-[18px] font-semibold">Не удалось загрузить данные</p>
            <p className="ui-body-muted">
              Попробуйте обновить страницу или зайти чуть позже.
            </p>
          </CardContent>
        </StudentSurfaceCard>
      </div>
    );
  }

  return (
    <div className="min-h-full bg-[radial-gradient(circle_at_top,_rgba(0,0,0,0.025),_transparent_36%)] px-5 pb-24 pt-5">
      <div className="space-y-5">
        <StudentPageIntro
          eyebrow="Личный кабинет"
          title="Расписание"
          description="Смотри ближайшие слоты, переносы и замены в одном спокойном экране."
        />

        <StudentSurfaceCard className="overflow-hidden">
          <CardContent className="space-y-3.5 py-3.5">
            <div className="ui-row-between">
              <div className="space-y-1">
                <p className="ui-overline">
                  Неделя занятий
                </p>
                <h2 className="text-[20px] font-semibold leading-tight">
                  {formatWeekLabel(currentMonday)}
                </h2>
                <p className="text-[13px] leading-6 text-muted-foreground">
                  {formatWeekScopeLabel(weekOffset)}
                </p>
              </div>

              <div className="flex flex-col items-end gap-2">
                <Badge
                  variant="outline"
                  className="border-black/8 bg-white/80 text-foreground/70"
                >
                  {formatLessonCount(totalSessions)}
                </Badge>
                {hasSpecialStates ? (
                  <Badge
                    variant="outline"
                    className="border-black/8 bg-amber-50 text-amber-700"
                  >
                    Есть переносы
                  </Badge>
                ) : null}
              </div>
            </div>

            <div className="space-y-3">
              <div className="flex items-center gap-2.5 rounded-[26px] border border-black/8 bg-black/[0.03] p-1.5 shadow-[inset_0_1px_0_rgba(255,255,255,0.4)]">
                <button
                  type="button"
                  className="inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-full bg-white text-foreground shadow-sm transition active:scale-[0.98]"
                  onClick={() => setWeekOffset((o) => o - 1)}
                  aria-label="Предыдущая неделя"
                >
                  <ChevronLeft size={20} />
                </button>

                <div className="min-w-0 flex-1 text-center">
                  <p className="truncate text-[14px] font-semibold leading-tight sm:text-[15px]">
                    {formatWeekLabel(currentMonday).toUpperCase()}
                  </p>
                  <p className="mt-1 text-[12px] leading-5 text-muted-foreground">
                    {formatWeekScopeLabel(weekOffset)}
                  </p>
                </div>

                <button
                  type="button"
                  className="inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-full bg-white text-foreground shadow-sm transition active:scale-[0.98]"
                  onClick={() => setWeekOffset((o) => o + 1)}
                  aria-label="Следующая неделя"
                >
                  <ChevronRight size={20} />
                </button>
              </div>

              <div className="flex justify-center">
                <button
                  type="button"
                  className="inline-flex min-h-[40px] items-center justify-center rounded-full border border-black/8 bg-white/90 px-3.5 text-[12px] font-semibold text-foreground shadow-sm transition active:scale-[0.98]"
                  onClick={() => setWeekOffset(0)}
                  aria-label="Вернуться к текущей неделе"
                >
                  Текущая неделя
                </button>
              </div>
            </div>
          </CardContent>
        </StudentSurfaceCard>

        {unifiedClientJourneyEnabled ? (
          <SelfServicePersonalBookingSection
            scope={{ audience: "student" }}
            enabled
            onlinePaymentsEnabled={hasOnlinePaymentsCapability(paymentCapabilitiesQuery)}
            className="rounded-[24px] bg-white/70 p-4 ring-1 ring-black/6"
          />
        ) : legacyPersonalAvailabilityEnabled ? (
          <Button
            type="button"
            className="min-h-11 w-full justify-center rounded-2xl"
            onClick={() => setBookingSheetOpen(true)}
          >
            <CalendarPlus className="size-4" />
            Записаться
          </Button>
        ) : (
          <SelfServicePersonalUnavailableNotice
            isError={
              personalAvailabilityCapability.isError || personalAvailabilityCapability.isRefetchError
            }
          />
        )}

        {unifiedClientJourneyEnabled || legacyPersonalAvailabilityEnabled ? (
          <SelfServicePersonalCommandCards
            scope={{ audience: "student" }}
            enabled
            onlinePaymentsEnabled={hasOnlinePaymentsCapability(paymentCapabilitiesQuery)}
            hideWhenEmpty={!unifiedClientJourneyEnabled}
          />
        ) : null}

        {legacyPersonalAvailabilityEnabled && visiblePersonalPaymentReservations.length > 0 ? (
          <section
            className="space-y-2"
            aria-label="Ожидающие оплаты персональные тренировки"
          >
            {visiblePersonalPaymentReservations.map((reservation) => (
              <PersonalPaymentReservationLinkPanel
                key={reservation.id}
                reservation={reservation}
                className="bg-white/95 shadow-sm"
                cancelingPaymentReservationId={
                  cancelPersonalPaymentReservationMutation.isPending
                    ? cancelPersonalPaymentReservationMutation.variables?.id
                    : null
                }
                onPaymentReservationCancel={(item) =>
                  cancelPersonalPaymentReservationMutation.mutate(item)
                }
              />
            ))}
          </section>
        ) : null}

        {legacyPersonalAvailabilityEnabled ? (
          <GuestSelfBookingSheet
          open={bookingSheetOpen}
          onOpenChange={setBookingSheetOpen}
          ariaLabel="Запись на групповую тренировку"
          eyebrow="Запись"
          title="Записаться на тренировку"
          description="Выберите группу или персональный слот. Персоналка без абонемента сначала удерживается ссылкой на оплату."
          dateOptions={bookingDates}
          selectedDate={bookingDate}
          onDateChange={(date) => {
            setBookedScheduleId(null);
            setBookedPersonalSlotId(null);
            setBookingErrorMessage(null);
            setPersonalBookingErrorMessage(null);
            setRequestedBookingDate(date);
          }}
          dateRangeLabel={formatWeekLabel(currentMonday)}
          onPreviousDateRange={() => setWeekOffset((o) => o - 1)}
          onNextDateRange={() => setWeekOffset((o) => o + 1)}
          onCurrentDateRange={() => setWeekOffset(0)}
          options={bookingOptions}
          isLoading={bookingOptionsLoading}
          isError={bookingOptionsError}
          onRetry={() => void refetchBookingOptions()}
          onBook={(option) => bookingMutation.mutate(option)}
          pendingScheduleId={
            bookingMutation.isPending ? bookingMutation.variables?.schedule_id : null
          }
          bookedScheduleId={bookedScheduleId}
          errorMessage={bookingErrorMessage}
          personalOptions={personalOptions}
          personalIsLoading={personalOptionsLoading}
          personalIsError={personalOptionsError}
          onPersonalRetry={() => void refetchPersonalOptions()}
          personalPaymentReservationsError={pendingPersonalPaymentReservationsError}
          onlinePaymentsEnabled={hasOnlinePaymentsCapability(paymentCapabilitiesQuery)}
          unifiedClientJourneyEnabled={unifiedClientJourneyEnabled}
          onPersonalBook={(option) => {
            if (option.booking_status === "can_pay") {
              personalPaymentReservationMutation.mutate(option);
              return;
            }
            personalBookingMutation.mutate(option);
          }}
          personalPaymentReservations={visiblePersonalPaymentReservations}
          onPersonalPaymentReservationCancel={(reservation) =>
            cancelPersonalPaymentReservationMutation.mutate(reservation)
          }
          pendingPersonalSlotId={
            personalBookingMutation.isPending
              ? personalBookingMutation.variables?.slot_id
              : null
          }
          pendingPersonalPaymentSlotId={
            personalPaymentReservationMutation.isPending
              ? personalPaymentReservationMutation.variables?.slot_id
              : null
          }
          bookedPersonalSlotId={bookedPersonalSlotId}
          cancelingPersonalPaymentReservationId={
            cancelPersonalPaymentReservationMutation.isPending
              ? cancelPersonalPaymentReservationMutation.variables?.id
              : null
          }
          personalErrorMessage={personalBookingErrorMessage}
          />
        ) : null}

        <section className="space-y-3">
          <StudentSectionTitle
            eyebrow="Дни недели"
            title="Твои занятия на неделю"
          />

          {grouped.length === 0 ? (
            <StudentSurfaceCard>
              <CardContent className="space-y-2 py-6 text-center">
                <p className="text-[18px] font-semibold">
                  {knownSchedule.length === 0
                    ? "Пока нет закреплённых занятий"
                    : "Нет занятий на этой неделе"}
                </p>
                <p className="ui-body-muted">
                  {knownSchedule.length === 0
                    ? "Когда тренер закрепит группу или появится персональная запись, расписание отобразится здесь."
                    : "Можно посмотреть соседнюю неделю или записаться на доступную тренировку."}
                </p>
              </CardContent>
            </StudentSurfaceCard>
          ) : (
            <div className="space-y-2.5">
              {grouped.map(({ date, label, items }) => (
                <StudentSurfaceCard key={date} className="overflow-hidden">
                  <CardContent className="space-y-3.5 py-3.5">
                    <div className="ui-row-between">
                      <div className="space-y-1">
                        <p className="ui-overline">
                          {label.split(", ")[0]}
                        </p>
                        <h3 className="text-[17px] font-semibold leading-tight">{label}</h3>
                      </div>
                      <Badge variant="outline" className="border-black/8 bg-white/80">
                        {formatLessonCount(items.length)}
                      </Badge>
                    </div>

                    <div className="space-y-2.5">
                      {items.map((item) => (
                        <div
                          key={`${item.schedule_id}-${item.effective_date}-${item.effective_start_time}`}
                          className="rounded-[20px] border border-black/6 bg-black/[0.02] p-3.5 shadow-[0_1px_0_rgba(255,255,255,0.55)]"
                        >
                          <div className="ui-row-between">
                            <div className="min-w-0 space-y-2">
                              <p className="text-[15px] font-semibold leading-tight">
                                {item.group_name}
                              </p>
                              <div className="flex flex-wrap gap-2">
                                <Badge
                                  variant="outline"
                                  className="border-black/8 bg-white/80 text-foreground/70"
                                >
                                  {getScheduleOccurrenceKindLabel(item)}
                                </Badge>
                                <Badge
                                  variant="outline"
                                  className="border-emerald-200 bg-emerald-50 text-emerald-700"
                                >
                                  {getScheduleOccurrenceStatusLabel(item)}
                                </Badge>
                                {item.training_type_name ? (
                                  <Badge
                                    variant="outline"
                                    className="border-black/8 bg-white/80 text-foreground/70"
                                  >
                                    {item.training_type_name}
                                  </Badge>
                                ) : null}
                                {item.is_rescheduled ? (
                                  <Badge
                                    variant="outline"
                                    className="border-amber-200 bg-amber-50 text-amber-700"
                                  >
                                    Перенос
                                  </Badge>
                                ) : null}
                                {item.is_substitute ? (
                                  <Badge
                                    variant="outline"
                                    className="border-sky-200 bg-sky-50 text-sky-700"
                                  >
                                    Замена тренера
                                  </Badge>
                                ) : null}
                              </div>
                            </div>

                            <div className="shrink-0 rounded-2xl border border-black/6 bg-white/90 px-3 py-1.5 text-right shadow-sm">
                              <p className="ui-overline">
                                Время
                              </p>
                              <p className="text-[14px] font-semibold leading-tight">
                                {formatClockTime(item.effective_start_time)}&ndash;
                                {formatClockTime(item.effective_end_time)}
                              </p>
                            </div>
                          </div>

                          <div className="mt-3 grid grid-cols-1 gap-1.5 text-[12px] text-muted-foreground sm:grid-cols-2">
                            <div className="ui-row-2">
                              <UserRound size={14} className="shrink-0" />
                              <span>{item.trainer_name}</span>
                            </div>
                            <div className="ui-row-2">
                              <MapPin size={14} className="shrink-0" />
                              <span>{item.location_name}</span>
                            </div>
                          </div>

                          {getCancelBookingPath(item) ? (
                            <div className="mt-3 space-y-2">
                              <Button
                                type="button"
                                variant="destructive"
                                aria-label={`Отменить запись ${item.group_name}`}
                                className="min-h-10 w-full justify-center rounded-2xl"
                                onClick={() => cancelBookingMutation.mutate(item)}
                                disabled={
                                  cancelBookingMutation.isPending &&
                                  cancelBookingMutation.variables?.enrollment_id ===
                                    item.enrollment_id
                                }
                              >
                                <XCircle className="size-4" />
                                {cancelBookingMutation.isPending &&
                                cancelBookingMutation.variables?.enrollment_id ===
                                  item.enrollment_id
                                  ? "Отменяем..."
                                  : "Отменить запись"}
                              </Button>
                              {cancelBookingErrorMessage ? (
                                <p className="text-[12px] leading-5 text-destructive">
                                  {cancelBookingErrorMessage}
                                </p>
                              ) : null}
                            </div>
                          ) : null}
                        </div>
                      ))}
                    </div>
                  </CardContent>
                </StudentSurfaceCard>
              ))}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
