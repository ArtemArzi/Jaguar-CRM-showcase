import { useNavigate } from "react-router";
import { Badge } from "@/components/ui/badge";
import { getInitials } from "@/lib/utils";
import { STATUS_CONFIG } from "../constants";
import type { StudentListItem } from "../types";

interface StudentCardProps {
  readonly student: StudentListItem;
}

export function StudentCard({ student }: StudentCardProps) {
  const navigate = useNavigate();
  const fullName = `${student.first_name} ${student.last_name}`.trim();
  const statusCfg = STATUS_CONFIG[student.status] ?? {
    label: student.status,
    variant: "outline" as const,
  };
  const badge = student.commercial_segment === "no_crm_entitlement"
    ? { label: "Без абонемента в CRM", variant: "outline" as const }
    : statusCfg;

  return (
    <div
      className="flex items-center gap-3 rounded-xl bg-white p-3 shadow-sm hover:shadow-md cursor-pointer active:scale-[0.98] transition-all min-h-[48px]"
      onClick={() => navigate(`/trainer/students/${student.id}`)}
    >
      {/* Avatar initials */}
      <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-[var(--branding-accent)] text-white text-[14px] font-semibold">
        {getInitials(fullName)}
      </div>

      {/* Name + phone */}
      <div className="flex flex-1 flex-col justify-center min-w-0">
        <span className="text-[14px] font-medium text-foreground truncate">
          {fullName}
        </span>
        <span className="ui-muted-12">{student.phone}</span>
      </div>

      {/* Status badge */}
      <Badge variant={badge.variant} className="shrink-0">
        {badge.label}
      </Badge>
    </div>
  );
}
