import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "@/features/auth/auth-store";
import TrainerShell from "./trainer-shell";

const { get } = vi.hoisted(() => ({ get: vi.fn() }));

vi.mock("@/api/custom-fetch", () => ({ default: { get } }));
vi.mock("react-router", () => ({ Outlet: () => <div>trainer-outlet</div> }));
vi.mock("@/features/branding/branding-provider", () => ({
  BrandingProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("@/features/trainer/components/branded-header", () => ({
  BrandedHeader: () => <div>trainer-header</div>,
}));
vi.mock("@/features/trainer/components/bottom-nav", () => ({
  BottomNav: () => <div>trainer-nav</div>,
}));

function jwt(subject: string) {
  return `header.${btoa(JSON.stringify({ sub: subject, role: "trainer", club_id: 1 }))}.sig`;
}

function renderShell() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <TrainerShell />
    </QueryClientProvider>,
  );
}

describe("TrainerShell identity bootstrap", () => {
  beforeEach(() => {
    get.mockReset();
    useAuthStore.setState({
      accessToken: jwt("user-1"),
      refreshToken: "session-cookie",
      role: "trainer",
      clubId: 1,
      trainerId: null,
      studentId: null,
      authBootstrapStatus: "ready",
      studentBootstrapStatus: "resolved",
      studentBootstrapError: null,
      isAuthenticated: true,
    });
  });

  it("blocks the trainer outlet on bootstrap failure and recovers through retry", async () => {
    get
      .mockRejectedValueOnce(new Error("identity unavailable"))
      .mockResolvedValueOnce({ data: { id: 9 } });

    renderShell();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Не удалось загрузить профиль тренера",
    );
    expect(screen.queryByText("trainer-outlet")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Повторить" }));

    expect(await screen.findByText("trainer-outlet")).toBeInTheDocument();
    expect(useAuthStore.getState().trainerId).toBe(9);
  });

  it("gives a concrete next action when the account has no trainer profile", async () => {
    get.mockRejectedValue({ response: { status: 404 } });

    renderShell();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Профиль тренера не привязан",
    );
    expect(screen.getByText(/владельцу или администратору/i)).toBeInTheDocument();
    expect(screen.queryByText("trainer-outlet")).not.toBeInTheDocument();
  });

  it("ignores a late identity response from the previous auth context", async () => {
    let resolveOld!: (value: { data: { id: number } }) => void;
    const oldResponse = new Promise<{ data: { id: number } }>((resolve) => {
      resolveOld = resolve;
    });
    get
      .mockReturnValueOnce(oldResponse)
      .mockResolvedValueOnce({ data: { id: 22 } });

    renderShell();
    await waitFor(() => expect(get).toHaveBeenCalledTimes(1));

    useAuthStore.setState({
      accessToken: jwt("user-2"),
      role: "trainer",
      clubId: 2,
      trainerId: null,
    });

    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("trainer-outlet")).toBeInTheDocument();
    expect(useAuthStore.getState().trainerId).toBe(22);

    resolveOld({ data: { id: 9 } });
    await waitFor(() => expect(useAuthStore.getState().trainerId).toBe(22));
  });
});
