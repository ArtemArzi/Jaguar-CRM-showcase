import { useState } from "react";
import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { CalendarCheck, CheckCircle2, MoreHorizontal, PhoneCall, Send, UserPlus, UserX } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import apiClient from "@/api/custom-fetch";
import { useBrandingStore } from "@/features/branding/use-branding";
import { toDateParamInTimeZone } from "@/lib/club-date";
import type { ScheduleOccurrenceOut } from "../types";
import type { LeadAction, LeadActionContext, LeadData } from "./lead-card";
import { PersonalBookingForm } from "./personal-booking-sheet";
import {
  PersonalCommercialReceiptList,
} from "./personal-commercial-context";
import {
  contextualRetryContextFromReceipt,
  getLeadPersonalCommercialContext,
  leadPersonalCommercialContextQueryKey,
  type ContextualCommercialContext,
  type PersonalCommercialContext,
  type PersonalCommercialReceipt,
  useCommercialCacheScope,
} from "./personal-commercial-context-api";
import { ContextualCommercialSheet } from "./contextual-commercial-sheet";

const STATUS_LABELS: Record<string, string> = {
  new: "Новый",
  contacted: "Связались",
  trial_booked: "Пробное назначено",
  trial_done: "Пробное проведено",
  thinking: "Думает",
};

const SOURCE_LABELS: Record<string, string> = {
  recommendation: "Рекомендация",
  instagram: "Instagram",
  vk: "ВКонтакте",
  signboard: "Вывеска",
  website: "Сайт",
  other: "Другое",
};

const LOSS_REASON_OPTIONS = [
  { value: "expensive", label: "Дорого" },
  { value: "didnt_like_training", label: "Не понравились тренировки" },
  { value: "didnt_like_trainer", label: "Не понравился тренер" },
  { value: "too_far", label: "Далеко" },
  { value: "no_time", label: "Нет времени" },
  { value: "chose_other_club", label: "Выбрал другой клуб" },
  { value: "injury_health", label: "Травма / здоровье" },
  { value: "child_didnt_like", label: "Ребёнку не понравилось" },
  { value: "changed_mind", label: "Передумал" },
  { value: "other", label: "Другое" },
] as const;

const ACTIVE_LEAD_STATUSES = new Set([
  "new",
  "contacted",
  "trial_booked",
  "trial_done",
  "thinking",
]);

interface LeadDetailSheetProps {
  readonly open: boolean;
  readonly onOpenChange: (open: boolean) => void;
  readonly lead: LeadData | null;
  readonly trainerId?: number | null;
  readonly onEnrollInGroup?: (lead: LeadData) => void;
  readonly onBookPersonal?: (lead: LeadData) => void;
  readonly onOpenExistingContext?: (lead: LeadData, action: LeadAction) => void;
  readonly unifiedEnabled?: boolean;
}

function leadName(lead: LeadData): string {
  return [lead.first_name, lead.last_name].filter(Boolean).join(" ") || "Заявка";
}

function formatTime(value: string): string {
  return value.slice(0, 5);
}

function scheduleLabel(schedule: ScheduleOccurrenceOut): string {
  const parts = [
    formatTime(schedule.effective_start_time),
    schedule.group_name,
    schedule.training_type_name,
    schedule.location_name,
  ].filter(Boolean);
  return parts.join(" · ");
}

function apiErrorCode(error: unknown): string | undefined {
  if (!error || typeof error !== "object") return undefined;
  const response = (error as { response?: { data?: unknown } }).response;
  if (!response?.data || typeof response.data !== "object") return undefined;
  const code = (response.data as { code?: unknown }).code;
  return typeof code === "string" ? code : undefined;
}

function trialBookingErrorMessage(error: unknown): string {
  if (apiErrorCode(error) === "trial_start_not_future") {
    return "Эта тренировка уже началась. Выберите будущую дату и тренировку.";
  }
  return "Не удалось записать на пробную. Проверьте дату и тренировку.";
}

