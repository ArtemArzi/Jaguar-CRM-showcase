import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "@/features/auth/auth-store";
import { toDateParamInTimeZone, toDateTimeParamInTimeZone } from "@/lib/club-date";
import ScheduleHome from "./schedule-home";

const { get, navigate } = vi.hoisted(() => ({
  get: vi.fn(),
  navigate: vi.fn(),
}));

vi.mock("@/api/custom-fetch", () => ({
  default: { get },
}));

vi.mock("react-router", async () => {
  const actual = await vi.importActual<typeof import("react-router")>("react-router");

  return {
    ...actual,
    useNavigate: () => navigate,
  };
});

function renderScheduleHome() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  const view = render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <ScheduleHome />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return { ...view, queryClient };
}

describe("ScheduleHome", () => {
  beforeEach(() => {
    get.mockReset();
    navigate.mockReset();
    useAuthStore.setState({
      accessToken: null,
      role: "trainer",
      clubId: 1,
      trainerId: 4,
    });
  });

  it("shows schedule failure instead of an honest empty state and recovers through retry", async () => {
    let scheduleAttempts = 0;
    get.mockImplementation((url: string) => {
      if (url === "/schedules/today/") {
        scheduleAttempts += 1;
        return scheduleAttempts === 1
          ? Promise.reject(new Error("schedule unavailable"))
          : Promise.resolve({ data: [] });
      }
      if (url === "/schedules/unclosed/") return Promise.resolve({ data: [] });
      if (url === "/trainers/me/") {
        return Promise.resolve({
          data: { id: 4, first_name: "Current", last_name: "Trainer", student_count: 12 },
        });
      }
      if (url === "/trainers/4/earnings/summary/") {
        return Promise.resolve({ data: { total_amount: "0.00" } });
      }
      if (url === "/retention/tasks/") return Promise.resolve({ data: { items: [] } });
      return Promise.reject(new Error(`Unexpected GET ${url}`));
    });

    renderScheduleHome();

    expect(await screen.findByText("Не удалось загрузить тренировки")).toBeInTheDocument();
    expect(screen.queryByText("Сегодня нет тренировок")).not.toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", { name: "Повторить загрузку тренировок" }),
    );

    expect(await screen.findByText("Сегодня нет тренировок")).toBeInTheDocument();
    await waitFor(() => expect(scheduleAttempts).toBe(2));
  });

  it("keeps unclosed sessions and earnings failures visible and retryable", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/schedules/today/") return Promise.resolve({ data: [] });
      if (url === "/schedules/unclosed/") {
        return Promise.reject(new Error("unclosed unavailable"));
      }
      if (url === "/trainers/me/") {
        return Promise.resolve({
          data: { id: 4, first_name: "Current", last_name: "Trainer", student_count: 12 },
        });
      }
      if (url === "/trainers/4/earnings/summary/") {
        return Promise.reject(new Error("earnings unavailable"));
      }
      if (url === "/retention/tasks/") return Promise.resolve({ data: { items: [] } });
      return Promise.reject(new Error(`Unexpected GET ${url}`));
    });

    renderScheduleHome();

    expect(await screen.findByText("Не удалось проверить незакрытые тренировки")).toBeInTheDocument();
    expect((await screen.findAllByText("Заработок недоступен")).length).toBeGreaterThan(0);
    expect(screen.queryByText("--")).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Повторить загрузку заработка" }),
    ).toBeInTheDocument();
  });

  it("keeps a cached schedule visible but blocks its actions after a failed refetch", async () => {
    let failSchedule = false;
    const futureOccurrence = new Date(Date.now() + 60 * 60 * 1000);
    const futureEnd = new Date(futureOccurrence.getTime() + 60 * 60 * 1000);
    const occurrence = {
      schedule_id: 77,
      group_name: "Вечерняя группа",
      effective_date: toDateParamInTimeZone(futureOccurrence),
      effective_start_time: toDateTimeParamInTimeZone(futureOccurrence).slice(11),
      effective_end_time: toDateTimeParamInTimeZone(futureEnd).slice(11),
      trainer_id: 4,
      trainer_name: "Current Trainer",
      location_id: 1,
      location_name: "Главный зал",
      one_time_date: null,
      is_rescheduled: false,
      is_substitute: false,
      training_type_kind: "group",
    };
    get.mockImplementation((url: string) => {
      if (url === "/schedules/today/") {
        return failSchedule
          ? Promise.reject(new Error("schedule unavailable"))
          : Promise.resolve({ data: [occurrence] });
      }
      if (url === "/schedules/unclosed/") return Promise.resolve({ data: [] });
      if (url === "/trainers/me/") {
        return Promise.resolve({
          data: { id: 4, first_name: "Current", last_name: "Trainer", student_count: 12 },
        });
      }
      if (url === "/trainers/4/earnings/summary/") {
        return Promise.resolve({ data: { total_amount: "0.00" } });
      }
      if (url === "/retention/tasks/") return Promise.resolve({ data: { items: [] } });
      return Promise.reject(new Error(`Unexpected GET ${url}`));
    });

    const { queryClient } = renderScheduleHome();
    expect(await screen.findByText("Вечерняя группа")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Открыть" })).toBeEnabled();

    failSchedule = true;
    await queryClient.refetchQueries({ queryKey: ["schedules", "today"] });

    expect(await screen.findByText("Расписание могло устареть")).toBeInTheDocument();
    expect(screen.getByText("Вечерняя группа")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Открыть" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Создать тренировку" })).not.toBeInTheDocument();
  });

  it("does not show the task empty state before trainer identity resolves", () => {
    get.mockImplementation((url: string) => {
      if (url === "/schedules/today/" || url === "/schedules/unclosed/") {
        return Promise.resolve({ data: [] });
      }
      if (url === "/trainers/me/") {
        return new Promise(() => undefined);
      }
      return Promise.reject(new Error(`Unexpected GET ${url}`));
    });

    renderScheduleHome();

    expect(screen.queryByText("Сегодня задач нет")).not.toBeInTheDocument();
    expect(screen.queryByText("Задачи появятся здесь")).not.toBeInTheDocument();
  });

  it("shows an explicit task empty state after an empty task response", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/schedules/today/" || url === "/schedules/unclosed/") {
        return Promise.resolve({ data: [] });
      }
      if (url === "/trainers/me/") {
        return Promise.resolve({
          data: { id: 4, first_name: "Current", last_name: "Trainer", student_count: 12 },
        });
      }
      if (url === "/trainers/4/earnings/summary/") {
        return Promise.resolve({ data: { total_amount: "0.00" } });
      }
      if (url === "/retention/tasks/") {
        return Promise.resolve({ data: { items: [] } });
      }
      return Promise.reject(new Error(`Unexpected GET ${url}`));
    });

    renderScheduleHome();

    expect(await screen.findByText("Сегодня задач нет")).toBeInTheDocument();
    expect(
      screen.getByText("Если появятся ученики для звонка, они будут здесь отдельными карточками."),
    ).toBeInTheDocument();
  });

  it("shows multiple dated unclosed sessions from the bounded range", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/schedules/today/") {
        return Promise.resolve({ data: [] });
      }
      if (url === "/schedules/unclosed/") {
        return Promise.resolve({
          data: [
            {
              schedule_id: 10,
              effective_date: "2026-06-25",
              effective_start_time: "10:00:00",
              group_name: "Утренняя группа",
            },
            {
              schedule_id: 11,
              effective_date: "2026-06-27",
              effective_start_time: "12:00:00",
              group_name: "Дневная группа",
            },
          ],
        });
      }
      if (url === "/trainers/me/") {
        return Promise.resolve({
          data: { id: 4, first_name: "Current", last_name: "Trainer", student_count: 12 },
        });
      }
      if (url === "/trainers/4/earnings/summary/") {
        return Promise.resolve({ data: { total_amount: "0.00" } });
      }
      if (url === "/retention/tasks/") {
        return Promise.resolve({ data: { items: [] } });
      }
      return Promise.reject(new Error(`Unexpected GET ${url}`));
    });

    renderScheduleHome();

    expect(await screen.findByText("Есть незакрытые тренировки (2)")).toBeInTheDocument();
    expect(screen.getByText("25.06")).toBeInTheDocument();
    expect(screen.getByText("27.06")).toBeInTheDocument();
    expect(screen.getByText("Утренняя группа")).toBeInTheDocument();
    expect(screen.getByText("Дневная группа")).toBeInTheDocument();
  });
});
