import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import StudentAttendance from "./student-attendance";
import { useAuthStore } from "@/features/auth/auth-store";

const { get } = vi.hoisted(() => ({
  get: vi.fn(),
}));

vi.mock("@/api/custom-fetch", () => ({
  default: { get },
}));

function renderAttendancePage() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  return render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <StudentAttendance />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

describe("StudentAttendance", () => {
  beforeEach(() => {
    localStorage.clear();
    get.mockReset();
    useAuthStore.setState({
      accessToken: null,
      refreshToken: null,
      role: "student",
      clubId: 1,
      trainerId: null,
      studentId: 7,
      isAuthenticated: true,
      studentBootstrapStatus: "resolved",
      studentBootstrapError: null,
    });
  });

  it("shows an explicit error state when attendance loading fails", async () => {
    get.mockRejectedValueOnce(new Error("attendance failed"));

    renderAttendancePage();

    expect(await screen.findByText("Не удалось загрузить посещения")).toBeInTheDocument();
    expect(
      screen.getByText("Проверьте соединение и повторите попытку. История посещений пока не обновлена."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Повторить" })).toBeInTheDocument();
    expect(screen.queryByText("0 всего")).not.toBeInTheDocument();
    expect(screen.queryByText("Показано")).not.toBeInTheDocument();
  });

  it("shows an explicit empty attendance state before the first trainer mark", async () => {
    get.mockResolvedValue({
      data: {
        attended_count: 0,
        items: [],
      },
    });

    renderAttendancePage();

    expect(await screen.findByText("Посещений пока нет")).toBeInTheDocument();
    expect(
      screen.getByText("После первой отметки тренера здесь появятся дата, группа, тренер и зал."),
    ).toBeInTheDocument();
    expect(screen.getByText("0 из 0")).toBeInTheDocument();
  });

  it("keeps calendar counters and day details consistent with the rendered month dataset", async () => {
    const now = new Date();
    const dateKey = [
      now.getFullYear(),
      String(now.getMonth() + 1).padStart(2, "0"),
      "10",
    ].join("-");

    get.mockImplementation((_url: string, config?: { params?: { month?: string } }) => {
      if (config?.params?.month) {
        return Promise.resolve({
          data: {
            attended_count: 99,
            items: [
              {
                id: 1,
                date: dateKey,
                group_name: "Evening Group",
                trainer_name: "Coach One",
                location_name: "Blue Hall",
                training_type_name: "Karate",
                start_time: "18:00",
              },
              {
                id: 2,
                date: dateKey,
                group_name: "Morning Group",
                trainer_name: "Coach Two",
                location_name: "Red Hall",
                training_type_name: "Karate",
                start_time: "09:00",
              },
            ],
          },
        });
      }

      return Promise.resolve({
        data: {
          attended_count: 1,
          items: [
            {
              id: 3,
              date: "2026-04-12",
              group_name: "List Group",
              trainer_name: "Coach Three",
              location_name: "Main Hall",
              training_type_name: "Muay Thai",
              start_time: "12:00",
            },
          ],
        },
      });
    });

    renderAttendancePage();

    fireEvent.click(await screen.findByRole("button", { name: "Календарь" }));

    expect(await screen.findByText("2 всего")).toBeInTheDocument();
    expect(screen.getByText("Показано")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /10 .*2 посещения, открыть/ }));

    await waitFor(() => {
      expect(screen.getByText("Evening Group")).toBeInTheDocument();
      expect(screen.getByText("Morning Group")).toBeInTheDocument();
      expect(screen.getAllByText("Karate")).toHaveLength(2);
    });
  });

  it("disables empty calendar days when the selected month has no visits", async () => {
    get.mockResolvedValue({
      data: {
        attended_count: 0,
        items: [],
      },
    });

    renderAttendancePage();

    fireEvent.click(await screen.findByRole("button", { name: "Календарь" }));

    expect(await screen.findByText(/За этот месяц отметок пока нет/)).toBeInTheDocument();
    const emptyDays = screen.getAllByRole("button", { name: /нет посещений/ });
    expect(emptyDays[0]).toBeDisabled();

    fireEvent.click(emptyDays[0]);

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("loads additional list pages with limit and offset instead of truncating at the first page", async () => {
    const makeItem = (id: number) => ({
      id,
      date: `2026-04-${String((id % 28) + 1).padStart(2, "0")}`,
      group_name: `Group ${id}`,
      trainer_name: "Coach List",
      location_name: "Main Hall",
      training_type_name: "Karate",
      start_time: "18:00",
    });
    const firstPage = Array.from({ length: 50 }, (_, index) => makeItem(index + 1));
    const secondPage = [
      {
        ...makeItem(51),
        group_name: "Loaded More Group",
      },
    ];

    get.mockImplementation((_url: string, config?: { params?: { limit?: string; offset?: string } }) => {
      if (config?.params?.offset === "0") {
        return Promise.resolve({
          data: {
            attended_count: 51,
            items: firstPage,
          },
        });
      }

      if (config?.params?.offset === "50") {
        return Promise.resolve({
          data: {
            attended_count: 51,
            items: secondPage,
          },
        });
      }

      return Promise.reject(
        new Error(`Unexpected attendance params: ${JSON.stringify(config?.params ?? {})}`),
      );
    });

    renderAttendancePage();

    expect(await screen.findByText("50 из 51")).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith("/students/me/attendance/", {
      params: { limit: "50", offset: "0" },
    });

    fireEvent.click(screen.getByRole("button", { name: /показать ещё/i }));

    expect(await screen.findByText("Loaded More Group")).toBeInTheDocument();
    expect(screen.getByText("51 из 51")).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith("/students/me/attendance/", {
      params: { limit: "50", offset: "50" },
    });
    expect(screen.queryByText(/Показаны последние/)).not.toBeInTheDocument();
  });
});
