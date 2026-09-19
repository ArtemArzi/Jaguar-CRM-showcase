import { cn } from "@/lib/utils";
import type { AlertType } from "../types";

const ALERT_CONFIG: Record<AlertType, { label: string; className: string }> = {
  newcomer: {
    label: "Новичок",
    className: "bg-blue-500 text-white",
  },
  debtor: {
    label: "Долг",
    className: "bg-red-500 text-white",
  },
  contraindications: {
    label: "Противопоказания",
    className: "bg-orange-500 text-white",
  },
  returned: {
    label: "Вернулся",
    className: "bg-green-500 text-white",
  },
  expiring: {
    label: "Истекает",
    className: "bg-amber-400 text-neutral-900",
  },
  last_training: {
    label: "Последнее",
    className: "bg-yellow-400 text-neutral-900",
  },
  birthday: {
    label: "День рождения",
    className: "bg-pink-500 text-white",
  },
  first_after_grade: {
    label: "После грейда",
    className: "bg-violet-500 text-white",
  },
  child: {
    label: "Ребёнок",
    className: "bg-blue-400 text-white",
  },
};

interface AlertBadgeProps {
  readonly type: AlertType;
  readonly className?: string;
}

export function AlertBadge({ type, className }: AlertBadgeProps) {
  const config = ALERT_CONFIG[type];
  if (!config) return null;

  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium whitespace-nowrap",
        config.className,
        className,
      )}
    >
      {config.label}
    </span>
  );
}
