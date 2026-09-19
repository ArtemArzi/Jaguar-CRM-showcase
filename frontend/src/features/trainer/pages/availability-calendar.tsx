import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  CalendarPlus,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Clock,
  Lock,
  RotateCcw,
  XCircle,
} from "lucide-react";
import { useNavigate, useSearchParams } from "react-router";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import apiClient from "@/api/custom-fetch";
import { useBrandingStore } from "@/features/branding/use-branding";
import { APP_TIME_ZONE } from "@/lib/locale";
import { cn, formatRub, getApiError, toISODate } from "@/lib/utils";
import {
  blockTrainerAvailabilitySlot,
  cancelTrainerAvailabilitySlot,
  generateTrainerAvailability,
  listTrainerAvailability,
  unblockTrainerAvailabilitySlot,
} from "../lib/personal-availability";
import type {
  StudentListItem,
  TrainerPersonalAvailabilityGenerateResult,
  TrainerPersonalAvailabilitySlot,
  TrainerPersonalAvailabilityStatus,
} from "../types";
import {
  PersonalBookingForm,
  PersonalBookingSheet,
  type FixedPersonalSlotContext,
  type PersonalBookingSuccess,
} from "../components/personal-booking-sheet";

interface LocationItem {
  id: number;
  name: string;
}

interface TrainingTypeItem {
  id: number;
  name: string;
  kind: string;
  is_active: boolean;
}

const WEEKDAY_LABELS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"] as const;
const HORIZON_OPTIONS = [
  { label: "1 неделя", value: 1 },
  { label: "2 недели", value: 2 },
  { label: "4 недели", value: 4 },
] as const;

type PublishMode = "single" | "repeat";

const SKIP_REASON_LABELS: Record<string, string> = {
  past: "Время уже прошло",
  schedule_overlap: "Пересекается с тренировкой",
  slot_overlap: "Слот уже опубликован",
  reservation_overlap: "Слот удержан оплатой",
};

function availabilityErrorMessage(error: unknown, fallback: string) {
  const code =
    error && typeof error === "object"
      ? (error as { response?: { data?: { code?: unknown } } }).response?.data?.code
      : undefined;
  if (
    code === "personal_booking_tariff_not_configured" ||
    code === "personal_booking_tariff_ambiguous"
  ) {
    return "Нельзя опубликовать персональные слоты: владелец должен настроить единую цену персоналки.";
  }
  return getApiError(error, fallback);
}

const STATUS_META: Record<
  TrainerPersonalAvailabilityStatus,
  {
    label: string;
    className: string;
    icon: typeof CheckCircle2;
  }
> = {
  published: {
    label: "Свободно",
    className: "bg-emerald-50 text-emerald-700 ring-emerald-100",
    icon: CheckCircle2,
  },
  held: {
    label: "Ожидает оплаты",
    className: "bg-amber-50 text-amber-700 ring-amber-100",
    icon: Clock,
  },
  booked: {
    label: "Записан ученик",
    className: "bg-sky-50 text-sky-700 ring-sky-100",
    icon: CheckCircle2,
  },
  blocked: {
    label: "Заблокировано",
    className: "bg-neutral-100 text-neutral-700 ring-neutral-200",
    icon: Lock,
  },
  cancelled: {
    label: "Закрыт",
    className: "bg-red-50 text-red-700 ring-red-100",
    icon: XCircle,
  },
};

type SlotSheetState = "actions" | "clientSearch" | "bookingForm";

type StudentSearchResponse = StudentListItem[] | { items?: StudentListItem[] };

function normalizeStudentSearchResponse(data: StudentSearchResponse): StudentListItem[] {
  return Array.isArray(data) ? data : data.items ?? [];
}

function studentDisplayName(student: StudentListItem): string {
  return [student.first_name, student.last_name].filter(Boolean).join(" ") || "Клиент";
}

function toFixedSlotContext(slot: TrainerPersonalAvailabilitySlot): FixedPersonalSlotContext {
  return {
    slotId: slot.id,
    startsAt: slot.starts_at,
    endsAt: slot.ends_at,
    trainerId: slot.trainer_id,
    trainerName: slot.trainer_name,
    locationId: slot.location_id,
    locationName: slot.location_name,
    trainingTypeId: slot.training_type_id,
    trainingTypeName: slot.training_type_name,
    offerTariffId: slot.offer_tariff_id,
    offerTariffName: slot.offer_tariff_name,
    offerPrice: slot.offer_price,
    offerDigest: slot.offer_digest,
    offerErrorCode: slot.offer_error_code,
  };
}

function successNotice(success: PersonalBookingSuccess): string {
  const date = success.startsAt.slice(0, 10).split("-").reverse().join(".");
  const time = `${success.startsAt.slice(11, 16)}–${success.endsAt.slice(11, 16)}`;
  const price = success.price == null ? "по абонементу" : `${success.price} ₽`;
  return `Клиент записан: ${date}, ${time}, ${price}.`;
}

