import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  createStableCommandKeyFactory,
  type SelfServicePersonalCommandCard,
  type SelfServicePersonalOption,
} from "@/api/self-service-personal";
import {
  ENABLED_SBP_PAYMENT_CAPABILITIES,
  hasOnlinePaymentsCapability,
} from "@/api/payment-capabilities";
import { useAuthStore } from "@/features/auth/auth-store";
import { useBrandingStore } from "@/features/branding/use-branding";
import {
  SelfServicePersonalBookingSection,
  SelfServicePersonalCommandCards,
} from "./self-service-personal";

const { get, post } = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));

vi.mock("@/api/custom-fetch", () => ({ default: { get, post } }));

function renderPortal(children: React.ReactNode) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={queryClient}>{children}</QueryClientProvider>);
}

function option(overrides: Partial<SelfServicePersonalOption> = {}): SelfServicePersonalOption {
  return {
    slot_id: 55,
    date: "2026-08-20",
    starts_at: "2026-08-20T11:00:00+05:00",
    ends_at: "2026-08-20T12:00:00+05:00",
    trainer_id: 2,
    trainer_name: "Иван Петров",
    location_id: 3,
    location_name: "Основной зал",
    training_type_id: 4,
    training_type_name: "Персональная тренировка",
    capability: "can_pay",
    offer_tariff_name: "Разовая тренировка",
    offer_price: "2500.00",
    offer_digest: "offer-v1",
    ...overrides,
  };
}

function card(overrides: Partial<SelfServicePersonalCommandCard> = {}): SelfServicePersonalCommandCard {
  return {
    command_id: 71,
    slot_id: 55,
    capability: "can_pay",
    status: "expired",
    starts_at: "2026-08-20T11:00:00+05:00",
    ends_at: "2026-08-20T12:00:00+05:00",
    booking_id: null,
    reservation_id: 81,
    bank_payment_order_id: 91,
    provider_payment_url: "",
    amount_snapshot: "2500.00",
    order_status: "",
    allowed_actions: [],
    ...overrides,
  };
}

