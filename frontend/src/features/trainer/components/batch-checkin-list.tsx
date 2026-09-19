import { memo } from "react";
import { AlertTriangle, CheckCircle2, Clock3 } from "lucide-react";
import { cn } from "@/lib/utils";
import { AlertBadge } from "./alert-badge";
import type { SessionRosterStudent } from "../types";
import { getStudentCheckinBlockLabel } from "../pages/batch-checkin-helpers";

interface BatchCheckinListProps {
  readonly students: readonly SessionRosterStudent[];
  readonly onStudentSelect?: (student: SessionRosterStudent) => void;
}

const StudentRow = memo(function StudentRow({
  student,
  onSelect,
}: {
  student: SessionRosterStudent;
  onSelect?: (student: SessionRosterStudent) => void;
}) {
  const blockLabel = getStudentCheckinBlockLabel(student);
  const rosterBadges = getRosterBadges(student);
  const status = getStatusPresentation(student, blockLabel);
  const fullName = `${student.last_name} ${student.first_name}`;
  const rowContent = (
    <>
      <status.Icon size={20} className={cn("shrink-0", status.iconClassName)} />
      <div className="min-w-0 flex-1">
        <span className="text-[16px] leading-normal text-foreground">
          {fullName}
        </span>
        {rosterBadges.map((badge) => (
          <span
            key={badge}
            className="ml-2 inline-flex rounded-full bg-sky-50 px-2 py-0.5 text-[11px] font-medium text-sky-800 ring-1 ring-sky-200"
          >
            {badge}
          </span>
        ))}
        {blockLabel ? (
          <span className="ml-2 inline-flex rounded-full bg-amber-50 px-2 py-0.5 text-[11px] font-medium text-amber-800 ring-1 ring-amber-200">
            {blockLabel}
          </span>
        ) : null}
        <span className={cn("ml-2 inline-flex rounded-full px-2 py-0.5 text-[11px] font-medium ring-1", status.badgeClassName)}>
          {status.label}
        </span>
        {student.alerts.length > 0 && (
          <div className="mt-0.5 flex flex-wrap gap-1">
            {student.alerts.map((alert) => (
              <AlertBadge
                key={alert.type}
                type={alert.type}
                className="px-1.5 py-0 text-[11px]"
              />
            ))}
          </div>
        )}
      </div>
    </>
  );

  return onSelect ? (
    <button
      type="button"
      onClick={() => onSelect(student)}
      aria-label={`Открыть контекст ученика ${fullName}: ${status.label}`}
      className={cn(
        "flex min-h-[56px] w-full items-center gap-3 px-4 py-3 text-left",
        "border-b border-neutral-100 last:border-b-0",
        "transition-colors hover:bg-neutral-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--branding-accent)] focus-visible:ring-inset",
      )}
    >
      {rowContent}
    </button>
  ) : (
    <div
      className={cn(
        "flex min-h-[56px] items-center gap-3 px-4 py-3 text-left",
        "border-b border-neutral-100 last:border-b-0",
      )}
    >
      {rowContent}
    </div>
  );
});

function getStatusPresentation(student: SessionRosterStudent, blockLabel: string | null) {
  if (student.checkin_status === "checked_in") {
    return {
      Icon: CheckCircle2,
      label: "Отмечен",
      iconClassName: "text-[oklch(0.65_0.2_145)]",
      badgeClassName: "bg-emerald-50 text-emerald-800 ring-emerald-200",
    };
  }
  if (student.checkin_status === "blocked" || blockLabel !== null) {
    return {
      Icon: AlertTriangle,
      label: blockLabel ? `Недоступен: ${blockLabel.toLowerCase()}` : "Недоступен",
      iconClassName: "text-amber-600",
      badgeClassName: "bg-amber-50 text-amber-800 ring-amber-200",
    };
  }
  return {
    Icon: Clock3,
    label: "Ждет отметки",
    iconClassName: "text-muted-foreground",
    badgeClassName: "bg-neutral-100 text-neutral-700 ring-neutral-200",
  };
}

function getRosterBadges(student: SessionRosterStudent): string[] {
  const badges: string[] = [];
  if (student.created_from === "guest_visit" || student.is_guest_visit) {
    badges.push("Гость");
  }
  if (student.created_from === "student_self_booking") {
    badges.push("Сам записался");
  }
  return badges;
}

export function BatchCheckinList({
  students,
  onStudentSelect,
}: BatchCheckinListProps) {
  if (students.length === 0) {
    return (
      <div className="px-4 py-10 text-center">
        <p className="text-[15px] font-semibold text-foreground">
          В списке занятия пока нет учеников
        </p>
        <p className="ui-muted-detail">
          Добавленные гости и записанные ученики появятся здесь.
        </p>
      </div>
    );
  }

  return (
    <div className="flex flex-col">
      {students.map((student) => (
        <StudentRow
          key={student.id}
          student={student}
          onSelect={onStudentSelect}
        />
      ))}
    </div>
  );
}
