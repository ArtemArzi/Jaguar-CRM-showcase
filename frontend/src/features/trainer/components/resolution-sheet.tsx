import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import apiClient from "@/api/custom-fetch";
import type { RetentionTask } from "../types";

interface ResolutionSheetProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  task: RetentionTask | null;
  trainerId: number | null;
}

const RESOLUTION_OPTIONS = [
  {
    value: "called_will_come",
    label: "Позвонил, придёт",
    bg: "bg-green-100",
    border: "border-l-4 border-green-700",
  },
  {
    value: "no_answer",
    label: "Не ответил",
    bg: "bg-amber-100",
    border: "border-l-4 border-amber-700",
  },
  {
    value: "quit",
    label: "Бросил",
    bg: "bg-red-100",
    border: "border-l-4 border-red-700",
  },
] as const;

export function ResolutionSheet({
  open,
  onOpenChange,
  task,
  trainerId,
}: ResolutionSheetProps) {
  const [selectedResolution, setSelectedResolution] = useState<string | null>(
    null,
  );
  const [notes, setNotes] = useState("");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const queryClient = useQueryClient();

  const closeMutation = useMutation({
    mutationFn: (data: { resolution: string; notes: string }) =>
      apiClient.post(`/retention/tasks/${task!.id}/close/`, data),
    onError: () => {
      setErrorMsg("Не удалось закрыть задачу");
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["retention-tasks", trainerId],
      });
      queryClient.invalidateQueries({
        queryKey: ["task-badge-count"],
      });
      handleClose();
    },
  });

  function handleClose() {
    setSelectedResolution(null);
    setNotes("");
    setErrorMsg(null);
    onOpenChange(false);
  }

  function handleSubmit() {
    if (!selectedResolution || !task) return;
    setErrorMsg(null);
    closeMutation.mutate({ resolution: selectedResolution, notes });
  }

  return (
    <Sheet open={open} onOpenChange={handleClose}>
      <SheetContent side="bottom" showCloseButton={false} className="rounded-t-2xl">
        <SheetHeader>
          <SheetTitle>Закрыть задачу</SheetTitle>
          {task && (
            <SheetDescription>
              {task.student_name}
              {task.student_phone && (
                <>
                  {" "}
                  &middot;{" "}
                  <a
                    href={`tel:${task.student_phone}`}
                    className="text-[var(--branding-accent)]"
                  >
                    {task.student_phone}
                  </a>
                </>
              )}
              {task.days_missed > 0 && (
                <> &middot; Пропустил {task.days_missed} дн.</>
              )}
            </SheetDescription>
          )}
        </SheetHeader>

        <div className="flex flex-col gap-3 px-4">
          {RESOLUTION_OPTIONS.map((option) => (
            <button
              key={option.value}
              type="button"
              onClick={() => setSelectedResolution(option.value)}
              className={`flex items-center p-4 rounded-lg text-left transition-all min-h-[52px] ${option.bg} ${option.border} ${
                selectedResolution === option.value
                  ? "ring-2 ring-[var(--branding-accent)]"
                  : ""
              }`}
            >
              <span className="text-[16px] font-medium text-foreground">
                {option.label}
              </span>
            </button>
          ))}
        </div>

        <div className="px-4">
          <textarea
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            placeholder="Заметка (необязательно)"
            className="w-full rounded-lg border border-input bg-background px-3 py-2 text-[14px] text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring min-h-[72px] resize-none"
          />
        </div>

        {errorMsg && (
          <p className="px-4 text-[14px] text-destructive text-center">
            {errorMsg}
          </p>
        )}

        <div className="px-4 pb-4">
          <Button
            onClick={handleSubmit}
            disabled={!selectedResolution || closeMutation.isPending}
            className="ui-brand-button"
          >
            {closeMutation.isPending ? "Закрытие..." : "Закрыть задачу"}
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  );
}
