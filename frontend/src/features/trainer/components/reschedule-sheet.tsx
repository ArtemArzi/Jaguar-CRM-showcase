import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from "@/components/ui/sheet";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import apiClient from "@/api/custom-fetch";
import { getApiError } from "@/lib/utils";
import { formatDateLong } from "@/lib/format";

function formatTimeShort(time: string): string {
  return time.slice(0, 5);
}

interface RescheduleSheetProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  scheduleId: number;
  scheduleName: string;
  originalDate: string;
  originalStartTime: string;
  originalEndTime: string;
}

export function RescheduleSheet({
  open,
  onOpenChange,
  scheduleId,
  scheduleName,
  originalDate,
  originalStartTime,
  originalEndTime,
}: RescheduleSheetProps) {
  const queryClient = useQueryClient();
  const [newDate, setNewDate] = useState("");
  const [newStartTime, setNewStartTime] = useState(() => formatTimeShort(originalStartTime));
  const [newEndTime, setNewEndTime] = useState(() => formatTimeShort(originalEndTime));
  const [reason, setReason] = useState("");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const rescheduleMutation = useMutation({
    mutationFn: () =>
      apiClient.post(`/schedules/${scheduleId}/reschedule/`, {
        date: originalDate,
        new_date: newDate,
        new_start_time: newStartTime,
        new_end_time: newEndTime,
        ...(reason.trim() ? { reason: reason.trim() } : {}),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["schedules"] });
      onOpenChange(false);
    },
    onError: (error: unknown) => {
      setErrorMsg(getApiError(error, "Ошибка при переносе"));
    },
  });

  function handleOpenChange(nextOpen: boolean) {
    if (!nextOpen) {
      setNewDate("");
      setReason("");
      setErrorMsg(null);
    }
    onOpenChange(nextOpen);
  }

  function isFormValid(): boolean {
    if (!newDate || !newStartTime || !newEndTime) return false;
    return newEndTime > newStartTime;
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setErrorMsg(null);
    if (!isFormValid()) {
      setErrorMsg("Заполните дату и время. Время окончания должно быть позже начала.");
      return;
    }
    rescheduleMutation.mutate();
  }

  return (
    <Sheet open={open} onOpenChange={handleOpenChange}>
      <SheetContent side="bottom">
        <SheetHeader>
          <SheetTitle>Перенести тренировку</SheetTitle>
          <SheetDescription>{scheduleName}</SheetDescription>
        </SheetHeader>
        <form onSubmit={handleSubmit} className="ui-sheet-body">
          <div className="rounded-lg bg-neutral-50 p-3">
            <p className="text-[12px] text-muted-foreground mb-1">Текущее время</p>
            <p className="text-[14px] font-medium text-foreground">
              {formatDateLong(originalDate)},{" "}
              {formatTimeShort(originalStartTime)}&ndash;
              {formatTimeShort(originalEndTime)}
            </p>
          </div>

          <div>
            <label className="ui-field-label">
              Новая дата *
            </label>
            <Input
              type="date"
              value={newDate}
              onChange={(e) => setNewDate(e.target.value)}
              required
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="ui-field-label">
                Начало *
              </label>
              <Input
                type="time"
                value={newStartTime}
                onChange={(e) => setNewStartTime(e.target.value)}
                required
              />
            </div>
            <div>
              <label className="ui-field-label">
                Конец *
              </label>
              <Input
                type="time"
                value={newEndTime}
                onChange={(e) => setNewEndTime(e.target.value)}
                required
              />
            </div>
          </div>
          <div>
            <label className="ui-field-label">
              Причина (необязательно)
            </label>
            <textarea
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="Причина переноса..."
              rows={2}
              className="flex w-full rounded-md border border-input bg-background px-3 py-2 text-base ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 md:text-sm resize-none"
            />
          </div>

          {errorMsg && (
            <p className="ui-error-center">{errorMsg}</p>
          )}

          <Button
            type="submit"
            className="ui-brand-button"
            disabled={rescheduleMutation.isPending || !isFormValid()}
          >
            {rescheduleMutation.isPending ? "Перенос..." : "Перенести"}
          </Button>
        </form>
      </SheetContent>
    </Sheet>
  );
}
