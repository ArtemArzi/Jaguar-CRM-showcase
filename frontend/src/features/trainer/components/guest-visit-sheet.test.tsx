import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { GuestVisitSheet } from "./guest-visit-sheet";
import { filterGuestVisitCandidates } from "../lib/guest-visit-candidates";
import type { GuestVisitCandidate, StudentWithAlerts } from "../types";

const { get, post } = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}));

vi.mock("@/api/custom-fetch", () => ({
  default: {
    get,
    post,
  },
}));

function candidate(overrides: Partial<GuestVisitCandidate>): GuestVisitCandidate {
  return {
    id: 1,
    kind: "student",
    first_name: "Masha",
    last_name: "Petrova",
    masked_phone: "+***0001",
    status: "active",
    ...overrides,
  };
}

function rosterStudent(overrides: Partial<StudentWithAlerts>): StudentWithAlerts {
  return {
    id: 1,
    first_name: "Masha",
    last_name: "Petrova",
    alerts: [],
    ...overrides,
  };
}

function renderSheet({
  rosterStudents = [],
  rosterError = false,
  onRetryRoster = vi.fn(),
  onOpenChange = vi.fn(),
  onBooked = vi.fn(),
}: {
  rosterStudents?: StudentWithAlerts[];
  rosterError?: boolean;
  onRetryRoster?: () => void;
  onOpenChange?: (open: boolean) => void;
  onBooked?: (student: StudentWithAlerts) => void;
} = {}) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  const view = render(
    <QueryClientProvider client={queryClient}>
      <GuestVisitSheet
        open
        onOpenChange={onOpenChange}
        scheduleId="42"
        checkinDate="2026-06-20"
        rosterStudents={rosterStudents}
        rosterError={rosterError}
        onRetryRoster={onRetryRoster}
        trainerId={9}
        onBooked={onBooked}
      />
    </QueryClientProvider>,
  );

  return { ...view, onOpenChange, onBooked, onRetryRoster };
}

describe("filterGuestVisitCandidates", () => {
  it("keeps eligible non-roster students and leads", () => {
    const candidates = filterGuestVisitCandidates({
      candidates: [
        candidate({ id: 1, first_name: "Already", last_name: "InRoster" }),
        candidate({ id: 2, first_name: "Nina", last_name: "Ivanova" }),
        candidate({ id: 3, kind: "lead", first_name: "Lead", last_name: "Only", status: "lead" }),
      ],
      rosterStudents: [rosterStudent({ id: 1 })],
    });

    expect(candidates.map((item) => item.id)).toEqual([2, 3]);
  });
});

describe("GuestVisitSheet", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    post.mockResolvedValue({
      data: {
        enrollment_id: 77,
        student_id: 2,
        display_name: "Ivanova Nina",
        schedule_id: 42,
        created_from: "guest_visit",
        is_guest_visit: true,
        starts_on: "2026-06-20",
        ends_on: "2026-06-20",
        created: true,
        already_member: false,
        origin: "walk_in_checkin",
        financial_preview: {
          code: "resolved_at_checkin",
          message: "Финансы будут рассчитаны при check-in",
        },
      },
    });
    get.mockImplementation((url: string) => {
      if (url === "/schedules/42/guest-visit-candidates/") {
        return Promise.resolve({
          data: {
            items: [
              candidate({ id: 1, first_name: "Masha", last_name: "Petrova" }),
              candidate({
                id: 2,
                first_name: "Nina",
                last_name: "Ivanova",
                masked_phone: "+***0002",
              }),
              candidate({
                id: 3,
                kind: "lead",
                first_name: "Lead",
                last_name: "Only",
                status: "lead",
              }),
            ],
          },
        });
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });
  });

  it("posts the selected guest visit and closes on success", async () => {
    const { onOpenChange, onBooked } = renderSheet({
      rosterStudents: [rosterStudent({ id: 1 })],
    });

    fireEvent.change(screen.getByPlaceholderText("Имя или телефон"), {
      target: { value: "Nina" },
    });

    await screen.findByRole(
      "button",
      { name: /Ivanova Nina/ },
      { timeout: 5_000 },
    );
    expect(get).toHaveBeenCalledWith("/schedules/42/guest-visit-candidates/", {
      params: { date: "2026-06-20", q: "Nina" },
    });
    expect(screen.getByText("+***0002")).toBeInTheDocument();
    expect(screen.getByText("Only Lead")).toBeInTheDocument();
    expect(screen.queryByText("Petrova Masha")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Ivanova Nina/ }));
    expect(screen.getByText("Финансы будут рассчитаны при check-in")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Добавить гостя" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/schedules/42/guest-visits/", {
        date: "2026-06-20",
        student_id: 2,
        origin: "walk_in_checkin",
        idempotency_key: "trainer-walk-in-42-2026-06-20-student-2",
      });
    });
    await waitFor(() => {
      expect(onBooked).toHaveBeenCalledWith(
        expect.objectContaining({
          id: 2,
          enrollment_id: 77,
          created_from: "guest_visit",
          is_guest_visit: true,
        }),
      );
      expect(onOpenChange).toHaveBeenCalledWith(false);
    });
  });

  it("blocks guest search when the current roster is unavailable", async () => {
    const { onRetryRoster } = renderSheet({ rosterError: true });

    expect(await screen.findByText("Не удалось загрузить состав тренировки")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Имя или телефон")).toBeDisabled();

    fireEvent.click(
      screen.getByRole("button", { name: "Повторить загрузку состава" }),
    );
    expect(onRetryRoster).toHaveBeenCalledTimes(1);
  });

  it("shows candidate search failure instead of an empty result and retries", async () => {
    let searchAttempts = 0;
    get.mockImplementation((url: string) => {
      if (url === "/schedules/42/guest-visit-candidates/") {
        searchAttempts += 1;
        return searchAttempts === 1
          ? Promise.reject(new Error("search unavailable"))
          : Promise.resolve({ data: { items: [] } });
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });
    renderSheet();

    fireEvent.change(screen.getByPlaceholderText("Имя или телефон"), {
      target: { value: "Nina" },
    });

    expect(await screen.findByText("Не удалось выполнить поиск")).toBeInTheDocument();
    expect(screen.queryByText("Подходящих учеников нет")).not.toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", { name: "Повторить поиск" }),
    );

    expect(await screen.findByText("Подходящих учеников нет")).toBeInTheDocument();
    expect(searchAttempts).toBe(2);
  });
});
