import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  SERVER_REFRESH_TOKEN_SENTINEL,
  useAuthStore,
  type UserRole,
} from "@/features/auth/auth-store";

const { apiClient, post } = vi.hoisted(() => {
  const mockedApiClient = Object.assign(vi.fn(), {
    interceptors: {
      request: { use: vi.fn() },
      response: { use: vi.fn() },
    },
  });
  return {
    apiClient: mockedApiClient,
    post: vi.fn(),
  };
});

vi.mock("axios", () => ({
  default: {
    create: vi.fn(() => apiClient),
    post,
  },
}));

import { refreshAccessToken } from "./custom-fetch";

function createJwt({
  role = "parent",
  clubId = 7,
}: {
  role?: UserRole | null;
  clubId?: number;
} = {}) {
  const payload =
    role === null
      ? { exp: Math.floor(Date.now() / 1000) + 60 * 60 }
      : {
          exp: Math.floor(Date.now() / 1000) + 60 * 60,
          role,
          club_id: clubId,
        };
  return `eyJhbGciOiJIUzI1NiJ9.${btoa(JSON.stringify(payload))}.sig`;
}

function resetAuthState() {
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

describe("refreshAccessToken", () => {
  beforeEach(() => {
    post.mockReset();
    resetAuthState();
  });

  it("syncs role and club from the refreshed access token", async () => {
    const accessToken = createJwt({ role: "parent", clubId: 9 });
    post.mockResolvedValue({
      data: {
        access_token: accessToken,
      },
    });

    await expect(refreshAccessToken("old-refresh")).resolves.toBe(accessToken);

    expect(post).toHaveBeenCalledWith("/api/auth/refresh/", null, {
      withCredentials: true,
    });
    expect(useAuthStore.getState()).toMatchObject({
      accessToken,
      refreshToken: SERVER_REFRESH_TOKEN_SENTINEL,
      role: "parent",
      clubId: 9,
      isAuthenticated: true,
    });
  });

  it("refreshes through the backend-managed refresh cookie without sending a JS refresh token", async () => {
    const accessToken = createJwt({ role: "parent", clubId: 9 });
    post.mockResolvedValue({
      data: {
        access_token: accessToken,
      },
    });

    await expect(refreshAccessToken()).resolves.toBe(accessToken);

    expect(post).toHaveBeenCalledWith("/api/auth/refresh/", null, {
      withCredentials: true,
    });
    expect(useAuthStore.getState()).toMatchObject({
      accessToken,
      role: "parent",
      clubId: 9,
      isAuthenticated: true,
    });
    expect(JSON.stringify(useAuthStore.getState())).not.toContain("refresh_token");
  });

  it("coalesces direct guard and interceptor refreshes onto one cookie rotation", async () => {
    const accessToken = createJwt({ role: "trainer", clubId: 3 });
    let resolveRefresh: ((value: { data: { access_token: string } }) => void) | undefined;
    post.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveRefresh = resolve;
        }),
    );

    const guardRefresh = refreshAccessToken(SERVER_REFRESH_TOKEN_SENTINEL);
    const interceptorRefresh = refreshAccessToken(SERVER_REFRESH_TOKEN_SENTINEL);

    expect(post).toHaveBeenCalledTimes(1);
    resolveRefresh?.({ data: { access_token: accessToken } });

    await expect(Promise.all([guardRefresh, interceptorRefresh])).resolves.toEqual([
      accessToken,
      accessToken,
    ]);
    expect(post).toHaveBeenCalledTimes(1);
  });

  it("clears stale role-specific ids when refreshed membership changes", async () => {
    const accessToken = createJwt({ role: "parent", clubId: 9 });
    useAuthStore.setState({
      accessToken: "old-access",
      refreshToken: "old-refresh",
      role: "trainer",
      clubId: 3,
      trainerId: 42,
      studentId: 77,
      isAuthenticated: true,
    });
    post.mockResolvedValue({
      data: {
        access_token: accessToken,
      },
    });

    await refreshAccessToken("old-refresh");

    expect(useAuthStore.getState()).toMatchObject({
      accessToken,
      refreshToken: SERVER_REFRESH_TOKEN_SENTINEL,
      role: "parent",
      clubId: 9,
      trainerId: null,
      studentId: null,
      studentBootstrapStatus: "resolved",
    });
  });

  it("clears stale role-specific ids when refreshed membership keeps the same role and club", async () => {
    const accessToken = createJwt({ role: "trainer", clubId: 3 });
    useAuthStore.setState({
      accessToken: "old-access",
      refreshToken: "old-refresh",
      role: "trainer",
      clubId: 3,
      trainerId: 42,
      studentId: 77,
      isAuthenticated: true,
    });
    post.mockResolvedValue({
      data: {
        access_token: accessToken,
      },
    });

    await refreshAccessToken("old-refresh");

    expect(useAuthStore.getState()).toMatchObject({
      accessToken,
      refreshToken: SERVER_REFRESH_TOKEN_SENTINEL,
      role: "trainer",
      clubId: 3,
      trainerId: null,
      studentId: null,
      studentBootstrapStatus: "resolved",
    });
  });

  it("clears membership state when refreshed access token has no membership claims", async () => {
    const accessToken = createJwt({ role: null });
    useAuthStore.setState({
      accessToken: "old-access",
      refreshToken: "old-refresh",
      role: "student",
      clubId: 3,
      trainerId: 42,
      studentId: 77,
      studentBootstrapStatus: "resolved",
      isAuthenticated: true,
    });
    post.mockResolvedValue({
      data: {
        access_token: accessToken,
      },
    });

    await refreshAccessToken("old-refresh");

    expect(useAuthStore.getState()).toMatchObject({
      accessToken,
      refreshToken: SERVER_REFRESH_TOKEN_SENTINEL,
      role: null,
      clubId: null,
      trainerId: null,
      studentId: null,
      studentBootstrapStatus: "idle",
    });
  });
});