describe("self-service personal booking", () => {
  beforeEach(() => {
    window.localStorage.clear();
    useAuthStore.setState({
      accessToken: "header.eyJzdWIiOiJzdHVkZW50LTEifQ.signature",
      clubId: 1,
      role: "student",
      isAuthenticated: true,
    });
    useBrandingStore.setState({
      timeZone: "Asia/Yekaterinburg",
      timeZoneStatus: "ready",
      timeZoneClubId: 1,
      isTimeZoneAuthoritative: true,
    });
  });

  it("hides an empty legacy history but renders a retained terminal receipt", async () => {
    get.mockReset();
    post.mockReset();
    get.mockResolvedValueOnce({ data: { live: [], latest_terminal: [] } });

    const empty = renderPortal(
      <SelfServicePersonalCommandCards
        scope={{ audience: "student" }}
        enabled
        onlinePaymentsEnabled={false}
        hideWhenEmpty
      />,
    );

    await waitFor(() => expect(get).toHaveBeenCalledWith(
      "/personal-availability/self-service/commands/",
      { params: undefined },
    ));
    expect(screen.queryByRole("region", { name: "Персональные тренировки" })).not.toBeInTheDocument();
    empty.unmount();

    get.mockReset();
    get.mockResolvedValueOnce({
      data: {
        live: [],
        latest_terminal: [card({ status: "cancelled", order_status: "cancelled" })],
      },
    });
    renderPortal(
      <SelfServicePersonalCommandCards
        scope={{ audience: "student" }}
        enabled
        onlinePaymentsEnabled={false}
        hideWhenEmpty
      />,
    );

    expect(await screen.findByText("Отменено")).toBeInTheDocument();
  });

  it("keeps the unchanged command key stable and never sends commercial authority", async () => {
    get.mockReset();
    post.mockReset();
    get.mockImplementation((url: string) => {
      if (url.endsWith("/options/")) return Promise.resolve({ data: [option({ capability: "can_book", offer_tariff_name: "", offer_price: null, offer_digest: "" })] });
      if (url.endsWith("/commands/")) return Promise.resolve({ data: { live: [], latest_terminal: [] } });
      return Promise.reject(new Error(`Unexpected GET ${url}`));
    });
    post.mockResolvedValue({ data: card({ capability: "can_book", booking_id: 44, allowed_actions: ["view_booking"] }) });

    renderPortal(
      <SelfServicePersonalBookingSection
        scope={{ audience: "student" }}
        enabled
        onlinePaymentsEnabled
      />,
    );

    const commandButton = await screen.findByRole("button", { name: "Записаться" });
    fireEvent.click(commandButton);
    fireEvent.click(commandButton);

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/personal-availability/self-service/slots/55/command/",
        expect.objectContaining({ idempotency_key: expect.stringMatching(/^personal-self-service-/) }),
      );
    });
    const payload = post.mock.calls[0]?.[1] as Record<string, unknown>;
    expect(Object.keys(payload).sort()).toEqual(["idempotency_key"]);
    expect(payload).not.toHaveProperty("tariff_id");
    expect(payload).not.toHaveProperty("subscription_id");
    expect(payload).not.toHaveProperty("payment_method");
    expect(payload).not.toHaveProperty("amount");

    const commandKey = payload.idempotency_key;
    const factory = createStableCommandKeyFactory();
    expect(factory(option())).toBe(factory(option()));
    expect(commandKey).toMatch(/^personal-self-service-/);
    expect(
      new Set(
        post.mock.calls.map(([, repeatedPayload]) =>
          (repeatedPayload as Record<string, unknown>).idempotency_key,
        ),
      ),
    ).toEqual(new Set([commandKey]));
  });

  it("reuses a persisted key after a lost response and a portal reload", async () => {
    get.mockReset();
    post.mockReset();
    get.mockImplementation((url: string) => {
      if (url.endsWith("/options/")) return Promise.resolve({ data: [option({ capability: "can_book", offer_tariff_name: "", offer_price: null, offer_digest: "" })] });
      if (url.endsWith("/commands/")) return Promise.resolve({ data: { live: [], latest_terminal: [] } });
      return Promise.reject(new Error(`Unexpected GET ${url}`));
    });
    post.mockRejectedValue(new Error("network interrupted"));

    const firstRender = renderPortal(
      <SelfServicePersonalBookingSection
        scope={{ audience: "student" }}
        enabled
        onlinePaymentsEnabled
      />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Записаться" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    const lostResponseKey = (post.mock.calls[0]?.[1] as Record<string, unknown>).idempotency_key;
    expect(await screen.findByRole("alert")).toHaveTextContent("Не удалось записаться");
    expect(screen.getAllByRole("alert")).toHaveLength(1);
    firstRender.unmount();

    renderPortal(
      <SelfServicePersonalBookingSection
        scope={{ audience: "student" }}
        enabled
        onlinePaymentsEnabled
      />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Записаться" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect((post.mock.calls[1]?.[1] as Record<string, unknown>).idempotency_key).toBe(lostResponseKey);
  });

  it("shows the server price before SBP and scopes the parent command to the selected child", async () => {
    get.mockReset();
    post.mockReset();
    get.mockImplementation((url: string, config?: { params?: Record<string, unknown> }) => {
      if (url.endsWith("/options/")) {
        expect(config?.params).toMatchObject({ child_student_id: 12 });
        return Promise.resolve({ data: [option()] });
      }
      if (url.endsWith("/commands/")) return Promise.resolve({ data: { live: [], latest_terminal: [] } });
      return Promise.reject(new Error(`Unexpected GET ${url}`));
    });
    post.mockResolvedValue({ data: card({ status: "pending", allowed_actions: ["open_bank_payment_order"] }) });

    renderPortal(
      <SelfServicePersonalBookingSection
        scope={{ audience: "parent", childStudentId: 12 }}
        enabled
        onlinePaymentsEnabled
      />,
    );

    expect(await screen.findByText(/2[\s\u00a0]500/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Оплатить через СБП" }));
    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/personal-availability/self-service/slots/55/command/",
        expect.objectContaining({
          child_student_id: 12,
          offer_digest: "offer-v1",
          idempotency_key: expect.stringMatching(/^personal-self-service-/),
        }),
      );
    });
    expect(Object.keys(post.mock.calls[0]?.[1] as Record<string, unknown>).sort()).toEqual([
      "child_student_id",
      "idempotency_key",
      "offer_digest",
    ]);
  });

  it("uses the club time zone across midnight for a terminal SBP retry", async () => {
    get.mockReset();
    post.mockReset();
    useBrandingStore.setState({ timeZone: "Asia/Vladivostok" });
    const terminal = card({
      starts_at: "2026-08-20T23:30:00Z",
      allowed_actions: ["retry_bank_payment"],
    });
    get.mockImplementation((url: string, config?: { params?: Record<string, unknown> }) => {
      if (url.endsWith("/commands/")) {
        expect(config?.params).toMatchObject({ child_student_id: 12 });
        return Promise.resolve({ data: { live: [], latest_terminal: [terminal] } });
      }
      if (url.endsWith("/options/")) {
        return Promise.resolve({
          data: [option({ date: "2026-08-21", offer_digest: "fresh-offer-v2" })],
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${url}`));
    });
    post.mockResolvedValue({ data: card({ command_id: 72, status: "pending" }) });

    renderPortal(
      <SelfServicePersonalCommandCards
        scope={{ audience: "parent", childStudentId: 12 }}
        enabled
        onlinePaymentsEnabled
        childName="Маша"
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "Оплатить заново" }));
    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/personal-availability/self-service/slots/55/command/",
        expect.objectContaining({
          child_student_id: 12,
          offer_digest: "fresh-offer-v2",
          idempotency_key: expect.stringMatching(/^personal-self-service-/),
        }),
      );
    });
    expect(get).toHaveBeenCalledWith(
      "/personal-availability/self-service/options/",
      expect.objectContaining({ params: expect.objectContaining({ date: "2026-08-21" }) }),
    );
  });

  it("announces cancellation and retry failures once", async () => {
    get.mockReset();
    post.mockReset();
    get.mockImplementation((url: string) => {
      if (url.endsWith("/commands/")) {
        return Promise.resolve({
          data: {
            live: [card({ command_id: 80, status: "pending", allowed_actions: ["cancel_bank_payment_order"] })],
            latest_terminal: [
              card({
                command_id: 81,
                status: "expired",
                order_status: "expired",
                allowed_actions: ["retry_bank_payment"],
              }),
            ],
          },
        });
      }
      if (url.endsWith("/options/")) return Promise.resolve({ data: [option()] });
      return Promise.reject(new Error(`Unexpected GET ${url}`));
    });
    post.mockRejectedValue(new Error("offline"));

    renderPortal(
      <SelfServicePersonalCommandCards
        scope={{ audience: "student" }}
        enabled
        onlinePaymentsEnabled
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "Отменить оплату" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Не удалось отменить оплату");
    expect(screen.getAllByRole("alert")).toHaveLength(1);

    fireEvent.click(screen.getByRole("button", { name: "Оплатить заново" }));
    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent("Не удалось создать новую ссылку");
    });
    expect(screen.getAllByRole("alert")).toHaveLength(1);
  });

  it("fails closed for an unknown runtime option capability", async () => {
    get.mockReset();
    post.mockReset();
    const unsupportedOption = {
      ...option(),
      capability: "unsupported",
    } as unknown as SelfServicePersonalOption;
    get.mockImplementation((url: string) => {
      if (url.endsWith("/options/")) return Promise.resolve({ data: [unsupportedOption] });
      if (url.endsWith("/commands/")) return Promise.resolve({ data: { live: [], latest_terminal: [] } });
      return Promise.reject(new Error(`Unexpected GET ${url}`));
    });

    renderPortal(
      <SelfServicePersonalBookingSection
        scope={{ audience: "student" }}
        enabled
        onlinePaymentsEnabled
      />,
    );

    expect(await screen.findByText("Конфигурация этого слота не поддерживается. Обновите список позже.")).toBeInTheDocument();
    const unavailable = screen.getByRole("button", { name: "Недоступно" });
    expect(unavailable).toBeDisabled();
    fireEvent.click(unavailable);
    expect(post).not.toHaveBeenCalled();
  });

  it("fails closed for every unready online-payment capability state without a command POST", async () => {
    const unreadyStates = [
      { label: "pending", query: { isSuccess: false, data: ENABLED_SBP_PAYMENT_CAPABILITIES } },
      {
        label: "refetch error",
        query: {
          isSuccess: true,
          isRefetchError: true,
          data: ENABLED_SBP_PAYMENT_CAPABILITIES,
        },
      },
      {
        label: "malformed response",
        query: { isSuccess: true, data: { online_payments_enabled: true } },
      },
      {
        label: "disabled response",
        query: {
          isSuccess: true,
          data: { ...ENABLED_SBP_PAYMENT_CAPABILITIES, online_payments_enabled: false },
        },
      },
    ];

    for (const { label, query } of unreadyStates) {
      get.mockReset();
      post.mockReset();
      get.mockImplementation((url: string) => {
        if (url.endsWith("/options/")) return Promise.resolve({ data: [option()] });
        if (url.endsWith("/commands/")) return Promise.resolve({ data: { live: [], latest_terminal: [] } });
        return Promise.reject(new Error(`Unexpected GET ${url}`));
      });
      const onlinePaymentsEnabled = hasOnlinePaymentsCapability(query);
      expect(onlinePaymentsEnabled, label).toBe(false);

      const view = renderPortal(
        <SelfServicePersonalBookingSection
          scope={{ audience: "student" }}
          enabled
          onlinePaymentsEnabled={onlinePaymentsEnabled}
        />,
      );
      const unavailable = await screen.findByRole("button", { name: "Онлайн-оплата недоступна" });
      expect(unavailable, label).toBeDisabled();
      fireEvent.click(unavailable);
      expect(post, label).not.toHaveBeenCalled();
      view.unmount();
    }
  });

  it("keeps can_book usable while online payments are unavailable", async () => {
    get.mockReset();
    post.mockReset();
    get.mockImplementation((url: string) => {
      if (url.endsWith("/options/")) {
        return Promise.resolve({
          data: [option({ capability: "can_book", offer_tariff_name: "", offer_price: null, offer_digest: "" })],
        });
      }
      if (url.endsWith("/commands/")) return Promise.resolve({ data: { live: [], latest_terminal: [] } });
      return Promise.reject(new Error(`Unexpected GET ${url}`));
    });
    post.mockResolvedValue({ data: card({ capability: "can_book" }) });

    renderPortal(
      <SelfServicePersonalBookingSection
        scope={{ audience: "student" }}
        enabled
        onlinePaymentsEnabled={false}
      />,
    );

    const commandButton = await screen.findByRole("button", { name: "Записаться" });
    expect(commandButton).toBeEnabled();
    fireEvent.click(commandButton);
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
  });

  it("keeps terminal cancellation truth ahead of an older approved order status", async () => {
    get.mockReset();
    get.mockImplementation((url: string) => {
      if (url.endsWith("/commands/")) {
        return Promise.resolve({
          data: {
            live: [],
            latest_terminal: [
              card({
                status: "cancelled",
                order_status: "approved",
                booking_id: null,
                allowed_actions: [],
              }),
            ],
          },
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${url}`));
    });

    renderPortal(
      <SelfServicePersonalCommandCards
        scope={{ audience: "student" }}
        enabled
        onlinePaymentsEnabled
      />,
    );

    expect(await screen.findByText("Отменено")).toBeInTheDocument();
    expect(screen.queryByText("Оплата подтверждена")).not.toBeInTheDocument();
    expect(screen.queryByText("Запись сохранена в расписании.")).not.toBeInTheDocument();
  });

  it("does not reuse another actor or club self-service cards and options in one SPA", async () => {
    get.mockReset();
    const pendingResponses: Array<{
      url: string;
      resolve: (value: { data: unknown }) => void;
    }> = [];
    get.mockImplementation((url: string) => {
      const isSecondActor = useAuthStore.getState().clubId === 2;
      if (!isSecondActor && url.endsWith("/options/")) {
        return Promise.resolve({ data: [option({ training_type_name: "Старый слот" })] });
      }
      if (!isSecondActor && url.endsWith("/commands/")) {
        return Promise.resolve({
          data: {
            live: [],
            latest_terminal: [card({ status: "cancelled", order_status: "cancelled" })],
          },
        });
      }
      return new Promise<{ data: unknown }>((resolve) => pendingResponses.push({ url, resolve }));
    });

    renderPortal(
      <>
        <SelfServicePersonalBookingSection
          scope={{ audience: "student" }}
          enabled
          onlinePaymentsEnabled
        />
        <SelfServicePersonalCommandCards
          scope={{ audience: "student" }}
          enabled
          onlinePaymentsEnabled
        />
      </>,
    );

    expect(await screen.findByText("Старый слот")).toBeInTheDocument();
    expect(await screen.findByText("Отменено")).toBeInTheDocument();

    await act(async () => {
      useAuthStore.setState({
        accessToken: "header.eyJzdWIiOiJzdHVkZW50LTIifQ.signature",
        clubId: 2,
      });
      useBrandingStore.setState({ timeZoneClubId: 2 });
    });

    await waitFor(() => expect(pendingResponses).toHaveLength(2));
    expect(screen.queryByText("Старый слот")).not.toBeInTheDocument();
    expect(screen.queryByText("Отменено")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Загружаем персональные слоты")).toBeInTheDocument();

    await act(async () => {
      pendingResponses.forEach(({ resolve, url }) =>
        resolve({
          data: url.endsWith("/commands/") ? { live: [], latest_terminal: [] } : [],
        }),
      );
    });
    expect(await screen.findByText("На этот день нет персональных слотов")).toBeInTheDocument();
  });

  it("uses the newest exact bank order as payment authority and keeps terminal history distinct", async () => {
    get.mockReset();
    post.mockReset();
    get.mockImplementation((url: string) => {
      if (url.endsWith("/commands/")) {
        return Promise.resolve({
          data: {
            live: [
              card({
                command_id: 11,
                status: "pending",
                order_status: "pending",
                provider_payment_url: "https://pay.example/11",
                allowed_actions: ["open_bank_payment_order", "cancel_bank_payment_order"],
              }),
            ],
            latest_terminal: [
              card({
                command_id: 12,
                status: "manual_review",
                allowed_actions: ["retry_bank_payment"],
              }),
            ],
          },
        });
      }
      if (url === "/students/me/bank-payment-orders/91/") {
        return Promise.resolve({
          data: {
            id: 91,
            subscription_id: 0,
            tariff_id: 0,
            debt_ids: [],
            status: "manual_review",
            amount_snapshot: "2500.00",
            currency: "RUB",
            provider_payment_url: "https://pay.example/11",
            expires_at: "2099-08-20T12:00:00+05:00",
            can_pay: true,
          },
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${url}`));
    });

    renderPortal(
      <SelfServicePersonalCommandCards
        scope={{ audience: "student" }}
        enabled
        onlinePaymentsEnabled
      />,
    );

    expect(await screen.findByText(/Не создавайте новую попытку/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Оплатить через СБП" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Отменить оплату" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Оплатить заново" })).not.toBeInTheDocument();
    const commandReadsBeforeBankReturn = get.mock.calls.filter(([url]) => url.endsWith("/commands/")).length;
    fireEvent(window, new Event("focus"));
    await waitFor(() => {
      expect(get.mock.calls.filter(([url]) => url.endsWith("/commands/")).length).toBeGreaterThan(
        commandReadsBeforeBankReturn,
      );
    });

    get.mockReset();
    get.mockRejectedValue(new Error("offline"));
    renderPortal(
      <SelfServicePersonalCommandCards
        scope={{ audience: "parent", childStudentId: 13 }}
        enabled
        onlinePaymentsEnabled
      />,
    );
    expect(await screen.findByText("Не удалось загрузить статус записи")).toBeInTheDocument();
  });

  it("refetches a stale offer, shows its new price and digest, then submits with a new key", async () => {
    get.mockReset();
    post.mockReset();
    let optionReads = 0;
    get.mockImplementation((url: string) => {
      if (url.endsWith("/options/")) {
        optionReads += 1;
        return Promise.resolve({
          data: [
            option(
              optionReads === 1
                ? {}
                : { offer_price: "2700.00", offer_digest: "offer-v2" },
            ),
          ],
        });
      }
      if (url.endsWith("/commands/")) return Promise.resolve({ data: { live: [], latest_terminal: [] } });
      return Promise.reject(new Error(`Unexpected GET ${url}`));
    });
    post
      .mockRejectedValueOnce({ response: { data: { code: "personal_offer_changed" } } })
      .mockResolvedValueOnce({ data: card({ status: "pending" }) });

    renderPortal(
      <SelfServicePersonalBookingSection
        scope={{ audience: "student" }}
        enabled
        onlinePaymentsEnabled
      />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Оплатить через СБП" }));
    expect(
      await screen.findByText("Цена или условия изменились. Проверьте обновлённый вариант перед новой оплатой."),
    ).toBeInTheDocument();
    expect(await screen.findByText(/2[\s\u00a0]700/)).toBeInTheDocument();
    expect(screen.getByText("Условия предложения: offer-v2")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Оплатить через СБП" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    const firstPayload = post.mock.calls[0]?.[1] as Record<string, unknown>;
    const secondPayload = post.mock.calls[1]?.[1] as Record<string, unknown>;
    expect(firstPayload.offer_digest).toBe("offer-v1");
    expect(secondPayload.offer_digest).toBe("offer-v2");
    expect(secondPayload.idempotency_key).not.toBe(firstPayload.idempotency_key);
    expect(screen.queryByText("Запись сохранена в расписании.")).not.toBeInTheDocument();
  });

  it("fails closed while the club time zone is not authoritative and keeps query errors distinct", async () => {
    get.mockReset();
    get.mockRejectedValue(new Error("offline"));
    useBrandingStore.setState({
      timeZoneStatus: "pending",
      timeZoneClubId: 1,
      isTimeZoneAuthoritative: false,
    });
    renderPortal(
      <SelfServicePersonalBookingSection
        scope={{ audience: "student" }}
        enabled
        onlinePaymentsEnabled
      />,
    );
    expect(await screen.findByText("Проверяем доступность персональной записи")).toBeInTheDocument();
    expect(get).not.toHaveBeenCalled();

    useBrandingStore.setState({
      timeZoneStatus: "ready",
      timeZoneClubId: 1,
      isTimeZoneAuthoritative: true,
    });
    expect(await screen.findByText("Не удалось загрузить персональные слоты")).toBeInTheDocument();
  });
});
