import { StrictMode, Suspense, lazy } from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import {
  Outlet,
  RouterProvider,
  createMemoryRouter,
} from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  AuthGuard,
  resetAuthGuardSessionRestoreForTests,
} from "@/features/auth/auth-guard";
import { useAuthStore } from "@/features/auth/auth-store";

const { refreshAccessToken } = vi.hoisted(() => ({
  refreshAccessToken: vi.fn<(refreshToken: string) => Promise<string>>(),
}));

vi.mock("@/api/custom-fetch", () => ({
  refreshAccessToken,
}));

function createValidJwt() {
  const payload = btoa(
    JSON.stringify({
      exp: Math.floor(Date.now() / 1000) + 60 * 60,
      role: "parent",
      club_id: 1,
    }),
  );
  return `eyJhbGciOiJIUzI1NiJ9.${payload}.sig`;
}

function TestParentShell() {
  return (
    <div>
      <div>parent-shell</div>
      <Suspense fallback={<div>parent-route-loading</div>}>
        <Outlet />
      </Suspense>
    </div>
  );
}

const LazyParentChild = lazy(async () => ({
  default: function ParentChildPage() {
    return <div>parent-child-page</div>;
  },
}));

function renderParentReloadRoute(initialPath: string) {
  const router = createMemoryRouter(
    [
      {
        path: "/login",
        element: <div>login-page</div>,
      },
      {
        path: "/parent",
        element: (
          <Suspense fallback={<div>app-loading</div>}>
            <AuthGuard role="parent">
              <TestParentShell />
            </AuthGuard>
          </Suspense>
        ),
        children: [
          { index: true, element: <div>parent-home-page</div> },
          { path: "child/:childId", element: <LazyParentChild /> },
        ],
      },
    ],
    { initialEntries: [initialPath] },
  );

  return render(
    <StrictMode>
      <RouterProvider router={router} />
    </StrictMode>,
  );
}

describe("parent route reload recovery", () => {
  beforeEach(() => {
    localStorage.clear();
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

  it("recovers a parent deep-link after silent refresh without duplicate refresh calls", async () => {
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
      role: "parent",
      clubId: 1,
      trainerId: null,
      studentId: null,
      authBootstrapStatus: "idle",
      studentBootstrapStatus: "resolved",
      studentBootstrapError: null,
      isAuthenticated: true,
    });

    renderParentReloadRoute("/parent/child/1");

    expect(
      screen.getByText("Восстанавливаем сессию..."),
    ).toBeInTheDocument();

    await waitFor(() => {
      expect(screen.getByText("parent-shell")).toBeInTheDocument();
      expect(screen.getByText("parent-child-page")).toBeInTheDocument();
    });

    expect(refreshAccessToken).toHaveBeenCalledTimes(1);
    expect(refreshAccessToken).toHaveBeenCalledWith("refresh-token");
  });
});
