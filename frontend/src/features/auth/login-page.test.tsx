import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore, type UserRole } from "./auth-store";
import LoginPage from "./login-page";

const { create, navigateToDocumentRoute, post, isAxiosError } = vi.hoisted(() => {
  const mockedApiClient = Object.assign(vi.fn(), {
    interceptors: {
      request: { use: vi.fn() },
      response: { use: vi.fn() },
    },
  });
  return {
    create: vi.fn(() => mockedApiClient),
    navigateToDocumentRoute: vi.fn(),
    post: vi.fn(),
    isAxiosError: vi.fn(() => false),
  };
});

vi.mock("axios", () => ({
  default: { create, post, isAxiosError },
  create,
  post,
  isAxiosError,
}));

vi.mock("@/lib/document-routes", () => ({
  isDocumentRoute: (path: string) => path.startsWith("/dashboard/"),
  navigateToDocumentRoute,
}));

function createJwt({
  withMembership = true,
  role = "parent",
}: { withMembership?: boolean; role?: UserRole } = {}) {
  const payload = btoa(
    JSON.stringify(
      withMembership
        ? {
            exp: Math.floor(Date.now() / 1000) + 60 * 60,
            role,
            club_id: 7,
          }
        : {
            exp: Math.floor(Date.now() / 1000) + 60 * 60,
          },
    ),
  );
  return `eyJhbGciOiJIUzI1NiJ9.${payload}.sig`;
}

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="current-location">{`${location.pathname}${location.search}${location.hash}`}</div>;
}

function renderLoginWithInviteReturn() {
  return render(
    <MemoryRouter
      initialEntries={[
        {
          pathname: "/login",
          state: {
            from: {
              pathname: "/parent-invite/11111111-1111-4111-8111-111111111111",
              search: "?source=card",
              hash: "#accept",
            },
          },
        },
      ]}
    >
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/parent-invite/:token" element={<LocationProbe />} />
      </Routes>
    </MemoryRouter>,
  );
}