function invalidateLifecycleQueries({
  queryClient,
  trainerId,
  trialBooking,
}: {
  queryClient: QueryClient;
  trainerId?: number | null;
  trialBooking?: { scheduleId?: number; date: string };
}) {
  queryClient.invalidateQueries({ queryKey: ["leads"] });
  queryClient.invalidateQueries({ queryKey: ["retention-tasks"] });
  queryClient.invalidateQueries({ queryKey: ["task-badge-count"] });

  if (trainerId) {
    queryClient.invalidateQueries({ queryKey: ["retention-tasks", trainerId] });
  }

  if (!trialBooking) return;

  queryClient.invalidateQueries({ queryKey: ["schedules", "by-date"] });
  queryClient.invalidateQueries({
    queryKey: ["schedules", "by-date", trialBooking.date],
  });
  queryClient.invalidateQueries({ queryKey: ["schedules", "today"] });
  if (!trialBooking.scheduleId) return;

  const scheduleIdText = String(trialBooking.scheduleId);
  queryClient.invalidateQueries({ queryKey: ["schedule", trialBooking.scheduleId] });
  queryClient.invalidateQueries({ queryKey: ["schedule", scheduleIdText] });
  queryClient.invalidateQueries({
    queryKey: ["schedule", trialBooking.scheduleId, "students"],
  });
  queryClient.invalidateQueries({
    queryKey: ["schedule", scheduleIdText, "students"],
  });
}

