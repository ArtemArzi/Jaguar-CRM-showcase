import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import TaskDetail from "./task-detail";

const { get, post } = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}));

vi.mock("@/api/custom-fetch", () => ({
  default: { get, post },
}));

function renderTaskDetail() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  return render(
    <MemoryRouter initialEntries={["/trainer/tasks/123"]}>
      <QueryClientProvider client={queryClient}>
        <Routes>
          <Route path="/trainer/tasks/:taskId" element={<TaskDetail />} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

function retentionTaskResponse(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    id: 123,
    student_id: 42,
    student_name: "Иван Петров",
    student_phone: "",
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
    task_type: "retention",
    attempt_count: 0,
    automation_source: null,
    automation_step_message: null,
    ...overrides,
  };
}

function mockTaskDetailGet(
  taskOverrides: Partial<Record<string, unknown>> = {},
  subscriptionItems: unknown[] = [],
) {
  get.mockImplementation((url: string) => {
    if (url === "/trainers/me/") {
      return Promise.resolve({ data: { id: 9 } });
    }

    if (url === "/retention/tasks/123/") {
      return Promise.resolve({
        data: retentionTaskResponse(taskOverrides),
      });
    }

    if (url === "/retention/tasks/123/comments/") {
      return Promise.resolve({ data: [] });
    }

    if (url === "/billing/subscriptions/") {
      return Promise.resolve({ data: { items: subscriptionItems } });
    }

    if (url === "/grades/students/42/progress/") {
      return Promise.resolve({ data: [] });
    }

    if (url === "/students/42/checkins/") {
      return Promise.resolve({ data: [] });
    }

    if (url === "/students/42/attendance/") {
      return Promise.resolve({ data: [] });
    }

    return Promise.reject(new Error(`Unexpected GET ${url}`));
  });
}

describe("TaskDetail", () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
  });

  it("loads student checkins through the staff endpoint instead of legacy attendance", async () => {
    mockTaskDetailGet();

    renderTaskDetail();

    expect(await screen.findByText("Иван Петров")).toBeInTheDocument();
    await waitFor(() =>
      expect(get).toHaveBeenCalledWith("/billing/subscriptions/", {
        params: { student_id: 42 },
      }),
    );

    expect(get).toHaveBeenCalledWith("/students/42/checkins/", {
      params: { limit: 10 },
    });
    expect(get).not.toHaveBeenCalledWith("/students/42/attendance/");
  });

  it("shows pipeline provenance in the task info block", async () => {
    mockTaskDetailGet({
      automation_source: "pipeline",
      automation_step_message: "Позвонить, спросить впечатления",
    });

    renderTaskDetail();

    expect(await screen.findByText("Иван Петров")).toBeInTheDocument();
    expect(screen.getByText("Автоворонка")).toBeInTheDocument();
    expect(screen.getByText("Позвонить, спросить впечатления")).toBeInTheDocument();
  });

  it("renders subscription context from the billing backend response shape", async () => {
    mockTaskDetailGet(
      {},
      [
        {
          id: 5,
          tariff: {
            name: "Backend Plan",
            trainings_limit: 12,
          },
          trainings_used: 4,
          trainings_left: 8,
          expires_at: "2026-07-01T00:00:00Z",
          status: "active",
        },
      ],
    );

    renderTaskDetail();

    expect(await screen.findByText("Иван Петров")).toBeInTheDocument();
    expect(await screen.findByText("Backend Plan")).toBeInTheDocument();
  });

  it("labels a cancelled subscription instead of reporting no active subscription", async () => {
    mockTaskDetailGet(
      {},
      [
        {
          id: 6,
          tariff: {
            name: "Refunded Plan",
            trainings_limit: 8,
          },
          trainings_used: 3,
          trainings_left: 5,
          expires_at: "2026-07-01T00:00:00Z",
          status: "cancelled",
        },
      ],
    );

    renderTaskDetail();

    expect(await screen.findByText("Refunded Plan (отменён после возврата)")).toBeInTheDocument();
  });
});