function getWeekStart(date: Date): Date {
  const start = new Date(date);
  const day = start.getDay();
  const diff = day === 0 ? -6 : 1 - day;
  start.setDate(start.getDate() + diff);
  start.setHours(0, 0, 0, 0);
  return start;
}

function addDays(date: Date, days: number): Date {
  const next = new Date(date);
  next.setDate(next.getDate() + days);
  return next;
}

function getWeekDays(referenceDate: Date): Date[] {
  const weekStart = getWeekStart(referenceDate);
  return Array.from({ length: 7 }, (_, index) => addDays(weekStart, index));
}

function toBackendWeekday(date: Date): number {
  const day = date.getDay();
  return day === 0 ? 6 : day - 1;
}

function formatDateLabel(date: Date): string {
  return date.toLocaleDateString("ru-RU", {
    day: "numeric",
    month: "long",
  });
}

function formatTime(value: string, timeZone = APP_TIME_ZONE): string {
  return new Date(value).toLocaleTimeString("ru-RU", {
    hour: "2-digit",
    minute: "2-digit",
    timeZone,
  });
}

function formatDuration(startsAt: string, endsAt: string): string {
  const minutes = Math.max(
    0,
    Math.round((new Date(endsAt).getTime() - new Date(startsAt).getTime()) / 60_000),
  );
  return `${minutes} мин`;
}

function isPersonalKind(kind?: string) {
  return kind === "personal" || kind === "mini_group";
}

function countByStatus(slots: TrainerPersonalAvailabilitySlot[]) {
  return slots.reduce(
    (acc, slot) => {
      acc[slot.status] += 1;
      return acc;
    },
    {
      published: 0,
      held: 0,
      booked: 0,
      blocked: 0,
      cancelled: 0,
    } satisfies Record<TrainerPersonalAvailabilityStatus, number>,
  );
}

function summarizeSkippedReasons(
  skipped: TrainerPersonalAvailabilityGenerateResult["skipped"],
) {
  const counts = new Map<string, number>();
  skipped.forEach((item) => {
    counts.set(item.reason_code, (counts.get(item.reason_code) ?? 0) + 1);
  });
  return Array.from(counts.entries()).map(([reason, count]) => ({
    reason,
    count,
    label: SKIP_REASON_LABELS[reason] ?? "Не удалось опубликовать",
  }));
}

function SlotSkeleton() {
  return (
    <div className="ui-col-3">
      {[1, 2, 3].map((item) => (
        <Skeleton key={item} className="h-[92px] rounded-xl" />
      ))}
    </div>
  );
}

function AvailabilitySlotCard({
  slot,
  timeZone,
  onSelect,
}: {
  slot: TrainerPersonalAvailabilitySlot;
  timeZone: string;
  onSelect: (slot: TrainerPersonalAvailabilitySlot) => void;
}) {
  const meta = STATUS_META[slot.status];
  const Icon = meta.icon;

  return (
    <button
      type="button"
      onClick={() => onSelect(slot)}
      className="flex min-h-[92px] w-full items-stretch gap-3 rounded-xl bg-white p-3 text-left shadow-sm ring-1 ring-foreground/5 active:scale-[0.99] transition-transform"
    >
      <div className="flex min-w-[72px] flex-col items-center justify-center rounded-lg bg-[var(--branding-accent)]/10 px-3 py-2 text-[var(--branding-accent)]">
        <span className="text-[17px] font-semibold leading-tight">
          {formatTime(slot.starts_at, timeZone)}
        </span>
        <span className="text-[12px] leading-tight opacity-80">
          {formatDuration(slot.starts_at, slot.ends_at)}
        </span>
      </div>

      <div className="flex min-w-0 flex-1 flex-col justify-center gap-2">
        <div className="flex min-w-0 items-start justify-between gap-2">
          <div className="min-w-0">
            <p className="truncate text-[15px] font-semibold text-foreground">
              {slot.training_type_name}
            </p>
            <p className="truncate text-[13px] text-muted-foreground">
              {slot.location_name}
            </p>
          </div>
          <span
            className={cn(
              "inline-flex shrink-0 items-center gap-1 rounded-full px-2 py-1 text-[11px] font-semibold ring-1",
              meta.className,
            )}
          >
            <Icon size={13} />
            {meta.label}
          </span>
        </div>
        {slot.block_reason ? (
          <p className="line-clamp-1 text-[13px] text-muted-foreground">
            {slot.block_reason}
          </p>
        ) : null}
        {slot.offer_price !== null && slot.offer_price !== undefined && slot.offer_price !== "" ? (
          <p className="text-[13px] font-semibold text-foreground">
            {formatRub(slot.offer_price)}
          </p>
        ) : slot.offer_error_code ? (
          <p className="text-[13px] font-medium text-amber-800">
            Цена не настроена
          </p>
        ) : null}
      </div>
    </button>
  );
}

