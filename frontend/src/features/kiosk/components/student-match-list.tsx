import { ChevronRight } from "lucide-react";
import type { StudentMatch } from "@/features/kiosk/lib/kiosk-api";

interface StudentMatchListProps {
  matches: StudentMatch[];
  onSelect: (student: StudentMatch) => void;
}

// Predefined avatar color palette (brown/olive/grey tones per Pencil K-03)
const AVATAR_COLORS = [
  "#8B7355",
  "#6B7B3A",
  "#7B6B8A",
  "#5C7A7A",
  "#9B7B5A",
  "#6A6A7A",
  "#7A8A5A",
  "#8A6A5A",
];

function getInitials(firstName: string, lastName: string): string {
  return `${(firstName[0] || "").toUpperCase()}${(lastName[0] || "").toUpperCase()}`;
}

function getAvatarColor(index: number): string {
  return AVATAR_COLORS[index % AVATAR_COLORS.length];
}

function formatSubscriptionContext(student: StudentMatch): string {
  if (!student.subscription_name) return "";
  if (student.trainings_left === null || student.trainings_left === undefined) {
    return `${student.subscription_name} · Безлимит`;
  }
  return `${student.subscription_name} · ${student.trainings_left} тр.`;
}

function buildStudentContext(student: StudentMatch): string {
  return [
    student.group_name,
    student.grade_name,
    formatSubscriptionContext(student),
  ]
    .filter(Boolean)
    .join(" · ");
}

export function StudentMatchList({ matches, onSelect }: StudentMatchListProps) {
  return (
    <ul className="ui-col-2" aria-label="Список учеников">
      {matches.map((student, i) => {
        const context = buildStudentContext(student);

        return (
          <li key={student.id}>
            <button
              type="button"
              onClick={() => onSelect(student)}
              className="flex min-h-16 w-full items-center gap-4 rounded-lg bg-[#2A2A2A] px-4 py-3 text-left transition-transform duration-[60ms] ease-out active:scale-[0.98] motion-reduce:transition-none motion-reduce:active:scale-100"
              style={{
                animationDelay: `${i * 50}ms`,
              }}
            >
              {/* Colored initials avatar */}
              <div
                className="flex h-12 w-12 shrink-0 items-center justify-center rounded-full text-base font-semibold text-white"
                style={{ backgroundColor: getAvatarColor(i) }}
              >
                {getInitials(student.first_name, student.last_name)}
              </div>

              {/* Name + safe context */}
              <div className="flex-1 overflow-hidden">
                <div className="truncate text-base font-semibold text-white">
                  {student.first_name} {student.last_name}
                </div>
                {context && (
                  <div className="truncate text-sm text-neutral-400">
                    {context}
                  </div>
                )}
              </div>

              <ChevronRight className="h-5 w-5 shrink-0 text-neutral-500" />
            </button>
          </li>
        );
      })}
    </ul>
  );
}
