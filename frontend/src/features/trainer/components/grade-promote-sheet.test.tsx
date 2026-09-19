import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { GradePromoteSheet } from "./grade-promote-sheet";
import type { GradeProgress } from "../types";

const { get } = vi.hoisted(() => ({
  get: vi.fn(),
}));

vi.mock("@/api/custom-fetch", () => ({
  default: {
    get,
    post: vi.fn(),
  },
}));

function renderSheet(gradeProgress: GradeProgress) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <GradePromoteSheet
        open
        onOpenChange={vi.fn()}
        studentId={7}
        studentName="Masha Ivanova"
        gradeProgress={gradeProgress}
      />
    </QueryClientProvider>,
  );
}

describe("GradePromoteSheet", () => {
  it("loads grades by grade_system_id instead of matching duplicate discipline names", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/grades/systems/") {
        return Promise.resolve({
          data: {
            items: [
              { id: 11, discipline: "BJJ", is_active: true },
              { id: 22, discipline: "BJJ", is_active: true },
            ],
          },
        });
      }

      if (url === "/grades/systems/11/grades/") {
        return Promise.resolve({ data: { items: [] } });
      }

      if (url === "/grades/systems/22/grades/") {
        return Promise.resolve({
          data: {
            items: [
              { id: 3, name: "Orange", order: 2, min_trainings: 10 },
            ],
          },
        });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderSheet({
      student_grade_id: 5,
      grade_system_id: 22,
      grade_system_name: "BJJ",
      current_grade: {
        id: 2,
        name: "Yellow",
        order: 1,
        min_trainings: 5,
      },
      trainings_since_last_grade: 10,
      next_grade: {
        id: 3,
        name: "Orange",
        order: 2,
        min_trainings: 10,
      },
      trainings_to_next: 0,
    });

    await waitFor(() => {
      expect(get).toHaveBeenCalledWith("/grades/systems/22/grades/");
    });
    expect(get).not.toHaveBeenCalledWith("/grades/systems/11/grades/");
  });
});
