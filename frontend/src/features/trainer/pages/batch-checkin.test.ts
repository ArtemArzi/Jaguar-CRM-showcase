import { describe, expect, it } from "vitest";
import {
  buildDefaultCheckedIds,
  invalidateBatchCheckinQueries,
  resolveOccurrenceTimeRange,
  selectEligibleStudents,
  selectSubmittableCheckedIds,
} from "./batch-checkin-helpers";

describe("resolveOccurrenceTimeRange", () => {
  it("prefers server occurrence times over raw schedule time", () => {
    const result = resolveOccurrenceTimeRange({
      effective_start_time: "20:00:00",
      effective_end_time: "21:00:00",
    }, {
      start_time: "18:00:00",
      end_time: "19:00:00",
    });

    expect(result).toEqual({
      startTime: "20:00:00",
      endTime: "21:00:00",
    });
  });
});

describe("session status helpers", () => {
  const students = [
    {
      id: 1,
      first_name: "Masha",
      last_name: "Ivanova",
      alerts: [],
    },
    {
      id: 2,
      first_name: "Petr",
      last_name: "Petrov",
      alerts: [],
      enrollment_status: "frozen",
      checkin_blocked_reason: "enrollment_frozen",
    },
  ];

  it("does not preselect students before kiosk or app check-in", () => {
    expect(buildDefaultCheckedIds(students, {
      student_ids: [],
      has_group_session: false,
    })).toEqual(new Set());
  });

  it("uses only server checked-in ids and excludes frozen students", () => {
    expect(buildDefaultCheckedIds(students, {
      student_ids: [1, 2],
      has_group_session: false,
    })).toEqual(new Set([1]));
  });

  it("keeps frozen students out of eligible status counts", () => {
    const checked = new Set([1, 2]);

    expect(selectSubmittableCheckedIds(checked, students)).toEqual(new Set([1]));
    expect(selectEligibleStudents(students).map((student) => student.id)).toEqual([1]);
  });

  it("invalidates session-detail and related trainer schedule queries", () => {
    const invalidated: unknown[] = [];
    const queryClient = {
      invalidateQueries: (payload: unknown) => {
        invalidated.push(payload);
        return Promise.resolve();
      },
    };

    invalidateBatchCheckinQueries(queryClient, {
      scheduleId: "42",
      checkinDate: "2026-06-03",
      studentIds: [],
      trainerId: 9,
    });

    expect(invalidated).toContainEqual({ queryKey: ["schedules", "unclosed"] });
    expect(invalidated).toContainEqual({ queryKey: ["schedules", "by-date", "2026-06-03"] });
    expect(invalidated).toContainEqual({ queryKey: ["schedule", "42", "session-detail", "2026-06-03"] });
    expect(invalidated).toContainEqual({ queryKey: ["trainer", "earnings", 9] });
  });
});
