import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { RouterProvider, createMemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "@/features/auth/auth-store";
import ParentInvitePage from "./parent-invite";

const { post, refreshAccessToken } = vi.hoisted(() => ({
  post: vi.fn(),
  refreshAccessToken: vi.fn<(refreshToken: string) => Promise<string>>(),
}));

vi.mock("@/api/custom-fetch", () => ({
  default: { post },
  refreshAccessToken,
}));

const INVITE_TOKEN = "11111111-1111-4111-8111-111111111111";
const INVITE_STORAGE_KEY = "jaguar-parent-invite-token";

function createParentJwt(clubId: number) {
  const payload = btoa(
    JSON.stringify({
      exp: Math.floor(Date.now() / 1000) + 60 * 60,
      role: "parent",
      club_id: clubId,
    }),
  );
  return `eyJhbGciOiJIUzI1NiJ9.${payload}.sig`;
}

function resetAuthStore() {
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
}

function renderInviteRoute(initialPath = `/parent-invite/${INVITE_TOKEN}`) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const router = createMemoryRouter(
    [
      { path: "/login", element: <div>login-page</div> },
      { path: "/parent", element: <div>parent-home</div> },
      { path: "/parent-invite/:token", element: <ParentInvitePage /> },
    ],
    { initialEntries: [initialPath] },
  );

  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return router;
}

function apiError(code?: string) {
  return {
    response: {
      status: code ? 400 : 422,
      data: {
        code,
        detail: "raw backend detail should not be shown",
      },
    },
  };
}

describe("ParentInvitePage", () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    post.mockReset();
    refreshAccessToken.mockReset();
    resetAuthStore();
  });

  it("redirects unauthenticated users to login while preserving the invite route and token", async () => {
    const router = renderInviteRoute();

    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/login");
    });

    expect(post).not.toHaveBeenCalled();
    expect(sessionStorage.getItem(INVITE_STORAGE_KEY)).toBe(INVITE_TOKEN);
    expect(router.state.location.state).toMatchObject({
      from: { pathname: `/parent-invite/${INVITE_TOKEN}` },
    });
  });

  it("accepts an invite, applies the returned parent token, redirects to parent, and removes token from URL", async () => {
    post.mockResolvedValue({
      data: {
        student_id: 10,
        student_name: "Masha Ivanova",
        club_id: 7,
        club_name: "Jaguar",
        access_token: createParentJwt(7),
      },
    });
    useAuthStore.setState({
      accessToken: "old-access",
      refreshToken: "refresh-token",
      role: "student",
      clubId: 3,
      isAuthenticated: true,
    });

    const router = renderInviteRoute();

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/parents/accept-invite/", {
        token: INVITE_TOKEN,
      });
    });
    expect(refreshAccessToken).not.toHaveBeenCalled();

    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/parent");
    });
    expect(router.state.location.pathname).not.toContain(INVITE_TOKEN);
    expect(sessionStorage.getItem(INVITE_STORAGE_KEY)).toBeNull();
    expect(useAuthStore.getState().role).toBe("parent");
    expect(useAuthStore.getState().clubId).toBe(7);
  });

  it("rejects invite completion when the returned token does not match the accepted club", async () => {
    post.mockResolvedValue({
      data: {
        student_id: 10,
        student_name: "Masha Ivanova",
        club_id: 7,
        club_name: "Jaguar",
        access_token: createParentJwt(3),
      },
    });
    useAuthStore.setState({
      accessToken: "old-access",
      refreshToken: "refresh-token",
      role: "student",
      clubId: 3,
      isAuthenticated: true,
    });

    const router = renderInviteRoute();

    expect(
      await screen.findByText("Приглашение принято, но доступ к клубу не обновился. Выйдите и войдите снова."),
    ).toBeInTheDocument();
    expect(router.state.location.pathname).toBe(`/parent-invite/${INVITE_TOKEN}`);
    expect(useAuthStore.getState().role).toBe("student");
    expect(useAuthStore.getState().clubId).toBe(3);
  });

  it.each([
    ["invite_expired", "Срок действия приглашения истёк."],
    ["invite_used", "Это приглашение уже использовано."],
    ["invite_not_found", "Ссылка приглашения не найдена или введена с ошибкой."],
    ["already_member", "Этот аккаунт уже состоит в клубе."],
    ["parent_already_linked", "У ребёнка уже привязан родитель."],
    ["not_child", "Приглашение доступно только для детской анкеты."],
  ])("shows safe invite acceptance copy for %s", async (code, message) => {
    post.mockRejectedValue(apiError(code));
    useAuthStore.setState({
      accessToken: "access",
      refreshToken: "refresh-token",
      role: "student",
      clubId: 3,
      isAuthenticated: true,
    });

    renderInviteRoute();

    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(screen.queryByText("raw backend detail should not be shown")).not.toBeInTheDocument();
  });

  it("shows a safe message for malformed invite tokens without posting them", async () => {
    useAuthStore.setState({
      accessToken: "access",
      refreshToken: "refresh-token",
      role: "student",
      clubId: 3,
      isAuthenticated: true,
    });

    renderInviteRoute("/parent-invite/not-a-token");

    expect(
      await screen.findByText("Ссылка приглашения не найдена или введена с ошибкой."),
    ).toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });
});