function renderLoginWithAppReturn() {
  return render(
    <MemoryRouter
      initialEntries={[
        {
          pathname: "/login",
          state: {
            from: {
              pathname: "/app",
            },
          },
        },
      ]}
    >
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/app" element={<LocationProbe />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("LoginPage", () => {
  beforeEach(() => {
    localStorage.clear();
    create.mockClear();
    navigateToDocumentRoute.mockReset();
    post.mockReset();
    isAxiosError.mockReset();
    isAxiosError.mockReturnValue(false);
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

  it("preserves the full return location after login", async () => {
    post.mockResolvedValue({
      data: {
        meta: {
          access_token: createJwt(),
          refresh_token: "refresh-token",
        },
      },
    });

    renderLoginWithInviteReturn();

    fireEvent.change(screen.getByLabelText(/Email/), {
      target: { value: "parent@example.com" },
    });
    fireEvent.change(screen.getByLabelText("Пароль"), {
      target: { value: "password" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Войти" }));

    await waitFor(() => {
      expect(screen.getByTestId("current-location")).toHaveTextContent(
        "/parent-invite/11111111-1111-4111-8111-111111111111?source=card#accept",
      );
    });
  });

  it("stores the login refresh token through the backend cookie bridge instead of persistent JS storage", async () => {
    const accessToken = createJwt();
    post
      .mockResolvedValueOnce({
        data: {
          meta: {
            access_token: accessToken,
            refresh_token: "login-refresh-token",
          },
        },
      })
      .mockResolvedValueOnce({ status: 204 });

    renderLoginWithAppReturn();

    fireEvent.change(screen.getByLabelText(/Email/), {
      target: { value: "parent@example.com" },
    });
    fireEvent.change(screen.getByLabelText("Пароль"), {
      target: { value: "password" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Войти" }));

    await waitFor(() => {
      expect(screen.getByTestId("current-location")).toHaveTextContent("/app");
    });

    expect(post).toHaveBeenNthCalledWith(2, "/api/auth/refresh-cookie/", {
      refresh_token: "login-refresh-token",
    }, {
      headers: { Authorization: `Bearer ${accessToken}` },
      withCredentials: true,
    });
    expect(useAuthStore.getState().refreshToken).not.toBe("login-refresh-token");
    expect(localStorage.getItem("jaguar-auth") ?? "").not.toContain(
      "login-refresh-token",
    );
  });

  it("allows no-club users to return to a parent invite after login", async () => {
    post.mockResolvedValue({
      data: {
        meta: {
          access_token: createJwt({ withMembership: false }),
          refresh_token: "refresh-token",
        },
      },
    });

    renderLoginWithInviteReturn();

    fireEvent.change(screen.getByLabelText(/Email/), {
      target: { value: "parent@example.com" },
    });
    fireEvent.change(screen.getByLabelText("Пароль"), {
      target: { value: "password" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Войти" }));

    await waitFor(() => {
      expect(screen.getByTestId("current-location")).toHaveTextContent(
        "/parent-invite/11111111-1111-4111-8111-111111111111?source=card#accept",
      );
    });
    expect(useAuthStore.getState().isAuthenticated).toBe(true);
    expect(useAuthStore.getState().role).toBeNull();
    expect(useAuthStore.getState().clubId).toBeNull();
  });

  it("returns users through the unified /app entry after login", async () => {
    post.mockResolvedValue({
      data: {
        meta: {
          access_token: createJwt(),
          refresh_token: "refresh-token",
        },
      },
    });

    renderLoginWithAppReturn();

    fireEvent.change(screen.getByLabelText(/Email/), {
      target: { value: "parent@example.com" },
    });
    fireEvent.change(screen.getByLabelText("Пароль"), {
      target: { value: "password" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Войти" }));

    await waitFor(() => {
      expect(screen.getByTestId("current-location")).toHaveTextContent("/app");
    });
  });

  it("allows no-club users to return through /app and see the pending membership state", async () => {
    post.mockResolvedValue({
      data: {
        meta: {
          access_token: createJwt({ withMembership: false }),
          refresh_token: "refresh-token",
        },
      },
    });
    useAuthStore.setState({
      accessToken: "stale-access",
      refreshToken: "stale-refresh",
      role: "trainer",
      clubId: 3,
      trainerId: 42,
      studentId: null,
      isAuthenticated: true,
    });

    renderLoginWithAppReturn();

    fireEvent.change(screen.getByLabelText(/Email/), {
      target: { value: "new@example.com" },
    });
    fireEvent.change(screen.getByLabelText("Пароль"), {
      target: { value: "password" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Войти" }));

    await waitFor(() => {
      expect(screen.getByTestId("current-location")).toHaveTextContent("/app");
    });
    expect(useAuthStore.getState()).toMatchObject({
      role: null,
      clubId: null,
      trainerId: null,
      studentId: null,
      isAuthenticated: true,
    });
  });

  it.each(["owner", "admin"] as const)(
    "uses document navigation for %s dashboard login",
    async (role) => {
      post.mockResolvedValue({
        data: {
          meta: {
            access_token: createJwt({ role }),
            refresh_token: "refresh-token",
          },
        },
      });

      render(
        <MemoryRouter initialEntries={["/login"]}>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
          </Routes>
        </MemoryRouter>,
      );

      fireEvent.change(screen.getByLabelText(/Email/), {
        target: { value: `${role}@example.com` },
      });
      fireEvent.change(screen.getByLabelText("Пароль"), {
        target: { value: "password" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Войти" }));

      await waitFor(() => {
        expect(navigateToDocumentRoute).toHaveBeenCalledWith(
          "/dashboard/login/",
        );
      });
    },
  );

  it("accepts a phone username in the login field", async () => {
    const phoneLogin = ["+7", "900", "123", "45", "67"].join("");
    const password = ["password", "for", "test"].join("-");
    post.mockResolvedValue({
      data: {
        meta: {
          access_token: createJwt({ role: "student" }),
          refresh_token: "refresh-token",
        },
      },
    });

    render(
      <MemoryRouter initialEntries={["/login"]}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/student" element={<LocationProbe />} />
        </Routes>
      </MemoryRouter>,
    );

    const loginInput = screen.getByLabelText(/Email/);
    expect(loginInput).toHaveAttribute("type", "text");

    fireEvent.change(loginInput, {
      target: { value: phoneLogin },
    });
    fireEvent.change(screen.getByLabelText("Пароль"), {
      target: { value: password },
    });
    fireEvent.click(screen.getByRole("button", { name: "Войти" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalled();
    });
    const submittedPhoneLogin = post.mock.calls.some(([url, payload]) => {
      const body = payload as Record<string, unknown>;
      return (
        url === "/_allauth/app/v1/auth/login" &&
        body.username === phoneLogin &&
        body.password === password &&
        body.email === undefined
      );
    });
    expect(submittedPhoneLogin).toBe(true);
  });
});
