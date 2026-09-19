import { useState, useMemo } from "react";
import { useNavigate } from "react-router";
import { useInfiniteQuery, useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, ChevronDown, ChevronRight, HelpCircle } from "lucide-react";
import { Skeleton } from "@/components/ui/skeleton";
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import apiClient from "@/api/custom-fetch";
import { toISODate } from "@/lib/utils";
import { TaskCard } from "../components/task-card";
import { CallResultSheet } from "../components/call-result-sheet";
import { ResolutionSheet } from "../components/resolution-sheet";
import { useCallTracker } from "../hooks/use-call-tracker";
import { SECTION_LABELS } from "../constants";
import type { RetentionTask } from "../types";
import { QueryStateNotice } from "../components/query-state-notice";
import { useTrainerIdentity } from "../hooks/use-trainer-identity";

interface GroupedTasks {
  readonly overdue: RetentionTask[];
  readonly today: RetentionTask[];
  readonly snoozed: RetentionTask[];
}
const TASKS_PAGE_LIMIT = 50;
const EMPTY_TASKS: RetentionTask[] = [];

type RetentionTaskListResponse = {
  readonly items: RetentionTask[];
  readonly count?: number;
};

function normalizeTaskResponse(data: RetentionTask[] | RetentionTaskListResponse): RetentionTaskListResponse {
  return Array.isArray(data)
    ? { items: data }
    : { items: data.items ?? [], count: data.count };
}

function nextTaskPageParam(
  lastPage: RetentionTaskListResponse,
  allPages: RetentionTaskListResponse[],
): number | undefined {
  const loaded = allPages.reduce((total, page) => total + page.items.length, 0);
  if (lastPage.count === undefined) return undefined;
  return loaded < lastPage.count ? loaded : undefined;
}

function groupTasks(tasks: RetentionTask[]): GroupedTasks {
  const todayDate = new Date(new Date().toDateString());
  const overdue: RetentionTask[] = [];
  const today: RetentionTask[] = [];
  const snoozed: RetentionTask[] = [];

  for (const t of tasks) {
    const dueDate = new Date(t.due_date);
    if (t.status === "snoozed" && dueDate > todayDate) {
      snoozed.push(t);
    } else if (dueDate < todayDate) {
      overdue.push(t);
    } else {
      today.push(t);
    }
  }

  const byMissed = (a: RetentionTask, b: RetentionTask) => b.days_missed - a.days_missed;
  overdue.sort(byMissed);
  today.sort(byMissed);

  return { overdue, today, snoozed };
}

function TasksSkeleton() {
  return (
    <div className="ui-col-3">
      {[1, 2, 3].map((i) => (
        <Skeleton key={i} className="h-[72px] rounded-xl" />
      ))}
    </div>
  );
}

function EmptyState() {
  return (
    <div className="ui-empty-state">
      <CheckCircle2 size={48} className="text-green-500" />
      <p className="text-lg font-semibold text-foreground">Задач сейчас нет</p>
      <p className="text-center text-sm leading-5 text-muted-foreground">
        Новых звонков и отложенных задач на сегодня нет.
      </p>
    </div>
  );
}

interface SectionProps {
  readonly label: string;
  readonly tasks: RetentionTask[];
  readonly variant: "overdue" | "today" | "snoozed";
  readonly defaultExpanded?: boolean;
  readonly onCall: (task: RetentionTask) => void;
  readonly onSnooze: (task: RetentionTask) => void;
  readonly onClose: (task: RetentionTask) => void;
  readonly onDetail: (task: RetentionTask) => void;
  readonly actionsDisabled?: boolean;
}

