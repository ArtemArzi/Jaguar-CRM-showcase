import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import PaymentReturnPage, { PAYMENT_RETURN_PENDING_EXCHANGE_KEY } from "./payment-return-page";
import { selfServicePersonalCommandsQueryKey } from "@/api/self-service-personal";
import { useAuthStore } from "@/features/auth/auth-store";
import { PAYMENT_RESUME_STORAGE_KEY } from "./payment-resume-context";

const { get, post } = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));

vi.mock("@/api/custom-fetch", () => ({ default: { get, post } }));

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location-probe">{JSON.stringify(location.state)}</output>;
}

function renderReturnPage(entry = "/payments/return?state=opaque-return-state") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const view = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>
        <PaymentReturnPage />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { queryClient, ...view };
}

class FakeBroadcastChannel {
  static instances: FakeBroadcastChannel[] = [];
  readonly postMessage = vi.fn();
  onmessage: ((event: MessageEvent) => void) | null = null;
  readonly close = vi.fn();
  readonly name: string;

  constructor(name: string) {
    this.name = name;
    FakeBroadcastChannel.instances.push(this);
  }
}

describe("PaymentReturnPage", () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
    post.mockResolvedValue({ data: { status: "checking" } });
    get.mockResolvedValue({ data: { status: "approved" } });
    useAuthStore.setState({
      accessToken: null,
      refreshToken: null,
      role: null,
      clubId: null,
      isAuthenticated: false,
    });
    localStorage.clear();
    sessionStorage.clear();
    FakeBroadcastChannel.instances = [];
    vi.stubGlobal("BroadcastChannel", FakeBroadcastChannel);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("strips the return state before exchanging it and renders only generic status", async () => {
    const replaceState = vi.spyOn(window.history, "replaceState");
    renderReturnPage();

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/billing/payment-returns/exchange/",
        { state: "opaque-return-state", browser_binding: expect.any(String) },
        { withCredentials: true },
      );
    });
    expect(replaceState).toHaveBeenCalledWith(window.history.state, "", "/payments/return");
    expect(await screen.findByRole("heading", { name: "Проверяем оплату" })).toBeInTheDocument();
    expect(screen.queryByText("opaque-return-state")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Войти в кабинет" })).toHaveAttribute("href", "/login");
    fireEvent.click(screen.getByRole("link", { name: "Войти в кабинет" }));
    expect(screen.getByTestId("location-probe")).toHaveTextContent('"pathname":"/payments/return"');
  });

  it("retries the opaque exchange a bounded number of times without putting raw state back in history", async () => {
    vi.useFakeTimers();
    const replaceState = vi.spyOn(window.history, "replaceState");
    post.mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce({ data: { status: "checking" } });
    renderReturnPage();

    await vi.advanceTimersByTimeAsync(1_000);
    expect(post).toHaveBeenCalledTimes(2);
    expect(replaceState).toHaveBeenCalledWith(window.history.state, "", "/payments/return");
    expect(window.location.search).not.toContain("opaque-return-state");
  });

  it("recovers a browser-bound pending exchange after a hard reload without restoring the URL state", async () => {
    post.mockRejectedValue(new Error("offline"));
    const first = renderReturnPage();
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(sessionStorage.getItem(PAYMENT_RETURN_PENDING_EXCHANGE_KEY)).toContain("opaque-return-state");
    first.unmount();

    post.mockReset();
    post.mockResolvedValue({ data: { status: "checking" } });
    renderReturnPage("/payments/return");

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/billing/payment-returns/exchange/",
        { state: "opaque-return-state", browser_binding: expect.any(String) },
        { withCredentials: true },
      ),
    );
    expect(sessionStorage.getItem(PAYMENT_RETURN_PENDING_EXCHANGE_KEY)).toBeNull();
    expect(window.location.search).not.toContain("opaque-return-state");
  });

  it("refreshes generic status when the page regains focus", async () => {
    renderReturnPage("/payments/return");
    await waitFor(() => expect(get).toHaveBeenCalledWith(
      "/billing/payment-returns/status/",
      { withCredentials: true },
    ));
    fireEvent.focus(window);
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole("heading", { name: "Оплата подтверждена" })).toBeInTheDocument();
  });

  it("invalidates source-scoped self-service command cards after a bank return without inferring success", async () => {
    const { queryClient } = renderReturnPage("/payments/return");
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");

    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({ queryKey: selfServicePersonalCommandsQueryKey }),
    );
    expect(screen.getByRole("heading", { name: "Оплата подтверждена" })).toBeInTheDocument();
  });

  it("keeps checking on a transient status failure and refreshes on pageshow and visibility", async () => {
    get.mockRejectedValueOnce({ response: { status: 503 } }).mockResolvedValue({ data: { status: "checking" } });
    renderReturnPage("/payments/return");
    expect(await screen.findByRole("heading", { name: "Проверяем оплату" })).toBeInTheDocument();
    expect(screen.getByText(/Не удалось обновить статус/)).toBeInTheDocument();

    window.dispatchEvent(new Event("pageshow"));
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    document.dispatchEvent(new Event("visibilitychange"));
    await waitFor(() => expect(get).toHaveBeenCalledTimes(3));
  });

  it("treats cross-tab events only as invalidations, not as payment evidence", async () => {
    get.mockResolvedValue({ data: { status: "checking" } });
    renderReturnPage("/payments/return");
    await waitFor(() => expect(get).toHaveBeenCalledTimes(1));
    const inboundChannel = FakeBroadcastChannel.instances.find((channel) => channel.onmessage);
    expect(inboundChannel).toBeDefined();

    inboundChannel?.onmessage?.({ data: { type: "status-changed", status: "approved" } } as MessageEvent);

    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("heading", { name: "Проверяем оплату" })).toBeInTheDocument();
    expect(FakeBroadcastChannel.instances.flatMap((channel) => channel.postMessage.mock.calls)).toEqual([]);
  });

  it("re-enables a manually throttled refresh after the cooldown", async () => {
    vi.useFakeTimers();
    renderReturnPage("/payments/return");
    await act(async () => {
      await vi.runOnlyPendingTimersAsync();
    });
    const button = screen.getByRole("button", { name: "Обновить статус" });
    fireEvent.click(button);
    expect(button).toBeDisabled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000);
    });
    expect(screen.getByRole("button", { name: "Обновить статус" })).toBeEnabled();
  });

  it("resumes the exact self-service order after authentication and retains context on a transient error", async () => {
    useAuthStore.setState({
      accessToken: `header.${btoa(JSON.stringify({ sub: "student-17" }))}.signature`,
      refreshToken: null,
      role: "student",
      clubId: 7,
      isAuthenticated: true,
    });
    localStorage.setItem(PAYMENT_RESUME_STORAGE_KEY, JSON.stringify({
      orderId: 44,
      role: "student",
      clubId: 7,
      actorSubject: "student-17",
      expiresAt: Date.now() + 60_000,
    }));
    get.mockImplementation((url: string) => {
      if (url === "/billing/payment-returns/status/") return Promise.resolve({ data: { status: "approved" } });
      if (url === "/students/me/bank-payment-orders/44/") return Promise.reject({ response: { status: 503 } });
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });
    renderReturnPage("/payments/return");

    await waitFor(() => expect(get).toHaveBeenCalledWith("/students/me/bank-payment-orders/44/"));
    expect(localStorage.getItem(PAYMENT_RESUME_STORAGE_KEY)).not.toBeNull();
    expect(screen.getByText(/Не удалось обновить подробности/)).toBeInTheDocument();
  });

  it("offers an actor-scoped manual retry after fulfillment retries are exhausted", async () => {
    vi.useFakeTimers();
    useAuthStore.setState({
      accessToken: `header.${btoa(JSON.stringify({ sub: "student-17" }))}.signature`,
      refreshToken: null,
      role: "student",
      clubId: 7,
      isAuthenticated: true,
    });
    localStorage.setItem(PAYMENT_RESUME_STORAGE_KEY, JSON.stringify({
      orderId: 44,
      role: "student",
      clubId: 7,
      actorSubject: "student-17",
      expiresAt: Date.now() + 60_000,
    }));
    const pendingOrder = {
      id: 44,
      subscription_id: 54,
      tariff_id: 3,
      debt_ids: [],
      status: "approved",
      amount_snapshot: "5000.00",
      currency: "RUB",
      purpose_snapshot: "Абонемент Base",
      expires_at: "2099-06-28T12:00:00Z",
      fulfillment_state: "fulfillment_pending",
      can_request_refresh: false,
    };
    get.mockImplementation((url: string) => {
      if (url === "/billing/payment-returns/status/") {
        return Promise.resolve({ data: { status: "approved" } });
      }
      if (url === "/students/me/bank-payment-orders/44/") {
        return Promise.resolve({ data: pendingOrder });
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderReturnPage("/payments/return");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    for (const delay of [1_000, 2_000, 4_000, 6_000]) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(delay);
      });
    }

    expect(
      get.mock.calls.filter(([url]) => url === "/students/me/bank-payment-orders/44/"),
    ).toHaveLength(5);
    expect(screen.getByText(/Автоматическая проверка завершена/)).toBeInTheDocument();
    const retryButton = screen.getByRole("button", { name: "Обновить данные кабинета" });

    fireEvent.click(retryButton);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(
      get.mock.calls.filter(([url]) => url === "/students/me/bank-payment-orders/44/"),
    ).toHaveLength(6);
  });

  it("clears a loaded order and its provider actions when the authenticated actor logs out", async () => {
    useAuthStore.setState({
      accessToken: `header.${btoa(JSON.stringify({ sub: "student-17" }))}.signature`,
      refreshToken: null,
      role: "student",
      clubId: 7,
      isAuthenticated: true,
    });
    localStorage.setItem(PAYMENT_RESUME_STORAGE_KEY, JSON.stringify({
      orderId: 44,
      role: "student",
      clubId: 7,
      actorSubject: "student-17",
      expiresAt: Date.now() + 60_000,
    }));
    get.mockImplementation((url: string) => {
      if (url === "/billing/payment-returns/status/") return Promise.resolve({ data: { status: "approved" } });
      if (url === "/students/me/bank-payment-orders/44/") {
        return Promise.resolve({
          data: {
            id: 44,
            subscription_id: 54,
            tariff_id: 3,
            debt_ids: [],
            status: "pending",
            amount_snapshot: "5000.00",
            currency: "RUB",
            purpose_snapshot: "Абонемент прежнего пользователя",
            provider_payment_url: "https://pay.example/old-user",
            expires_at: "2099-06-28T12:00:00Z",
            can_pay: true,
            can_request_refresh: true,
          },
        });
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderReturnPage("/payments/return");

    expect(await screen.findByRole("region", { name: "Последняя онлайн-оплата" })).toBeInTheDocument();
    expect(screen.getByText("Абонемент прежнего пользователя")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Оплатить через СБП" })).toBeInTheDocument();

    await act(async () => {
      useAuthStore.getState().logout();
    });

    await waitFor(() => {
      expect(screen.queryByRole("region", { name: "Последняя онлайн-оплата" })).not.toBeInTheDocument();
    });
    expect(screen.queryByText("Абонемент прежнего пользователя")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Оплатить через СБП" })).not.toBeInTheDocument();
    expect(localStorage.getItem(PAYMENT_RESUME_STORAGE_KEY)).toBeNull();
  });
});
