import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { formatDateRu } from "@/lib/locale";
import { CalendarClock, Flame } from "lucide-react";
import { PortalActionLink } from "@/components/portal/portal-action-link";
import { formatTrainingCount } from "../lib/student-home-presenters";

interface SubscriptionCardProps {
  tariffName: string;
  trainingsUsed: number;
  trainingsTotal: number | null; // null = unlimited
  trainingsLeft: number | null;
  expiresAt: string | null;
  status: string;
  freezeStatus?: string | null;
  actionLabel?: string;
  actionTo?: string;
}

function getStatusMeta(status: string, freezeStatus?: string | null) {
  if (freezeStatus === "pending") {
    return {
      badge: "Ожидает",
      badgeClass: "bg-amber-50 text-amber-800 ring-1 ring-amber-200 shadow-none",
      footer: "ожидает",
      notice: "Заморозка ожидает подтверждения",
      description: "Клуб проверяет заявку на заморозку абонемента.",
    };
  }

  if (status === "frozen") {
    return {
      badge: "Пауза",
      badgeClass: "bg-amber-50 text-amber-800 ring-1 ring-amber-200 shadow-none",
      footer: "на паузе",
      notice: "Абонемент на паузе",
      description: "Занятия по этому абонементу временно недоступны.",
    };
  }

  if (status === "pending") {
    return {
      badge: "Ожидает",
      badgeClass: "bg-sky-50 text-sky-800 ring-1 ring-sky-200 shadow-none",
      footer: "ожидает",
      notice: "Абонемент ожидает активации",
      description: "Клуб ещё подтверждает или активирует этот абонемент.",
    };
  }

  return {
    badge: null,
    badgeClass: "",
    footer: "в работе",
    notice: null,
    description: null,
  };
}

export function SubscriptionCard({
  tariffName,
  trainingsUsed,
  trainingsTotal,
  trainingsLeft,
  expiresAt,
  status,
  freezeStatus = null,
  actionLabel,
  actionTo,
}: SubscriptionCardProps) {
  const remaining =
    trainingsLeft ?? (trainingsTotal ? trainingsTotal - trainingsUsed : null);
  const percent = trainingsTotal ? (trainingsUsed / trainingsTotal) * 100 : 0;
  const isWarning = remaining !== null && remaining <= 3;
  const isExpired = status === "expired";
  const isCancelled = status === "cancelled";
  const formattedExpiry = expiresAt ? formatDateRu(expiresAt) : null;
  const statusMeta = getStatusMeta(status, freezeStatus);

  if (isCancelled) {
    return (
      <Card className="border-0 bg-white/90 shadow-sm ring-1 ring-red-500/15 backdrop-blur-sm">
        <CardHeader className="pb-1.5">
          <div className="ui-row-between">
            <CardTitle className="text-[16px] font-semibold">{tariffName}</CardTitle>
            <Badge variant="destructive">Возврат</Badge>
          </div>
        </CardHeader>
        <CardContent className="space-y-2.5 pt-0">
          <p className="ui-title-14">Абонемент отменён</p>
          <p className="ui-muted-13">
            Оплата возвращена, оставшиеся занятия недоступны.
          </p>
        </CardContent>
      </Card>
    );
  }

  if (isExpired) {
    return (
      <Card className="border-0 bg-white/90 shadow-sm ring-1 ring-black/6 backdrop-blur-sm">
        <CardHeader className="pb-1.5">
          <CardTitle className="text-[16px] font-semibold">{tariffName}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2.5 pt-0">
          <p className="ui-muted-13">Абонемент завершился и требует продления.</p>
          <Badge variant="destructive">Абонемент истек</Badge>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="border-0 bg-white/90 shadow-sm ring-1 ring-black/6 backdrop-blur-sm">
      <CardHeader className="pb-1">
        <div className="ui-row-between">
          <div className="space-y-1">
            <p className="ui-overline">
              Абонемент
            </p>
            <CardTitle className="text-[17px] font-semibold leading-tight">
              {tariffName}
            </CardTitle>
          </div>
          {statusMeta.badge ? (
            <Badge className={statusMeta.badgeClass}>
              {statusMeta.badge}
            </Badge>
          ) : isWarning ? (
            <Badge className="bg-[var(--branding-accent)] text-black shadow-none">
              Заканчивается
            </Badge>
          ) : null}
        </div>
      </CardHeader>
      <CardContent className="space-y-3.5 pt-1">
        {trainingsTotal ? (
          <>
            <div className="flex items-end justify-between gap-3">
              <div>
                <p className="text-[12px] uppercase tracking-[0.16em] text-muted-foreground">
                  Осталось
                </p>
                <p className="mt-1 text-[28px] font-semibold leading-none">
                  {remaining ?? 0}
                </p>
              </div>
              <div
                className="rounded-xl px-3 py-1.5 text-right"
                style={{
                  background:
                    "linear-gradient(0deg, rgba(255,255,255,0.88), rgba(255,255,255,0.88)), var(--branding-accent)",
                }}
              >
                <p className="ui-muted-12">Использовано</p>
                <p className="text-[14px] font-semibold">
                  {trainingsUsed} / {trainingsTotal}
                </p>
              </div>
            </div>
            <div
              className="h-1.5 overflow-hidden rounded-full bg-black/8"
              role="progressbar"
              aria-valuenow={trainingsUsed}
              aria-valuemin={0}
              aria-valuemax={trainingsTotal}
            >
              <div
                className="h-full rounded-full transition-all"
                style={{
                  width: `${Math.min(percent, 100)}%`,
                  backgroundColor: "var(--branding-accent)",
                }}
              />
            </div>
            <div className="flex items-start justify-between gap-2.5">
              <p
                className={`text-[14px] ${isWarning ? "font-semibold text-[var(--branding-accent)]" : "text-muted-foreground"}`}
              >
                {isWarning
                  ? `Осталось всего ${formatTrainingCount(remaining ?? 0)}`
                  : `${formatTrainingCount(remaining ?? 0)} из пакета ещё доступны`}
              </p>
              <div className="flex shrink-0 items-center gap-1 text-[11px] text-muted-foreground">
                <Flame size={13} />
                <span>{statusMeta.footer}</span>
              </div>
            </div>
          </>
        ) : (
          <div className="space-y-1">
            <p className="text-[28px] font-semibold leading-none">Безлимит</p>
            <p className="ui-muted-14">
              Тренируйся без ограничения по количеству занятий.
            </p>
          </div>
        )}
        {statusMeta.notice ? (
          <div className="rounded-2xl bg-black/[0.035] px-3 py-2">
            <p className="text-[13px] font-semibold text-foreground">
              {statusMeta.notice}
            </p>
            <p className="mt-0.5 text-[12px] leading-5 text-muted-foreground">
              {statusMeta.description}
            </p>
          </div>
        ) : null}
        {formattedExpiry ? (
          <div className="flex items-center gap-2 text-[13px] text-muted-foreground">
            <CalendarClock size={14} />
            <span>Действует до {formattedExpiry}</span>
          </div>
        ) : (
          <div className="flex items-center gap-2 text-[13px] text-muted-foreground">
            <CalendarClock size={14} />
            <span>Без срока окончания</span>
          </div>
        )}

        {actionLabel && actionTo ? (
          <PortalActionLink to={actionTo}>
            {actionLabel}
          </PortalActionLink>
        ) : null}
      </CardContent>
    </Card>
  );
}
