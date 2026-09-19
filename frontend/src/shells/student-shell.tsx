import { Suspense, useCallback, useEffect, useState } from "react";
import { Outlet } from "react-router";
import { BrandingProvider } from "@/features/branding/branding-provider";
import { StudentBrandedHeader } from "@/features/student/components/student-branded-header";
import { StudentBottomNav } from "@/features/student/components/student-bottom-nav";
import { useAuthStore } from "@/features/auth/auth-store";
import apiClient from "@/api/custom-fetch";
import { Button } from "@/components/ui/button";
import { StudentSurfaceCard } from "@/features/student/components/student-surface-card";
import { CardContent } from "@/components/ui/card";

function StudentLoading() {
  return (
    <div className="flex items-center justify-center h-screen bg-[#F5F2ED]">
      Loading...
    </div>
  );
}

function useResolveStudentId() {
  const role = useAuthStore((s) => s.role);
  const studentId = useAuthStore((s) => s.studentId);
  const bootstrapStatus = useAuthStore((s) => s.studentBootstrapStatus);
  const studentBootstrapError = useAuthStore((s) => s.studentBootstrapError);
  const startStudentBootstrap = useAuthStore((s) => s.startStudentBootstrap);
  const resolveStudentBootstrap = useAuthStore((s) => s.resolveStudentBootstrap);
  const failStudentBootstrap = useAuthStore((s) => s.failStudentBootstrap);
  const [attempt, setAttempt] = useState(0);

  const retry = useCallback(() => {
    setAttempt((value) => value + 1);
  }, []);

  useEffect(() => {
    if (role !== "student") return;
    if (studentId !== null) {
      resolveStudentBootstrap(studentId);
      return;
    }

    let active = true;
    startStudentBootstrap();

    apiClient
      .get<{ id: number }>("/students/me/")
      .then((res) => {
        if (!active) return;
        resolveStudentBootstrap(res.data.id);
      })
      .catch(() => {
        if (!active) return;
        failStudentBootstrap(
          "Попробуйте снова. Пока данные ученика не подтверждены, разделы кабинета скрыты.",
        );
      });

    return () => {
      active = false;
    };
  }, [
    attempt,
    failStudentBootstrap,
    resolveStudentBootstrap,
    role,
    studentId,
    startStudentBootstrap,
  ]);

  return {
    bootstrapStatus,
    studentBootstrapError,
    retry,
  };
}

function StudentBootstrapError({
  message,
  onRetry,
}: {
  message: string;
  onRetry: () => void;
}) {
  return (
    <div className="px-5 pt-5">
      <StudentSurfaceCard>
        <CardContent className="space-y-3 py-5 text-center">
          <p className="ui-overline">
            Кабинет ученика
          </p>
          <p className="text-[20px] font-semibold">Не удалось открыть кабинет ученика</p>
          <p className="ui-body-muted">{message}</p>
          <Button className="min-h-[44px] w-full" onClick={onRetry}>
            Повторить
          </Button>
        </CardContent>
      </StudentSurfaceCard>
    </div>
  );
}

export default function StudentShell() {
  const { bootstrapStatus, studentBootstrapError, retry } = useResolveStudentId();

  return (
    <BrandingProvider>
      <div className="min-h-screen bg-[#F5F2ED]">
        <StudentBrandedHeader />
        <div className="pb-28">
          {bootstrapStatus === "failed" ? (
            <StudentBootstrapError
              message={
                studentBootstrapError ??
                "Попробуйте снова. Пока данные ученика не подтверждены, разделы кабинета скрыты."
              }
              onRetry={retry}
            />
          ) : (
            <Suspense fallback={<StudentLoading />}>
              <Outlet />
            </Suspense>
          )}
        </div>
        <StudentBottomNav />
      </div>
    </BrandingProvider>
  );
}
