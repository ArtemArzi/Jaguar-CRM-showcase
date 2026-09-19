import { ChevronRight } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

export interface LeadData {
  id: number;
  first_name: string;
  last_name: string;
  phone?: string;
  masked_phone?: string | null;
  lead_status: string | null;
  status?: string;
  loss_reason?: string | null;
  is_child: boolean;
  source: string;
  assigned_trainer_id: number | null;
  trial_date?: string | null;
  created_at: string;
  workspace?: "leads_active" | "leads_archived";
  primary_action?: LeadAction | null;
}

export interface LeadAction {
  kind: string;
  label: string;
  supporting_text: string;
  target_resource_type?: string | null;
  target_resource_id?: number | null;
  context: string;
}

export interface LeadActionContext {
  primary_action: LeadAction | null;
  active_context: "trial" | "personal_booking" | "payment" | null;
  secondary_capabilities: LeadAction[];
}

const LEAD_STATUS_LABELS: Record<string, string> = {
  new: "Новый",
  contacted: "Связались",
  trial_booked: "Пробное назначено",
  trial_done: "Пробное проведено",
  thinking: "Думает",
};

function daysSinceCreated(createdAt: string): number {
  const created = new Date(createdAt);
  const now = new Date();
  return Math.floor(
    (now.getTime() - created.getTime()) / (1000 * 60 * 60 * 24),
  );
}

interface LeadCardProps {
  lead: LeadData;
  onSelect?: (lead: LeadData) => void;
  action?: {
    label: string;
    pendingLabel?: string;
    disabled?: boolean;
    isPending?: boolean;
    onClick: (lead: LeadData) => void;
  };
}

function LeadSummary({ lead }: { lead: LeadData }) {
  const days = daysSinceCreated(lead.created_at);
  const name = [lead.first_name, lead.last_name].filter(Boolean).join(" ");
  const phoneText = lead.phone ?? lead.masked_phone ?? "Телефон скрыт";
  const statusLabel = lead.workspace === "leads_archived"
    ? "Завершена"
    : LEAD_STATUS_LABELS[lead.lead_status ?? ""] ?? lead.lead_status ?? "Заявка";

  return (
    <div className="min-w-0 flex-1">
      <div className="flex flex-wrap items-center gap-2">
        <p className="truncate text-[16px] text-foreground">{name}</p>
        <Badge variant="secondary" className="text-[12px]">
          {statusLabel}
        </Badge>
        {lead.is_child && (
          <Badge className="bg-[var(--branding-accent)] text-[12px] text-white">
            Ребёнок
          </Badge>
        )}
      </div>
      <p className="ui-muted-14">{phoneText}</p>
      <p className="ui-muted-14">
        {days} {days === 1 ? "день" : days < 5 ? "дня" : "дней"} в CRM
      </p>
      {lead.primary_action ? (
        <p className="mt-1 text-[13px] font-medium text-foreground">
          Следующее: {lead.primary_action.label}
        </p>
      ) : null}
    </div>
  );
}

export function LeadCard({ lead, onSelect, action }: LeadCardProps) {
  if (action) {
    return (
      <article className="flex min-h-[88px] w-full flex-col gap-3 rounded-xl border-l-4 border-[var(--branding-accent)] bg-white p-4 text-left shadow-sm sm:flex-row sm:items-center">
        <LeadSummary lead={lead} />
        <Button
          type="button"
          onClick={() => action.onClick(lead)}
          disabled={action.disabled}
          className="min-h-11 w-full bg-[var(--branding-accent)] text-white hover:opacity-90 sm:w-auto"
        >
          {action.isPending ? (action.pendingLabel ?? action.label) : action.label}
        </Button>
      </article>
    );
  }

  return (
    <button
      type="button"
      onClick={() => onSelect?.(lead)}
      className="flex min-h-[72px] w-full items-center gap-3 rounded-xl border-l-4 border-[var(--branding-accent)] bg-white p-4 text-left shadow-sm transition-all hover:shadow-md active:scale-[0.98]"
    >
      <LeadSummary lead={lead} />
      <ChevronRight size={20} className="text-muted-foreground shrink-0" />
    </button>
  );
}
