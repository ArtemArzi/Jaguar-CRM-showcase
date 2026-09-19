import { lazy, Suspense } from "react";
import { useNavigate } from "react-router";
import { LogOut, User } from "lucide-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Skeleton } from "@/components/ui/skeleton";
import { Button } from "@/components/ui/button";
import { StudentSectionTitle } from "@/features/student/components/student-section-title";
import { StudentSurfaceCard } from "@/features/student/components/student-surface-card";
import { useAuthStore } from "@/features/auth/auth-store";
import apiClient from "@/api/custom-fetch";
import { DocumentChecklist } from "../components/document-checklist";
import { StudentProfileSummary } from "../components/student-profile-summary";
import {
  normalizeChecklistItems,
  type StudentChecklistItem,
} from "../lib/student-normalizers";
import { getInitials } from "@/lib/utils";

const NotificationPreferences = lazy(
  () => import("@/features/notifications/components/notification-preferences"),
);

interface StudentInfo {
  id: number;
  first_name: string;
  last_name: string;
  phone: string;
  email: string;
  status: string;
  is_child: boolean;
  date_of_birth: string | null;
}

interface ChecklistItem {
  document_type: {
    id: number;
    name: string;
    description: string;
    is_required: boolean;
    is_active: boolean;
  };
  is_provided: boolean;
  has_file: boolean;
}

function ProfileSkeleton() {
  return (
    <div className="space-y-5 px-4 pb-24 pt-4">
      <div className="rounded-[28px] bg-white/90 p-5 shadow-sm ring-1 ring-black/6 backdrop-blur-sm">
        <div className="flex items-center gap-4">
          <Skeleton className="h-16 w-16 rounded-full" />
          <div className="space-y-2">
            <Skeleton className="h-5 w-40" />
            <Skeleton className="h-4 w-24" />
          </div>
        </div>
      </div>
      {Array.from({ length: 3 }).map((_, i) => (
        <Skeleton key={i} className="h-20 w-full rounded-2xl" />
      ))}
    </div>
  );
}

const STATUS_LABELS: Record<string, string> = {
  lead: "Лид",
  trial: "Пробное",
  active: "Активен",
  at_risk: "В риске",
  churned: "Не посещает 30+ дней",
  lost: "Потерян",
};

