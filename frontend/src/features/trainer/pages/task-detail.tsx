import { useState } from "react";
import { useParams, useNavigate } from "react-router";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  Phone,
  Clock,
  PlayCircle,
  PauseCircle,
  XCircle,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { formatDaysMissed } from "@/lib/format";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import apiClient from "@/api/custom-fetch";
import { formatDateFull } from "@/lib/format";
import { toISODate } from "@/lib/utils";
import { TASK_STATUS_LABELS } from "../constants";
import { ResolutionSheet } from "../components/resolution-sheet";
import { CallResultSheet } from "../components/call-result-sheet";
import { TaskCommentForm } from "../components/task-comment-form";
import { useCallTracker } from "../hooks/use-call-tracker";
import { normalizeStudentSubscriptionsPayload } from "../lib/subscriptions";
import type {
  RetentionTask,
  TaskComment,
  StudentSubscription,
  GradeProgress,
  AttendanceItem,
} from "../types";

function formatDateTime(dateStr: string): string {
  const d = new Date(dateStr);
  return d.toLocaleDateString("ru-RU", {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function getDefaultSnoozeDate(dueDate: string): string {
  const d = new Date(dueDate);
  d.setDate(d.getDate() + 3);
  return toISODate(d);
}

const SUB_STATUS_LABEL: Record<string, string> = {
  active: "активен",
  frozen: "заморожен",
  pending: "ожидает",
  expired: "истёк",
  cancelled: "отменён после возврата",
};

function formatSubscriptionSummary(
  subscriptions: StudentSubscription[] | undefined,
): string {
  const subscription =
    subscriptions?.find((s) => s.status === "active") ??
    subscriptions?.find((s) =>
      ["frozen", "pending", "expired", "cancelled"].includes(s.status),
    );
  if (!subscription) return "Нет активного";
  if (subscription.status === "active") return subscription.tariff_name;
  return `${subscription.tariff_name} (${SUB_STATUS_LABEL[subscription.status] ?? subscription.status})`;
}

export default function TaskDetail() {
  const { taskId } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const numericTaskId = taskId ? Number(taskId) : null;

  const [resolutionOpen, setResolutionOpen] = useState(false);
  const [snoozeOpen, setSnoozeOpen] = useState(false);
  const [snoozeDate, setSnoozeDate] = useState("");

  const callTracker = useCallTracker();

  const { data: trainerInfo } = useQuery<{ id: number }>({
    queryKey: ["trainer", "me"],
    queryFn: () => apiClient.get("/trainers/me/").then((r) => r.data),
    staleTime: 5 * 60_000,
  });
  const trainerId = trainerInfo?.id ?? null;

  const {
    data: task,
    isLoading: taskLoading,
  } = useQuery<RetentionTask>({
    queryKey: ["retention-task", numericTaskId],
    queryFn: () =>
      apiClient
        .get(`/retention/tasks/${numericTaskId}/`)
        .then((r) => r.data),
    staleTime: 30_000,
    enabled: !!numericTaskId,
  });

  const { data: comments, isLoading: commentsLoading } = useQuery<
    TaskComment[]
  >({
    queryKey: ["retention-task", numericTaskId, "comments"],
    queryFn: () =>
      apiClient
        .get(`/retention/tasks/${numericTaskId}/comments/`)
        .then((r) => r.data),
    staleTime: 30_000,
    enabled: !!numericTaskId,
  });

  const { data: subscriptions } = useQuery<StudentSubscription[]>({
    queryKey: ["student", task?.student_id, "subscriptions"],
    queryFn: () =>
      apiClient
        .get("/billing/subscriptions/", { params: { student_id: task!.student_id } })
        .then((r) => normalizeStudentSubscriptionsPayload(r.data)),
    staleTime: 60_000,
    enabled: !!task?.student_id,
  });

  const { data: grades } = useQuery<GradeProgress[]>({
    queryKey: ["student", task?.student_id, "grades"],
    queryFn: () =>
      apiClient.get(`/grades/students/${task!.student_id}/progress/`).then((r) => r.data),
    staleTime: 2 * 60_000,
    enabled: !!task?.student_id,
  });

  const { data: attendance } = useQuery<AttendanceItem[]>({
    queryKey: ["student", task?.student_id, "checkins"],
    queryFn: () =>
      apiClient
        .get(`/students/${task!.student_id}/checkins/`, {
          params: { limit: 10 },
        })
        .then((r) => r.data),
    staleTime: 2 * 60_000,
    enabled: !!task?.student_id,
  });

  const statusMutation = useMutation({
    mutationFn: (status: string) =>
      apiClient.post(`/retention/tasks/${numericTaskId}/status/`, { status }),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["retention-task", numericTaskId],
      });
      queryClient.invalidateQueries({
        queryKey: ["retention-tasks"],
      });
      queryClient.invalidateQueries({
        queryKey: ["task-badge-count"],
      });
    },
  });

  const snoozeMutation = useMutation({
    mutationFn: (newDueDate: string) =>
      apiClient.post(`/retention/tasks/${numericTaskId}/snooze/`, {
        new_due_date: newDueDate,
      }),
    onSuccess: () => {
      setSnoozeOpen(false);
      queryClient.invalidateQueries({
        queryKey: ["retention-task", numericTaskId],
      });
      queryClient.invalidateQueries({
        queryKey: ["retention-tasks"],
      });
      queryClient.invalidateQueries({
        queryKey: ["task-badge-count"],
      });
    },
  });

  function handleSnooze() {
    if (!snoozeDate) return;
    snoozeMutation.mutate(snoozeDate);
  }

  function openSnoozeSheet() {
    setSnoozeDate(task ? getDefaultSnoozeDate(task.due_date) : "");
    setSnoozeOpen(true);
  }

  function handleCall() {
    if (task?.student_phone) {
      callTracker.startCall(task.id, task.student_phone);
    }
  }

  if (taskLoading) {
    return (
      <div className="flex flex-col gap-4 px-4 pt-6 pb-4">
        <Skeleton className="h-6 w-48" />
        <Skeleton className="h-24 rounded-xl" />
        <Skeleton className="h-12 rounded-xl" />
        <Skeleton className="h-32 rounded-xl" />
      </div>
    );
  }

  if (!task) {
    return (
      <div className="flex flex-col items-center justify-center py-16 gap-3 px-4">
        <p className="text-[16px] text-muted-foreground">Задача не найдена</p>
        <Button variant="outline" onClick={() => navigate("/trainer/tasks")}>
          Назад к задачам
        </Button>
      </div>
    );
  }

  const isOpen = !task.resolved_at;
  const canSetInProgress =
    isOpen && task.status !== "in_progress" && task.status !== "closed";
  const isPipelineTask = task.automation_source === "pipeline";
  const automationStepMessage = task.automation_step_message?.trim();

  return (
    <div className="flex flex-col gap-4 px-4 pt-6 pb-24">
      {/* Back button */}
      <button
        type="button"
        onClick={() => navigate("/trainer/tasks")}
        className="flex items-center gap-2 text-[14px] text-muted-foreground -ml-1"
      >
        <ArrowLeft size={18} />
        Назад к задачам
      </button>

      {/* Student info card */}
      <div className="ui-card">
        <p className="text-[18px] font-semibold text-foreground">
          {task.student_name}
        </p>
        <div className="flex flex-col gap-1 mt-1.5">
          {task.days_missed > 0 && (
            <span className="text-[14px] text-red-600 font-medium">
              {formatDaysMissed(task.days_missed)}
            </span>
          )}
          {task.last_visit_date && (
            <span className="ui-muted-13">
              Последний визит: {formatDateFull(task.last_visit_date)}
            </span>
          )}
        </div>
      </div>

      {/* Call button */}
      {isOpen && task.student_phone && (
        <button
          type="button"
          onClick={handleCall}
          className="flex items-center justify-center gap-2 w-full rounded-xl py-3.5 text-[16px] font-medium text-white"
          style={{ backgroundColor: "var(--branding-accent)" }}
        >
          <Phone size={20} />
          Позвонить
        </button>
      )}

      {/* Student context */}
      <div className="ui-card">
        <h2 className="text-[14px] font-semibold text-muted-foreground uppercase tracking-wider mb-3">
          Контекст
        </h2>
        <div className="flex flex-col gap-2 text-[14px]">
          <div className="flex justify-between">
            <span className="ui-muted">Абонемент</span>
            <span className="text-foreground font-medium">
              {formatSubscriptionSummary(subscriptions)}
            </span>
          </div>
          {grades?.[0]?.current_grade && (
            <div className="flex justify-between">
              <span className="ui-muted">Грейд</span>
              <span className="text-foreground font-medium">
                {grades[0].current_grade.name}
              </span>
            </div>
          )}
          <div className="flex justify-between">
            <span className="ui-muted">Последних посещений</span>
            <span className="text-foreground font-medium">
              {attendance?.length ?? 0}
            </span>
          </div>
        </div>
      </div>

      {/* Task info */}
      <div className="ui-card">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="outline" className="text-[12px]">
            {TASK_STATUS_LABELS[task.status] ?? task.status}
          </Badge>
          {task.attempt_count > 0 && (
            <Badge variant="outline" className="text-[12px]">
              {task.attempt_count} {task.attempt_count === 1 ? "звонок" : task.attempt_count < 5 ? "звонка" : "звонков"}
            </Badge>
          )}
          {isPipelineTask && (
            <Badge variant="secondary" className="text-[12px]">
              Автоворонка
            </Badge>
          )}
        </div>
        <div className="flex flex-wrap gap-4 mt-3 text-[14px] text-muted-foreground">
          <span>Срок: {formatDateFull(task.due_date)}</span>
          <span>Создана: {formatDateFull(task.created_at)}</span>
          {task.resolved_at && (
            <span>Закрыта: {formatDateTime(task.resolved_at)}</span>
          )}
        </div>
        {isPipelineTask && automationStepMessage && (
          <div className="mt-3 rounded-lg bg-muted/50 px-3 py-2 text-[13px]">
            <span className="ui-muted">Действие: </span>
            <span className="font-medium text-foreground">{automationStepMessage}</span>
          </div>
        )}
      </div>

      {/* Secondary action buttons */}
      {isOpen && (
        <div className="flex gap-2 overflow-x-auto">
          {canSetInProgress && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => statusMutation.mutate("in_progress")}
              disabled={statusMutation.isPending}
              className="shrink-0 gap-1.5"
            >
              <PlayCircle size={16} />
              В работе
            </Button>
          )}
          <Button
            variant="outline"
            size="sm"
            onClick={openSnoozeSheet}
            className="shrink-0 gap-1.5"
          >
            <PauseCircle size={16} />
            Отложить
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setResolutionOpen(true)}
            className="shrink-0 gap-1.5"
          >
            <XCircle size={16} />
            Закрыть
          </Button>
        </div>
      )}

      {/* Comments section */}
      <div className="ui-card">
        <h2 className="text-[16px] font-semibold text-foreground mb-3">
          Комментарии
        </h2>

        {commentsLoading ? (
          <div className="ui-col-2">
            <Skeleton className="h-12" />
            <Skeleton className="h-12" />
          </div>
        ) : !comments?.length ? (
          <p className="text-[14px] text-muted-foreground py-4 text-center">
            Пока нет комментариев
          </p>
        ) : (
          <div className="ui-col-3">
            {comments.map((comment) => (
              <div
                key={comment.id}
                className="flex flex-col gap-1 border-b border-foreground/5 pb-3 last:border-0"
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[13px] font-medium text-foreground truncate">
                    {comment.author_email}
                  </span>
                  <span className="text-[12px] text-muted-foreground shrink-0">
                    {formatDateTime(comment.created_at)}
                  </span>
                </div>
                <p className="text-[14px] text-foreground whitespace-pre-wrap">
                  {comment.text}
                </p>
              </div>
            ))}
          </div>
        )}

        {/* Comment form */}
        {isOpen && numericTaskId && (
          <div className="mt-4 pt-3 border-t border-foreground/5">
            <TaskCommentForm taskId={numericTaskId} />
          </div>
        )}
      </div>

      {/* Snooze sheet */}
      <Sheet open={snoozeOpen} onOpenChange={setSnoozeOpen}>
        <SheetContent side="bottom" showCloseButton={false} className="rounded-t-2xl">
          <SheetHeader>
            <SheetTitle>Отложить задачу</SheetTitle>
          </SheetHeader>
          <div className="flex flex-col gap-4 px-4 pb-4">
            <label className="flex flex-col gap-1.5">
              <span className="ui-muted-14">
                Новый срок
              </span>
              <div className="ui-row-2">
                <Clock size={16} className="ui-muted" />
                <input
                  type="date"
                  value={snoozeDate}
                  onChange={(e) => setSnoozeDate(e.target.value)}
                  className="flex-1 rounded-lg border border-input bg-background px-3 py-2 text-[14px] text-foreground focus:outline-none focus:ring-2 focus:ring-ring"
                />
              </div>
            </label>
            <Button
              onClick={handleSnooze}
              disabled={!snoozeDate || snoozeMutation.isPending}
              className="ui-brand-button"
            >
              {snoozeMutation.isPending ? "Сохранение..." : "Отложить"}
            </Button>
          </div>
        </SheetContent>
      </Sheet>

      {/* Resolution sheet */}
      <ResolutionSheet
        open={resolutionOpen}
        onOpenChange={setResolutionOpen}
        task={task}
        trainerId={trainerId}
      />

      {/* Call result sheet */}
      <CallResultSheet
        open={callTracker.showResult}
        onOpenChange={() => callTracker.dismissResult()}
        task={task}
      />
    </div>
  );
}
