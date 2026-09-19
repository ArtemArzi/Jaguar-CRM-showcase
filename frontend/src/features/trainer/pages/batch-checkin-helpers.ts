import type { QueryClient } from "@tanstack/react-query";
import type {
  BatchCheckinResultOut,
  ScheduleOccurrenceOut,
  ScheduleOut,
  SubmittedStudentWithCheckin,
  StudentWithAlerts,
} from "../types";

const FROZEN_BLOCK_REASONS = new Set([
  "frozen",
  "enrollment_frozen",
  "frozen_enrollment",
  "frozen_subscription",
]);

export function resolveOccurrenceTimeRange(
  occurrence?: Pick<ScheduleOccurrenceOut, "effective_start_time" | "effective_end_time">,
  schedule?: Pick<ScheduleOut, "start_time" | "end_time">,
) {
  return {
    startTime: occurrence?.effective_start_time ?? schedule?.start_time ?? "",
    endTime: occurrence?.effective_end_time ?? schedule?.end_time ?? "",
  };
}

export function getStudentCheckinBlockLabel(student: StudentWithAlerts): string | null {
  if (
    student.enrollment_status === "frozen" ||
    (student.checkin_blocked_reason &&
      FROZEN_BLOCK_REASONS.has(student.checkin_blocked_reason))
  ) {
    return "Заморожен";
  }

  return student.checkin_blocked_reason ? "Недоступен" : null;
}

export function isStudentCheckinBlocked(student: StudentWithAlerts): boolean {
  return getStudentCheckinBlockLabel(student) !== null;
}

export function selectEligibleStudents(
  students: readonly StudentWithAlerts[] | undefined,
): StudentWithAlerts[] {
  return (students ?? []).filter((student) => !isStudentCheckinBlocked(student));
}

export function selectSubmittableCheckedIds(
  checkedIds: ReadonlySet<number>,
  students: readonly StudentWithAlerts[] | undefined,
): Set<number> {
  if (!students) return new Set(checkedIds);

  const blockedIds = new Set(
    students
      .filter((student) => isStudentCheckinBlocked(student))
      .map((student) => student.id),
  );

  return new Set(
    Array.from(checkedIds).filter((studentId) => !blockedIds.has(studentId)),
  );
}

export function buildDefaultCheckedIds(
  students: readonly StudentWithAlerts[] | undefined,
  checkinStatus: {
    student_ids: readonly number[];
    has_group_session: boolean;
  } | undefined,
): Set<number> {
  if (!students || !checkinStatus) return new Set<number>();
  return selectSubmittableCheckedIds(new Set(checkinStatus.student_ids), students);
}

export function buildSubmittedStudents(
  students: readonly StudentWithAlerts[] | undefined,
  checkins: Readonly<BatchCheckinResultOut["checkins"]>,
  checkedIds: ReadonlySet<number>,
): SubmittedStudentWithCheckin[] {
  if (!students) return [];

  const checkinsByStudentId = new Map(
    checkins.map((checkin) => [checkin.student_id, checkin]),
  );

  return students.flatMap((student) => {
    if (!checkedIds.has(student.id)) return [];

    const checkin = checkinsByStudentId.get(student.id);
    if (!checkin) return [];

    return [
      {
        ...student,
        alerts: checkin.alerts,
        checkin,
      },
    ];
  });
}

export function invalidateBatchCheckinQueries(
  queryClient: Pick<QueryClient, "invalidateQueries">,
  {
    scheduleId,
    checkinDate,
    studentIds,
    trainerId,
  }: {
    scheduleId: string;
    checkinDate: string;
    studentIds: readonly number[];
    trainerId?: number | null;
  },
) {
  queryClient.invalidateQueries({ queryKey: ["schedules", "unclosed"] });
  queryClient.invalidateQueries({ queryKey: ["schedules", "by-date", checkinDate] });
  queryClient.invalidateQueries({ queryKey: ["schedules", "today", checkinDate] });
  queryClient.invalidateQueries({ queryKey: ["schedule", scheduleId] });
  queryClient.invalidateQueries({ queryKey: ["schedule", scheduleId, "session-detail", checkinDate] });
  queryClient.invalidateQueries({ queryKey: ["schedule", scheduleId, "checked-in"] });
  queryClient.invalidateQueries({ queryKey: ["schedule", scheduleId, "students"] });

  for (const studentId of studentIds) {
    queryClient.invalidateQueries({ queryKey: ["student", studentId, "subscriptions"] });
    queryClient.invalidateQueries({ queryKey: ["student", studentId, "checkins"] });
    queryClient.invalidateQueries({ queryKey: ["student", studentId, "grades"] });

    const routeStudentId = String(studentId);
    queryClient.invalidateQueries({ queryKey: ["student", routeStudentId] });
    queryClient.invalidateQueries({ queryKey: ["student", routeStudentId, "subscriptions"] });
    queryClient.invalidateQueries({ queryKey: ["student", routeStudentId, "checkins"] });
    queryClient.invalidateQueries({ queryKey: ["student", routeStudentId, "grades"] });
  }

  if (trainerId) {
    queryClient.invalidateQueries({ queryKey: ["trainer", "earnings", trainerId] });
    queryClient.invalidateQueries({ queryKey: ["trainer-earnings", trainerId] });
    queryClient.invalidateQueries({ queryKey: ["trainer-earnings-summary", trainerId] });
    queryClient.invalidateQueries({ queryKey: ["retention-tasks", trainerId] });
  }
}
