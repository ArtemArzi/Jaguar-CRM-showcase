import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "@/features/auth/auth-store";
import { useBrandingStore } from "@/features/branding/use-branding";
import SalaryDetail from "./salary-detail";

const { get } = vi.hoisted(() => ({
  get: vi.fn(),
}));

vi.mock("@/api/custom-fetch", () => ({
  default: { get },
}));

function renderSalaryDetailPage() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  return render(
    <MemoryRouter initialEntries={["/trainer/profile/salary"]}>
      <QueryClientProvider client={queryClient}>
        <SalaryDetail />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

function mockAdjustmentOnlySalary() {
  get.mockImplementation((url: string) => {
    if (url === "/trainers/9/earnings/") {
      return Promise.resolve({
        data: [
          {
            id: 22,
            row_type: "adjustment",
            earning_type: "manual_adjustment",
            amount: "-500.00",
            rate_percent: "0.00",
            subscription_price: null,
            checkin_date: "2026-06-15",
            schedule_name: "",
            adjustment_direction: "debit",
            adjustment_reason: "manual penalty",
            adjustment_effective_date: "2026-06-15",
          },
        ],
      });
    }
    if (url === "/trainers/9/earnings/summary/") {
      return Promise.resolve({
        data: {
          total_amount: "-500.00",
          total_sessions: 0,
          by_type: {
            manual_adjustment: { count: 1, total: "-500.00" },
          },
        },
      });
    }
    return Promise.reject(new Error(`Unexpected GET ${url}`));
  });
}

describe("Trainer Salary detail page", () => {
  beforeEach(() => {
    get.mockReset();
    useBrandingStore.setState({ timeZone: "Asia/Yekaterinburg" });
    useAuthStore.setState({
      accessToken: "trainer-token",
      refreshToken: "refresh-token",
      role: "trainer",
      clubId: 1,
      trainerId: 9,
      studentId: null,
      authBootstrapStatus: "ready",
      studentBootstrapStatus: "resolved",
      studentBootstrapError: null,
      isAuthenticated: true,
    });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("uses the club time zone when choosing the default salary month", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date("2026-06-30T20:30:00.000Z"));
    get.mockImplementation((url: string) => {
      if (url === "/trainers/9/earnings/") {
        return Promise.resolve({ data: [] });
      }
      if (url === "/trainers/9/earnings/summary/") {
        return Promise.resolve({
          data: { total_amount: "0.00", total_sessions: 0, by_type: {} },
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${url}`));
    });

    renderSalaryDetailPage();

    await waitFor(() => {
      expect(get).toHaveBeenCalledWith("/trainers/9/earnings/", {
        params: { date_from: "2026-07-01", date_to: "2026-07-31" },
      });
      expect(get).toHaveBeenCalledWith("/trainers/9/earnings/summary/", {
        params: { date_from: "2026-07-01", date_to: "2026-07-31" },
      });
    });
  });

  it("renders correction rows instead of an empty state for adjustment-only salary", async () => {
    mockAdjustmentOnlySalary();

    renderSalaryDetailPage();

    expect((await screen.findAllByText("Корректировка")).length).toBeGreaterThan(0);
    expect(screen.getByText("manual penalty")).toBeInTheDocument();
    expect(screen.getAllByText(/-500\s₽/).length).toBeGreaterThan(0);
    expect(screen.queryByText("Нет начислений")).not.toBeInTheDocument();
  });

  it("keeps a successful summary visible when the earning rows fail", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/trainers/9/earnings/") {
        return Promise.reject(new Error("earnings unavailable"));
      }
      if (url === "/trainers/9/earnings/summary/") {
        return Promise.resolve({
          data: { total_amount: "2750.00", total_sessions: 5, by_type: {} },
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${url}`));
    });

    renderSalaryDetailPage();

    expect((await screen.findAllByText(/2\s750\s₽/)).length).toBeGreaterThan(0);
    expect(screen.getByText("5 тренировок")).toBeInTheDocument();
    expect(screen.getByText("Не удалось загрузить начисления")).toBeInTheDocument();
    expect(screen.queryByText("Нет начислений")).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Повторить загрузку начислений" }),
    ).toBeInTheDocument();
  });
});
