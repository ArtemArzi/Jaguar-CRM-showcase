import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { TaskCard } from "./task-card";
import type { RetentionTask } from "../types";

const baseTask: RetentionTask = {
  id: 123,
  student_id: 42,
  student_name: "Иван Петров",
  student_phone: "+70000000000",
  last_visit_date: "2026-06-01",
  days_missed: 3,
  trainer_id: 9,
  level: "yellow",
  status: "open",
  due_date: "2026-06-10",
  resolved_at: null,
  resolution: "",
  notes: "",
  created_at: "2026-06-01T10:00:00Z",
  last_activity_date: null,
  task_type: "post_trial",
  attempt_count: 0,
  automation_source: null,
  automation_step_message: null,
};

describe("TaskCard", () => {
  it("shows compact pipeline provenance when the task comes from automation", () => {
    render(
      <TaskCard
        task={{
          ...baseTask,
          automation_source: "pipeline",
          automation_step_message: "Позвонить, спросить впечатления",
        }}
        onCall={vi.fn()}
        onSnooze={vi.fn()}
        onClose={vi.fn()}
        onDetail={vi.fn()}
      />,
    );

    expect(screen.getByText("Автоворонка")).toBeInTheDocument();
    expect(screen.getByText("Позвонить, спросить впечатления")).toBeInTheDocument();
  });
});
