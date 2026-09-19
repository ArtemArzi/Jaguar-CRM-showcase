import { useState } from "react";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router";
import { UserPlus, Plus, ChevronDown, ChevronRight } from "lucide-react";
import { Skeleton } from "@/components/ui/skeleton";
import apiClient from "@/api/custom-fetch";
import {
  getUnifiedClientJourneyCapabilityMode,
  getGroupSaleCommandProtocol,
  usePersonalAvailabilityCapability,
  useUnifiedClientJourneyCapabilityQuery,
} from "@/api/unified-client-journey";
import {
  hasCanonicalTrainingGroupSelectionCapability,
  usePaymentCapabilities,
} from "@/api/payment-capabilities";
import { formatRub } from "@/lib/utils";
import { LeadCard, type LeadData } from "../components/lead-card";
import { LeadDetailSheet } from "../components/lead-detail-sheet";
import { ClientIntakeSheet } from "../components/client-intake-sheet";
import { PaymentSheet, type PaymentRecordedSuccess } from "../components/payment-sheet";
import { PersonalBookingSheet, type PersonalBookingSuccess } from "../components/personal-booking-sheet";
import {
  ContextualGroupSalePickerSheet,
  ContextualGroupSaleUnavailableSheet,
} from "../components/contextual-commercial-sheet";

const STATUS_ORDER = [
  { key: "new", label: "НОВЫЕ" },
  { key: "contacted", label: "СВЯЗАЛИСЬ" },
  { key: "trial_booked", label: "ПРОБНОЕ" },
  { key: "trial_done", label: "ПРОБНОЕ ПРОЙДЕНО" },
  { key: "thinking", label: "ДУМАЮТ" },
] as const;

type LeadScope = "mine" | "pool" | "archived";
const LEADS_PAGE_LIMIT = 50;

type LeadListResponse = LeadData[] | { items?: LeadData[]; count?: number };
type LeadPage = { readonly items: LeadData[]; readonly count?: number };

function normalizeLeadResponse(data: LeadListResponse): LeadPage {
  return Array.isArray(data)
    ? { items: data }
    : { items: data.items ?? [], count: data.count };
}

function nextLeadPageParam(lastPage: LeadPage, allPages: LeadPage[]): number | undefined {
  const loaded = allPages.reduce((total, page) => total + page.items.length, 0);
  if (lastPage.count === undefined) return undefined;
  return loaded < lastPage.count ? loaded : undefined;
}

function errorStatus(error: unknown): number | undefined {
  return (error as { response?: { status?: number } })?.response?.status;
}

function leadDisplayName(lead: LeadData) {
  return [lead.first_name, lead.last_name].filter(Boolean).join(" ") || "Заявка";
}

function LeadsSkeleton() {
  return (
    <div className="ui-col-3">
      {[1, 2, 3].map((i) => (
        <Skeleton key={i} className="h-[72px] rounded-xl" />
      ))}
    </div>
  );
}

function EmptyState({ scope }: { scope: LeadScope }) {
  const isPool = scope === "pool";
  const isArchived = scope === "archived";

  return (
    <div className="ui-empty-state">
      <UserPlus size={48} className="ui-muted" />
      <p className="ui-title-20">
        {isPool
          ? "Свободных заявок нет"
          : isArchived
            ? "Завершённых заявок нет"
            : "Нет активных заявок"}
      </p>
      <p className="text-[16px] text-muted-foreground">
        {isPool
          ? "Все активные заявки уже распределены"
          : isArchived
            ? "Здесь появятся never-converted потерянные заявки"
          : "Создайте новую заявку через + или проверьте завершённые заявки в CRM"}
      </p>
    </div>
  );
}

