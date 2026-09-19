import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "@/features/auth/auth-store";
import { useBrandingStore } from "@/features/branding/use-branding";
import Profile from "./profile";

const { get, put, requestPermission } = vi.hoisted(() => ({
  get: vi.fn(),
  put: vi.fn(),
  requestPermission: vi.fn(),
}));

vi.mock("@/api/custom-fetch", () => ({
  default: { get, put },
}));

vi.mock("@/features/notifications/hooks/use-push-subscription", () => ({
  usePushSubscription: () => ({
    isPushSupported: true,
    permission: "granted",
    prompted: true,
    requestPermission,
    unsubscribe: vi.fn(),
  }),
}));

function renderProfilePage() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  return render(
    <MemoryRouter initialEntries={["/trainer/profile"]}>
      <QueryClientProvider client={queryClient}>
        <Routes>
          <Route path="/trainer/profile" element={<Profile />} />
          <Route path="/trainer/salary" element={<div>salary-route</div>} />
          <Route path="/login" element={<div>login-route</div>} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

function mockProfileApi(disabledCategories: string[] = []) {
  get.mockImplementation((url: string) => {
    if (url === "/trainers/me/") {
      return Promise.resolve({
        data: {
          id: 9,
          first_name: "Ada",
          last_name: "Coach",
          student_count: 12,
        },
      });
    }

    if (url === "/trainers/9/earnings/summary/") {
      return Promise.resolve({
        data: {
          total_amount: "12345",
          total_sessions: 7,
        },
      });
    }

    if (url === "/notifications/preferences/") {
      return Promise.resolve({
        data: {
          disabled_categories: disabledCategories,
        },
      });
    }

    return Promise.reject(new Error(`Unexpected GET ${url}`));
  });
}

describe("Trainer Profile", () => {
  beforeEach(() => {
    localStorage.clear();
    get.mockReset();
    put.mockReset();
    requestPermission.mockReset();
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ ok: true })));
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
    useBrandingStore.setState({
      primaryColor: "#111111",
      accentColor: "#f5c542",
      clubName: "Jaguar North",
      logoUrl: "",
    });
  });

  it("shows trainer profile context and saves trainer task notification preference changes", async () => {
    mockProfileApi();
    put.mockResolvedValue({ data: {} });

    renderProfilePage();

    expect(await screen.findByText("Ada Coach")).toBeInTheDocument();
    expect(screen.getByText("Jaguar North")).toBeInTheDocument();
    expect(await screen.findByText(/12\s345\s₽ за текущий месяц/)).toBeInTheDocument();

    const taskSwitch = await screen.findByRole("switch", { name: "Задачи" });
    expect(taskSwitch).toBeChecked();

    fireEvent.click(taskSwitch);

    await waitFor(
      () =>
        expect(put).toHaveBeenCalledWith("/notifications/preferences/", {
          disabled_categories: ["trainer_tasks"],
        }),
      { timeout: 1000 },
    );
  });

  it("opens salary from the profile salary entry", async () => {
    mockProfileApi();

    renderProfilePage();

    fireEvent.click(await screen.findByRole("button", { name: /мой заработок/i }));

    expect(screen.getByText("salary-route")).toBeInTheDocument();
  });

  it("requires confirmation before logging out and then clears trainer auth", async () => {
    mockProfileApi();

    renderProfilePage();

    fireEvent.click(await screen.findByRole("button", { name: "Выйти из аккаунта" }));
    expect(screen.getByText("Вы уверены?")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Отмена" }));
    expect(
      screen.getByRole("button", { name: "Выйти из аккаунта" }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Выйти из аккаунта" }));
    fireEvent.click(screen.getByRole("button", { name: "Выйти" }));

    await waitFor(() => expect(screen.getByText("login-route")).toBeInTheDocument());
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(fetch).toHaveBeenCalledWith(
      "/api/auth/logout/",
      expect.objectContaining({ method: "POST" }),
    );
  });
});
