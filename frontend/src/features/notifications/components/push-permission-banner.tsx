import { useState } from "react";
import { Bell } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { usePushSubscription } from "../hooks/use-push-subscription";

interface PushPermissionBannerProps {
  role: "student" | "parent" | "trainer";
}

const COPY = {
  student: {
    title: "Будьте в курсе",
    body: "Получайте напоминания о тренировках и абонементе",
  },
  parent: {
    title: "Следите за ребенком",
    body: "Узнавайте, когда ребенок приходит на тренировку",
  },
  trainer: {
    title: "Не пропустите важное",
    body: "Получайте уведомления о расписании",
  },
} as const;

export function PushPermissionBanner({ role }: PushPermissionBannerProps) {
  const { isPushSupported, permission, prompted, requestPermission } =
    usePushSubscription();
  const [dismissed, setDismissed] = useState(
    () => localStorage.getItem("push_prompted") === "true",
  );

  // Don't render if push is not supported
  if (!isPushSupported) return null;

  // Don't render if already decided or dismissed
  if (permission !== "default" || prompted || dismissed) return null;

  const copy = COPY[role];

  const handleEnable = async () => {
    await requestPermission();
  };

  const handleDismiss = () => {
    localStorage.setItem("push_prompted", "true");
    setDismissed(true);
  };

  return (
    <Card className="bg-white p-4 rounded-lg" role="status" aria-live="polite">
      <div className="flex items-center gap-2 mb-1">
        <Bell size={24} className="text-[var(--branding-accent)] shrink-0" />
        <span className="text-[16px] font-semibold">{copy.title}</span>
      </div>
      <p className="text-[14px] text-neutral-500 mb-3">{copy.body}</p>
      <div className="flex items-center gap-3">
        <Button
          className="min-h-[44px] text-[14px] font-semibold text-white px-4"
          style={{ backgroundColor: "var(--branding-accent)" }}
          aria-label="Включить push-уведомления"
          onClick={handleEnable}
        >
          Включить уведомления
        </Button>
        <Button
          variant="ghost"
          className="min-h-[44px] text-[14px]"
          onClick={handleDismiss}
        >
          Не сейчас
        </Button>
      </div>
    </Card>
  );
}

export default PushPermissionBanner;