export function LeadDetailSheet({
  open,
  onOpenChange,
  lead,
  trainerId,
  onEnrollInGroup,
  onBookPersonal,
  onOpenExistingContext,
  unifiedEnabled = false,
}: LeadDetailSheetProps) {
  const queryClient = useQueryClient();
  const commercialCacheScope = useCommercialCacheScope();
  const timeZone = useBrandingStore((state) => state.timeZone);
  const [bookingOpen, setBookingOpen] = useState(false);
  const [lossOpen, setLossOpen] = useState(false);
  const [trialDate, setTrialDate] = useState("");
  const [scheduleId, setScheduleId] = useState("");
  const [lossReason, setLossReason] = useState("");
  const [releaseOpen, setReleaseOpen] = useState(false);
  const [releaseReason, setReleaseReason] = useState("");
  const [moreOpen, setMoreOpen] = useState(false);
  const [contactOpen, setContactOpen] = useState(false);
  const [contactOutcome, setContactOutcome] = useState("contacted");
  const [contactDueDate, setContactDueDate] = useState("");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [retryBankPaymentReceipt, setRetryBankPaymentReceipt] =
    useState<PersonalCommercialReceipt | null>(null);
  const [contextualCommercialContext, setContextualCommercialContext] =
    useState<ContextualCommercialContext | null>(null);
  const [contextualCommercialRetry, setContextualCommercialRetry] = useState(false);
  const [contextualCommercialPaymentMethod, setContextualCommercialPaymentMethod] = useState<
    "cash" | "transfer" | "sbp"
  >("cash");

  const actionContextQuery = useQuery<LeadActionContext>({
    queryKey: ["lead", lead?.id, "action-context"],
    queryFn: () =>
      apiClient
        .get(`/leads/${lead!.id}/action-context`)
        .then((response) => response.data),
    enabled: open && unifiedEnabled && Boolean(lead?.id) && lead?.lead_status !== null,
    staleTime: 30_000,
  });

  const commercialContextQuery = useQuery<PersonalCommercialContext>({
    queryKey: leadPersonalCommercialContextQueryKey(lead?.id ?? 0, commercialCacheScope),
    queryFn: () => getLeadPersonalCommercialContext(lead!.id),
    enabled: open && Boolean(lead?.id),
    staleTime: 30_000,
    retry: false,
  });

  const schedulesQuery = useQuery<ScheduleOccurrenceOut[]>({
    queryKey: ["schedules", "by-date", trialDate],
    queryFn: () =>
      apiClient
        .get("/schedules/by-date/", { params: { date: trialDate } })
        .then((r) => r.data),
    enabled: open && bookingOpen && Boolean(trialDate),
    staleTime: 60_000,
  });

  const statusMutation = useMutation({
    mutationFn: (status: string) =>
      apiClient.post(`/leads/${lead!.id}/status`, { status }),
    onSuccess: () => {
      invalidateLifecycleQueries({ queryClient, trainerId });
      handleClose();
    },
    onError: () => setErrorMsg("Не удалось изменить статус. Попробуйте ещё раз."),
  });

  const bookTrialMutation = useMutation({
    mutationFn: () =>
      apiClient.post(`/leads/${lead!.id}/book-trial`, {
        mode: "group",
        schedule_id: Number(scheduleId),
        occurrence_date: trialDate,
      }),
    onSuccess: () => {
      invalidateLifecycleQueries({
        queryClient,
        trainerId,
        trialBooking: {
          scheduleId: Number(scheduleId),
          date: trialDate,
        },
      });
      handleClose();
    },
    onError: (error) => setErrorMsg(trialBookingErrorMessage(error)),
  });

  const loseMutation = useMutation({
    mutationFn: () =>
      unifiedEnabled
        ? apiClient.post(`/leads/${lead!.id}/contact-outcomes/`, {
            outcome: "lost",
            due_date: null,
            loss_reason: lossReason,
            notes: "",
          })
        : apiClient.post(`/leads/${lead!.id}/lose`, { loss_reason: lossReason }),
    onSuccess: () => {
      invalidateLifecycleQueries({ queryClient, trainerId });
      handleClose();
    },
    onError: () => setErrorMsg("Не удалось сохранить потерю. Попробуйте ещё раз."),
  });

  const releaseMutation = useMutation({
    mutationFn: () =>
      apiClient.post(`/leads/${lead!.id}/release`, {
        reason: releaseReason.trim(),
      }),
    onSuccess: () => {
      invalidateLifecycleQueries({ queryClient, trainerId });
      handleClose();
    },
    onError: () => setErrorMsg("Не удалось передать заявку. Попробуйте ещё раз."),
  });

  const contactMutation = useMutation({
    mutationFn: () =>
      apiClient.post(`/leads/${lead!.id}/contact-outcomes/`, {
        outcome: contactOutcome,
        due_date: contactDueDate || null,
        loss_reason: "",
        notes: "",
      }),
    onSuccess: (response) => {
      invalidateLifecycleQueries({ queryClient, trainerId });
      const nextFlow = (response.data as { next_flow?: string | null }).next_flow;
      if (nextFlow === "book_trial") {
        setContactOpen(false);
        setBookingOpen(true);
        return;
      }
      if (nextFlow === "sell_group" && onEnrollInGroup) {
        handleClose();
        onEnrollInGroup(lead!);
        return;
      }
      if (nextFlow === "sell_personal" && onBookPersonal) {
        handleClose();
        onBookPersonal(lead!);
        return;
      }
      handleClose();
    },
    onError: () => setErrorMsg("Не удалось сохранить результат контакта."),
  });

  const reopenMutation = useMutation({
    mutationFn: () => apiClient.post(`/leads/${lead!.id}/reopen`),
    onSuccess: () => {
      invalidateLifecycleQueries({ queryClient, trainerId });
      handleClose();
    },
    onError: () => setErrorMsg("Не удалось вернуть заявку в работу."),
  });

  function resetLocalState() {
    setBookingOpen(false);
    setLossOpen(false);
    setReleaseOpen(false);
    setTrialDate("");
    setScheduleId("");
    setLossReason("");
    setReleaseReason("");
    setMoreOpen(false);
    setContactOpen(false);
    setContactOutcome("contacted");
    setContactDueDate("");
    setErrorMsg(null);
    setRetryBankPaymentReceipt(null);
  }

  function handleClose() {
    resetLocalState();
    onOpenChange(false);
  }

  function handleOpenChange(nextOpen: boolean) {
    if (!nextOpen) {
      handleClose();
      return;
    }
    onOpenChange(true);
  }

  function handleDateChange(value: string) {
    setTrialDate(value);
    setScheduleId("");
    setErrorMsg(null);
  }

  function handleScheduleChange(value: string) {
    setScheduleId(value);
    setErrorMsg(null);
  }

  if (!lead) return null;

  const status = lead.lead_status ?? "";
  const isPending =
    statusMutation.isPending ||
    bookTrialMutation.isPending ||
    loseMutation.isPending ||
    releaseMutation.isPending;
  const commandPending = isPending || contactMutation.isPending || reopenMutation.isPending;
  const canBookTrial = status === "contacted" || status === "thinking";
  const enrollmentActionLabel =
    status === "trial_done"
      ? "Оформить в группу"
      : status === "trial_booked"
        ? "Оформить без ожидания пробной"
        : "Оформить сразу в группу";
  const canLose = ACTIVE_LEAD_STATUSES.has(status);
  const canRelease = ACTIVE_LEAD_STATUSES.has(status);
  const canSubmitTrial = Boolean(trialDate && scheduleId);
  const earliestTrialDate = toDateParamInTimeZone(new Date(), timeZone);
  const displayName = leadName(lead);
  const trialDateId = `lead-${lead.id}-trial-date`;
  const scheduleIdField = `lead-${lead.id}-schedule`;
  const lossReasonId = `lead-${lead.id}-loss-reason`;
  const releaseReasonId = `lead-${lead.id}-release-reason`;
  const contactOutcomeId = `lead-${lead.id}-contact-outcome`;
  const contactDueDateId = `lead-${lead.id}-contact-due-date`;
  const isArchived = lead.workspace === "leads_archived" || lead.lead_status === null;
  const primaryAction = actionContextQuery.data?.primary_action ?? lead.primary_action ?? null;
  const requiresContactDueDate = contactOutcome === "no_answer";
  const canSubmitContactOutcome = !requiresContactDueDate || Boolean(contactDueDate);

  return (
    <Sheet open={open} onOpenChange={handleOpenChange}>
      <SheetContent
        side="bottom"
        showCloseButton={false}
        className="max-h-[90vh] overflow-y-auto rounded-t-2xl"
      >
        <SheetHeader>
          <SheetTitle className="break-words leading-snug">
            {displayName}
          </SheetTitle>
          <SheetDescription className="break-words leading-snug">
            {isArchived ? "Завершённая заявка" : STATUS_LABELS[status] ?? status}
          </SheetDescription>
        </SheetHeader>

        <div className="flex flex-col gap-4 px-4 pb-4">
          <section className="rounded-lg bg-muted/50 p-3 text-[14px]">
            <dl className="grid grid-cols-[88px_minmax(0,1fr)] gap-x-3 gap-y-2">
              <dt className="ui-muted">Телефон</dt>
              <dd className="ui-value">{lead.phone ?? "Телефон скрыт"}</dd>
              <dt className="ui-muted">Источник</dt>
              <dd className="ui-value">
                {SOURCE_LABELS[lead.source] ?? lead.source}
              </dd>
              <dt className="ui-muted">Тип</dt>
              <dd className="text-foreground">{lead.is_child ? "Ребёнок" : "Взрослый"}</dd>
            </dl>
          </section>

          {errorMsg && (
            <p className="rounded-lg bg-destructive/10 px-3 py-2 text-[14px] text-destructive">
              {errorMsg}
            </p>
          )}

          {retryBankPaymentReceipt ? (
            <section aria-label="Повтор оплаты персоналки" className="ui-col-3">
              <p className="ui-caption-label">Повтор оплаты СБП</p>
              <PersonalBookingForm
                studentId={lead.id}
                studentName={displayName}
                retryBankPaymentReceipt={retryBankPaymentReceipt}
                onClose={() => setRetryBankPaymentReceipt(null)}
                onStaffIntentCreated={() => setRetryBankPaymentReceipt(null)}
              />
            </section>
          ) : commercialContextQuery.isLoading ? (
            <section className="ui-col-2">
              <p className="ui-muted-14">Загрузка записи и оплаты...</p>
            </section>
          ) : commercialContextQuery.isError ? (
            <section aria-label="Коммерческий контекст персоналки" className="ui-col-2">
              <p role="status" className="ui-warning">
                Коммерческий контекст сейчас недоступен. Обновите карточку заявки.
              </p>
            </section>
          ) : commercialContextQuery.data?.attempts?.length ? (
            <section className="ui-col-2">
              <h3 className="ui-section-label">Персональная запись и оплата</h3>
              <PersonalCommercialReceiptList
                attempts={commercialContextQuery.data.attempts}
                studentId={lead.id}
                onReceiptChanged={() => {
                  void queryClient.invalidateQueries({
                    queryKey: leadPersonalCommercialContextQueryKey(lead.id, commercialCacheScope),
                  });
                  void queryClient.invalidateQueries({ queryKey: ["leads"] });
                }}
                onRetryPersonalBankPayment={setRetryBankPaymentReceipt}
                canRetryContextualBankPayment={(receipt) =>
                  contextualRetryContextFromReceipt({
                    receipt,
                    studentId: lead.id,
                    studentName: displayName,
                  }) !== null
                }
                onRetryContextualBankPayment={(receipt) => {
                  const retryContext = contextualRetryContextFromReceipt({
                    receipt,
                    studentId: lead.id,
                    studentName: displayName,
                  });
                  if (retryContext) {
                    setContextualCommercialRetry(true);
                    setContextualCommercialPaymentMethod(
                      receipt.payment_method === "transfer"
                        ? "transfer"
                        : receipt.payment_method === "sbp"
                          ? "sbp"
                          : "cash",
                    );
                    setContextualCommercialContext(retryContext);
                  }
                }}
              />
            </section>
          ) : null}

          {unifiedEnabled ? (
            <section className="ui-col-3">
              {primaryAction?.supporting_text ? (
                <p className="rounded-lg bg-[var(--branding-accent)]/10 px-3 py-3 text-[14px] leading-snug text-foreground">
                  {primaryAction.supporting_text}
                </p>
              ) : null}
              {!bookingOpen && !contactOpen ? (
                <Button
                  type="button"
                  disabled={commandPending}
                  className="ui-brand-action"
                  onClick={() => {
                    setErrorMsg(null);
                    if (isArchived) {
                      reopenMutation.mutate();
                      return;
                    }
                    if (!primaryAction || primaryAction.kind === "contact_lead") {
                      setContactOpen(true);
                      return;
                    }
                    if (primaryAction.kind === "sell_training" && onEnrollInGroup) {
                      handleClose();
                      onEnrollInGroup(lead);
                      return;
                    }
                    if (primaryAction) {
                      onOpenExistingContext?.(lead, primaryAction);
                    }
                  }}
                >
                  <PhoneCall />
                  {isArchived
                    ? reopenMutation.isPending
                      ? "Возвращаю..."
                      : "Вернуть в работу"
                    : primaryAction?.label ?? "Связаться"}
                </Button>
              ) : null}

              {contactOpen ? (
                <form
                  className="ui-col-3"
                  onSubmit={(event) => {
                    event.preventDefault();
                    setErrorMsg(null);
                    if (requiresContactDueDate && !contactDueDate) {
                      setErrorMsg("Выберите дату следующего контакта.");
                      return;
                    }
                    contactMutation.mutate();
                  }}
                >
                  <div>
                    <label htmlFor={contactOutcomeId} className="ui-field-label">
                      Результат контакта
                    </label>
                    <select
                      id={contactOutcomeId}
                      value={contactOutcome}
                      onChange={(event) => setContactOutcome(event.target.value)}
                      className="min-h-11 w-full rounded-md border border-input bg-background px-3 py-2 text-base text-foreground md:text-sm"
                    >
                      <option value="contacted">Связался</option>
                      <option value="no_answer">Не дозвонился</option>
                      <option value="follow_up">Перезвонить позже</option>
                      <option value="book_trial">Записать на пробную</option>
                      <option value="sell_group">Оформить в группу</option>
                      <option value="sell_personal">Записать персоналку</option>
                    </select>
                  </div>
                  {contactOutcome === "follow_up" || contactOutcome === "no_answer" ? (
                    <div>
                      <label htmlFor={contactDueDateId} className="ui-field-label">
                        Дата следующего контакта
                      </label>
                      <Input
                        id={contactDueDateId}
                        type="date"
                        min={earliestTrialDate}
                        required={requiresContactDueDate}
                        value={contactDueDate}
                        onChange={(event) => setContactDueDate(event.target.value)}
                      />
                    </div>
                  ) : null}
                  <Button
                    type="submit"
                    disabled={commandPending || !canSubmitContactOutcome}
                    className="ui-brand-action"
                  >
                    {contactMutation.isPending ? "Сохраняю..." : "Сохранить результат"}
                  </Button>
                </form>
              ) : null}

              {!isArchived && !bookingOpen && !contactOpen ? (
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => setMoreOpen((value) => !value)}
                  className="min-h-11 w-full"
                >
                  <MoreHorizontal />
                  Ещё
                </Button>
              ) : null}

              {moreOpen && !bookingOpen && !contactOpen ? (
                <div className="ui-col-2 rounded-lg border border-border p-3">
                  {canBookTrial ? (
                    <Button
                      type="button"
                      variant="outline"
                      onClick={() => {
                        setBookingOpen(true);
                        setMoreOpen(false);
                      }}
                    >
                      <CalendarCheck />
                      Записать на пробную
                    </Button>
                  ) : null}
                  {onBookPersonal ? (
                    <Button
                      type="button"
                      variant="outline"
                      onClick={() => {
                        handleClose();
                        onBookPersonal(lead);
                      }}
                    >
                      Записать персоналку
                    </Button>
                  ) : null}
                  {onEnrollInGroup ? (
                    <Button
                      type="button"
                      variant="outline"
                      onClick={() => {
                        handleClose();
                        onEnrollInGroup(lead);
                      }}
                    >
                      {enrollmentActionLabel}
                    </Button>
                  ) : null}
                </div>
              ) : null}
            </section>
          ) : (
            <section className="ui-col-2">
              {status === "new" && (
              <Button
                type="button"
                onClick={() => {
                  setErrorMsg(null);
                  statusMutation.mutate("contacted");
                }}
                disabled={isPending}
                className="ui-brand-action"
              >
                <PhoneCall />
                {statusMutation.isPending ? "Сохранение..." : "Связался"}
              </Button>
            )}

              {onBookPersonal && ACTIVE_LEAD_STATUSES.has(status) && !bookingOpen && (
              <Button
                type="button"
                onClick={() => {
                  resetLocalState();
                  onOpenChange(false);
                  onBookPersonal(lead);
                }}
                disabled={isPending}
                className="ui-brand-action"
              >
                <UserPlus />
                Записать персоналку
              </Button>
            )}

              {canBookTrial && !bookingOpen && (
              <Button
                type="button"
                onClick={() => {
                  setBookingOpen(true);
                  setLossOpen(false);
                  setReleaseOpen(false);
                  setErrorMsg(null);
                }}
                disabled={isPending}
                className="ui-brand-action"
              >
                <CalendarCheck />
                Записать на пробную
              </Button>
            )}

              {status === "trial_booked" && (
              <Button
                type="button"
                onClick={() => {
                  setErrorMsg(null);
                  statusMutation.mutate("trial_done");
                }}
                disabled={isPending}
                className="ui-brand-action"
              >
                <CheckCircle2 />
                {statusMutation.isPending ? "Сохранение..." : "Пробная прошла"}
              </Button>
            )}

              {status === "trial_done" && (
              <p className="rounded-lg bg-[var(--branding-accent)]/10 px-3 py-3 text-[14px] leading-snug text-foreground">
                Готов к оплате: оформите абонемент или передайте администратору.
              </p>
            )}

              {onEnrollInGroup && ACTIVE_LEAD_STATUSES.has(status) && !bookingOpen && (
              <Button
                type="button"
                onClick={() => {
                  resetLocalState();
                  onOpenChange(false);
                  onEnrollInGroup(lead);
                }}
                disabled={isPending}
                className="ui-brand-action"
              >
                <UserPlus />
                {enrollmentActionLabel}
              </Button>
            )}
            </section>
          )}

          {bookingOpen && (
            <form
              className="ui-col-3"
              onSubmit={(event) => {
                event.preventDefault();
                if (!canSubmitTrial) return;
                setErrorMsg(null);
                bookTrialMutation.mutate();
              }}
            >
              <div>
                <label
                  htmlFor={trialDateId}
                  className="ui-field-label"
                >
                  Дата пробной *
                </label>
                <Input
                  id={trialDateId}
                  type="date"
                  min={earliestTrialDate}
                  value={trialDate}
                  onChange={(event) => handleDateChange(event.target.value)}
                />
              </div>

              <div>
                <label
                  htmlFor={scheduleIdField}
                  className="ui-field-label"
                >
                  Тренировка *
                </label>
                <select
                  id={scheduleIdField}
                  value={scheduleId}
                  onChange={(event) => handleScheduleChange(event.target.value)}
                  disabled={!trialDate || schedulesQuery.isLoading}
                  className="min-h-11 w-full rounded-md border border-input bg-background px-3 py-2 text-base text-foreground disabled:cursor-not-allowed disabled:opacity-50 md:text-sm"
                >
                  <option value="">
                    {schedulesQuery.isLoading
                      ? "Загрузка..."
                      : trialDate
                        ? "Выберите тренировку"
                        : "Сначала выберите дату"}
                  </option>
                  {(schedulesQuery.data ?? []).map((schedule) => (
                    <option key={schedule.schedule_id} value={schedule.schedule_id}>
                      {scheduleLabel(schedule)}
                    </option>
                  ))}
                </select>
              </div>

              {trialDate &&
                !schedulesQuery.isLoading &&
                schedulesQuery.data?.length === 0 && (
                  <p className="ui-muted-14">
                    На эту дату нет доступных тренировок.
                  </p>
                )}

              <Button
                type="submit"
                disabled={!canSubmitTrial || isPending}
                className="ui-brand-action"
              >
                {bookTrialMutation.isPending ? "Сохранение..." : "Сохранить пробную"}
              </Button>
            </form>
          )}

          {canLose && (!unifiedEnabled || moreOpen) && (
            <section className="flex flex-col gap-3 border-t border-border pt-3">
              {!lossOpen ? (
                <Button
                  type="button"
                  variant="destructive"
                  onClick={() => {
                    setLossOpen(true);
                    setBookingOpen(false);
                    setReleaseOpen(false);
                    setErrorMsg(null);
                  }}
                  disabled={isPending}
                  className="min-h-11 w-full"
                >
                  <UserX />
                  Потерян
                </Button>
              ) : (
                <form
                  className="ui-col-3"
                  onSubmit={(event) => {
                    event.preventDefault();
                    if (!lossReason) {
                      setErrorMsg("Выберите причину потери");
                      return;
                    }
                    setErrorMsg(null);
                    loseMutation.mutate();
                  }}
                >
                  <div>
                    <label
                      htmlFor={lossReasonId}
                      className="ui-field-label"
                    >
                      Причина потери *
                    </label>
                    <select
                      id={lossReasonId}
                      value={lossReason}
                      onChange={(event) => {
                        setLossReason(event.target.value);
                        setErrorMsg(null);
                      }}
                      className="min-h-11 w-full rounded-md border border-input bg-background px-3 py-2 text-base text-foreground md:text-sm"
                    >
                      <option value="">Выберите причину</option>
                      {LOSS_REASON_OPTIONS.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </div>
                  <Button
                    type="submit"
                    variant="destructive"
                    disabled={isPending}
                    className="min-h-11 w-full"
                  >
                    {loseMutation.isPending ? "Сохранение..." : "Сохранить потерю"}
                  </Button>
                </form>
              )}
            </section>
          )}

          {canRelease && (!unifiedEnabled || moreOpen) && (
            <section className="flex flex-col gap-3 border-t border-border pt-3">
              {!releaseOpen ? (
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => {
                    setReleaseOpen(true);
                    setBookingOpen(false);
                    setLossOpen(false);
                    setErrorMsg(null);
                  }}
                  disabled={isPending}
                  className="min-h-11 w-full"
                >
                  <Send />
                  Передать администратору
                </Button>
              ) : (
                <form
                  className="ui-col-3"
                  onSubmit={(event) => {
                    event.preventDefault();
                    if (!releaseReason.trim()) {
                      setErrorMsg("Укажите причину передачи");
                      return;
                    }
                    setErrorMsg(null);
                    releaseMutation.mutate();
                  }}
                >
                  <div>
                    <label
                      htmlFor={releaseReasonId}
                      className="ui-field-label"
                    >
                      Причина передачи *
                    </label>
                    <textarea
                      id={releaseReasonId}
                      value={releaseReason}
                      onChange={(event) => {
                        setReleaseReason(event.target.value);
                        setErrorMsg(null);
                      }}
                      placeholder="Например: хочет к другому тренеру"
                      className="min-h-24 w-full rounded-md border border-input bg-background px-3 py-2 text-base text-foreground placeholder:text-muted-foreground md:text-sm"
                    />
                  </div>
                  <p className="text-[14px] leading-snug text-muted-foreground">
                    Заявка вернётся в общий пул. Администратор сможет назначить ответственного.
                  </p>
                  <Button
                    type="submit"
                    variant="outline"
                    disabled={isPending}
                    className="min-h-11 w-full"
                  >
                    {releaseMutation.isPending
                      ? "Передача..."
                      : "Передать администратору"}
                  </Button>
                </form>
              )}
            </section>
          )}

          <Button
            type="button"
            variant="ghost"
            onClick={handleClose}
            className="min-h-11 w-full"
          >
            Закрыть
          </Button>
        </div>
      </SheetContent>
      <ContextualCommercialSheet
        key={
          contextualCommercialContext?.kind === "group_sale"
            ? `group:${contextualCommercialContext.trainingGroupId}:${contextualCommercialContext.scheduleId}:${contextualCommercialContext.startDate}:${contextualCommercialPaymentMethod}:${contextualCommercialRetry}`
            : contextualCommercialContext?.kind === "subscription_renewal"
              ? `renewal:${contextualCommercialContext.renewedFromSubscriptionId}:${contextualCommercialPaymentMethod}:${contextualCommercialRetry}`
              : "none"
        }
        open={contextualCommercialContext !== null}
        onOpenChange={(nextOpen) => {
          if (!nextOpen) {
            setContextualCommercialContext(null);
            setContextualCommercialRetry(false);
            setContextualCommercialPaymentMethod("cash");
          }
        }}
        context={contextualCommercialContext}
        initialPaymentMethod={contextualCommercialPaymentMethod}
        freshCommand={contextualCommercialRetry}
        onOfferChanged={() => {
          setContextualCommercialContext(null);
          setContextualCommercialRetry(false);
          setContextualCommercialPaymentMethod("cash");
          void commercialContextQuery.refetch();
        }}
      />
    </Sheet>
  );
}