function Section({
  label, tasks, variant, defaultExpanded = true,
  onCall, onSnooze, onClose, onDetail, actionsDisabled = false,
}: SectionProps) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  if (tasks.length === 0) return null;

  const dotColor =
    variant === "overdue" ? "text-red-500"
    : variant === "today" ? "text-amber-500"
    : "text-muted-foreground";

  return (
    <section>
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="flex items-center gap-2 w-full mb-3"
      >
        {expanded ? <ChevronDown size={16} className="ui-muted" /> : <ChevronRight size={16} className="ui-muted" />}
        <span className={`text-sm font-semibold ${dotColor}`}>●</span>
        <span className="text-[14px] font-semibold uppercase tracking-wider text-muted-foreground">{label}</span>
        <span className="ui-muted-14">({tasks.length})</span>
      </button>
      {expanded && (
        <div className="ui-col-2">
          {tasks.map((task) => (
            <TaskCard
              key={task.id}
              task={task}
              onCall={onCall}
              onSnooze={onSnooze}
              onClose={onClose}
              onDetail={onDetail}
              actionsDisabled={actionsDisabled}
            />
          ))}
        </div>
      )}
    </section>
  );
}

export default function Tasks() {
  const navigate = useNavigate();
  const callTracker = useCallTracker();
  const queryClient = useQueryClient();
  const [resolutionOpen, setResolutionOpen] = useState(false);
  const [resolutionTask, setResolutionTask] = useState<RetentionTask | null>(null);
  const [snoozeOpen, setSnoozeOpen] = useState(false);
  const [snoozeTask, setSnoozeTask] = useState<RetentionTask | null>(null);
  const [snoozeDate, setSnoozeDate] = useState("");
  const [infoOpen, setInfoOpen] = useState(false);

  const trainerIdentity = useTrainerIdentity();
  const trainerInfo = trainerIdentity.data;
  const trainerId = trainerInfo?.id ?? null;

  const {
    data: tasksResponse,
    isLoading: tasksLoading,
    isError: tasksError,
    isFetchingNextPage: tasksFetchingNextPage,
    hasNextPage: hasMoreTasks,
    fetchNextPage: fetchMoreTasks,
    refetch: refetchTasks,
    isRefetchError: tasksRefetchError,
  } = useInfiniteQuery<RetentionTaskListResponse>({
    queryKey: ["retention-tasks", trainerId, { resolved: false, limit: TASKS_PAGE_LIMIT }],
    queryFn: ({ pageParam }) =>
      apiClient
        .get("/retention/tasks/", {
          params: {
            trainer_id: trainerId ?? undefined,
            resolved: false,
            limit: TASKS_PAGE_LIMIT,
            offset: pageParam,
          },
        })
        .then((r) => normalizeTaskResponse(r.data)),
    initialPageParam: 0,
    getNextPageParam: nextTaskPageParam,
    enabled: trainerId !== null,
    staleTime: 60_000,
  });

  const closedTodayQuery = useQuery<number>({
    queryKey: ["tasks-closed-today", trainerId],
    queryFn: () =>
      apiClient
        .get("/retention/tasks/", {
          params: {
            trainer_id: trainerId ?? undefined,
            resolved: true,
            resolved_today: true,
            limit: 1,
            offset: 0,
          },
        })
        .then((r) => {
          if (!Array.isArray(r.data) && typeof r.data.count === "number") {
            return r.data.count;
          }
          const items = r.data.items ?? r.data;
          return Array.isArray(items) ? items.length : 0;
        }),
    enabled: trainerId !== null,
    staleTime: 30_000,
  });

  const closedTodayCount = closedTodayQuery.data;
  const taskDataUnavailable =
    trainerIdentity.isError ||
    trainerIdentity.isRefetchError ||
    tasksError ||
    tasksRefetchError;

  const snoozeMutation = useMutation({
    mutationFn: ({ taskId, newDueDate }: { taskId: number; newDueDate: string }) => {
      if (taskDataUnavailable) {
        throw new Error("Task data is unavailable");
      }
      return apiClient.post(`/retention/tasks/${taskId}/snooze/`, { new_due_date: newDueDate });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["retention-tasks"] });
      queryClient.invalidateQueries({ queryKey: ["task-badge-count"] });
      setSnoozeOpen(false);
      setSnoozeTask(null);
    },
  });

  const tasks = tasksResponse?.pages.flatMap((page) => page.items) ?? EMPTY_TASKS;
  const totalTasks = tasksResponse?.pages[0]?.count ?? tasks.length;
  const grouped = useMemo(() => groupTasks(tasks), [tasks]);
  const activeCount = grouped ? grouped.overdue.length + grouped.today.length : 0;
  const pendingTask = tasks?.find((t) => t.id === callTracker.pendingTaskId) ?? null;
  const tasksPending =
    trainerIdentity.isLoading || (trainerId !== null && tasksLoading);
  const tasksUnavailable =
    taskDataUnavailable || (!trainerIdentity.isLoading && trainerId === null);

  function handleCall(task: RetentionTask) {
    if (taskDataUnavailable) return;
    if (task.student_phone) {
      callTracker.startCall(task.id, task.student_phone);
    }
  }

  function handleSnooze(task: RetentionTask) {
    if (taskDataUnavailable) return;
    const d = new Date();
    d.setDate(d.getDate() + 1);
    setSnoozeDate(toISODate(d));
    setSnoozeTask(task);
    setSnoozeOpen(true);
  }

  function handleClose(task: RetentionTask) {
    if (taskDataUnavailable) return;
    setResolutionTask(task);
    setResolutionOpen(true);
  }

  function handleDetail(task: RetentionTask) {
    if (taskDataUnavailable) return;
    navigate(`/trainer/tasks/${task.id}`);
  }

  return (
    <div className="flex flex-col gap-5 px-4 pt-6 pb-24">
      <div className="flex items-baseline justify-between">
        <div className="ui-row-2">
          <h1 className="text-lg font-semibold text-foreground">
            Нужно обзвонить{activeCount > 0 ? ` (${activeCount})` : ""}
          </h1>
          <button
            type="button"
            onClick={() => setInfoOpen(true)}
            className="flex h-11 w-11 items-center justify-center rounded-xl text-muted-foreground transition-colors hover:bg-neutral-100 hover:text-foreground"
            aria-label="Как это работает?"
          >
            <HelpCircle size={18} />
          </button>
        </div>
        {(closedTodayCount ?? 0) > 0 && (
          <span className="text-[14px] text-green-600 font-medium">
            ✓ {closedTodayCount} сегодня
          </span>
        )}
      </div>

      {closedTodayQuery.isError || closedTodayQuery.isRefetchError ? (
        <QueryStateNotice
          title={
            closedTodayCount !== undefined
              ? "Итог за сегодня мог устареть"
              : "Не удалось загрузить итог за сегодня"
          }
          message="Список задач доступен отдельно, но число закрытых сегодня пока не подтверждено."
          retryLabel="Повторить загрузку итога"
          retrying={closedTodayQuery.isFetching}
          onRetry={() => void closedTodayQuery.refetch()}
        />
      ) : null}

      {tasksPending ? (
        <TasksSkeleton />
      ) : tasksUnavailable ? (
        <QueryStateNotice
          title={tasksResponse ? "Задачи могли устареть" : "Не удалось загрузить задачи"}
          message="Повторите загрузку перед звонком, переносом или закрытием задачи."
          retryLabel="Повторить загрузку задач"
          retrying={trainerIdentity.isFetching || tasksFetchingNextPage}
          onRetry={() => {
            if (trainerIdentity.isError || trainerIdentity.isRefetchError) {
              void trainerIdentity.refetch();
            } else {
              void refetchTasks();
            }
          }}
        />
      ) : null}

      {!tasksPending && (!tasksUnavailable || tasksResponse) &&
      (activeCount === 0 && grouped.snoozed.length === 0 ? (
        <EmptyState />
      ) : (
        <div className="flex flex-col gap-6">
          {totalTasks > tasks.length ? (
            <p className="ui-muted-12">
              Показано {tasks.length} из {totalTasks}
            </p>
          ) : null}
          <Section label={SECTION_LABELS.overdue} tasks={grouped.overdue} variant="overdue" onCall={handleCall} onSnooze={handleSnooze} onClose={handleClose} onDetail={handleDetail} actionsDisabled={taskDataUnavailable} />
          <Section label={SECTION_LABELS.today} tasks={grouped.today} variant="today" onCall={handleCall} onSnooze={handleSnooze} onClose={handleClose} onDetail={handleDetail} actionsDisabled={taskDataUnavailable} />
          <Section label={SECTION_LABELS.snoozed} tasks={grouped.snoozed} variant="snoozed" defaultExpanded={false} onCall={handleCall} onSnooze={handleSnooze} onClose={handleClose} onDetail={handleDetail} actionsDisabled={taskDataUnavailable} />
          {hasMoreTasks ? (
            <Button
              type="button"
              variant="outline"
              onClick={() => void fetchMoreTasks()}
              disabled={tasksFetchingNextPage || taskDataUnavailable}
              className="w-full"
            >
              {tasksFetchingNextPage ? "Загружаю..." : "Загрузить ещё"}
            </Button>
          ) : null}
        </div>
      ))}

      <CallResultSheet
        open={callTracker.showResult}
        onOpenChange={() => callTracker.dismissResult()}
        task={pendingTask}
      />

      <ResolutionSheet
        open={resolutionOpen}
        onOpenChange={setResolutionOpen}
        task={resolutionTask}
        trainerId={trainerId}
      />

      {snoozeOpen && snoozeTask && (
        <Sheet open onOpenChange={() => { setSnoozeOpen(false); setSnoozeTask(null); }}>
          <SheetContent side="bottom" className="rounded-t-2xl">
            <SheetHeader>
              <SheetTitle>Отложить — {snoozeTask.student_name}</SheetTitle>
            </SheetHeader>
            <div className="flex flex-col gap-4 px-4 pb-4">
              <input
                type="date"
                value={snoozeDate}
                onChange={(e) => setSnoozeDate(e.target.value)}
                className="rounded-lg border border-input bg-background px-3 py-2 text-[14px]"
              />
              <Button
                onClick={() => snoozeMutation.mutate({ taskId: snoozeTask.id, newDueDate: snoozeDate })}
                disabled={!snoozeDate || snoozeMutation.isPending}
                className="ui-brand-button"
              >
                {snoozeMutation.isPending ? "Сохранение..." : "Отложить"}
              </Button>
            </div>
          </SheetContent>
        </Sheet>
      )}

      {/* Info sheet — как работает система задач */}
      <Sheet open={infoOpen} onOpenChange={setInfoOpen}>
        <SheetContent side="bottom" className="rounded-t-2xl">
          <SheetHeader>
            <SheetTitle>Как это работает?</SheetTitle>
          </SheetHeader>
          <div className="flex flex-col gap-3 px-4 pb-6 text-[14px] text-foreground">
            <p className="ui-muted">
              Система автоматически следит за посещениями учеников. Если ученик перестал ходить — создаётся задача позвонить.
            </p>
            <div className="ui-col-2">
              <div className="flex items-start gap-2">
                <span className="text-amber-500 font-bold mt-0.5">●</span>
                <p><span className="font-medium">4–10 дней</span> без визита — попадает в список</p>
              </div>
              <div className="flex items-start gap-2">
                <span className="text-red-500 font-bold mt-0.5">●</span>
                <p><span className="font-medium">7–21 день</span> — срочный, нужно звонить быстрее</p>
              </div>
              <div className="flex items-start gap-2">
                <span className="text-muted-foreground font-bold mt-0.5">●</span>
                <p><span className="font-medium">30+ дней</span> — высокий риск ухода</p>
              </div>
            </div>
            <p className="ui-muted">
              Сроки зависят от того, как часто ученик обычно ходит. Кто ходил каждый день — попадёт быстрее. Кто ходил раз в неделю — позже.
            </p>
            <p className="ui-muted">
              Когда ученик возвращается на тренировку — задача закрывается автоматически.
            </p>
          </div>
        </SheetContent>
      </Sheet>
    </div>
  );
}
