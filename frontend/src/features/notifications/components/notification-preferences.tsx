import { useCallback, useEffect, useRef, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { Separator } from "@/components/ui/separator";
import { usePushSubscription } from "../hooks/use-push-subscription";
import apiClient from "@/api/custom-fetch";

interface NotificationPreferencesProps {
  role: "student" | "parent" | "trainer";
}

interface CategoryDef {
  key: string;
  label: string;
  desc: string;
}

const CATEGORIES: Record<string, CategoryDef[]> = {
  student: [
    {
      key: "training_reminders",
      label: "Напоминания о тренировках",
      desc: "За 24 часа и 1 час до занятия",
    },
    {
      key: "subscription_alerts",
      label: "Абонемент",
      desc: "Истечение срока и остаток тренировок",
    },
    {
      key: "feedback_surveys",
      label: "Опросы",
      desc: "Обратная связь после занятий",
    },
  ],
  parent: [
    {
      key: "child_checkin",
      label: "Чек-ин ребенка",
      desc: "Когда ребенок отмечается на тренировке",
    },
    {
      key: "subscription_alerts",
      label: "Абонемент ребенка",
      desc: "Истечение абонемента ребенка",
    },
    {
      key: "feedback_surveys",
      label: "Опросы",
      desc: "Обратная связь о тренировках ребенка",
    },
  ],
  trainer: [
    {
      key: "trainer_tasks",
      label: "Задачи",
      desc: "Новые задачи по удержанию и работе с учениками",
    },
  ],
};

export function NotificationPreferences({
  role,
}: NotificationPreferencesProps) {
  const { isPushSupported, permission, requestPermission } =
    usePushSubscription();

  const [disabledCategories, setDisabledCategories] = useState<string[]>([]);
  const [loaded, setLoaded] = useState(false);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const isSubscribed = isPushSupported && permission === "granted";
  const categories = CATEGORIES[role] ?? [];

  // Fetch current preferences on mount
  useEffect(() => {
    if (!isSubscribed) return;

    apiClient
      .get<{ disabled_categories: string[] }>("/notifications/preferences/")
      .then((res) => {
        setDisabledCategories(res.data.disabled_categories);
        setLoaded(true);
      })
      .catch(() => {
        setLoaded(true);
      });
  }, [isSubscribed]);

  const persistPreferences = useCallback((nextDisabled: string[]) => {
    if (debounceRef.current) clearTimeout(debounceRef.current);

    debounceRef.current = setTimeout(() => {
      apiClient
        .put("/notifications/preferences/", {
          disabled_categories: nextDisabled,
        })
        .catch(() => {
          // Best-effort save — preferences will re-sync on next load
        });
    }, 500);
  }, []);

  const handleToggle = useCallback(
    (categoryKey: string, checked: boolean) => {
      setDisabledCategories((prev) => {
        const next = checked
          ? prev.filter((c) => c !== categoryKey)
          : [...prev, categoryKey];
        persistPreferences(next);
        return next;
      });
    },
    [persistPreferences],
  );

  // Not supported
  if (!isPushSupported) return null;

  return (
    <div className="space-y-3">
      <div className="ui-row-2">
        <h2 className="text-[16px] font-semibold">Уведомления</h2>
        {!isSubscribed && (
          <Badge variant="outline" className="text-[12px]">
            Отключены
          </Badge>
        )}
      </div>

      {!isSubscribed ? (
        <Button
          size="sm"
          className="text-[14px] font-semibold text-white min-h-[44px]"
          style={{ backgroundColor: "var(--branding-accent)" }}
          onClick={() => void requestPermission()}
        >
          Включить
        </Button>
      ) : !loaded ? null : (
        <div className="space-y-0">
          {categories.map((cat, idx) => (
            <div key={cat.key}>
              {idx > 0 && <Separator />}
              <div className="flex items-center justify-between min-h-[56px] py-2">
                <div className="flex-1 min-w-0 pr-3">
                  <p className="text-[14px]">{cat.label}</p>
                  <p className="text-[12px] text-neutral-500">{cat.desc}</p>
                </div>
                <Switch
                  checked={!disabledCategories.includes(cat.key)}
                  onCheckedChange={(checked: boolean) =>
                    handleToggle(cat.key, checked)
                  }
                  aria-label={cat.label}
                />
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default NotificationPreferences;
