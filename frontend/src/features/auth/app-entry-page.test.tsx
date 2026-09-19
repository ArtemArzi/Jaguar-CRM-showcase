import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore, type UserRole } from "./auth-store";
import { useKioskStore } from "@/features/kiosk/lib/kiosk-store";

const { refreshAccessToken } = vi.hoisted(() => ({
  refreshAccessToken: vi.fn(),
}));

vi.mock("@/api/custom-fetch", () => ({
  refreshAccessToken,
}));

import AppEntryPage from "./app-entry-page";

function createJwt({
  role = "parent",
  clubId = 7,
  exp = Math.floor(Date.now() / 1000) + 60 * 60,
}: {
  role?: UserRole | null;
  clubId?: number;
  exp?: number;
} = {}) {
  const payload =
    role === null
      ? { exp }
      : {
          exp,
          role,
          club_id: clubId,
        };
  return `eyJhbGciOiJIUzI1NiJ9.${btoa(JSON.stringify(payload))}.sig`;
}

function LocationProbe() {
  const location = useLocation();
  const from = (location.state as { from?: { pathname?: string } } | null)?.from;
  return (
    <>
      <div data-testid="current-location">
        {`${location.pathname}${location.search}${location.hash}`}
      </div>
      <div data-testid="return-location">{from?.pathname ?? ""}</div>
    </>
  );
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
  useKioskStore.setState({
    deviceToken: null,
    clubId: null,
    clubName: null,
    isActivated: false,
  });
}

function authenticateAs(
  role: UserRole | null,
  {
    accessToken = createJwt({ role }),
    refreshToken = "refresh-token",
  }: { accessToken?: string | null; refreshToken?: string | null } = {},
) {
  useAuthStore.setState({
    accessToken,
    refreshToken,
    role,
    clubId: role ? 7 : null,
    trainerId: null,
    studentId: null,
    isAuthenticated: true,
  });
}

function renderAppEntry() {
  return render(
    <MemoryRouter initialEntries={["/app"]}>
      <Routes>
        <Route path="/app" element={<AppEntryPage />} />
        <Route path="/login" element={<LocationProbe />} />
        <Route path="/parent/*" element={<LocationProbe />} />
        <Route path="/student/*" element={<LocationProbe />} />
        <Route path="/trainer/*" element={<LocationProbe />} />
        <Route path="/kiosk/*" element={<LocationProbe />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("AppEntryPage", () => {
  beforeEach(() => {
    localStorage.clear();
    refreshAccessToken.mockReset();
    resetAuthState();
  });

  it("shows one branded entry for unauthenticated users", () => {
    renderAppEntry();

    expect(
      screen.getByRole("heading", { name: "Кабинет клуба" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /Войти в кабинет/ }),
    ).toBeInTheDocument();
  });

  it("sends unauthenticated users to login while preserving /app as return path", async () => {
    renderAppEntry();

    fireEvent.click(screen.getByRole("link", { name: /Войти в кабинет/ }));

    await waitFor(() => {
      expect(screen.getByTestId("current-location")).toHaveTextContent(
        "/login",
      );
    });
    expect(screen.getByTestId("return-location")).toHaveTextContent("/app");
  });

  it("opens the kiosk surface from /app when this device is already activated", async () => {
    useKioskStore.setState({
      deviceToken: "fixture-device-value",
      clubId: 7,
      clubName: "Jaguar",
      isActivated: true,
    });

    renderAppEntry();

    await waitFor(() => {
      expect(screen.getByTestId("current-location")).toHaveTextContent(
        "/kiosk",
      );
    });
  });

  it("routes authenticated parent, student, and trainer users to their role surface", async () => {
    for (const [role, expectedPath] of [
      ["parent", "/parent/"],
      ["student", "/student/"],
      ["trainer", "/trainer/"],
    ] as const) {
      resetAuthState();
      authenticateAs(role);
      const { unmount } = renderAppEntry();

      await waitFor(() => {
        expect(screen.getByTestId("current-location")).toHaveTextContent(
          expectedPath,
        );
      });

      unmount();
    }
  });

  it.each(["owner", "admin"] as const)(
    "shows dashboard entry for authenticated %s role",
    (role) => {
      authenticateAs(role);

      renderAppEntry();

      expect(
        screen.getByRole("heading", { name: "Панель управления клубом" }),
      ).toBeInTheDocument();
      expect(screen.queryByText(/HTMX|PWA/i)).not.toBeInTheDocument();
      expect(
        screen.getByRole("link", { name: /Открыть панель управления/ }),
      ).toHaveAttribute("href", "/dashboard/login/");
    },
  );

  it("keeps authenticated no-club users out of role surfaces", () => {
    authenticateAs(null);

    renderAppEntry();

    expect(
      screen.getByRole("heading", { name: "Нужна ссылка-приглашение" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /Войти другим аккаунтом/ }),
    ).toHaveAttribute("href", "/login?reset=1");
  });

  it("waits for silent refresh before routing from a persisted session", async () => {
    const freshParentToken = createJwt({ role: "parent" });
    let resolveRefresh: (() => void) | undefined;
    refreshAccessToken.mockImplementation(() => {
      return new Promise<string>((resolve) => {
        resolveRefresh = () => {
          useAuthStore
            .getState()
            .setTokens(freshParentToken, "fresh-refresh-token");
          useAuthStore.getState().setUserInfo("parent", 7);
          resolve(freshParentToken);
        };
      });
    });
    authenticateAs("trainer", { accessToken: null });

    renderAppEntry();

    expect(screen.getByText("Восстанавливаем вход")).toBeInTheDocument();
    await act(async () => {
      resolveRefresh?.();
    });
    await waitFor(() => {
      expect(screen.getByTestId("current-location")).toHaveTextContent(
        "/parent/",
      );
    });
    expect(refreshAccessToken).toHaveBeenCalledWith("refresh-token");
  });
});