export default function StudentProfile() {
  const studentId = useAuthStore((s) => s.studentId);
  const bootstrapStatus = useAuthStore((s) => s.studentBootstrapStatus);
  const logout = useAuthStore((s) => s.logout);
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const { data: student, isLoading: studentLoading } = useQuery({
    queryKey: ["student", "me", studentId],
    queryFn: () =>
      apiClient.get<StudentInfo>("/students/me/").then((r) => r.data),
    enabled: bootstrapStatus === "resolved" && !!studentId,
    staleTime: 5 * 60_000,
  });

  const {
    data: checklist = [],
    isLoading: checklistLoading,
    isError: checklistError,
  } = useQuery<
    StudentChecklistItem[]
  >({
    queryKey: ["student", "checklist", studentId],
    queryFn: async () => {
      const res = await apiClient.get(
        `/documents/students/${studentId}/checklist/`,
      );
      const raw = res.data;
      const items = Array.isArray(raw)
        ? (raw as ChecklistItem[])
        : ((raw.items ?? []) as ChecklistItem[]);
      return normalizeChecklistItems(items);
    },
    enabled: bootstrapStatus === "resolved" && !!studentId,
    staleTime: 5 * 60_000,
  });

  const handleLogout = () => {
    logout();
    navigate("/login");
  };

  const loading = studentLoading || checklistLoading;
  if (bootstrapStatus === "idle" || bootstrapStatus === "loading") {
    return <ProfileSkeleton />;
  }

  if (!studentId || loading) return <ProfileSkeleton />;

  if (!student) {
    return (
      <div className="p-6 pb-20 space-y-4">
        <p className="ui-error-14">
          Не удалось загрузить профиль. Попробуйте обновить страницу.
        </p>
      </div>
    );
  }

  const displayName = student
    ? `${student.first_name} ${student.last_name}`.trim()
    : "";
  const statusLabel = STATUS_LABELS[student.status] ?? student.status;
  const profileRole = student.is_child ? "Ребёнок" : "Взрослый";
  const initials = getInitials(displayName || "Ученик");

  return (
    <div className="min-h-full bg-[radial-gradient(circle_at_top,_rgba(0,0,0,0.03),_transparent_40%)] px-4 pb-24 pt-4">
      <div className="space-y-4">
        {/* Identity hero */}
        <StudentSurfaceCard className="relative overflow-hidden p-4.5">
          <div
            className="absolute inset-x-0 top-0 h-1.5"
            style={{ backgroundColor: "var(--branding-accent)" }}
          />
          <div className="flex items-start gap-4">
            <div
              className="flex h-16 w-16 shrink-0 items-center justify-center rounded-2xl text-[22px] font-semibold text-white shadow-sm"
              style={{ backgroundColor: "var(--branding-accent)" }}
            >
              {displayName ? initials : <User className="size-7" />}
            </div>
            <div className="min-w-0 flex-1">
              <p className="ui-overline">
                Личный кабинет
              </p>
              <h1 className="mt-1 text-[26px] font-semibold leading-tight text-foreground">
                {displayName || "Ученик"}
              </h1>
              <div className="mt-3 flex flex-wrap gap-2">
                <span className="inline-flex items-center rounded-full bg-[var(--branding-accent)]/10 px-3 py-1 text-[12px] font-medium text-foreground">
                  {statusLabel}
                </span>
                <span className="inline-flex items-center rounded-full bg-black/5 px-3 py-1 text-[12px] font-medium text-foreground">
                  {profileRole}
                </span>
              </div>
            </div>
          </div>
          <div className="mt-3.5 grid grid-cols-1 gap-2 text-[14px] text-muted-foreground sm:grid-cols-2">
            <div className="rounded-2xl bg-black/5 px-3 py-2">
              <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">
                Телефон
              </p>
              <p className="mt-1 font-medium text-foreground">
                {student.phone || "Не указан"}
              </p>
            </div>
            <div className="rounded-2xl bg-black/5 px-3 py-2">
              <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">
                Email
              </p>
              <p className="mt-1 truncate font-medium text-foreground">
                {student.email || "Не указан"}
              </p>
            </div>
          </div>
        </StudentSurfaceCard>

        {student && <StudentProfileSummary student={student} />}

        <div className="space-y-3">
          {checklistError ? (
            <StudentSurfaceCard className="p-3.5">
              <StudentSectionTitle eyebrow="Профиль" title="Документы" />
              <p className="mt-3 rounded-2xl border border-red-200 bg-red-50 px-3 py-2 text-[14px] text-red-700">
                Не удалось загрузить документы. Попробуйте обновить страницу.
              </p>
            </StudentSurfaceCard>
          ) : (
            <DocumentChecklist
              items={checklist}
              studentId={studentId}
              onUploadComplete={() =>
                queryClient.invalidateQueries({
                  queryKey: ["student", "checklist", studentId],
                })
              }
            />
          )}
        </div>

        <StudentSurfaceCard className="p-3.5">
          <StudentSectionTitle eyebrow="Профиль" title="Уведомления" />
          <p className="mt-2 text-[13px] leading-5 text-muted-foreground">
            Настройки уведомлений помогут не пропускать важные изменения по
            расписанию, документам и сообщениями от команды.
          </p>
          <div className="mt-3">
            <Suspense fallback={null}>
              <NotificationPreferences role="student" />
            </Suspense>
          </div>
        </StudentSurfaceCard>

        <StudentSurfaceCard className="border-red-100 p-3.5">
          <StudentSectionTitle eyebrow="Аккаунт" title="Выход" />
          <p className="mt-2 text-[13px] leading-5 text-muted-foreground">
            Используйте выход, если хотите завершить сеанс на этом устройстве.
          </p>
          <Button
            variant="destructive"
            className="mt-4 w-full min-h-[44px]"
            onClick={handleLogout}
          >
            <LogOut className="size-4 mr-2" />
            Выйти
          </Button>
        </StudentSurfaceCard>
      </div>
    </div>
  );
}
