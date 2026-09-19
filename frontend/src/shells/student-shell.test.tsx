import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import StudentShell from "./student-shell";
import { useAuthStore } from "@/features/auth/auth-store";

const { get } = vi.hoisted(() => ({
  get: vi.fn(),
}));

vi.mock("@/api/custom-fetch", () => ({
  default: { get },
}));

vi.mock("@/features/branding/branding-provider", () => ({
  BrandingProvider: ({ children }: { children: ReactNode }) => (
    <div data-testid="branding-provider">{children}</div>
  ),
}));

vi.mock("@/features/student/components/student-branded-header", () => ({
  StudentBrandedHeader: () => <div>student-header</div>,
}));

vi.mock("@/features/student/components/student-bottom-nav", () => ({
  StudentBottomNav: () => <div>student-nav</div>,
}));

function renderStudentShell() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  return render(
    <MemoryRouter initialEntries={["/student"]}>
      <QueryClientProvider client={queryClient}>
        <Routes>
          <Route path="/student" element={<StudentShell />}>
            <Route index element={<div>student-content</div>} />
          </Route>
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

describe("StudentShell", () => {
  beforeEach(() => {
    localStorage.clear();
    get.mockReset();
    useAuthStore.setState({
      accessToken: null,
      refreshToken: null,
      role: "student",
      clubId: 1,
      trainerId: null,
      studentId: null,
      isAuthenticated: true,
      studentBootstrapStatus: "idle",
      studentBootstrapError: null,
    });
  });

  it("shows an explicit bootstrap error instead of hanging on loading forever", async () => {
    get.mockRejectedValueOnce(new Error("bootstrap failed"));

    renderStudentShell();

    expect(
      await screen.findByText("Не удалось открыть кабинет ученика"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Попробуйте снова. Пока данные ученика не подтверждены, разделы кабинета скрыты."),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Повторить" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("student-content")).not.toBeInTheDocument();
  });

  it("recovers after retry when bootstrap eventually succeeds", async () => {
    get
      .mockRejectedValueOnce(new Error("bootstrap failed"))
      .mockResolvedValueOnce({ data: { id: 7 } });

    renderStudentShell();

    const retryButton = await screen.findByRole("button", { name: "Повторить" });
    fireEvent.click(retryButton);

    expect(await screen.findByText("student-content")).toBeInTheDocument();

    await waitFor(() => {
      expect(useAuthStore.getState().studentId).toBe(7);
      expect(useAuthStore.getState().studentBootstrapStatus).toBe("resolved");
      expect(useAuthStore.getState().studentBootstrapError).toBeNull();
    });
  });

  it("keeps the outlet visible when studentId is already known", async () => {
    get.mockRejectedValueOnce(new Error("should not be called"));
    useAuthStore.setState({
      accessToken: null,
      refreshToken: null,
      role: "student",
      clubId: 1,
      trainerId: null,
      studentId: 7,
      isAuthenticated: true,
      studentBootstrapStatus: "idle",
      studentBootstrapError: null,
    });

    renderStudentShell();

    expect(await screen.findByText("student-content")).toBeInTheDocument();
    expect(screen.queryByText("Не удалось открыть кабинет ученика")).not.toBeInTheDocument();
    expect(get).not.toHaveBeenCalled();
  });
});
