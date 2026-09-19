import { formatDateRu } from "@/lib/locale";
import { StudentSectionTitle } from "./student-section-title";
import { StudentSurfaceCard } from "./student-surface-card";

interface StudentProfileSummaryProps {
  student: {
    id: number;
    first_name: string;
    last_name: string;
    phone: string;
    email: string;
    status: string;
    is_child: boolean;
    date_of_birth: string | null;
  };
}

const STATUS_LABELS: Record<string, string> = {
  lead: "Лид",
  trial: "Пробное",
  active: "Активен",
  at_risk: "В риске",
  churned: "Не посещает 30+ дней",
  lost: "Потерян",
};

export function StudentProfileSummary({
  student,
}: StudentProfileSummaryProps) {
  const rows = [
    { label: "Статус", value: STATUS_LABELS[student.status] ?? student.status },
    { label: "Телефон", value: student.phone || "Не указан" },
    { label: "Email", value: student.email || "Не указан" },
    { label: "Тип", value: student.is_child ? "Ребёнок" : "Взрослый" },
    {
      label: "Дата рождения",
      value: student.date_of_birth
        ? formatDateRu(student.date_of_birth)
        : "Не указана",
    },
  ];

  return (
    <div className="space-y-3">
      <StudentSectionTitle eyebrow="Профиль" title="Личные данные" />
      <StudentSurfaceCard className="p-3.5">
        <div className="space-y-2.5">
          {rows.map((row) => (
            <div
              key={row.label}
              className="flex items-start justify-between gap-4 rounded-xl px-0 py-0 text-[13px]"
            >
              <span className="text-neutral-500">{row.label}</span>
              <span className="text-right font-medium text-neutral-900">
                {row.value}
              </span>
            </div>
          ))}
        </div>
      </StudentSurfaceCard>
    </div>
  );
}
