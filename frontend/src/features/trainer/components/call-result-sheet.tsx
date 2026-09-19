import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import apiClient from "@/api/custom-fetch";
import { toISODate } from "@/lib/utils";
import type { RetentionTask } from "../types";

interface CallResultSheetProps {
  readonly open: boolean;
  readonly onOpenChange: (open: boolean) => void;
  readonly task: RetentionTask | null;
}

const RESULT_OPTIONS = [
  { value: "will_come", label: "Придёт", icon: "🟢" },
  { value: "no_answer", label: "Не берёт", icon: "🔴" },
  { value: "snooze_tomorrow", label: "Перезвонить завтра", icon: "⏸" },
  { value: "snooze_later", label: "Перезвонить позже", icon: "🔄" },
  { value: "quit", label: "Ушёл из клуба", icon: "👋" },
] as const;

type ResultValue = (typeof RESULT_OPTIONS)[number]["value"];

function getTomorrowDate(): string {
  const d = new Date();
  d.setDate(d.getDate() + 1);
  return toISODate(d);
}

export function CallResultSheet({
  open,
  onOpenChange,
  task,
}: CallResultSheetProps) {
  const [selected, setSelected] = useState<ResultValue | null>(null);
  const [customDate, setCustomDate] = useState("");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const queryClient = useQueryClient();

  function invalidateAll() {
    queryClient.invalidateQueries({ queryKey: ["retention-tasks"] });
    queryClient.invalidateQueries({ queryKey: ["tasks-closed-today"] });
    queryClient.invalidateQueries({ queryKey: ["task-badge-count"] });
    if (task) {
      queryClient.invalidateQueries({ queryKey: ["retention-task", task.id] });
    }
  }

  const closeMutation = useMutation({
    mutationFn: (data: { resolution: string; notes: string }) =>
      apiClient.post(`/retention/tasks/${task!.id}/close/`, data),
    onSuccess: () => {
      invalidateAll();
      handleDone();
    },
    onError: () => setErrorMsg("Не удалось сохранить результат"),
  });

  const snoozeMutation = useMutation({
    mutationFn: (data: { new_due_date: string; increment_attempt?: boolean }) =>
      apiClient.post(`/retention/tasks/${task!.id}/snooze/`, data),
    onSuccess: () => {
      invalidateAll();
      handleDone();
    },
    onError: () => setErrorMsg("Не удалось отложить задачу"),
  });

  function handleDone() {
    setSelected(null);
    setCustomDate("");
    setErrorMsg(null);
    onOpenChange(false);
  }

  function handleSubmit() {
    if (!selected || !task) return;

    if (selected === "will_come") {
      const d = new Date();
      d.setDate(d.getDate() + 5);
      snoozeMutation.mutate({
        new_due_date: toISODate(d),
        increment_attempt: true,
      });
    } else if (selected === "no_answer") {
      snoozeMutation.mutate({
        new_due_date: getTomorrowDate(),
        increment_attempt: true,
      });
    } else if (selected === "snooze_tomorrow") {
      snoozeMutation.mutate({
        new_due_date: getTomorrowDate(),
        increment_attempt: true,
      });
    } else if (selected === "snooze_later") {
      if (!customDate) return;
      snoozeMutation.mutate({
        new_due_date: customDate,
        increment_attempt: true,
      });
    } else if (selected === "quit") {
      closeMutation.mutate({ resolution: "quit", notes: "" });
    }
  }

  const isPending = closeMutation.isPending || snoozeMutation.isPending;
  const needsDate = selected === "snooze_later";
  const canSubmit = selected && (!needsDate || customDate);

  return (
    <Sheet open={open} onOpenChange={handleDone}>
      <SheetContent side="bottom" showCloseButton={false} className="rounded-t-2xl">
        <SheetHeader>
          <SheetTitle>Как прошёл звонок?</SheetTitle>
        </SheetHeader>
        <div className="flex flex-col gap-2 px-4 pb-4">
          {RESULT_OPTIONS.map((option) => (
            <button
              key={option.value}
              type="button"
              onClick={() => setSelected(option.value)}
              className={`flex items-center gap-3 p-4 rounded-lg text-left transition-all min-h-[52px] border ${
                selected === option.value
                  ? "border-[var(--branding-accent)] bg-[var(--branding-accent)]/5"
                  : "border-border bg-background"
              }`}
            >
              <span className="text-xl">{option.icon}</span>
              <span className="text-[16px] font-medium text-foreground">
                {option.label}
              </span>
            </button>
          ))}
          {needsDate && (
            <input
              type="date"
              value={customDate}
              onChange={(e) => setCustomDate(e.target.value)}
              className="rounded-lg border border-input bg-background px-3 py-2 text-[14px]"
            />
          )}
          {errorMsg && (
            <p className="ui-error-center">{errorMsg}</p>
          )}
          <Button
            onClick={handleSubmit}
            disabled={!canSubmit || isPending}
            className="w-full mt-2 bg-[var(--branding-accent)] text-white hover:opacity-90"
          >
            {isPending ? "Сохранение..." : "Готово"}
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  );
}
