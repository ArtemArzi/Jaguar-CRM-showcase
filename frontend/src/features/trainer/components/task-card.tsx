import { Phone, MoreHorizontal } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { formatDaysMissed, formatDateShort } from "@/lib/format";
import { Badge } from "@/components/ui/badge";
import type { RetentionTask } from "../types";

function getTaskContext(task: RetentionTask): string {
  switch (task.task_type) {
    case "new_lead":
      return "Новая заявка · записать на пробную";
    case "post_trial":
      return "Был на пробной · предложить абонемент";
    case "renewal":
      return "Абонемент · продлить";
    default:
      return formatDaysMissed(task.days_missed);
  }
}

function formatAttemptCount(count: number): string {
  if (count < 2) return "";
  return `${count}-й звонок`;
}

interface TaskCardProps {
  readonly task: RetentionTask;
  readonly onCall: (task: RetentionTask) => void;
  readonly onSnooze: (task: RetentionTask) => void;
  readonly onClose: (task: RetentionTask) => void;
  readonly onDetail: (task: RetentionTask) => void;
  readonly actionsDisabled?: boolean;
}

export function TaskCard({
  task,
  onCall,
  onSnooze,
  onClose,
  onDetail,
  actionsDisabled = false,
}: TaskCardProps) {
  const isPipelineTask = task.automation_source === "pipeline";
  const automationStepMessage = task.automation_step_message?.trim();

  return (
    <div
      role="button"
      aria-disabled={actionsDisabled}
      tabIndex={actionsDisabled ? -1 : 0}
      onClick={() => {
        if (!actionsDisabled) onDetail(task);
      }}
      onKeyDown={(e) => {
        if (!actionsDisabled && (e.key === "Enter" || e.key === " ")) {
          e.preventDefault();
          onDetail(task);
        }
      }}
      className="flex items-center gap-3 rounded-xl bg-white p-4 ring-1 ring-foreground/5 cursor-pointer active:bg-muted/50 transition-colors"
    >
      <div className="flex-1 min-w-0">
        <p className="text-[16px] font-medium text-foreground truncate">
          {task.student_name}
        </p>
        <p className="text-[14px] text-muted-foreground mt-0.5">
          {isPipelineTask ? (
            <>
              <Badge variant="secondary" className="mr-1 align-middle text-[11px]">
                Автоворонка
              </Badge>
              {automationStepMessage ? (
                <span>{automationStepMessage}</span>
              ) : (
                <span>{getTaskContext(task)}</span>
              )}
            </>
          ) : task.attempt_count >= 3 ? (
            <span className="text-orange-600 font-medium">Не отвечает</span>
          ) : (
            <>
              <span>{getTaskContext(task)}</span>
              {task.attempt_count >= 2 && (
                <>
                  <span className="mx-1">·</span>
                  <span>{formatAttemptCount(task.attempt_count)}</span>
                </>
              )}
            </>
          )}
          <span className="mx-1">·</span>
          <span>до {formatDateShort(task.due_date)}</span>
        </p>
      </div>

      <button
        type="button"
        disabled={actionsDisabled}
        onClick={(e) => {
          e.stopPropagation();
          onCall(task);
        }}
        className="flex items-center gap-1.5 rounded-lg px-4 py-2.5 text-[14px] font-medium text-white shrink-0"
        style={{ backgroundColor: "var(--branding-accent)", minHeight: 44 }}
      >
        <Phone size={16} />
        Позвонить
      </button>

      <DropdownMenu>
        <DropdownMenuTrigger
          disabled={actionsDisabled}
          className="flex h-10 w-10 items-center justify-center rounded-lg text-muted-foreground hover:bg-muted/50 shrink-0"
          onClick={(e) => e.stopPropagation()}
        >
          <MoreHorizontal size={18} />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem onClick={() => onDetail(task)}>Подробнее</DropdownMenuItem>
          <DropdownMenuItem onClick={() => onSnooze(task)}>Отложить</DropdownMenuItem>
          <DropdownMenuItem onClick={() => onClose(task)}>Закрыть без звонка</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}
