import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { GradePromotionButtons } from "./student-detail";
import type { GradeProgress } from "../types";

function buildGradeProgress(
  studentGradeId: number,
  gradeSystemName: string,
): GradeProgress {
  return {
    student_grade_id: studentGradeId,
    grade_system_id: studentGradeId + 100,
    grade_system_name: gradeSystemName,
    current_grade: {
      id: studentGradeId,
      name: "Current",
      order: 1,
      min_trainings: 0,
    },
    next_grade: {
      id: studentGradeId + 10,
      name: "Next",
      order: 2,
      min_trainings: 10,
    },
    trainings_since_last_grade: 9,
    trainings_to_next: 1,
  };
}

describe("GradePromotionButtons", () => {
  it("opens promotion for the selected discipline instead of the first grade", () => {
    const bjj = buildGradeProgress(1, "BJJ");
    const boxing = buildGradeProgress(2, "Boxing");
    const openPromote = vi.fn();

    render(
      <GradePromotionButtons
        grades={[bjj, boxing]}
        openPromote={openPromote}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Повысить Boxing" }));

    expect(openPromote).toHaveBeenCalledWith(boxing);
  });
});
