export type ParentSubscriptionAlertTone =
  | "danger"
  | "warning"
  | "success"
  | "neutral";

export interface ParentSubscriptionAlert {
  title: string;
  description: string;
  badge: string | null;
  tone: ParentSubscriptionAlertTone;
  requiresAttention: boolean;
  remainingText: string | null;
}

function formatTrainingCount(count: number) {
  const mod10 = Math.abs(count) % 10;
  const mod100 = Math.abs(count) % 100;

  if (mod10 === 1 && mod100 !== 11) {
    return `${count} занятие`;
  }

  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) {
    return `${count} занятия`;
  }

  return `${count} занятий`;
}

export function getParentSubscriptionAlert({
  hasSubscription,
  remaining,
  total,
  status,
  freezeStatus,
}: {
  hasSubscription: boolean;
  remaining: number | null;
  total: number | null;
  status?: string | null;
  freezeStatus?: string | null;
}): ParentSubscriptionAlert {
  if (!hasSubscription) {
    return {
      title: "Нет активного абонемента",
      description: "Нужно оформить или продлить абонемент перед тренировкой.",
      badge: "Срочно",
      tone: "danger",
      requiresAttention: true,
      remainingText: null,
    };
  }

  if (freezeStatus === "pending") {
    return {
      title: "Заморозка ожидает подтверждения",
      description: "Клуб проверяет заявку на заморозку абонемента.",
      badge: "Ожидает",
      tone: "warning",
      requiresAttention: true,
      remainingText: remaining !== null ? `Осталось ${formatTrainingCount(remaining)}` : null,
    };
  }

  if (status === "frozen") {
    return {
      title: "Абонемент на паузе",
      description: "Занятия по этому абонементу временно недоступны.",
      badge: "Пауза",
      tone: "warning",
      requiresAttention: true,
      remainingText: remaining !== null ? `Осталось ${formatTrainingCount(remaining)}` : null,
    };
  }

  if (status === "pending") {
    return {
      title: "Абонемент ожидает активации",
      description: "Клуб ещё подтверждает или активирует этот абонемент.",
      badge: "Ожидает",
      tone: "warning",
      requiresAttention: true,
      remainingText: remaining !== null ? `Осталось ${formatTrainingCount(remaining)}` : null,
    };
  }

  if (status === "cancelled") {
    return {
      title: "Абонемент отменён",
      description: "Оплата возвращена, оставшиеся занятия недоступны.",
      badge: "Возврат",
      tone: "danger",
      requiresAttention: true,
      remainingText: null,
    };
  }

  if (status === "expired" || (remaining !== null && remaining <= 0)) {
    return {
      title: "Абонемент закончился",
      description: "Нужно продлить абонемент перед следующей тренировкой.",
      badge: "Срочно",
      tone: "danger",
      requiresAttention: true,
      remainingText: "Осталось 0 занятий",
    };
  }

  if (remaining === 1) {
    return {
      title: "Последняя тренировка",
      description: "После неё абонемент нужно продлить.",
      badge: "Последняя",
      tone: "warning",
      requiresAttention: true,
      remainingText: "Осталось 1 занятие",
    };
  }

  if (remaining !== null && remaining <= 3) {
    const remainingText = `Осталось ${formatTrainingCount(remaining)}`;

    return {
      title: "Абонемент заканчивается",
      description: `${remainingText}. Лучше продлить заранее.`,
      badge: "Скоро",
      tone: "warning",
      requiresAttention: true,
      remainingText,
    };
  }

  if (remaining !== null && total !== null) {
    return {
      title: `${remaining} из ${total} тренировок`,
      description: "Абонемент активен.",
      badge: null,
      tone: "success",
      requiresAttention: false,
      remainingText: `${remaining} из ${total} тренировок`,
    };
  }

  return {
    title: "Безлимит",
    description: "Абонемент активен без лимита тренировок.",
    badge: null,
    tone: "success",
    requiresAttention: false,
    remainingText: "Безлимит",
  };
}

export function rankSubscriptionAlert(alert: ParentSubscriptionAlert) {
  if (!alert.requiresAttention) {
    return 0;
  }

  if (alert.tone === "danger") {
    return 3;
  }

  if (alert.title === "Последняя тренировка") {
    return 2;
  }

  return 1;
}
