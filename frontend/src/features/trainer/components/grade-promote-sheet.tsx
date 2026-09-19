import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Award } from "lucide-react";
import apiClient from "@/api/custom-fetch";
import { getApiError } from "@/lib/utils";
import type { GradeProgress } from "../types";

interface GradePromoteSheetProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  studentId: number;
  studentName: string;
  gradeProgress: GradeProgress | null;
}

interface GradeItem {
  id: number;
  name: string;
  order: number;
  min_trainings: number;
}

export function GradePromoteSheet({
  open,
  onOpenChange,
  studentId,
  studentName,
  gradeProgress,
}: GradePromoteSheetProps) {
  const queryClient = useQueryClient();
  const [selectedGradeId, setSelectedGradeId] = useState<number | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const studentGradeId = gradeProgress?.student_grade_id ?? null;
  const currentGrade = gradeProgress?.current_grade ?? null;
  const nextGrade = gradeProgress?.next_grade ?? null;
  const trainingsToNext = gradeProgress?.trainings_to_next ?? null;
  const gradeSystemName = gradeProgress?.grade_system_name ?? null;
  const gradeSystemId = gradeProgress?.grade_system_id ?? null;

  const { data: allGrades } = useQuery<GradeItem[]>({
    queryKey: ["grades", gradeSystemId],
    queryFn: () =>
      apiClient
        .get(`/grades/systems/${gradeSystemId}/grades/`)
        .then((r) => r.data.items ?? r.data),
    staleTime: 5 * 60_000,
    enabled: open && !!gradeSystemId,
  });

  const sortedGrades = allGrades
    ? [...allGrades].sort((a, b) => a.order - b.order)
    : [];

  const currentOrder = currentGrade?.order ?? -1;

  const promoteMutation = useMutation({
    mutationFn: () =>
      apiClient.post(`/grades/student-grades/${studentGradeId}/promote/`, {
        new_grade_id: selectedGradeId,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["student", String(studentId), "grades"],
      });
      setSelectedGradeId(null);
      setErrorMsg(null);
      onOpenChange(false);
    },
    onError: (error: unknown) => {
      setErrorMsg(getApiError(error, "Ошибка при повышении грейда"));
    },
  });

  function handleOpenChange(nextOpen: boolean) {
    if (!nextOpen) {
      setSelectedGradeId(null);
      setErrorMsg(null);
    }
    onOpenChange(nextOpen);
  }

  // Edge case: no grade system assigned
  if (!gradeProgress || !gradeSystemName) {
    return (
      <Sheet open={open} onOpenChange={handleOpenChange}>
        <SheetContent side="bottom" showCloseButton={false}>
          <SheetHeader>
            <SheetTitle>Повышение грейда</SheetTitle>
            <SheetDescription>{studentName}</SheetDescription>
          </SheetHeader>
          <div className="ui-sheet-body">
            <p className="ui-muted-14">
              Грейд-система не назначена
            </p>
            <Button
              variant="outline"
              className="w-full"
              onClick={() => handleOpenChange(false)}
            >
              Закрыть
            </Button>
          </div>
        </SheetContent>
      </Sheet>
    );
  }

  // Edge case: no student_grade
  if (!studentGradeId) {
    return (
      <Sheet open={open} onOpenChange={handleOpenChange}>
        <SheetContent side="bottom" showCloseButton={false}>
          <SheetHeader>
            <SheetTitle>Повышение грейда</SheetTitle>
            <SheetDescription>{studentName}</SheetDescription>
          </SheetHeader>
          <div className="ui-sheet-body">
            <p className="ui-muted-14">
              Грейд не назначен ученику
            </p>
            <Button
              variant="outline"
              className="w-full"
              onClick={() => handleOpenChange(false)}
            >
              Закрыть
            </Button>
          </div>
        </SheetContent>
      </Sheet>
    );
  }

  // Edge case: already at max grade
  if (!nextGrade && currentGrade) {
    return (
      <Sheet open={open} onOpenChange={handleOpenChange}>
        <SheetContent side="bottom" showCloseButton={false}>
          <SheetHeader>
            <SheetTitle>Повышение грейда</SheetTitle>
            <SheetDescription>{studentName}</SheetDescription>
          </SheetHeader>
          <div className="ui-sheet-body">
            <div className="ui-row-2">
              <Award size={20} className="text-amber-600" />
              <p className="text-[14px] text-foreground">
                Ученик уже на максимальном грейде:{" "}
                <span className="font-semibold">{currentGrade.name}</span>
              </p>
            </div>
            <Button
              variant="outline"
              className="w-full"
              onClick={() => handleOpenChange(false)}
            >
              Закрыть
            </Button>
          </div>
        </SheetContent>
      </Sheet>
    );
  }

  return (
    <Sheet open={open} onOpenChange={handleOpenChange}>
      <SheetContent side="bottom" showCloseButton={false}>
        <SheetHeader>
          <SheetTitle>Повышение грейда</SheetTitle>
          <SheetDescription>
            {studentName} -- {gradeSystemName}
          </SheetDescription>
        </SheetHeader>

        <div className="ui-sheet-body">
          {/* Current grade */}
          {currentGrade && (
            <div className="ui-row-2">
              <span className="ui-muted-14">Текущий:</span>
              <Badge variant="outline">{currentGrade.name}</Badge>
            </div>
          )}

          {/* Grade list for selection */}
          <div>
            <p className="text-[14px] text-muted-foreground mb-2">
              Выберите новый грейд
            </p>
            <div className="flex flex-col gap-2 max-h-[40vh] overflow-y-auto">
              {sortedGrades.map((grade) => {
                const isCurrent = grade.id === currentGrade?.id;
                const isLowerOrEqual = grade.order <= currentOrder;
                const isDisabled = isCurrent || isLowerOrEqual;
                const isRecommended =
                  nextGrade &&
                  grade.id === nextGrade.id &&
                  trainingsToNext != null &&
                  trainingsToNext <= 0;

                return (
                  <button
                    key={grade.id}
                    type="button"
                    disabled={isDisabled}
                    onClick={() => {
                      setSelectedGradeId(grade.id);
                      setErrorMsg(null);
                    }}
                    className={`flex items-center justify-between rounded-xl p-3 text-left ring-1 transition-colors ${
                      isDisabled
                        ? "opacity-40 cursor-not-allowed ring-foreground/5 bg-muted"
                        : selectedGradeId === grade.id
                          ? "bg-[var(--branding-accent)]/10 ring-[var(--branding-accent)]"
                          : "bg-white ring-foreground/5 active:bg-muted"
                    }`}
                  >
                    <div className="ui-row-2">
                      <span className="text-[14px] font-medium text-foreground">
                        {grade.name}
                      </span>
                      {isCurrent && (
                        <Badge variant="secondary" className="text-[11px]">
                          Текущий
                        </Badge>
                      )}
                      {isRecommended && (
                        <Badge
                          variant="default"
                          className="bg-green-500 text-white text-[11px]"
                        >
                          Рекомендация
                        </Badge>
                      )}
                    </div>
                    <span className="ui-muted-12">
                      #{grade.order}
                    </span>
                  </button>
                );
              })}
            </div>
          </div>

          {/* Error */}
          {errorMsg && (
            <p className="ui-error-center">{errorMsg}</p>
          )}

          {/* Submit */}
          <Button
            className="ui-brand-button"
            disabled={!selectedGradeId || promoteMutation.isPending}
            onClick={() => promoteMutation.mutate()}
          >
            {promoteMutation.isPending ? "Отправка..." : "Повысить грейд"}
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  );
}