function ErrorState({ scope }: { scope: LeadScope }) {
  return (
    <div className="ui-empty-state">
      <UserPlus size={48} className="text-destructive" />
      <p className="ui-title-20">
        Не удалось загрузить заявки
      </p>
      <p className="text-[16px] text-muted-foreground text-center">
        {scope === "pool"
          ? "Обновите свободные заявки или попробуйте позже"
          : "Обновите список или попробуйте позже"}
      </p>
    </div>
  );
}

interface StatusSectionProps {
  label: string;
  leads: LeadData[];
  onSelectLead: (lead: LeadData) => void;
}

function StatusSection({ label, leads, onSelectLead }: StatusSectionProps) {
  const [expanded, setExpanded] = useState(true);

  if (leads.length === 0) return null;

  return (
    <section>
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="flex items-center gap-2 w-full mb-2"
      >
        {expanded ? (
          <ChevronDown size={16} className="ui-muted" />
        ) : (
          <ChevronRight size={16} className="ui-muted" />
        )}
        <span className="text-[14px] font-semibold uppercase tracking-wider text-muted-foreground">
          {label}
        </span>
        <span className="ui-muted-14">
          ({leads.length})
        </span>
      </button>
      {expanded && (
        <div className="ui-col-2">
          {leads.map((lead) => (
            <LeadCard key={lead.id} lead={lead} onSelect={onSelectLead} />
          ))}
        </div>
      )}
    </section>
  );
}

