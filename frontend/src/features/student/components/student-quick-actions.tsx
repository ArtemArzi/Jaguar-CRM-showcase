import { CalendarDays, CheckCircle2, MessageSquareText, UserRound } from "lucide-react";
import { Link } from "react-router";
import { StudentSectionTitle } from "./student-section-title";

const ACTIONS = [
  {
    to: "/student/schedule",
    label: "Расписание",
    description: "Ближайшие тренировки и переносы",
    icon: CalendarDays,
    span: "",
  },
  {
    to: "/student/attendance",
    label: "Посещения",
    description: "Отметки и журнал занятий",
    icon: CheckCircle2,
    span: "",
  },
  {
    to: "/student/feedback",
    label: "Опрос",
    description: "Обратная связь после тренировки",
    icon: MessageSquareText,
    span: "",
  },
  {
    to: "/student/profile",
    label: "Профиль",
    description: "Данные, документы и уведомления",
    icon: UserRound,
    span: "",
  },
] as const;

export function StudentQuickActions() {
  return (
    <section className="space-y-2.5">
      <StudentSectionTitle
        eyebrow="Быстрые действия"
        title="Главные разделы под рукой"
      />

      <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
        {ACTIONS.map(({ to, label, description, icon: Icon, span }) => (
          <Link
            key={to}
            to={to}
            className={`group rounded-[22px] border border-black/6 bg-white/92 px-3.5 py-3.5 shadow-sm ring-1 ring-white/55 transition active:scale-[0.99] ${span}`}
          >
            <div className="flex items-start gap-3">
              <div
                className="flex h-11 w-11 shrink-0 items-center justify-center rounded-2xl"
                style={{
                  background:
                    "linear-gradient(0deg, rgba(255,255,255,0.9), rgba(255,255,255,0.9)), var(--branding-accent)",
                }}
              >
                <Icon size={18} style={{ color: "var(--branding-accent)" }} />
              </div>

              <div className="min-w-0 flex-1">
                <p className="text-[15px] font-semibold leading-tight text-foreground">
                  {label}
                </p>
                <p className="mt-1 text-[12px] leading-5 text-muted-foreground">
                  {description}
                </p>
              </div>
            </div>
          </Link>
        ))}
      </div>
    </section>
  );
}
