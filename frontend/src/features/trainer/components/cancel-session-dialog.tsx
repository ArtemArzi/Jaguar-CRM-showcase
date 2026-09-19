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
import { getApiError } from "@/lib/utils";
import { formatDateLong } from "@/lib/format";

interface CancelSessionDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  scheduleId: number;
  scheduleName: string;
  date: string;
}

export function CancelSessionDialog({
  open,
  onOpenChange,
  scheduleId,
  scheduleName,
  date,
}: CancelSessionDialogProps) {
  const queryClient = useQueryClient();
  const [reason, setReason] = useState("");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const cancelMutation = useMutation({
    mutationFn: () =>
      apiClient.post(`/schedules/${scheduleId}/cancel/`, {
        date,
        ...(reason.trim() ? { reason: reason.trim() } : {}),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["schedules"] });
      resetAndClose();
    },
    onError: (error: unknown) => {
      setErrorMsg(getApiError(error, "Ошибка при отмене"));
    },
  });

  function resetAndClose() {
    setReason("");
    setErrorMsg(null);
    onOpenChange(false);
  }

  function handleOpenChange(nextOpen: boolean) {
    if (!nextOpen) {
      setReason("");
      setErrorMsg(null);
    }
    onOpenChange(nextOpen);
  }

  return (
    <Sheet open={open} onOpenChange={handleOpenChange}>
      <SheetContent side="bottom" showCloseButton={false}>
        <SheetHeader>
          <SheetTitle>Отменить тренировку</SheetTitle>
          <SheetDescription>
            {scheduleName} на {formatDateLong(date)}
          </SheetDescription>
        </SheetHeader>
        <div className="ui-sheet-body">
          <p className="ui-muted-14">
            Тренировка будет отменена. Это действие нельзя отменить.
          </p>
          <div>
            <label className="ui-field-label">
              Причина (необязательно)
            </label>
            <textarea
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="Укажите причину отмены..."
              rows={2}
              className="flex w-full rounded-md border border-input bg-background px-3 py-2 text-base ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 md:text-sm resize-none"
            />
          </div>

          {errorMsg && (
            <p className="ui-error-center">{errorMsg}</p>
          )}

          <div className="flex gap-3">
            <Button
              variant="outline"
              className="flex-1"
              onClick={resetAndClose}
              disabled={cancelMutation.isPending}
            >
              Назад
            </Button>
            <Button
              variant="destructive"
              className="flex-1"
              onClick={() => cancelMutation.mutate()}
              disabled={cancelMutation.isPending}
            >
              {cancelMutation.isPending ? "Отмена..." : "Отменить тренировку"}
            </Button>
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
}
