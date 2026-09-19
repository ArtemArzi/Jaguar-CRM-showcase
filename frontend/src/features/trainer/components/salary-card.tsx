import { Badge } from "@/components/ui/badge";
import { formatRub } from "@/lib/utils";
import { formatDateShort } from "@/lib/format";

export interface EarningData {
  id: number;
  row_type?: "earning" | "adjustment";
  earning_type: string;
  amount: string;
  rate_percent: string;
  subscription_price: string | null;
  checkin_date: string;
  schedule_name: string;
  package_owner_trainer_id?: number | null;
  package_owner_trainer_name?: string;
  package_transfer_amount_basis?: string | null;
  package_transfer_payable_delta?: string | null;
  package_transfer_affects_payroll?: boolean | null;
  package_transfer_reason?: string;
  adjustment_direction?: string;
  adjustment_reason?: string;
  adjustment_effective_date?: string;
  adjustment_affects_payroll?: boolean | null;
  source_checkin_id?: number | null;
  source_checkin_date?: string;
  source_schedule_name?: string;
  source_payment_id?: number | null;
}

const TYPE_LABELS: Record<string, string> = {
  group: "Групповая",
  personal: "Персональная",
  mini_group: "Мини-группа",
  manual_adjustment: "Корректировка",
  refund: "Возврат оплаты",
};

interface SalaryCardProps {
  earning: EarningData;
}

export function SalaryCard({ earning }: SalaryCardProps) {
  const isAdjustment = earning.row_type === "adjustment";
  const typeLabel = TYPE_LABELS[earning.earning_type] ??
    (isAdjustment ? "Корректировка" : earning.earning_type);
  const title = isAdjustment ? typeLabel : earning.schedule_name || typeLabel;
  const displayDate = earning.adjustment_effective_date || earning.checkin_date;
  const adjustmentReason = earning.adjustment_reason?.trim() ?? "";
  const packageOwnerName = earning.package_owner_trainer_name?.trim() ?? "";
  const hasPackageTransfer = !isAdjustment && (
    Boolean(packageOwnerName) ||
    (earning.package_owner_trainer_id !== null &&
      earning.package_owner_trainer_id !== undefined) ||
    Boolean(earning.package_transfer_reason)
  );

  return (
    <div className="flex items-start gap-3 rounded-xl bg-white p-4 ring-1 ring-foreground/5">
      <div className="flex-1 min-w-0">
        <p className="text-[16px] text-foreground truncate">
          {title}
        </p>
        <div className="flex items-center gap-2 mt-0.5">
          <span className="ui-muted-14">
            {displayDate ? formatDateShort(displayDate) : "Дата не указана"}
          </span>
          <Badge variant="secondary" className="text-[12px]">
            {typeLabel}
          </Badge>
        </div>
        {isAdjustment && adjustmentReason && (
          <p className="mt-2 text-[13px] text-muted-foreground break-words">
            {adjustmentReason}
          </p>
        )}
        {isAdjustment && earning.source_schedule_name && (
          <p className="mt-1 text-[12px] text-muted-foreground break-words">
            Источник: {earning.source_schedule_name}
          </p>
        )}
        {hasPackageTransfer && (
          <div className="mt-2 rounded-lg bg-muted/50 p-2">
            <p className="text-[12px] font-medium text-foreground break-words">
              {packageOwnerName
                ? `Пакет куплен у ${packageOwnerName}`
                : "Пакет привязан к другому тренеру, но имя не загрузилось"}
            </p>
            {!packageOwnerName && (
              <p className="mt-0.5 text-[12px] text-destructive">
                Проверьте владельца пакета в карточке начисления.
              </p>
            )}
            {earning.package_transfer_affects_payroll === false && (
              <p className="mt-0.5 text-[12px] text-muted-foreground">
                Не влияет на сумму зарплаты
              </p>
            )}
          </div>
        )}
      </div>
      <p className="text-[16px] font-semibold text-foreground shrink-0">
        {formatRub(earning.amount)}
      </p>
    </div>
  );
}
