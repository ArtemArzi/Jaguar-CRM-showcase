import { lazy, Suspense } from "react";
import { Link } from "react-router";
import { useQuery } from "@tanstack/react-query";
import {
  CalendarDays,
  CheckCircle2,
  CreditCard,
  MessageSquareText,
  RotateCcw,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { PortalSectionTitle } from "@/components/portal/portal-section-title";
import { usePrivateQueryScope } from "@/features/auth/private-query-scope";
import { ChildCard } from "../components/child-card";
import apiClient from "@/api/custom-fetch";
import { ParentPageIntro } from "../components/parent-page-intro";
import { ParentSurfaceCard } from "../components/parent-surface-card";
import {
  getPersonalAvailabilityCapabilityMode,
  usePersonalAvailabilityCapabilityQuery,
} from "@/api/unified-client-journey";
import {
  hasOnlinePaymentsCapability,
  usePaymentCapabilities,
} from "@/api/payment-capabilities";
import {
  SelfServicePersonalCommandCards,
  SelfServicePersonalUnavailableNotice,
} from "@/components/portal/self-service-personal";
import {
  getParentSubscriptionAlert,
  rankSubscriptionAlert,
} from "../lib/subscription-alerts";

const PushPermissionBanner = lazy(
  () => import("@/features/notifications/components/push-permission-banner"),
);
const NotificationPreferences = lazy(
  () => import("@/features/notifications/components/notification-preferences"),
);

interface ChildSummary {
  id: number;
  first_name: string;
  last_name: string;
  status: string;
  is_child: boolean;
  grade_name: string | null;
  subscription_remaining: number | null;
  subscription_total: number | null;
  subscription_status: string | null;
  subscription_freeze_status?: string | null;
  last_visit_date: string | null;
  next_training_day_of_week: number | null;
  next_training_start_time: string | null;
  next_training_group_name: string | null;
  next_training_trainer_name: string | null;
  next_training_is_rescheduled?: boolean | null;
  next_training_is_substitute?: boolean | null;
}

function ParentQuickActions({ childId }: { childId: number }) {
  const actions = [
    {
      to: `/parent/child/${childId}#subscription`,
      label: "Абонемент",
      description: "Остаток занятий и срок действия",
      icon: CreditCard,
    },
    {
      to: `/parent/child/${childId}#groups`,
      label: "Группы",
      description: "Направления, тренеры и зал",
      icon: CalendarDays,
    },
    {
      to: `/parent/child/${childId}#activity`,
      label: "Активность",
      description: "Последние посещения ребёнка",
      icon: CheckCircle2,
    },
    {
      to: `/parent/child/${childId}/feedback`,
      label: "Опрос",
      description: "Обратная связь по тренировке",
      icon: MessageSquareText,
    },
  ] as const;

  return (
    <section
      aria-label="Быстрые действия"
      className="parent-section-enter parent-delay-2 space-y-2.5"
    >
      <PortalSectionTitle
        eyebrow="Быстрые действия"
        title="Главные разделы под рукой"
      />
      <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
        {actions.map(({ to, label, description, icon: Icon }) => (
          <Link
            key={to}
            to={to}
            className="group rounded-[22px] border border-black/6 bg-white/92 px-3.5 py-3.5 shadow-sm ring-1 ring-white/55 transition-[transform,background-color,box-shadow] duration-150 ease-out active:scale-[0.99]"
          >
            <div className="flex items-start gap-3">
              <div
                className="flex h-11 w-11 shrink-0 items-center justify-center rounded-2xl"
                style={{
                  background:
                    "linear-gradient(0deg, rgba(255,255,255,0.9), rgba(255,255,255,0.9)), var(--branding-accent)",
                }}
              >
                <Icon size={18} style={{ color: "var(--branding-accent)" }} />
              </div>
              <div className="min-w-0 flex-1">
                <p className="text-[15px] font-semibold leading-tight text-foreground">
                  {label}
                </p>
                <p className="portal-pretty-text mt-1 text-[12px] leading-5 text-muted-foreground">
                  {description}
                </p>
              </div>
            </div>
          </Link>
        ))}
      </div>
    </section>
  );
}

export default function ParentHome() {
  const { scope: parentPrivateScope, isReady: parentPrivateReady } = usePrivateQueryScope("parent");
  const paymentCapabilitiesQuery = usePaymentCapabilities();
  const personalAvailabilityCapability = usePersonalAvailabilityCapabilityQuery();
  const personalAvailabilityCapabilityMode = getPersonalAvailabilityCapabilityMode(
    personalAvailabilityCapability,
  );
  const unifiedClientJourneyEnabled = personalAvailabilityCapabilityMode === "unified";
  const {
    data: children = [],
    isLoading: loading,
    error,
    refetch,
    isFetching,
  } = useQuery<ChildSummary[]>({
    queryKey: ["parent", "children", ...parentPrivateScope],
    queryFn: () =>
      apiClient.get<ChildSummary[]>("/parents/children/").then((r) => r.data),
    staleTime: 5 * 60_000,
    enabled: parentPrivateReady,
  });
  const [mainChild, ...secondaryChildren] = children;
  const hasMultipleChildren = children.length > 1;
  const attentionChildren = children
    .map((child) => ({
      child,
      alert: getParentSubscriptionAlert({
        hasSubscription:
          child.subscription_remaining !== null ||
          child.subscription_total !== null ||
          (child.subscription_status ?? null) !== null ||
          (child.subscription_freeze_status ?? null) !== null,
        remaining: child.subscription_remaining,
        total: child.subscription_total,
        status: child.subscription_status,
        freezeStatus: child.subscription_freeze_status ?? null,
      }),
    }))
    .filter(({ alert }) => alert.requiresAttention)
    .sort(
      (a, b) => rankSubscriptionAlert(b.alert) - rankSubscriptionAlert(a.alert),
    );
  const primaryAttention = attentionChildren[0] ?? null;
  const primaryAttentionTone = primaryAttention?.alert.tone ?? "danger";
  const attentionPanelClass =
    primaryAttentionTone === "danger"
      ? "border-red-200 bg-red-50/85 ring-red-500/10"
      : "border-amber-200 bg-amber-50/85 ring-amber-500/10";
  const attentionTitleClass =
    primaryAttentionTone === "danger" ? "text-red-900" : "text-amber-950";
  const attentionDescriptionClass =
    primaryAttentionTone === "danger" ? "text-red-900/75" : "text-amber-900/75";
  const renderChildCard = (child: ChildSummary) => (
    <ChildCard
      key={child.id}
      id={child.id}
      firstName={child.first_name}
      lastName={child.last_name}
      gradeName={child.grade_name}
      subscriptionRemaining={child.subscription_remaining}
      subscriptionTotal={child.subscription_total}
      subscriptionStatus={child.subscription_status}
      subscriptionFreezeStatus={child.subscription_freeze_status ?? null}
      lastVisitDate={child.last_visit_date}
      nextTrainingDayOfWeek={child.next_training_day_of_week}
      nextTrainingStartTime={child.next_training_start_time}
      nextTrainingGroupName={child.next_training_group_name}
      nextTrainingTrainerName={child.next_training_trainer_name}
      nextTrainingIsRescheduled={child.next_training_is_rescheduled}
      nextTrainingIsSubstitute={child.next_training_is_substitute}
    />
  );

  return (
    <div className="space-y-4 px-5 pb-24 pt-4">
      <div className="parent-section-enter">
        <Suspense fallback={null}>
          <PushPermissionBanner role="parent" />
        </Suspense>
      </div>

      <ParentPageIntro
        className="parent-section-enter parent-delay-1"
        eyebrow="Кабинет родителя"
        title={children.length === 1 ? "Мой ребёнок" : "Мои дети"}
        description="Следите за абонементом, прогрессом и активностью ребёнка в одном месте."
      />

      {loading ? (
        <>
          <Skeleton className="h-[184px] rounded-2xl" />
          <Skeleton className="h-[104px] rounded-2xl" />
        </>
      ) : error ? (
        <ParentSurfaceCard className="parent-section-enter parent-delay-2">
          <div className="space-y-3 p-5">
            <p className="ui-overline">
              Кабинет родителя
            </p>
            <p className="text-[18px] font-semibold">
              Не удалось загрузить кабинет родителя
            </p>
            <p className="ui-body-muted">
              Проверьте подключение и попробуйте ещё раз.
            </p>
            <Button
              type="button"
              className="min-h-[44px] w-full"
              onClick={() => void refetch()}
              disabled={isFetching}
            >
              <RotateCcw />
              Повторить
            </Button>
          </div>
        </ParentSurfaceCard>
      ) : children.length === 0 ? (
        <ParentSurfaceCard className="parent-section-enter parent-delay-2 p-6 text-center">
          <p className="text-[16px] font-semibold text-neutral-700">
            Нет привязанных детей
          </p>
          <p className="mt-2 text-[14px] text-neutral-500">
            Попросите тренера отправить вам приглашение
          </p>
        </ParentSurfaceCard>
      ) : (
        <>
          {mainChild ? <ParentQuickActions childId={mainChild.id} /> : null}

          <section
            aria-label={hasMultipleChildren ? "Главный ребёнок" : "Ребёнок"}
            className="parent-section-enter parent-delay-2 space-y-2.5"
          >
            <PortalSectionTitle
              eyebrow={hasMultipleChildren ? "Главный ребёнок" : "Ребёнок"}
              title={hasMultipleChildren ? "Основной профиль" : "Профиль ребёнка"}
            />
            {mainChild ? renderChildCard(mainChild) : null}
          </section>

          {secondaryChildren.length > 0 ? (
            <section
              aria-label="Остальные дети"
              className="parent-section-enter parent-delay-3 space-y-2.5"
            >
              <PortalSectionTitle
                eyebrow="Остальные дети"
                title="Другие профили"
              />
              <div className="space-y-3">
                {secondaryChildren.map(renderChildCard)}
              </div>
            </section>
          ) : null}

          {unifiedClientJourneyEnabled || personalAvailabilityCapabilityMode === "legacy" ? (
            <section className="parent-section-enter parent-delay-3 space-y-3" aria-label="Персональные тренировки детей">
              {children.map((child) => (
                <SelfServicePersonalCommandCards
                  key={child.id}
                  scope={{ audience: "parent", childStudentId: child.id }}
                  enabled
                  onlinePaymentsEnabled={hasOnlinePaymentsCapability(paymentCapabilitiesQuery)}
                  childName={`${child.first_name} ${child.last_name}`.trim() || "Ребёнок"}
                  hideWhenEmpty={!unifiedClientJourneyEnabled}
                />
              ))}
            </section>
          ) : personalAvailabilityCapabilityMode === "unavailable" ? (
            <SelfServicePersonalUnavailableNotice
              isError={
                personalAvailabilityCapability.isError || personalAvailabilityCapability.isRefetchError
              }
              className="parent-section-enter parent-delay-3"
            />
          ) : null}

          {attentionChildren.length > 0 ? (
            <section
              aria-label="Требует внимания"
              className="parent-section-enter parent-delay-3 space-y-2.5"
            >
              <PortalSectionTitle
                eyebrow="Требует внимания"
                title="Важный сигнал"
              />
              <div className={`rounded-2xl border px-4 py-3 shadow-sm ring-1 ${attentionPanelClass}`}>
                <p className={`text-[14px] font-semibold ${attentionTitleClass}`}>
                  {primaryAttention?.alert.title ?? "Проверьте абонемент"}
                </p>
                <p className={`text-[13px] leading-5 ${attentionDescriptionClass}`}>
                  {primaryAttention
                    ? `${primaryAttention.child.first_name}: ${primaryAttention.alert.description}`
                    : "Проверьте активность абонемента."}
                </p>
                <p className="mt-1 text-[12px] text-neutral-500">
                  {attentionChildren.length === 1
                    ? "Один ребёнок требует внимания."
                    : `${attentionChildren.length} ребёнка требуют внимания.`}
                </p>
              </div>
            </section>
          ) : null}
        </>
      )}

      {/* Notification preferences (parent has no dedicated profile page) */}
      {!loading && !error && (
        <ParentSurfaceCard
          id="settings"
          className="parent-section-enter parent-delay-3 scroll-mt-24 p-4"
        >
          <Suspense fallback={null}>
            <NotificationPreferences role="parent" />
          </Suspense>
        </ParentSurfaceCard>
      )}
    </div>
  );
}
