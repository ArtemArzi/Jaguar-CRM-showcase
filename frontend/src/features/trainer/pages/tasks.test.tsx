import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "@/features/auth/auth-store";
import Tasks from "./tasks";
import type { RetentionTask } from "../types";

const { get, post, navigate } = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  navigate: vi.fn(),
}));

vi.mock("@/api/custom-fetch", () => ({
  default: { get, post },
}));

vi.mock("react-router", async () => {
  const actual = await vi.importActual<typeof import("react-router")>("react-router");

  return {
    ...actual,
    useNavigate: () => navigate,
  };
});

function renderTasks() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
      mutations: {
        retry: false,
      },
    },
  });

  const view = render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <Tasks />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return { ...view, queryClient };
}

function buildTask(overrides: Partial<RetentionTask> = {}): RetentionTask {
  return {
    id: 1,
    student_id: 1,
    student_name: "Анна Тестова",
    student_phone: "+70000000001",
    last_visit_date: null,
    days_missed: 5,
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
    ...overrides,
  };
}

describe("Tasks", () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
    navigate.mockReset();
    useAuthStore.setState({
      accessToken: null,
      role: "trainer",
      clubId: 1,
      trainerId: 4,
    });
  });

  it("shows a task request failure and recovers without presenting an empty list", async () => {
    let taskAttempts = 0;
    get.mockImplementation((url: string, config?: { params?: { resolved?: boolean } }) => {
      if (url === "/trainers/me/") return Promise.resolve({ data: { id: 4 } });
      if (url === "/retention/tasks/" && config?.params?.resolved === false) {
        taskAttempts += 1;
        return taskAttempts === 1
          ? Promise.reject(new Error("tasks unavailable"))
          : Promise.resolve({ data: { items: [], count: 0 } });
      }
      if (url === "/retention/tasks/" && config?.params?.resolved === true) {
        return Promise.resolve({ data: { items: [], count: 0 } });
      }
      return Promise.reject(new Error(`Unexpected GET ${url}`));
    });

    renderTasks();

    expect(await screen.findByText("Не удалось загрузить задачи")).toBeInTheDocument();
    expect(screen.queryByText("Задач сейчас нет")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Повторить загрузку задач" }));
    expect(await screen.findByText("Задач сейчас нет")).toBeInTheDocument();
  });

  it("keeps the failed closed-today counter visible as an independent error", async () => {
    get.mockImplementation((url: string, config?: { params?: { resolved?: boolean } }) => {
      if (url === "/trainers/me/") return Promise.resolve({ data: { id: 4 } });
      if (url === "/retention/tasks/" && config?.params?.resolved === false) {
        return Promise.resolve({ data: { items: [], count: 0 } });
      }
      if (url === "/retention/tasks/" && config?.params?.resolved === true) {
        return Promise.reject(new Error("closed counter unavailable"));
      }
      return Promise.reject(new Error(`Unexpected GET ${url}`));
    });

    renderTasks();

    expect(await screen.findByText("Не удалось загрузить итог за сегодня")).toBeInTheDocument();
    expect(screen.getByText("Задач сейчас нет")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Повторить загрузку итога" }),
    ).toBeInTheDocument();
  });

  it("keeps cached tasks visible but disables task actions after a failed refetch", async () => {
    let failTasks = false;
    const task = buildTask({ due_date: "2099-06-25" });
    get.mockImplementation((url: string, config?: { params?: { resolved?: boolean } }) => {
      if (url === "/trainers/me/") return Promise.resolve({ data: { id: 4 } });
      if (url === "/retention/tasks/" && config?.params?.resolved === false) {
        return failTasks
          ? Promise.reject(new Error("tasks unavailable"))
          : Promise.resolve({ data: { items: [task], count: 1 } });
      }
      if (url === "/retention/tasks/" && config?.params?.resolved === true) {
        return Promise.resolve({ data: { items: [], count: 0 } });
      }
      return Promise.reject(new Error(`Unexpected GET ${url}`));
    });

    const { queryClient } = renderTasks();
    expect(await screen.findByText(task.student_name)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Позвонить" })).toBeEnabled();

    failTasks = true;
    await queryClient.refetchQueries({ queryKey: ["retention-tasks", 4] });

    expect(await screen.findByText("Задачи могли устареть")).toBeInTheDocument();
    expect(screen.getByText(task.student_name)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Позвонить" })).toBeDisabled();
  });

  it("does not show the task empty state before trainer identity resolves", () => {
    get.mockImplementation((url: string) => {
      if (url === "/trainers/me/") {
        return new Promise(() => undefined);
      }
      return Promise.reject(new Error(`Unexpected GET ${url}`));
    });

    renderTasks();

    expect(screen.queryByText("Задач сейчас нет")).not.toBeInTheDocument();
    expect(screen.queryByText("Все обзвонены!")).not.toBeInTheDocument();
  });

  it("shows an explicit empty state after open and snoozed tasks are loaded empty", async () => {
    get.mockImplementation((url: string, config?: { params?: { resolved?: boolean } }) => {
      if (url === "/trainers/me/") {
        return Promise.resolve({ data: { id: 4 } });
      }
      if (url === "/retention/tasks/" && config?.params?.resolved === false) {
        return Promise.resolve({ data: { items: [] } });
      }
      if (url === "/retention/tasks/" && config?.params?.resolved === true) {
        return Promise.resolve({ data: { items: [] } });
      }
      return Promise.reject(new Error(`Unexpected GET ${url}`));
    });

    renderTasks();

    expect(await screen.findByText("Задач сейчас нет")).toBeInTheDocument();
    expect(
      screen.getByText("Новых звонков и отложенных задач на сегодня нет."),
    ).toBeInTheDocument();
  });

  it("loads additional task pages instead of hiding records after the first page", async () => {
    const tasks = Array.from({ length: 51 }, (_, index) =>
      buildTask({
        id: index + 1,
        student_id: index + 1,
        student_name: `Клиент ${index + 1}`,
      }),
    );
    get.mockImplementation((url: string, config?: { params?: { resolved?: boolean; limit?: number; offset?: number } }) => {
      if (url === "/trainers/me/") {
        return Promise.resolve({ data: { id: 4 } });
      }
      if (url === "/retention/tasks/" && config?.params?.resolved === false) {
        const offset = config.params.offset ?? 0;
        const limit = config.params.limit ?? 50;
        return Promise.resolve({
          data: { items: tasks.slice(offset, offset + limit), count: tasks.length },
        });
      }
      if (url === "/retention/tasks/" && config?.params?.resolved === true) {
        return Promise.resolve({ data: { items: [], count: 0 } });
      }
      return Promise.reject(new Error(`Unexpected GET ${url}`));
    });

    renderTasks();

    expect(await screen.findByText("Клиент 1")).toBeInTheDocument();
    expect(screen.queryByText("Клиент 51")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Загрузить ещё/ }));

    expect(await screen.findByText("Клиент 51")).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith("/retention/tasks/", {
      params: {
        trainer_id: 4,
        resolved: false,
        limit: 50,
        offset: 50,
      },
    });
  }, 10_000);
});