export default function Leads() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const queryClient = useQueryClient();
  const unifiedJourneyCapability = useUnifiedClientJourneyCapabilityQuery();
  const unifiedJourneyMode = getUnifiedClientJourneyCapabilityMode(unifiedJourneyCapability);
  const groupSaleCommandProtocol = getGroupSaleCommandProtocol(unifiedJourneyCapability);
  const unifiedIntakeEnabled = unifiedJourneyMode === "unified";
  const unifiedPersonalAvailabilityEnabled = usePersonalAvailabilityCapability();
  const paymentCapabilitiesQuery = usePaymentCapabilities();
  const canonicalGroupSelectionEnabled = hasCanonicalTrainingGroupSelectionCapability(paymentCapabilitiesQuery);
  const contextualGroupSaleEnabled =
    unifiedIntakeEnabled && canonicalGroupSelectionEnabled && groupSaleCommandProtocol === "v2";
  const legacyGroupSaleEnabled =
    unifiedIntakeEnabled && canonicalGroupSelectionEnabled && groupSaleCommandProtocol === "v1";
  const [sheetOpen, setSheetOpen] = useState(false);
  const [selectedLead, setSelectedLead] = useState<LeadData | null>(null);
  const [enrollmentLead, setEnrollmentLead] = useState<LeadData | null>(null);
  const [personalBookingLead, setPersonalBookingLead] = useState<LeadData | null>(null);
  const [activeScope, setActiveScope] = useState<LeadScope>(() => {
    if (searchParams.get("workspace") === "archived") return "archived";
    if (searchParams.get("scope") === "pool") return "pool";
    return "mine";
  });
  const [claimError, setClaimError] = useState<string | null>(null);
  const [paymentNotice, setPaymentNotice] = useState<PaymentRecordedSuccess | null>(null);
  const [groupSalePendingNotice, setGroupSalePendingNotice] = useState<{
    readonly studentId: number;
    readonly orderId: number;
    readonly providerPaymentUrl: string;
    readonly status: string | null;
    readonly fulfillmentState: string | null;
    readonly allowedActions: readonly string[];
  } | null>(null);
  const [groupSalePendingAction, setGroupSalePendingAction] = useState<"cancel" | "refresh" | null>(null);
  const [groupSalePendingActionError, setGroupSalePendingActionError] = useState<string | null>(null);
  const [personalBookingNotice, setPersonalBookingNotice] = useState<string | null>(null);

  function handlePersonalBookingSuccess(success: PersonalBookingSuccess) {
    const date = success.startsAt.slice(0, 10).split("-").reverse().join(".");
    const time = `${success.startsAt.slice(11, 16)}–${success.endsAt.slice(11, 16)}`;
    const price = success.price == null ? "по абонементу" : formatRub(success.price);
    setPersonalBookingNotice(`Персоналка записана: ${date}, ${time}, ${price}.`);
  }

  async function runGroupSalePendingAction(action: "cancel" | "refresh") {
    if (!groupSalePendingNotice || !groupSalePendingNotice.allowedActions.includes(action)) return;
    setGroupSalePendingAction(action);
    setGroupSalePendingActionError(null);
    try {
      const response = await apiClient.post<{
        status?: string;
        provider_payment_url?: string;
        fulfillment_state?: string;
        can_share?: boolean;
        can_copy?: boolean;
        can_cancel?: boolean;
        can_request_refresh?: boolean;
      }>(`/billing/bank-payment-orders/${groupSalePendingNotice.orderId}/${action}/`, {});
      if (response.data.fulfillment_state === "fulfilled") {
        await Promise.all([
          queryClient.invalidateQueries({ queryKey: ["billing", "bank-payment-orders"] }),
          queryClient.invalidateQueries({ queryKey: ["lead"] }),
          queryClient.invalidateQueries({ queryKey: ["leads"] }),
          queryClient.invalidateQueries({ queryKey: ["student"] }),
        ]);
        setGroupSalePendingNotice(null);
        navigate(`/trainer/students/${groupSalePendingNotice.studentId}`);
        return;
      }
      const linkUsable = Boolean(response.data.can_share || response.data.can_copy);
      setGroupSalePendingNotice({
        ...groupSalePendingNotice,
        status: response.data.status ?? groupSalePendingNotice.status,
        fulfillmentState:
          response.data.fulfillment_state ?? groupSalePendingNotice.fulfillmentState,
        providerPaymentUrl: linkUsable ? response.data.provider_payment_url ?? "" : "",
        allowedActions: [
          ...(response.data.can_cancel ? ["cancel"] : []),
          ...(response.data.can_request_refresh ? ["refresh"] : []),
        ],
      });
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["billing", "bank-payment-orders"] }),
        queryClient.invalidateQueries({ queryKey: ["lead"] }),
        queryClient.invalidateQueries({ queryKey: ["leads"] }),
      ]);
    } catch {
      setGroupSalePendingActionError("Не удалось обновить статус оплаты. Повторите позже.");
    } finally {
      setGroupSalePendingAction(null);
    }
  }

  const { data: trainerInfo } = useQuery<{ id: number }>({
    queryKey: ["trainer", "me"],
    queryFn: () => apiClient.get("/trainers/me/").then((r) => r.data),
    staleTime: 5 * 60_000,
  });
  const trainerId = trainerInfo?.id ?? null;
  const groupSaleAttemptTerminal = Boolean(
    groupSalePendingNotice &&
      (groupSalePendingNotice.fulfillmentState === "not_fulfilled" ||
        ["cancelled", "failed", "expired"].includes(groupSalePendingNotice.status ?? "")),
  );
  const groupSaleFulfillmentPending =
    groupSalePendingNotice?.fulfillmentState === "fulfillment_pending";
  const requestedLeadId = Number(searchParams.get("lead"));
  const deepLinkedLead = useQuery<LeadData>({
    queryKey: ["lead", requestedLeadId],
    queryFn: () => apiClient.get(`/leads/${requestedLeadId}`).then((response) => response.data),
    enabled: Number.isInteger(requestedLeadId) && requestedLeadId > 0,
    retry: false,
  });

  const mineQuery = useInfiniteQuery<LeadPage>({
    queryKey: ["leads", "mine", { limit: LEADS_PAGE_LIMIT }],
    queryFn: ({ pageParam }) =>
      apiClient
        .get("/leads/", { params: { scope: "mine", limit: LEADS_PAGE_LIMIT, offset: pageParam } })
        .then((r) => normalizeLeadResponse(r.data)),
    initialPageParam: 0,
    getNextPageParam: nextLeadPageParam,
    staleTime: 2 * 60_000,
  });

  const poolQuery = useInfiniteQuery<LeadPage>({
    queryKey: ["leads", "pool", { limit: LEADS_PAGE_LIMIT }],
    queryFn: ({ pageParam }) =>
      apiClient
        .get("/leads/", { params: { scope: "pool", limit: LEADS_PAGE_LIMIT, offset: pageParam } })
        .then((r) => normalizeLeadResponse(r.data)),
    initialPageParam: 0,
    getNextPageParam: nextLeadPageParam,
    enabled: activeScope === "pool",
    staleTime: 2 * 60_000,
  });

  const archivedQuery = useInfiniteQuery<LeadPage>({
    queryKey: ["leads", "archived", { limit: LEADS_PAGE_LIMIT }],
    queryFn: ({ pageParam }) =>
      apiClient
        .get("/leads/", {
          params: {
            workspace: "archived",
            scope: "mine",
            limit: LEADS_PAGE_LIMIT,
            offset: pageParam,
          },
        })
        .then((response) => normalizeLeadResponse(response.data)),
    initialPageParam: 0,
    getNextPageParam: nextLeadPageParam,
    enabled: unifiedIntakeEnabled && activeScope === "archived",
    staleTime: 2 * 60_000,
  });

  const claimMutation = useMutation({
    mutationFn: (lead: LeadData) =>
      apiClient.post(`/leads/${lead.id}/claim`).then((r) => r.data as LeadData),
    onSuccess: (claimedLead) => {
      void claimedLead;
      queryClient.invalidateQueries({ queryKey: ["leads", "mine"] });
      queryClient.invalidateQueries({ queryKey: ["leads", "pool"] });
      setClaimError(null);
      setActiveScope("mine");
    },
    onError: (error) => {
      setClaimError(
        errorStatus(error) === 409
          ? "Заявку уже забрали"
          : "Не удалось забрать заявку. Попробуйте ещё раз.",
      );
      queryClient.invalidateQueries({ queryKey: ["leads", "pool"] });
    },
  });

  function groupByStatus(items: LeadData[]): Map<string, LeadData[]> {
    const grouped = new Map<string, LeadData[]>();
    for (const status of STATUS_ORDER) {
      grouped.set(status.key, []);
    }
    for (const lead of items) {
      const leadStatus = lead.lead_status;
      if (!leadStatus) continue;
      const existing = grouped.get(leadStatus);
      if (existing) {
        existing.push(lead);
      } else {
        grouped.set(leadStatus, [lead]);
      }
    }
    return grouped;
  }

  function handleScopeChange(scope: LeadScope) {
    setActiveScope(scope);
    setClaimError(null);
    const nextParams = new URLSearchParams(searchParams);
    nextParams.delete("lead");
    nextParams.delete("scope");
    nextParams.delete("workspace");
    if (scope === "pool") nextParams.set("scope", "pool");
    if (scope === "archived") nextParams.set("workspace", "archived");
    setSearchParams(nextParams, { replace: true });
  }

  const activeQuery =
    activeScope === "mine"
      ? mineQuery
      : activeScope === "pool"
        ? poolQuery
        : archivedQuery;
  const leadResponse = activeQuery.data;
  const leads = leadResponse ? leadResponse.pages.flatMap((page) => page.items) : undefined;
  const totalLeads = leadResponse?.pages[0]?.count ?? leads?.length ?? 0;
  const isLoading = activeQuery.isLoading;
  const isError = activeQuery.isError;
  const hasMoreLeads = activeQuery.hasNextPage;
  const isFetchingMoreLeads = activeQuery.isFetchingNextPage;
  const fetchMoreLeads = activeQuery.fetchNextPage;
  const grouped = leads ? groupByStatus(leads) : null;
  const claimingLeadId =
    claimMutation.isPending && claimMutation.variables
      ? claimMutation.variables.id
      : null;
  const loadMoreLeadsButton = hasMoreLeads ? (
    <button
      type="button"
      onClick={() => void fetchMoreLeads()}
      disabled={isFetchingMoreLeads}
      className="min-h-11 rounded-xl border border-input bg-white px-4 text-[14px] font-medium text-foreground disabled:opacity-60"
    >
      {isFetchingMoreLeads ? "Загружаю..." : "Загрузить ещё"}
    </button>
  ) : null;

  return (
    <div className="flex flex-col gap-4 px-4 pt-6 pb-4">
      <h1 className="ui-title-20">Заявки</h1>

      <div
        role="tablist"
        aria-label="Режим заявок"
        className={`grid ${unifiedIntakeEnabled ? "grid-cols-3" : "grid-cols-2"} rounded-xl bg-muted p-1`}
      >
        <button
          type="button"
          role="tab"
          aria-selected={activeScope === "mine"}
          onClick={() => handleScopeChange("mine")}
          className={`min-h-11 rounded-lg px-3 text-[15px] font-medium transition ${
            activeScope === "mine"
              ? "bg-background text-foreground shadow-sm"
              : "text-muted-foreground"
          }`}
        >
          Мои
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={activeScope === "pool"}
          onClick={() => handleScopeChange("pool")}
          className={`min-h-11 rounded-lg px-3 text-[15px] font-medium transition ${
            activeScope === "pool"
              ? "bg-background text-foreground shadow-sm"
              : "text-muted-foreground"
          }`}
        >
          Свободные заявки
        </button>
        {unifiedIntakeEnabled ? (
          <button
            type="button"
            role="tab"
            aria-selected={activeScope === "archived"}
            onClick={() => handleScopeChange("archived")}
            className={`min-h-11 rounded-lg px-2 text-[14px] font-medium transition ${
              activeScope === "archived"
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground"
            }`}
          >
            Завершённые
          </button>
        ) : null}
      </div>

      {claimError && (
        <p className="rounded-lg bg-destructive/10 px-3 py-2 text-[14px] text-destructive">
          {claimError}
        </p>
      )}

      {paymentNotice && (
        <p
          role="status"
          className="rounded-lg bg-emerald-50 px-3 py-2 text-[14px] text-emerald-800"
        >
          <span>{paymentNotice.message}</span>
          <button
            type="button"
            className="ml-2 min-h-[44px] rounded-md px-2 font-semibold underline underline-offset-2"
            onClick={() => navigate(`/trainer/students/${paymentNotice.studentId}`)}
          >
            Открыть карточку ученика
          </button>
        </p>
      )}

      {groupSalePendingNotice && (
        <p
          role="status"
          className="rounded-lg bg-amber-50 px-3 py-2 text-[14px] text-amber-900"
        >
          <span>
            {groupSaleAttemptTerminal
              ? "Попытка оплаты завершена. Чтобы создать новую ссылку, снова откройте оформление обучения."
              : groupSaleFulfillmentPending
                ? "Оплата подтверждена. Завершаем оформление ученика."
                : "Ссылка создана. Ученик будет оформлен после подтверждения оплаты."}
          </span>
          {groupSalePendingNotice.providerPaymentUrl ? (
            <a
              className="ml-2 inline-flex min-h-[44px] items-center rounded-md px-2 font-semibold underline underline-offset-2"
              href={groupSalePendingNotice.providerPaymentUrl}
              target="_blank"
              rel="noreferrer"
            >
              Открыть ссылку СБП
            </a>
          ) : null}
          {groupSalePendingNotice.allowedActions.includes("cancel") ? (
            <button
              type="button"
              className="ml-2 min-h-[44px] rounded-md px-2 font-semibold underline underline-offset-2"
              disabled={groupSalePendingAction !== null}
              onClick={() => void runGroupSalePendingAction("cancel")}
            >
              Отменить попытку
            </button>
          ) : null}
          {groupSalePendingNotice.allowedActions.includes("refresh") ? (
            <button
              type="button"
              className="ml-2 min-h-[44px] rounded-md px-2 font-semibold underline underline-offset-2"
              disabled={groupSalePendingAction !== null}
              onClick={() => void runGroupSalePendingAction("refresh")}
            >
              Сверить оплату
            </button>
          ) : null}
          {groupSaleAttemptTerminal ? (
            <button
              type="button"
              className="ml-2 min-h-[44px] rounded-md px-2 font-semibold underline underline-offset-2"
              onClick={() => setGroupSalePendingNotice(null)}
            >
              Закрыть уведомление
            </button>
          ) : null}
          {groupSalePendingActionError ? <span className="block">{groupSalePendingActionError}</span> : null}
        </p>
      )}

      {personalBookingNotice && (
        <p
          role="status"
          className="rounded-lg bg-emerald-50 px-3 py-2 text-[14px] text-emerald-800"
        >
          {personalBookingNotice}
        </p>
      )}

      {leads && totalLeads > leads.length ? (
        <p className="ui-muted-12">
          Показано {leads.length} из {totalLeads}
        </p>
      ) : null}

      {isLoading ? (
        <LeadsSkeleton />
      ) : isError ? (
        <ErrorState scope={activeScope} />
      ) : !leads?.length ? (
        <EmptyState scope={activeScope} />
      ) : activeScope === "pool" ? (
        <div className="ui-col-2">
          {leads.map((lead) => (
            <LeadCard
              key={lead.id}
              lead={lead}
              action={{
                label: "Забрать",
                pendingLabel: "Забираю...",
                disabled: claimMutation.isPending,
                isPending: claimingLeadId === lead.id,
                onClick: (poolLead) => {
                  setClaimError(null);
                  claimMutation.mutate(poolLead);
                },
              }}
            />
          ))}
          {loadMoreLeadsButton}
        </div>
      ) : activeScope === "archived" ? (
        <div className="ui-col-2">
          {leads.map((lead) => (
            <LeadCard key={lead.id} lead={lead} onSelect={setSelectedLead} />
          ))}
          {loadMoreLeadsButton}
        </div>
      ) : (
        <div className="flex flex-col gap-4">
          {STATUS_ORDER.map(({ key, label }) => {
            const sectionLeads = grouped?.get(key) ?? [];
            return (
              <StatusSection
                key={key}
                label={label}
                leads={sectionLeads}
                onSelectLead={setSelectedLead}
              />
            );
          })}
          {loadMoreLeadsButton}
        </div>
      )}

      {/* FAB */}
      <button
        type="button"
        onClick={() => setSheetOpen(true)}
        aria-label={unifiedIntakeEnabled ? "Добавить ученика" : "Новая заявка"}
        className="fixed right-4 z-30 flex h-14 w-14 items-center justify-center rounded-full bg-[var(--branding-accent)] text-white shadow-lg active:scale-95 transition-transform"
        style={{ bottom: 80 }}
      >
        <Plus size={24} />
      </button>

      <ClientIntakeSheet
        open={sheetOpen}
        onOpenChange={setSheetOpen}
        onNavigate={navigate}
        onOpenPoolLeads={() => setActiveScope("pool")}
        unifiedEnabled={unifiedIntakeEnabled}
      />
      <LeadDetailSheet
        open={selectedLead !== null || deepLinkedLead.data !== undefined}
        onOpenChange={(open) => {
          if (!open) {
            setSelectedLead(null);
            if (searchParams.has("lead")) {
              const nextParams = new URLSearchParams(searchParams);
              nextParams.delete("lead");
              setSearchParams(nextParams, { replace: true });
            }
          }
        }}
        lead={selectedLead ?? deepLinkedLead.data ?? null}
        trainerId={trainerId}
        onEnrollInGroup={(lead) => {
          setSelectedLead(null);
          setPaymentNotice(null);
          setGroupSalePendingNotice(null);
          setEnrollmentLead(lead);
        }}
        onBookPersonal={(lead) => {
          if (unifiedPersonalAvailabilityEnabled) {
            navigate(`/trainer/availability?student_id=${lead.id}`);
            return;
          }
          setSelectedLead(null);
          setPersonalBookingNotice(null);
          setPersonalBookingLead(lead);
        }}
        onOpenExistingContext={(lead) => {
          const nextParams = new URLSearchParams(searchParams);
          nextParams.set("lead", String(lead.id));
          setSelectedLead(null);
          setSearchParams(nextParams, { replace: true });
        }}
        unifiedEnabled={unifiedIntakeEnabled}
      />
      {enrollmentLead && contextualGroupSaleEnabled ? (
        <ContextualGroupSalePickerSheet
          open
          onOpenChange={(open) => {
            if (!open) setEnrollmentLead(null);
          }}
          studentId={enrollmentLead.id}
          studentName={leadDisplayName(enrollmentLead)}
          leadStatus={enrollmentLead.lead_status}
          trialDate={enrollmentLead.trial_date}
          onAuthoritativeResult={(result) => {
            const sameStudentId = enrollmentLead.id;
            setEnrollmentLead(null);
            // Manual admission is immediately usable; an SBP order remains a lead
            // until the provider confirms it, so it must not be sent to student UI.
            if (result.workspace_state === "student") {
              const manualAdmissionNotice =
                result.finance_state === "pending_manual" &&
                Number.isSafeInteger(result.payment_id)
                  ? {
                      groupSaleManualAdmission: {
                        studentId: sameStudentId,
                        paymentId: result.payment_id,
                        financeState: result.finance_state,
                      },
                    }
                  : undefined;
              navigate(`/trainer/students/${sameStudentId}`, { state: manualAdmissionNotice });
              return;
            }
            if (result.finance_state === "provider_pending") {
              const orderId = result.bank_payment_order_id;
              if (!orderId) return;
              setGroupSalePendingNotice({
                studentId: sameStudentId,
                orderId,
                providerPaymentUrl: result.provider_payment_url ?? "",
                status: result.bank_payment_order_status ?? null,
                fulfillmentState: result.fulfillment_state ?? null,
                allowedActions: result.allowed_actions ?? [],
              });
            }
          }}
          onRenewalConflict={() => {
            const sameStudentId = enrollmentLead.id;
            setEnrollmentLead(null);
            navigate(`/trainer/students/${sameStudentId}`);
          }}
        />
      ) : enrollmentLead && (unifiedJourneyMode === "legacy" || legacyGroupSaleEnabled) ? (
        <PaymentSheet
          open
          onOpenChange={(open) => {
            if (!open) setEnrollmentLead(null);
          }}
          studentId={enrollmentLead.id}
          studentName={leadDisplayName(enrollmentLead)}
          groupEnrollmentOnly
          onPaymentRecorded={setPaymentNotice}
        />
      ) : enrollmentLead ? (
        <ContextualGroupSaleUnavailableSheet
          open
          onOpenChange={(open) => {
            if (!open) setEnrollmentLead(null);
          }}
          isRetrying={unifiedJourneyCapability.isFetching || paymentCapabilitiesQuery.isFetching}
          onRetry={() => {
            void Promise.all([
              unifiedJourneyCapability.refetch(),
              paymentCapabilitiesQuery.refetch(),
            ]);
          }}
        />
      ) : null}
      {personalBookingLead ? (
        <PersonalBookingSheet
          open
          onOpenChange={(open) => {
            if (!open) setPersonalBookingLead(null);
          }}
          studentId={personalBookingLead.id}
          studentName={leadDisplayName(personalBookingLead)}
          onBooked={handlePersonalBookingSuccess}
        />
      ) : null}
    </div>
  );
}
