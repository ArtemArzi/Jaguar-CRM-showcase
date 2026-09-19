import { act, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AuthGuard, resetAuthGuardSessionRestoreForTests } from "./auth-guard";
import { useAuthStore } from "./auth-store";

const { navigateToDocumentRoute, refreshAccessToken } = vi.hoisted(() => ({
  navigateToDocumentRoute: vi.fn(),
  refreshAccessToken: vi.fn<(refreshToken: string) => Promise<string>>(),
}));

vi.mock("@/api/custom-fetch", () => ({
  refreshAccessToken,
}));

vi.mock("@/lib/document-routes", () => ({
  isDocumentRoute: (path: string) => path.startsWith("/dashboard/"),
  navigateToDocumentRoute,
}));

function createValidJwt(role = "trainer") {
  const payload = btoa(
    JSON.stringify({
      exp: Math.floor(Date.now() / 1000) + 60 * 60,
      role,
      club_id: 1,
    }),
  );
  return `eyJhbGciOiJIUzI1NiJ9.${payload}.sig`;
}

function renderGuard(children: ReactNode) {
  return render(
    <MemoryRouter initialEntries={["/trainer"]}>
      <Routes>
        <Route path="/login" element={<div>login-page</div>} />
        <Route
          path="/trainer"
          element={<AuthGuard role="trainer">{children}</AuthGuard>}
        />
      </Routes>
    </MemoryRouter>,
  );
}

describe("AuthGuard", () => {
  beforeEach(() => {
    localStorage.clear();
    navigateToDocumentRoute.mockReset();
    refreshAccessToken.mockReset();
    resetAuthGuardSessionRestoreForTests();
    useAuthStore.setState({
      accessToken: null,
      refreshToken: null,
      role: null,
      clubId: null,
      trainerId: null,
      studentId: null,
      authBootstrapStatus: "idle",
      studentBootstrapStatus: "idle",
      studentBootstrapError: null,
      isAuthenticated: false,
    });
  });

  it("waits for silent refresh before rendering protected content", async () => {
    const protectedRender = vi.fn();
    function ProtectedProbe() {
      protectedRender();
      return <div>trainer-home</div>;
    }
    refreshAccessToken.mockImplementation(
      () => new Promise<string>(() => {}),
    );

    useAuthStore.setState({
      accessToken: null,
      refreshToken: "refresh-token",
      role: "trainer",
      clubId: 1,
      trainerId: 1,
      studentId: null,
      isAuthenticated: true,
    });

    renderGuard(<ProtectedProbe />);

    expect(refreshAccessToken).toHaveBeenCalledWith("refresh-token");
    expect(protectedRender).not.toHaveBeenCalled();
    expect(screen.queryByText("login-page")).not.toBeInTheDocument();
    expect(
      screen.getByText("Восстанавливаем сессию..."),
    ).toBeInTheDocument();
  });

  it("renders protected content after silent refresh succeeds", async () => {
    refreshAccessToken.mockImplementation(async () => {
      await act(async () => {
        useAuthStore
          .getState()
          .setTokens(createValidJwt(), "fresh-refresh");
      });
      return createValidJwt();
    });

    useAuthStore.setState({
      accessToken: null,
      refreshToken: "refresh-token",
      role: "trainer",
      clubId: 1,
      trainerId: 1,
      studentId: null,
      isAuthenticated: true,
    });

    renderGuard(<div>trainer-home</div>);

    await waitFor(() => {
      expect(screen.getByText("trainer-home")).toBeInTheDocument();
    });
    expect(screen.queryByText("login-page")).not.toBeInTheDocument();
  });

  it.each(["owner", "admin"] as const)(
    "uses document navigation when %s role mismatch target is the dashboard",
    async (role) => {
      useAuthStore.setState({
        accessToken: createValidJwt(role),
        refreshToken: "refresh-token",
        role,
        clubId: 1,
        trainerId: null,
        studentId: null,
        isAuthenticated: true,
      });

      renderGuard(<div>trainer-home</div>);

      await waitFor(() => {
        expect(navigateToDocumentRoute).toHaveBeenCalledWith(
          "/dashboard/login/",
        );
      });
      expect(screen.queryByText("trainer-home")).not.toBeInTheDocument();
    },
  );
});
