import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { RetentionTask } from "../types";
import { ResolutionSheet } from "./resolution-sheet";

const post = vi.hoisted(() => vi.fn());

vi.mock("@/api/custom-fetch", () => ({
  default: { post },
}));

function renderSheet(onOpenChange = vi.fn()) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  const task: RetentionTask = {
    id: 12,
    student_id: 1,
    student_name: "Анна Тестова",
    student_phone: "+70000000001",
    last_visit_date: null,
    days_missed: 7,
    trainer_id: 4,
    level: "yellow",
    status: "open",
    due_date: "2026-06-25",
    resolved_at: null,
    resolution: "",
    notes: "",
    created_at: "2026-06-20T10:00:00Z",
    last_activity_date: null,
    task_type: "retention",
    attempt_count: 1,
    automation_source: null,
    automation_step_message: null,
  };

  render(
    <QueryClientProvider client={queryClient}>
      <ResolutionSheet open task={task} trainerId={4} onOpenChange={onOpenChange} />
    </QueryClientProvider>,
  );
}

describe("ResolutionSheet", () => {
  beforeEach(() => {
    post.mockReset();
  });

  it("keeps the sheet open and shows an error when closing a task fails", async () => {
    const onOpenChange = vi.fn();
    post.mockRejectedValue(new Error("network"));

    renderSheet(onOpenChange);

    fireEvent.click(screen.getByRole("button", { name: "Позвонил, придёт" }));
    fireEvent.click(screen.getByRole("button", { name: "Закрыть задачу" }));

    expect(await screen.findByText("Не удалось закрыть задачу")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Закрыть задачу" })).toBeInTheDocument();
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
  });
});
