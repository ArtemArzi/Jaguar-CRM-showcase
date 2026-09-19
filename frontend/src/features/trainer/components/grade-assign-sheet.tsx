import { useState, useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import apiClient from "@/api/custom-fetch";

interface GradeSystem {
  id: number;
  discipline: string;
  is_active: boolean;
}

interface Grade {
  id: number;
  name: string;
  order: number;
  min_trainings: number;
}

interface GradeAssignSheetProps {
  readonly open: boolean;
  readonly onOpenChange: (open: boolean) => void;
  readonly studentId: number;
  readonly assignedSystemIds: readonly number[];
}

export function GradeAssignSheet({
  open,
  onOpenChange,
  studentId,
  assignedSystemIds,
}: GradeAssignSheetProps) {
  const queryClient = useQueryClient();
  const [selectedSystem, setSelectedSystem] = useState<number | null>(null);
  const [selectedGrade, setSelectedGrade] = useState<number | null>(null);

  const { data: systems } = useQuery<GradeSystem[]>({
    queryKey: ["grade-systems"],
    queryFn: () => apiClient.get("/grades/systems/").then((r) => r.data),
    staleTime: 5 * 60_000,
    enabled: open,
  });

  const { data: grades } = useQuery<Grade[]>({
    queryKey: ["grade-system", selectedSystem, "grades"],
    queryFn: () =>
      apiClient.get(`/grades/systems/${selectedSystem}/grades/`).then((r) => r.data),
    staleTime: 5 * 60_000,
    enabled: !!selectedSystem,
  });

  const availableSystems = useMemo(
    () => systems?.filter((s) => s.is_active && !assignedSystemIds.includes(s.id)) ?? [],
    [systems, assignedSystemIds],
  );

  const assignMutation = useMutation({
    mutationFn: () =>
      apiClient.post("/grades/student-grades/", {
        student_id: studentId,
        grade_system_id: selectedSystem,
        initial_grade_id: selectedGrade,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["student", String(studentId), "grades"] });
      handleClose();
    },
  });

  function handleClose() {
    setSelectedSystem(null);
    setSelectedGrade(null);
    onOpenChange(false);
  }

  return (
    <Sheet open={open} onOpenChange={handleClose}>
      <SheetContent side="bottom" showCloseButton={false} className="rounded-t-2xl">
        <SheetHeader>
          <SheetTitle>Добавить дисциплину</SheetTitle>
        </SheetHeader>
        <div className="flex flex-col gap-3 px-4 pb-4">
          {/* Step 1: select discipline */}
          {!selectedSystem ? (
            <>
              {availableSystems.length === 0 ? (
                <p className="text-[14px] text-muted-foreground text-center py-4">
                  Все дисциплины уже назначены
                </p>
              ) : (
                availableSystems.map((s) => (
                  <button
                    key={s.id}
                    type="button"
                    onClick={() => setSelectedSystem(s.id)}
                    className="flex items-center p-4 rounded-lg text-left border border-border bg-background hover:bg-muted/50 transition-colors min-h-[52px]"
                  >
                    <span className="text-[16px] font-medium text-foreground">
                      {s.discipline}
                    </span>
                  </button>
                ))
              )}
            </>
          ) : (
            <>
              {/* Step 2: select initial grade (optional) */}
              <p className="ui-muted-14">
                Начальный уровень (опционально)
              </p>
              <button
                type="button"
                onClick={() => setSelectedGrade(null)}
                className={`flex items-center p-4 rounded-lg text-left border transition-colors min-h-[48px] ${
                  selectedGrade === null
                    ? "border-[var(--branding-accent)] bg-[var(--branding-accent)]/5"
                    : "border-border bg-background"
                }`}
              >
                <span className="text-[14px] text-foreground">С нуля (без уровня)</span>
              </button>
              {grades?.map((g) => (
                <button
                  key={g.id}
                  type="button"
                  onClick={() => setSelectedGrade(g.id)}
                  className={`flex items-center justify-between p-4 rounded-lg text-left border transition-colors min-h-[48px] ${
                    selectedGrade === g.id
                      ? "border-[var(--branding-accent)] bg-[var(--branding-accent)]/5"
                      : "border-border bg-background"
                  }`}
                >
                  <span className="text-[14px] font-medium text-foreground">{g.name}</span>
                  {g.min_trainings > 0 && (
                    <span className="ui-muted-12">от {g.min_trainings} тр.</span>
                  )}
                </button>
              ))}

              <div className="flex gap-2 mt-2">
                <Button
                  variant="outline"
                  onClick={() => setSelectedSystem(null)}
                  className="flex-1"
                >
                  Назад
                </Button>
                <Button
                  onClick={() => assignMutation.mutate()}
                  disabled={assignMutation.isPending}
                  className="flex-1 bg-[var(--branding-accent)] text-white hover:opacity-90"
                >
                  {assignMutation.isPending ? "Сохранение..." : "Назначить"}
                </Button>
              </div>

              {assignMutation.isError && (
                <p className="ui-error-center">
                  Не удалось назначить грейд
                </p>
              )}
            </>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
