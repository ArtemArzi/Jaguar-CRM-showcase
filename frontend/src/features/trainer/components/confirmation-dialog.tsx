import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";

interface ConfirmationData {
  presentCount: number;
  absentNames: string[];
  criticalAlerts: Array<{
    studentName: string;
    alertType: string;
    message: string;
  }>;
}

interface CheckinConfirmationProps {
  readonly data: ConfirmationData;
  readonly onConfirm: () => void;
  readonly onCancel: () => void;
  readonly isPending: boolean;
}

export function CheckinConfirmation({
  data,
  onConfirm,
  onCancel,
  isPending,
}: CheckinConfirmationProps) {
  return (
    <Sheet open onOpenChange={(open) => !open && onCancel()}>
      <SheetContent side="bottom" showCloseButton={false} className="rounded-t-2xl">
        <SheetHeader>
          <SheetTitle className="text-[18px]">
            Подтвердите отметку
          </SheetTitle>
        </SheetHeader>

        <div className="max-h-[70vh] overflow-y-auto px-4 pb-2">
          {/* Present count */}
          <p className="text-[16px] text-foreground">
            Пришли: <span className="font-semibold">{data.presentCount}</span>
          </p>

          {/* Absent students */}
          {data.absentNames.length > 0 && (
            <p className="mt-2 text-[14px] text-muted-foreground">
              Отсутствуют ({data.absentNames.length}):{" "}
              {data.absentNames.join(", ")}
            </p>
          )}

          {/* Critical alerts (D-12) */}
          {data.criticalAlerts.length > 0 && (
            <div className="mt-3 rounded-lg bg-red-50 p-3">
              <p className="text-[14px] font-semibold text-red-800">
                Обратите внимание:
              </p>
              <div className="mt-1 flex flex-col gap-1">
                {data.criticalAlerts.map((alert, i) => (
                  <p
                    key={`${alert.studentName}-${alert.alertType}-${i}`}
                    className="text-[13px] text-red-700"
                  >
                    {alert.studentName}: {alert.message}
                  </p>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Action buttons */}
        <div className="flex gap-3 px-4 pb-4 pt-2">
          <Button
            variant="outline"
            className="flex-1"
            onClick={onCancel}
            disabled={isPending}
          >
            Отмена
          </Button>
          <Button
            className="flex-1"
            onClick={onConfirm}
            disabled={isPending}
          >
            {isPending ? "Сохранение..." : "Подтвердить"}
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  );
}
