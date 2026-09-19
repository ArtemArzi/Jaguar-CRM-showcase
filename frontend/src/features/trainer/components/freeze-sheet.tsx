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
import { CheckCircle2 } from "lucide-react";
import apiClient from "@/api/custom-fetch";
import { getApiError } from "@/lib/utils";

const DURATION_OPTIONS = [7, 14, 21, 30] as const;

const REASON_OPTIONS: { value: string; label: string }[] = [
  { value: "vacation", label: "Отпуск" },
  { value: "injury", label: "Травма" },
  { value: "illness", label: "Болезнь" },
  { value: "other", label: "Другое" },
];

interface FreezeSheetProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  subscriptionId: number | null;
  freezeStatus?: string | null;
  studentName: string;
}

export function FreezeSheet({
  open,
  onOpenChange,
  subscriptionId,
  freezeStatus = null,
  studentName,
}: FreezeSheetProps) {
  const queryClient = useQueryClient();
  const [selectedDays, setSelectedDays] = useState<number | null>(null);
  const [reason, setReason] = useState("vacation");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [submitted, setSubmitted] = useState(false);

  const freezeMutation = useMutation({
    mutationFn: () =>
      apiClient.post(`/billing/subscriptions/${subscriptionId}/freeze/`, {
        days: selectedDays,
        reason,
      }),
    onSuccess: (response) => {
      const freezeData = response.data;
      queryClient.invalidateQueries({ queryKey: ["student"] });
      if (freezeData.status === "pending") {
        setSubmitted(true);
      } else {
        resetAndClose();
      }
    },
    onError: (error: unknown) => {
      setErrorMsg(getFreezeErrorMessage(error));
    },
  });

  function resetAndClose() {
    setSelectedDays(null);
    setReason("vacation");
    setErrorMsg(null);
    setSubmitted(false);
    onOpenChange(false);
  }

  function handleOpenChange(nextOpen: boolean) {
    if (!nextOpen) {
      setSelectedDays(null);
      setReason("vacation");
      setErrorMsg(null);
      setSubmitted(false);
    }
    onOpenChange(nextOpen);
  }

  // No active subscription
  if (!subscriptionId) {
    return (
      <Sheet open={open} onOpenChange={handleOpenChange}>
        <SheetContent side="bottom" showCloseButton={false}>
          <SheetHeader>
            <SheetTitle>Заморозка</SheetTitle>
            <SheetDescription>{studentName}</SheetDescription>
          </SheetHeader>
          <div className="ui-sheet-body">
            <p className="ui-muted-14">
              Нет активного абонемента
            </p>
            <Button variant="outline" className="w-full" onClick={resetAndClose}>
              Закрыть
            </Button>
          </div>
        </SheetContent>
      </Sheet>
    );
  }

  if (freezeStatus === "pending") {
    return (
      <Sheet open={open} onOpenChange={handleOpenChange}>
        <SheetContent side="bottom" showCloseButton={false}>
          <SheetHeader>
            <SheetTitle>Заморозка</SheetTitle>
            <SheetDescription>{studentName}</SheetDescription>
          </SheetHeader>
          <div className="ui-sheet-body">
            <div className="rounded-xl bg-amber-50 p-4 ring-1 ring-amber-100">
              <p className="text-[15px] font-medium text-amber-900">
                Заявка уже ждёт подтверждения
              </p>
              <p className="mt-1 text-[13px] leading-5 text-amber-800">
                Владелец клуба рассмотрит её в списке заявок на заморозку.
              </p>
            </div>
            <Button variant="outline" className="w-full" onClick={resetAndClose}>
              Закрыть
            </Button>
          </div>
        </SheetContent>
      </Sheet>
    );
  }

  // Success: pending approval
  if (submitted) {
    return (
      <Sheet open={open} onOpenChange={handleOpenChange}>
        <SheetContent side="bottom" showCloseButton={false}>
          <SheetHeader>
            <SheetTitle>Заморозка</SheetTitle>
            <SheetDescription>{studentName}</SheetDescription>
          </SheetHeader>
          <div className="flex flex-col items-center gap-4 p-4 pt-0">
            <CheckCircle2 size={48} className="text-green-500" />
            <p className="text-[16px] font-medium text-foreground text-center">
              Запрос на заморозку отправлен
            </p>
            <p className="text-[14px] text-muted-foreground text-center">
              Владелец клуба подтвердит заморозку.
            </p>
            <Button
              className="ui-brand-button"
              onClick={resetAndClose}
            >
              Понятно
            </Button>
          </div>
        </SheetContent>
      </Sheet>
    );
  }

  return (
    <Sheet open={open} onOpenChange={handleOpenChange}>
      <SheetContent side="bottom" showCloseButton={false}>
        <SheetHeader>
          <SheetTitle>Заморозка</SheetTitle>
          <SheetDescription>{studentName}</SheetDescription>
        </SheetHeader>

        <div className="ui-sheet-body">
          {/* Duration options */}
          <div>
            <p className="text-[14px] text-muted-foreground mb-2">
              Срок заморозки
            </p>
            <div className="flex gap-2">
              {DURATION_OPTIONS.map((days) => (
                <Button
                  key={days}
                  type="button"
                  variant={selectedDays === days ? "default" : "outline"}
                  className={`flex-1 ${selectedDays === days ? "bg-[var(--branding-accent)] text-white hover:opacity-90" : ""}`}
                  onClick={() => {
                    setSelectedDays(days);
                    setErrorMsg(null);
                  }}
                >
                  {days} дн.
                </Button>
              ))}
            </div>
          </div>

          {/* Reason options */}
          <div>
            <p className="text-[14px] text-muted-foreground mb-2">Причина</p>
            <div className="flex flex-wrap gap-2">
              {REASON_OPTIONS.map((opt) => (
                <Button
                  key={opt.value}
                  type="button"
                  variant={reason === opt.value ? "default" : "outline"}
                  className={`${reason === opt.value ? "bg-[var(--branding-accent)] text-white hover:opacity-90" : ""}`}
                  onClick={() => setReason(opt.value)}
                >
                  {opt.label}
                </Button>
              ))}
            </div>
          </div>

          {/* Error */}
          {errorMsg && (
            <p className="ui-error-center">{errorMsg}</p>
          )}

          {/* Submit */}
          <Button
            className="ui-brand-button"
            disabled={!selectedDays || freezeMutation.isPending}
            onClick={() => freezeMutation.mutate()}
          >
            {freezeMutation.isPending
              ? "Отправка..."
              : `Заморозить на ${selectedDays ?? "..."} дн.`}
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  );
}

function getFreezeErrorMessage(error: unknown): string {
  const response = (error as {
    response?: { data?: { code?: string; detail?: unknown } };
  })?.response;
  const detail = response?.data?.detail;
  const detailCode =
    typeof detail === "object" && detail !== null && "code" in detail
      ? String((detail as { code?: unknown }).code ?? "")
      : "";
  const code = response?.data?.code ?? detailCode;
  if (code === "freeze_pending_exists") {
    return "Заявка на заморозку уже ждёт подтверждения";
  }
  return getApiError(error, "Ошибка при заморозке");
}
