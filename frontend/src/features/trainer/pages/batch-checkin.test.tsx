import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import BatchCheckin from "./batch-checkin";

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

function renderBatchCheckin() {
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

  return render(
    <MemoryRouter initialEntries={["/trainer/schedule/42/checkin?date=2099-01-01"]}>
      <QueryClientProvider client={queryClient}>
        <Routes>
          <Route path="/trainer/schedule/:scheduleId/checkin" element={<BatchCheckin />} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

describe("BatchCheckin", () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
    navigate.mockReset();
    get.mockImplementation((url: string) => {
      if (url === "/schedules/42/session-detail/") {
        return Promise.resolve({
          data: {
            schedule_id: 42,
            date: "2099-01-01",
            occurrence: {
              schedule_id: 42,
              group_name: "Группа муай-тай",
              effective_date: "2099-01-01",
              effective_start_time: "18:00:00",
              effective_end_time: "19:00:00",
              trainer_id: 9,
              trainer_name: "Анна Иванова",
              location_id: 3,
              location_name: "Зал 1",
              training_type_id: 7,
              training_type_name: "Муай-тай",
              training_type_kind: "group",
            },
            is_closed: false,
            can_close: true,
            close_allowed_at: "2099-01-01T19:00:00+05:00",
            close_block_reason: "",
            summary: {
              expected_count: 1,
              checked_in_count: 0,
              waiting_count: 1,
              blocked_count: 0,
            },
            roster: [
              {
                id: 1,
                first_name: "Nina",
                last_name: "Ivanova",
                alerts: [],
                checkin_status: "waiting",
                checked_in_at: null,
              },
            ],
          },
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${url}`));
    });
  });

  it("opens roster row context without attendance mutation controls", async () => {
    renderBatchCheckin();

    expect(await screen.findByText("Группа муай-тай")).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", {
        name: /Открыть контекст ученика Ivanova Nina: Ждет отметки/,
      }),
    );

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Ivanova Nina")).toBeInTheDocument();
    expect(within(dialog).getByText("Ждет отметки")).toBeInTheDocument();
    expect(within(dialog).getByText("Плановая запись")).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: /Отметить|Снять отметку/ })).not.toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole("button", { name: "Открыть карточку" }));

    expect(navigate).toHaveBeenCalledWith("/trainer/students/1");
    expect(post).not.toHaveBeenCalled();
  });

  it("uses backend close eligibility instead of local date heuristics", async () => {
    renderBatchCheckin();

    expect(
      await screen.findByRole("button", { name: "Закрыть тренировку" }),
    ).toBeInTheDocument();
  });

  it("does not offer guest add action for one-time group sessions", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/schedules/42/session-detail/") {
        return Promise.resolve({
          data: {
            schedule_id: 42,
            date: "2099-01-01",
            occurrence: {
              schedule_id: 42,
              group_name: "Разовая группа",
              effective_date: "2099-01-01",
              effective_start_time: "18:00:00",
              effective_end_time: "19:00:00",
              trainer_id: 9,
              trainer_name: "Анна Иванова",
              location_id: 3,
              location_name: "Зал 1",
              training_type_id: 7,
              training_type_name: "Муай-тай",
              training_type_kind: "group",
              one_time_date: "2099-01-01",
            },
            is_closed: false,
            can_close: true,
            close_allowed_at: "2099-01-01T19:00:00+05:00",
            close_block_reason: "",
            summary: {
              expected_count: 0,
              checked_in_count: 0,
              waiting_count: 0,
              blocked_count: 0,
            },
            roster: [],
          },
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${url}`));
    });

    renderBatchCheckin();

    expect(
      await screen.findByText("Разовое занятие: список формируется вручную"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Добавить в список занятия" }),
    ).not.toBeInTheDocument();
  });
});