function GenerateAvailabilitySheet({
  open,
  onOpenChange,
  selectedDate,
  locations,
  trainingTypes,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  selectedDate: Date;
  locations: LocationItem[];
  trainingTypes: TrainingTypeItem[];
}) {
  const queryClient = useQueryClient();
  const weekStart = useMemo(() => getWeekStart(selectedDate), [selectedDate]);
  const selectedDay = useMemo(() => {
    const value = new Date(selectedDate);
    value.setHours(0, 0, 0, 0);
    return value;
  }, [selectedDate]);
  const [publishMode, setPublishMode] = useState<PublishMode>("single");
  const [weekdays, setWeekdays] = useState<number[]>([
    toBackendWeekday(selectedDate),
  ]);
  const [horizonWeeks, setHorizonWeeks] = useState<(typeof HORIZON_OPTIONS)[number]["value"]>(1);
  const [startTime, setStartTime] = useState("10:00");
  const [endTime, setEndTime] = useState("14:00");
  const [durationMinutes, setDurationMinutes] = useState(60);
  const [bufferMinutes, setBufferMinutes] = useState(0);
  const [locationId, setLocationId] = useState<number | "">("");
  const [trainingTypeId, setTrainingTypeId] = useState<number | "">("");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [lastResult, setLastResult] =
    useState<TrainerPersonalAvailabilityGenerateResult | null>(null);

  const personalTrainingTypes = trainingTypes.filter(
    (type) => type.is_active && isPersonalKind(type.kind),
  );
  const repeatDateTo = addDays(weekStart, horizonWeeks * 7 - 1);
  const generationDateFrom = publishMode === "single" ? selectedDay : weekStart;
  const generationDateTo = publishMode === "single" ? selectedDay : repeatDateTo;
  const generationWeekdays =
    publishMode === "single"
      ? [toBackendWeekday(selectedDay)]
      : [...weekdays].sort((a, b) => a - b);
  const skippedSummary = lastResult ? summarizeSkippedReasons(lastResult.skipped) : [];

  const mutation = useMutation({
    mutationFn: () => {
      if (!locationId || !trainingTypeId) {
        throw new Error("missing_required_fields");
      }
      return generateTrainerAvailability({
        date_from: toISODate(generationDateFrom),
        date_to: toISODate(generationDateTo),
        weekdays: generationWeekdays,
        start_time: startTime,
        end_time: endTime,
        slot_duration_minutes: durationMinutes,
        buffer_minutes: bufferMinutes,
        location_id: locationId,
        training_type_id: trainingTypeId,
      });
    },
    onSuccess: (result) => {
      setLastResult(result);
      queryClient.invalidateQueries({ queryKey: ["trainer", "availability"] });
    },
    onError: (error: unknown) => {
      setErrorMsg(availabilityErrorMessage(error, "Не удалось опубликовать слоты"));
    },
  });

  function toggleWeekday(day: number) {
    setWeekdays((current) =>
      current.includes(day)
        ? current.filter((item) => item !== day)
        : [...current, day],
    );
  }

  function isFormValid() {
    return (
      generationWeekdays.length > 0 &&
      startTime < endTime &&
      durationMinutes > 0 &&
      bufferMinutes >= 0 &&
      Boolean(locationId) &&
      Boolean(trainingTypeId)
    );
  }

  function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    setErrorMsg(null);
    setLastResult(null);
    if (!isFormValid()) {
      setErrorMsg("Проверьте дни, время, зал и тип тренировки");
      return;
    }
    mutation.mutate();
  }

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="bottom"
        className="max-h-[92dvh] overflow-y-auto rounded-t-2xl"
      >
        <SheetHeader>
          <SheetTitle>Опубликовать слоты</SheetTitle>
          <SheetDescription>
            {publishMode === "single"
              ? `${formatDateLabel(selectedDay)} - только выбранный день`
              : `${formatDateLabel(weekStart)} - ${formatDateLabel(repeatDateTo)}`}
          </SheetDescription>
        </SheetHeader>

        <form onSubmit={handleSubmit} className="ui-sheet-body">
          <div>
            <label className="mb-2 block text-[14px] text-muted-foreground">
              Публикация
            </label>
            <div className="grid grid-cols-2 gap-1 rounded-xl bg-neutral-100 p-1">
              {[
                { value: "single", label: "Один день" },
                { value: "repeat", label: "Повторять" },
              ].map((option) => (
                <button
                  key={option.value}
                  type="button"
                  aria-pressed={publishMode === option.value}
                  onClick={() => setPublishMode(option.value as PublishMode)}
                  className={cn(
                    "min-h-[42px] rounded-lg px-3 text-[14px] font-semibold transition-colors",
                    publishMode === option.value
                      ? "bg-white text-foreground shadow-sm"
                      : "text-muted-foreground",
                  )}
                >
                  {option.label}
                </button>
              ))}
            </div>
            <p className="mt-2 text-[13px] leading-snug text-muted-foreground">
              {publishMode === "single"
                ? "Создать слоты только на выбранную дату."
                : "Создать такие же слоты в выбранные дни недели."}
            </p>
          </div>

          {publishMode === "repeat" ? (
            <div>
              <label className="mb-2 block text-[14px] text-muted-foreground">
                Дни недели
              </label>
              <div className="grid grid-cols-7 gap-1">
                {WEEKDAY_LABELS.map((label, index) => {
                  const active = weekdays.includes(index);
                  return (
                    <button
                      key={label}
                      type="button"
                      aria-pressed={active}
                      onClick={() => toggleWeekday(index)}
                      className={cn(
                        "min-h-[44px] rounded-lg text-[13px] font-semibold ring-1 ring-foreground/10",
                        active
                          ? "bg-[var(--branding-accent)] text-white"
                          : "bg-white text-foreground",
                      )}
                    >
                      {label}
                    </button>
                  );
                })}
              </div>
            </div>
          ) : (
            <div className="rounded-xl bg-neutral-50 px-3 py-3 text-[14px] text-foreground ring-1 ring-foreground/5">
              Дата: <span className="font-semibold">{formatDateLabel(selectedDay)}</span>
            </div>
          )}

          {publishMode === "repeat" ? (
            <div>
              <label className="mb-2 block text-[14px] text-muted-foreground">
                На сколько недель
              </label>
              <div className="grid grid-cols-3 gap-2">
                {HORIZON_OPTIONS.map((option) => (
                  <button
                    key={option.value}
                    type="button"
                    aria-pressed={horizonWeeks === option.value}
                    onClick={() => setHorizonWeeks(option.value)}
                    className={cn(
                      "min-h-[44px] rounded-lg px-2 text-[13px] font-semibold ring-1 ring-foreground/10",
                      horizonWeeks === option.value
                        ? "bg-[var(--branding-primary)] text-white"
                        : "bg-white text-foreground",
                    )}
                  >
                    {option.label}
                  </button>
                ))}
              </div>
            </div>
          ) : null}

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label
                htmlFor="availability-start-time"
                className="ui-field-label"
              >
                С
              </label>
              <Input
                id="availability-start-time"
                type="time"
                value={startTime}
                onChange={(event) => setStartTime(event.target.value)}
                required
              />
            </div>
            <div>
              <label
                htmlFor="availability-end-time"
                className="ui-field-label"
              >
                До
              </label>
              <Input
                id="availability-end-time"
                type="time"
                value={endTime}
                onChange={(event) => setEndTime(event.target.value)}
                required
              />
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label
                htmlFor="availability-duration"
                className="ui-field-label"
              >
                Слот, мин
              </label>
              <Input
                id="availability-duration"
                type="number"
                min={15}
                step={15}
                value={durationMinutes}
                onChange={(event) => setDurationMinutes(Number(event.target.value))}
                required
              />
            </div>
            <div>
              <label
                htmlFor="availability-buffer"
                className="ui-field-label"
              >
                Буфер, мин
              </label>
              <Input
                id="availability-buffer"
                type="number"
                min={0}
                step={5}
                value={bufferMinutes}
                onChange={(event) => setBufferMinutes(Number(event.target.value))}
                required
              />
            </div>
          </div>

          <div>
            <label
              htmlFor="availability-training-type"
              className="ui-field-label"
            >
              Тип тренировки
            </label>
            <select
              id="availability-training-type"
              value={trainingTypeId}
              onChange={(event) =>
                setTrainingTypeId(event.target.value ? Number(event.target.value) : "")
              }
              className="flex h-11 w-full rounded-md border border-input bg-background px-3 py-2 text-base ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 md:text-sm"
              required
            >
              <option value="">Выберите тип</option>
              {personalTrainingTypes.map((type) => (
                <option key={type.id} value={type.id}>
                  {type.name}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label
              htmlFor="availability-location"
              className="ui-field-label"
            >
              Зал
            </label>
            <select
              id="availability-location"
              value={locationId}
              onChange={(event) =>
                setLocationId(event.target.value ? Number(event.target.value) : "")
              }
              className="flex h-11 w-full rounded-md border border-input bg-background px-3 py-2 text-base ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 md:text-sm"
              required
            >
              <option value="">Выберите зал</option>
              {locations.map((location) => (
                <option key={location.id} value={location.id}>
                  {location.name}
                </option>
              ))}
            </select>
          </div>

          {lastResult ? (
            <div
              className={cn(
                "rounded-lg p-3 text-[13px] leading-snug ring-1",
                lastResult.created.length > 0
                  ? "bg-emerald-50 text-emerald-900 ring-emerald-100"
                  : "bg-amber-50 text-amber-900 ring-amber-100",
              )}
            >
              <p className="font-semibold">
                Опубликовано: {lastResult.created.length}. Пропущено:{" "}
                {lastResult.skipped.length}.
              </p>
              {skippedSummary.length > 0 ? (
                <ul className="mt-2 space-y-1">
                  {skippedSummary.map((item) => (
                    <li key={item.reason}>
                      {item.label}: {item.count}
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
          ) : null}

          {errorMsg ? (
            <p className="ui-error-center">{errorMsg}</p>
          ) : null}

          <div className="grid grid-cols-2 gap-2">
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              className="min-h-[44px]"
            >
              Закрыть
            </Button>
            <Button
              type="submit"
              disabled={mutation.isPending || !isFormValid()}
              className="ui-brand-touch"
            >
              {mutation.isPending ? "Публикуем..." : "Опубликовать"}
            </Button>
          </div>
        </form>
      </SheetContent>
    </Sheet>
  );
}

function SlotActionSheet({
  slot,
  onOpenChange,
  preselectedClient,
  onStudentAdmitted,
}: {
  slot: TrainerPersonalAvailabilitySlot | null;
  onOpenChange: (open: boolean) => void;
  preselectedClient?: StudentListItem | null;
  onStudentAdmitted?: (studentId: number) => void;
}) {
  const queryClient = useQueryClient();
  const timeZone = useBrandingStore((s) => s.timeZone);
  const [reason, setReason] = useState(slot?.block_reason ?? "");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [sheetState, setSheetState] = useState<SlotSheetState>("actions");
  const [searchInput, setSearchInput] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [selectedClient, setSelectedClient] = useState<StudentListItem | null>(null);
  const [bookingNotice, setBookingNotice] = useState<string | null>(null);

  useEffect(() => {
    const timeoutId = window.setTimeout(() => setDebouncedSearch(searchInput.trim()), 250);
    return () => window.clearTimeout(timeoutId);
  }, [searchInput]);

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["trainer", "availability"] });
  };

  const blockMutation = useMutation({
    mutationFn: () => {
      if (!slot) throw new Error("missing_slot");
      return blockTrainerAvailabilitySlot({ slotId: slot.id, reason });
    },
    onSuccess: () => {
      invalidate();
      onOpenChange(false);
      setReason("");
    },
    onError: (error: unknown) => {
      setErrorMsg(getApiError(error, "Не удалось закрыть слот"));
    },
  });

  const unblockMutation = useMutation({
    mutationFn: () => {
      if (!slot) throw new Error("missing_slot");
      return unblockTrainerAvailabilitySlot(slot.id);
    },
    onSuccess: () => {
      invalidate();
      onOpenChange(false);
    },
    onError: (error: unknown) => {
      setErrorMsg(getApiError(error, "Не удалось открыть слот"));
    },
  });

  const cancelMutation = useMutation({
    mutationFn: () => {
      if (!slot) throw new Error("missing_slot");
      return cancelTrainerAvailabilitySlot(slot.id);
    },
    onSuccess: () => {
      invalidate();
      onOpenChange(false);
    },
    onError: (error: unknown) => {
      setErrorMsg(getApiError(error, "Не удалось отменить слот"));
    },
  });

  const clientSearchQuery = useQuery<StudentSearchResponse>({
    queryKey: ["trainer", "slot-client-search", slot?.id, debouncedSearch],
    queryFn: () =>
      apiClient
        .get("/students/", { params: { q: debouncedSearch } })
        .then((response) => response.data),
    enabled: sheetState === "clientSearch" && debouncedSearch.length >= 2,
    staleTime: 15_000,
  });

  if (!slot) return null;

  const meta = STATUS_META[slot.status];
  const searchResults = clientSearchQuery.data
    ? normalizeStudentSearchResponse(clientSearchQuery.data)
    : [];
  const isPending =
    blockMutation.isPending ||
    unblockMutation.isPending ||
    cancelMutation.isPending;
  const fixedPreselectedClient =
    slot.status === "published" ? preselectedClient : null;
  const bookingClient = fixedPreselectedClient ?? selectedClient;
  const showingBookingForm = Boolean(fixedPreselectedClient) || sheetState === "bookingForm";

  function handleSheetOpenChange(open: boolean) {
    if (!open) {
      setSheetState("actions");
      setSearchInput("");
      setDebouncedSearch("");
      setSelectedClient(null);
      setErrorMsg(null);
      onOpenChange(false);
    }
  }

  function openClientSearch() {
    setBookingNotice(null);
    setErrorMsg(null);
    setSheetState("clientSearch");
  }

  function selectClient(client: StudentListItem) {
    setSelectedClient(client);
    setErrorMsg(null);
    setSheetState("bookingForm");
  }

  return (
    <Sheet open={slot !== null} onOpenChange={handleSheetOpenChange}>
      <SheetContent
        side="bottom"
        showCloseButton={false}
        className="max-h-[92dvh] overflow-y-auto rounded-t-2xl"
      >
        <SheetHeader>
          <SheetTitle>
            {sheetState === "clientSearch"
              ? "Выберите клиента"
              : showingBookingForm
                ? "Записать персоналку"
                : `${formatTime(slot.starts_at, timeZone)} - ${formatTime(slot.ends_at, timeZone)}`}
          </SheetTitle>
          <SheetDescription>
            {slot.training_type_name}, {slot.location_name}
          </SheetDescription>
        </SheetHeader>

        <div className="ui-sheet-body">
          {sheetState === "clientSearch" ? (
            <section className="ui-col-3">
              <Input
                aria-label="Поиск клиента"
                value={searchInput}
                onChange={(event) => setSearchInput(event.target.value)}
                placeholder="Имя или телефон"
              />
              {searchInput.trim().length < 2 ? (
                <p className="ui-muted-14">Введите минимум 2 символа.</p>
              ) : clientSearchQuery.isLoading ? (
                <p role="status" className="ui-muted-14">Ищем клиентов...</p>
              ) : clientSearchQuery.isError ? (
                <p role="alert" className="rounded-xl bg-destructive/10 p-3 text-[14px] text-destructive">
                  Не удалось выполнить поиск. Повторите попытку.
                </p>
              ) : searchResults.length === 0 ? (
                <p className="ui-muted-14">Клиенты не найдены.</p>
              ) : (
                <div className="ui-col-2">
                  {searchResults.map((client) => (
                    <Button
                      key={client.id}
                      type="button"
                      variant="outline"
                      className="min-h-[44px] justify-start text-left"
                      onClick={() => selectClient(client)}
                    >
                      <span className="min-w-0">
                        <span className="block truncate">{studentDisplayName(client)}</span>
                        <span className="block truncate text-[12px] text-muted-foreground">{client.status}</span>
                      </span>
                    </Button>
                  ))}
                </div>
              )}
              <Button type="button" variant="outline" className="min-h-[44px]" onClick={() => setSheetState("actions")}>Назад</Button>
            </section>
          ) : showingBookingForm && bookingClient ? (
            <PersonalBookingForm
              studentId={bookingClient.id}
              studentName={studentDisplayName(bookingClient)}
              fixedSlot={toFixedSlotContext(slot)}
              onBack={() => {
                if (fixedPreselectedClient) {
                  handleSheetOpenChange(false);
                  return;
                }
                setSheetState("clientSearch");
              }}
              onClose={() => {
                invalidate();
                if (fixedPreselectedClient) {
                  handleSheetOpenChange(false);
                  return;
                }
                setSheetState("actions");
              }}
              onBooked={(success) => {
                setBookingNotice(successNotice(success));
                if (fixedPreselectedClient) {
                  handleSheetOpenChange(false);
                  return;
                }
                setSheetState("actions");
                invalidate();
              }}
              onStaffIntentCreated={(receipt) => {
                if (receipt.workspace_state !== "student") return;
                invalidate();
                onStudentAdmitted?.(bookingClient.id);
              }}
            />
          ) : (
            <>
          <span
            className={cn(
              "inline-flex w-fit items-center rounded-full px-2.5 py-1 text-[12px] font-semibold ring-1",
              meta.className,
            )}
          >
            {meta.label}
          </span>

          {bookingNotice ? (
            <p role="status" className="rounded-xl bg-emerald-50 p-3 text-[13px] text-emerald-900">
              {bookingNotice}
            </p>
          ) : null}

          {slot.status === "published" ? (
            <Button
              type="button"
              className="ui-brand-touch"
              onClick={openClientSearch}
            >
              Записать клиента
            </Button>
          ) : null}

          {slot.status === "published" && slot.can_block ? (
            <div className="ui-col-3">
              <label
                htmlFor="availability-block-reason"
                className="ui-muted-14"
              >
                Причина
              </label>
              <Input
                id="availability-block-reason"
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                placeholder="Например: занят"
              />
              <Button
                type="button"
                disabled={isPending}
                onClick={() => blockMutation.mutate()}
                className="ui-brand-touch"
              >
                Закрыть слот
              </Button>
            </div>
          ) : null}

          {slot.status === "blocked" && slot.can_unblock ? (
            <Button
              type="button"
              disabled={isPending}
              onClick={() => unblockMutation.mutate()}
              className="ui-brand-touch"
            >
              <RotateCcw size={16} />
              Открыть снова
            </Button>
          ) : null}

          {slot.can_cancel ? (
            <Button
              type="button"
              variant="outline"
              disabled={isPending}
              onClick={() => cancelMutation.mutate()}
              className="min-h-[44px] text-destructive"
            >
              Отменить публикацию
            </Button>
          ) : null}

          {slot.status === "held" ? (
            <p className="ui-muted-14">
              Слот удержан оплатой. Дождитесь оплаты или истечения брони.
            </p>
          ) : null}

          {slot.status === "booked" ? (
            <p className="ui-muted-14">
              Слот уже занят. Изменение идет через перенос или отмену занятия.
            </p>
          ) : null}

          {slot.status === "cancelled" ? (
            <p className="ui-muted-14">
              Слот закрыт и не виден ученикам.
            </p>
          ) : null}

          {!slot.can_block &&
          !slot.can_unblock &&
          !slot.can_cancel &&
          !["held", "booked", "cancelled"].includes(slot.status) ? (
            <p className="ui-muted-14">
              Прошедший слот нельзя изменить.
            </p>
          ) : null}

          {errorMsg ? (
            <p className="ui-error-center">{errorMsg}</p>
          ) : null}
            </>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}

export default function AvailabilityCalendar() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const timeZone = useBrandingStore((s) => s.timeZone);
  const [selectedDate, setSelectedDate] = useState(() => new Date());
  const [generateOpen, setGenerateOpen] = useState(false);
  const [selectedSlot, setSelectedSlot] =
    useState<TrainerPersonalAvailabilitySlot | null>(null);
  const [directBookingOpen, setDirectBookingOpen] = useState(false);
  const requestedStudentId = Number(searchParams.get("student_id"));
  const preselectedClientQuery = useQuery<StudentListItem>({
    queryKey: ["trainer", "availability", "preselected-student", requestedStudentId],
    queryFn: () => apiClient.get(`/students/${requestedStudentId}/`).then((response) => response.data),
    enabled: Number.isSafeInteger(requestedStudentId) && requestedStudentId > 0,
    retry: false,
    staleTime: 30_000,
  });
  const preselectedClient = preselectedClientQuery.data ?? null;

  const weekDays = useMemo(() => getWeekDays(selectedDate), [selectedDate]);
  const weekStart = weekDays[0];
  const weekEnd = weekDays[6];
  const weekStartStr = toISODate(weekStart);
  const weekEndStr = toISODate(weekEnd);
  const selectedDateStr = toISODate(selectedDate);

  const availabilityQuery = useQuery({
    queryKey: ["trainer", "availability", weekStartStr, weekEndStr],
    queryFn: () =>
      listTrainerAvailability({
        dateFrom: weekStartStr,
        dateTo: weekEndStr,
      }),
    staleTime: 60_000,
  });

  const { data: locations = [] } = useQuery<LocationItem[]>({
    queryKey: ["locations"],
    queryFn: () => apiClient.get("/clubs/locations/").then((response) => response.data),
    staleTime: 5 * 60_000,
  });

  const { data: trainingTypes = [] } = useQuery<TrainingTypeItem[]>({
    queryKey: ["training-types"],
    queryFn: () =>
      apiClient.get("/billing/training-types/").then((response) => response.data),
    staleTime: 5 * 60_000,
  });

  const slots = availabilityQuery.data ?? [];
  const selectedSlots = slots.filter((slot) => slot.date === selectedDateStr);
  const statusCounts = countByStatus(slots);

  return (
    <div className="flex flex-col gap-5 px-4 pt-5 pb-24">
      <header className="flex items-center gap-3">
        <button
          type="button"
          onClick={() => navigate("/trainer")}
          className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-white text-foreground shadow-sm ring-1 ring-foreground/5 active:scale-95 transition-transform"
          aria-label="Назад"
        >
          <ArrowLeft size={20} />
        </button>
        <div className="min-w-0 flex-1">
          <h1 className="text-lg font-semibold text-foreground">Доступность</h1>
          <p className="ui-muted-14">
            {formatDateLabel(weekStart)} - {formatDateLabel(weekEnd)}
          </p>
        </div>
        <button
          type="button"
          onClick={() => setGenerateOpen(true)}
          className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-[var(--branding-accent)] text-white shadow-sm active:scale-95 transition-transform"
          aria-label="Опубликовать слоты"
        >
          <CalendarPlus size={20} />
        </button>
      </header>

      <div className="grid grid-cols-4 gap-2">
        <div className="ui-card-compact">
          <p className="ui-muted-12">Свободно</p>
          <p className="ui-title-20">
            {statusCounts.published}
          </p>
        </div>
        <div className="ui-card-compact">
          <p className="ui-muted-12">Оплата</p>
          <p className="ui-title-20">
            {statusCounts.held}
          </p>
        </div>
        <div className="ui-card-compact">
          <p className="ui-muted-12">Запись</p>
          <p className="ui-title-20">
            {statusCounts.booked}
          </p>
        </div>
        <div className="ui-card-compact">
          <p className="ui-muted-12">Закрыто</p>
          <p className="ui-title-20">
            {statusCounts.blocked + statusCounts.cancelled}
          </p>
        </div>
      </div>

      {Number.isSafeInteger(requestedStudentId) && requestedStudentId > 0 ? (
        preselectedClientQuery.isLoading ? (
          <Skeleton className="h-[56px] rounded-xl" />
        ) : preselectedClient ? (
          <div
            role="status"
            className="flex flex-wrap items-center justify-between gap-3 rounded-xl bg-[var(--branding-accent)]/10 p-3 text-[13px] text-foreground"
          >
            <p>
              Выбран клиент: {studentDisplayName(preselectedClient)}. Выберите опубликованный слот.
            </p>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="min-h-[44px]"
              onClick={() => setDirectBookingOpen(true)}
            >
              Указать время
            </Button>
          </div>
        ) : (
          <p role="status" className="ui-warning">
            Выбранный клиент сейчас недоступен. Найдите клиента в карточке опубликованного слота.
          </p>
        )
      ) : null}

      <div className="flex items-center justify-between gap-2">
        <button
          type="button"
          onClick={() => setSelectedDate(addDays(selectedDate, -7))}
          className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-white text-foreground shadow-sm ring-1 ring-foreground/5 active:scale-95 transition-transform"
          aria-label="Предыдущая неделя"
        >
          <ChevronLeft size={20} />
        </button>
        <Button
          type="button"
          variant="outline"
          onClick={() => setSelectedDate(new Date())}
          className="min-h-[44px] flex-1 bg-white"
        >
          Сегодня
        </Button>
        <button
          type="button"
          onClick={() => setSelectedDate(addDays(selectedDate, 7))}
          className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-white text-foreground shadow-sm ring-1 ring-foreground/5 active:scale-95 transition-transform"
          aria-label="Следующая неделя"
        >
          <ChevronRight size={20} />
        </button>
      </div>

      <div className="flex gap-1 overflow-x-auto py-1">
        {weekDays.map((day, index) => {
          const dayStr = toISODate(day);
          const active = dayStr === selectedDateStr;
          const dayCount = slots.filter((slot) => slot.date === dayStr).length;
          return (
            <button
              key={dayStr}
              type="button"
              onClick={() => setSelectedDate(day)}
              aria-pressed={active}
              aria-label={`${formatDateLabel(day)}. Слотов: ${dayCount}`}
              className={cn(
                "flex min-h-[68px] min-w-[52px] flex-col items-center justify-center rounded-xl px-1 transition-colors",
                active
                  ? "bg-[var(--branding-accent)] text-white"
                  : "bg-white text-muted-foreground ring-1 ring-foreground/5",
              )}
            >
              <span className="text-[12px]">{WEEKDAY_LABELS[index]}</span>
              <span className="text-[17px] font-semibold">{day.getDate()}</span>
              {dayCount > 0 ? (
                <span
                  className={cn(
                    "mt-1 h-1.5 w-1.5 rounded-full",
                    active ? "bg-white" : "bg-[var(--branding-accent)]",
                  )}
                />
              ) : null}
            </button>
          );
        })}
      </div>

      <section>
        <div className="mb-3 flex items-center justify-between gap-3">
          <h2 className="text-sm font-medium text-foreground/70">
            {formatDateLabel(selectedDate)}
          </h2>
          <button
            type="button"
            onClick={() => setGenerateOpen(true)}
            className="flex min-h-[44px] items-center gap-1 rounded-lg px-3 text-[13px] font-semibold text-[var(--branding-accent)] active:scale-95 transition-transform"
          >
            <CalendarPlus size={16} />
            Слоты
          </button>
        </div>

        {availabilityQuery.isLoading ? (
          <SlotSkeleton />
        ) : availabilityQuery.isError ? (
          <div className="rounded-xl bg-white p-4 text-center ring-1 ring-foreground/5">
            <p className="text-[15px] font-semibold text-foreground">
              Не удалось загрузить слоты
            </p>
            <Button
              type="button"
              variant="outline"
              onClick={() => availabilityQuery.refetch()}
              className="mt-3 min-h-[44px]"
            >
              Повторить
            </Button>
          </div>
        ) : selectedSlots.length === 0 ? (
          <div className="flex flex-col items-center justify-center rounded-xl bg-white px-4 py-12 text-center ring-1 ring-foreground/5">
            <CalendarPlus size={40} className="ui-muted" />
            <p className="mt-3 text-[17px] font-semibold text-foreground">
              Нет опубликованных слотов
            </p>
            <Button
              type="button"
              onClick={() => setGenerateOpen(true)}
              className="mt-4 min-h-[44px] bg-[var(--branding-accent)] text-white hover:opacity-90"
            >
              Добавить слот
            </Button>
          </div>
        ) : (
          <div className="ui-col-3">
            {selectedSlots.map((slot) => (
              <AvailabilitySlotCard
                key={slot.id}
                slot={slot}
                timeZone={timeZone}
                onSelect={(nextSlot) => {
                  setSelectedSlot(nextSlot);
                }}
              />
            ))}
          </div>
        )}
      </section>

      <GenerateAvailabilitySheet
        key={selectedDateStr}
        open={generateOpen}
        onOpenChange={setGenerateOpen}
        selectedDate={selectedDate}
        locations={locations}
        trainingTypes={trainingTypes}
      />
      <SlotActionSheet
        key={selectedSlot?.id ?? "none"}
        slot={selectedSlot}
        onOpenChange={(open) => {
          if (!open) setSelectedSlot(null);
        }}
        preselectedClient={preselectedClient}
        onStudentAdmitted={(studentId) => {
          setSelectedSlot(null);
          navigate(`/trainer/students/${studentId}`);
        }}
      />
      {preselectedClient ? (
        <PersonalBookingSheet
          open={directBookingOpen}
          onOpenChange={setDirectBookingOpen}
          studentId={preselectedClient.id}
          studentName={studentDisplayName(preselectedClient)}
          onStaffIntentCreated={(receipt) => {
            if (receipt.workspace_state !== "student") return;
            setDirectBookingOpen(false);
            navigate(`/trainer/students/${preselectedClient.id}`);
          }}
        />
      ) : null}
    </div>
  );
}
