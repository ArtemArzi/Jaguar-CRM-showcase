import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "@/features/auth/auth-store";
import { useBrandingStore } from "@/features/branding/use-branding";
import Salary from "./salary";

const { get } = vi.hoisted(() => ({
  get: vi.fn(),
}));

vi.mock("@/api/custom-fetch", () => ({
  default: { get },
}));

function renderSalaryPage() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <Salary />
    </QueryClientProvider>,
  );
}

function mockAdjustmentOnlySalary() {
  get.mockImplementation((url: string) => {
    if (url === "/trainers/9/earnings/") {
      return Promise.resolve({
        data: [
          {
            id: 21,
            row_type: "adjustment",
            earning_type: "manual_adjustment",
            amount: "750.00",
            rate_percent: "0.00",
            subscription_price: null,
            checkin_date: "2026-06-15",
            schedule_name: "",
            adjustment_direction: "credit",
            adjustment_reason: "manual bonus",
            adjustment_effective_date: "2026-06-15",
          },
        ],
      });
    }
    if (url === "/trainers/9/earnings/summary/") {
      return Promise.resolve({
        data: {
          total_amount: "750.00",
          total_sessions: 0,
          by_type: {
            manual_adjustment: { count: 1, total: "750.00" },
          },
        },
      });
    }
    return Promise.reject(new Error(`Unexpected GET ${url}`));
  });
}

describe("Trainer Salary page", () => {
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

    renderSalaryPage();

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

    renderSalaryPage();

    expect((await screen.findAllByText("Корректировка")).length).toBeGreaterThan(0);
    expect(screen.getByText("manual bonus")).toBeInTheDocument();
    expect(screen.getAllByText(/750\s₽/).length).toBeGreaterThan(0);
    expect(screen.queryByText("Нет начислений за период")).not.toBeInTheDocument();
  });

  it("does not turn a failed salary summary into zero and recovers independently", async () => {
    let summaryAttempts = 0;
    get.mockImplementation((url: string) => {
      if (url === "/trainers/9/earnings/") return Promise.resolve({ data: [] });
      if (url === "/trainers/9/earnings/summary/") {
        summaryAttempts += 1;
        return summaryAttempts === 1
          ? Promise.reject(new Error("summary unavailable"))
          : Promise.resolve({
              data: { total_amount: "1250.00", total_sessions: 3, by_type: {} },
            });
      }
      return Promise.reject(new Error(`Unexpected GET ${url}`));
    });

    renderSalaryPage();

    expect(await screen.findByText("Не удалось загрузить итог зарплаты")).toBeInTheDocument();
    expect(screen.queryByText(/0\s₽/)).not.toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", { name: "Повторить загрузку итога" }),
    );

    expect((await screen.findAllByText(/1\s250\s₽/)).length).toBeGreaterThan(0);
    expect(screen.getByText("3 тренировок")).toBeInTheDocument();
  });

  it("shows a real zero and empty list only after both requests succeed", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/trainers/9/earnings/") return Promise.resolve({ data: [] });
      if (url === "/trainers/9/earnings/summary/") {
        return Promise.resolve({
          data: { total_amount: "0.00", total_sessions: 0, by_type: {} },
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${url}`));
    });

    renderSalaryPage();

    expect((await screen.findAllByText(/0\s₽/)).length).toBeGreaterThan(0);
    expect(screen.getByText("Нет начислений за период")).toBeInTheDocument();
  });
});
