export const STATUS_CONFIG: Record<
  string,
  {
    label: string;
    variant: "default" | "secondary" | "destructive" | "outline";
  }
> = {
  lead: { label: "Заявка", variant: "secondary" },
  trial: { label: "Пробный", variant: "secondary" },
  active: { label: "Активный", variant: "default" },
  at_risk: { label: "В риске", variant: "destructive" },
  churned: { label: "Ушёл", variant: "outline" },
  lost: { label: "Потерян", variant: "outline" },
};

export const TASK_STATUS_LABELS: Record<string, string> = {
  open: "Новая",
  in_progress: "В работе",
  snoozed: "Отложена",
  closed: "Закрыта",
};

export const SECTION_LABELS = {
  overdue: "Просрочено",
  today: "На сегодня",
  snoozed: "Отложено",
} as const;
