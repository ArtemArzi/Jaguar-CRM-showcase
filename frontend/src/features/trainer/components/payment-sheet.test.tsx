import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "@/features/auth/auth-store";
import {
  ENABLED_SBP_PAYMENT_CAPABILITIES,
  getPaymentCapabilitiesQueryKey,
} from "@/api/payment-capabilities";
import { clearAllContextualCommercialCommandKeys } from "@/api/contextual-commercial-command-key";
import {
  PaymentSheet,
  type ExactPersonalDebtSettlement,
  type PaymentRecordedSuccess,
} from "./payment-sheet";

const { get, post } = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}));

const discountOptions = [
  {
    id: 21,
    name: "Семейная скидка",
    discount_type: "percent",
    value: "10.00",
    is_active: true,
  },
  {
    id: 22,
    name: "Скидка на форму",
    discount_type: "fixed",
    value: "1000.00",
    is_active: true,
  },
  {
    id: 23,
    name: "Полная скидка",
    discount_type: "percent",
    value: "100.00",
    is_active: true,
  },
  {
    id: 24,
    name: "Точная скидка",
    discount_type: "percent",
    value: "12.34",
    is_active: true,
  },
];
const defaultGroupEnrollmentOptions = [
  {
    schedule_id: 31,
    group_name: "Tue Thu Group",
    trainer_id: 8,
    trainer_name: "Target Trainer",
    location_id: 2,
    location_name: "Main Gym",
    training_type_id: 5,
    training_type_name: "Group Boxing",
    day_of_week: 1,
    start_time: "18:00:00",
    end_time: "19:00:00",
    next_occurrence_date: "2030-01-08",
    occurrence_dates: ["2030-01-08", "2030-01-10"],
    is_latest_trial_group: true,
  },
];
let groupEnrollmentOptions: Array<Record<string, unknown>> = defaultGroupEnrollmentOptions;
let activeDiscounts: (typeof discountOptions)[number][] = [];
let baseTariffPrice = 5000;
let rejectDiscounts = false;
let pendingBankOrders: Array<Record<string, unknown>> = [];
let rejectedQueries = new Set<string>();
let dropInDebt = false;
let legacyDebtListEmpty = false;
const legacyGroupPaymentCapabilities = {
  ...ENABLED_SBP_PAYMENT_CAPABILITIES,
  training_group_rollout_mode: "off",
  training_group_payment_selection_mode: "legacy" as const,
  canonical_group_selection_enabled: false,
};

vi.mock("@/api/custom-fetch", () => ({
  default: {
    get,
    post,
  },
}));

function renderSheet(
  onOpenChange = vi.fn(),
  dropInProps: Partial<{
    dropInBookingId: number;
    requiredTariffId: number;
    requiredDebtId: number;
    exactPersonalDebtSettlement: ExactPersonalDebtSettlement;
    unifiedPersonalSettlement: boolean;
  }> = {},
  paymentCapabilities: unknown = legacyGroupPaymentCapabilities,
  skipPaymentCapabilitiesSeed = false,
  onPaymentRecorded?: (result: PaymentRecordedSuccess) => void,
) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  if (!skipPaymentCapabilitiesSeed) {
    queryClient.setQueryData(getPaymentCapabilitiesQueryKey(), paymentCapabilities);
  }

  const view = render(
    <QueryClientProvider client={queryClient}>
      <PaymentSheet
        open
        onOpenChange={onOpenChange}
        studentId={7}
        studentName="Masha Ivanova"
        onPaymentRecorded={onPaymentRecorded}
        {...dropInProps}
      />
    </QueryClientProvider>,
  );

  return { ...view, queryClient };
}

describe("PaymentSheet", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearAllContextualCommercialCommandKeys();
    activeDiscounts = [];
    baseTariffPrice = 5000;
    rejectDiscounts = false;
    pendingBankOrders = [];
    rejectedQueries = new Set<string>();
    dropInDebt = false;
    legacyDebtListEmpty = false;
    groupEnrollmentOptions = defaultGroupEnrollmentOptions;
    useAuthStore.setState({
      accessToken: "header.eyJzdWIiOiJ0cmFpbmVyLTQifQ.signature",
      role: "trainer",
      clubId: 1,
      trainerId: 4,
      isAuthenticated: true,
    });
    post.mockResolvedValue({ data: { id: 99 } });
    get.mockImplementation((url: string) => {
      if (url === "/billing/payment-capabilities/") {
        return Promise.resolve({ data: legacyGroupPaymentCapabilities });
      }

      if (url === "/billing/group-enrollment-options/") {
        return Promise.resolve({ data: groupEnrollmentOptions });
      }

      if (url === "/billing/discounts/") {
        if (rejectDiscounts) {
          return Promise.reject(new Error("Discounts unavailable"));
        }
        return Promise.resolve({ data: activeDiscounts });
      }

      if (url === "/billing/tariffs/") {
        if (rejectedQueries.has("tariffs")) {
          return Promise.reject(new Error("Tariffs unavailable"));
        }
        return Promise.resolve({
          data: {
            items: [
              {
                id: 3,
                name: "Base",
                price: baseTariffPrice,
                training_type: {
                  id: 2,
                  name: "Muay Thai",
                  slug: "muay-thai",
                  kind: "personal",
                  is_active: true,
                },
                trainings_limit: 8,
                duration_days: 30,
                is_active: true,
                payout_timing_hint: "per_checkin",
                requires_package_owner: true,
              },
              {
                id: 4,
                name: "Group Plan",
                price: 4500,
                training_type: {
                  id: 5,
                  name: "Group Boxing",
                  slug: "group-boxing",
                  kind: "group",
                  is_active: true,
                },
                trainings_limit: 8,
                duration_days: 30,
                is_active: true,
                payout_timing_hint: "after_payment_confirmation",
                requires_package_owner: false,
              },
              {
                id: 5,
                name: "Hybrid Plan",
                price: 9500,
                training_type: {
                  id: 5,
                  name: "Hybrid",
                  slug: "hybrid",
                  kind: "group",
                  is_active: true,
                },
                trainings_limit: null,
                duration_days: 30,
                is_active: true,
                payout_timing_hint: "mixed",
                requires_package_owner: true,
              },
            ],
          },
        });
      }

      if (url === "/trainers/me/") {
        if (rejectedQueries.has("trainer")) {
          return Promise.reject(new Error("Trainer unavailable"));
        }
        return Promise.resolve({ data: { id: 4, first_name: "Current", last_name: "Trainer" } });
      }

      if (url.startsWith("/billing/debts/?student_id=")) {
        if (rejectedQueries.has("debts")) {
          return Promise.reject(new Error("Debts unavailable"));
        }
        if (legacyDebtListEmpty) {
          return Promise.resolve({ data: { items: [] } });
        }
        return Promise.resolve({
          data: {
            items: [
              {
                id: 11,
                student_id: 7,
                student_name: "Masha Ivanova",
                checkin_id: 55,
                tariff_price: "1200.00",
                reason: "no_subscription",
                resolution_type: "",
                resolved_at: null,
                created_at: "2026-06-01T10:00:00Z",
                ...(dropInDebt
                  ? { booking_id: 71, required_tariff_id: 3 }
                  : {}),
              },
            ],
          },
        });
      }

      if (url === "/billing/bank-payment-orders/") {
        if (rejectedQueries.has("bank-orders")) {
          return Promise.reject(new Error("Bank orders unavailable"));
        }
        return Promise.resolve({ data: { items: pendingBankOrders } });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });
  });

  it("loads open debts for the selected student and submits selected debt ids", async () => {
    const onOpenChange = vi.fn();
    renderSheet(onOpenChange);

    await waitFor(() => {
      expect(get).toHaveBeenCalledWith("/billing/debts/?student_id=7");
    });

    fireEvent.click(await screen.findByRole("checkbox", { name: /Долг #55/ }));
    fireEvent.click(screen.getByRole("button", { name: /Base/ }));
    expect(screen.getByRole("button", { name: /Base/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    fireEvent.click(screen.getByRole("button", { name: "Принять оплату" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/billing/payments/",
        expect.objectContaining({
          student_id: 7,
          tariff_id: 3,
          payment_method: "cash",
          discount_ids: [],
          debt_ids: [11],
          seller_trainer_id: 4,
          package_owner_trainer_id: 4,
        }),
      );
    });
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("locks a personal drop-in to its exact tariff and debt, then uses the booking payment endpoint", async () => {
    dropInDebt = true;
    renderSheet(vi.fn(), {
      dropInBookingId: 71,
      requiredTariffId: 3,
      requiredDebtId: 11,
    });

    const tariff = await screen.findByRole("button", { name: /Base/ });
    expect(tariff).toHaveAttribute("aria-pressed", "true");
    expect(tariff).toBeDisabled();
    expect(screen.getByText("Тариф и долг этой персоналки зафиксированы. Другую оплату или долг выбрать нельзя.")).toBeInTheDocument();
    const debtCheckbox = await screen.findByRole("checkbox", { name: /Долг #55/ });
    expect(debtCheckbox).toBeChecked();
    expect(debtCheckbox).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Принять оплату" }));
    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/personal-drop-in-bookings/71/payments/",
        { payment_method: "cash", discount_ids: [] },
      );
    });
    expect(post).not.toHaveBeenCalledWith("/billing/payments/", expect.anything());
  });

  it("keeps a unified personal settlement exact and never exposes or submits discounts", async () => {
    dropInDebt = true;
    activeDiscounts = [...discountOptions];
    renderSheet(vi.fn(), {
      dropInBookingId: 71,
      requiredTariffId: 3,
      requiredDebtId: 11,
      unifiedPersonalSettlement: true,
    });

    expect(await screen.findByText("Тариф и долг этой персоналки зафиксированы. Другую оплату или долг выбрать нельзя.")).toBeInTheDocument();
    expect(screen.queryByRole("radiogroup", { name: "Скидка" })).not.toBeInTheDocument();
    const submit = screen.getByRole("button", { name: "Принять оплату" });
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/personal-drop-in-bookings/71/payments/",
        { payment_method: "cash", discount_ids: [] },
      );
    });
  });

  it("settles an exact personal debt from its frozen receipt without a current tariff or discounts", async () => {
    dropInDebt = true;
    activeDiscounts = [...discountOptions];
    post
      .mockRejectedValueOnce(new Error("temporary failure"))
      .mockRejectedValueOnce(new Error("temporary failure"))
      .mockResolvedValueOnce({ data: { id: 99 } });
    renderSheet(vi.fn(), {
      dropInBookingId: 71,
      requiredTariffId: 999,
      requiredDebtId: 11,
      exactPersonalDebtSettlement: {
        bookingId: 71,
        debtId: 11,
        tariffName: "Зафиксированная персоналка",
        amount: "2500.00",
      },
    });

    expect(await screen.findByText("Зафиксированная персоналка")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Зафиксированная сумма оплаты" })).toHaveTextContent(
      /2 500/,
    );
    expect(screen.queryByText("Base")).not.toBeInTheDocument();
    expect(screen.queryByRole("radiogroup", { name: "Скидка" })).not.toBeInTheDocument();
    expect(get).not.toHaveBeenCalledWith("/billing/tariffs/");
    expect(get).not.toHaveBeenCalledWith("/billing/discounts/");

    const submit = screen.getByRole("button", { name: "Принять оплату" });
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    const firstPayload = post.mock.calls[0]?.[1] as Record<string, unknown>;
    expect(post).toHaveBeenLastCalledWith(
      "/personal-drop-in-bookings/71/payments/",
      expect.objectContaining({
        payment_method: "cash",
        debt_id: 11,
        discount_ids: [],
        idempotency_key: expect.any(String),
      }),
    );

    fireEvent.click(submit);
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect((post.mock.calls[1]?.[1] as Record<string, unknown>).idempotency_key).toBe(
      firstPayload.idempotency_key,
    );

    fireEvent.click(screen.getByRole("button", { name: "Перевод" }));
    fireEvent.click(submit);
    await waitFor(() => expect(post).toHaveBeenCalledTimes(3));
    expect((post.mock.calls[2]?.[1] as Record<string, unknown>).idempotency_key).not.toBe(
      firstPayload.idempotency_key,
    );
  });

  it("creates an exact-debt SBP link with one frozen debt command and a stable retry key", async () => {
    legacyDebtListEmpty = true;
    post
      .mockRejectedValueOnce(new Error("temporary bank failure"))
      .mockResolvedValueOnce({
        data: {
          id: 91,
          booking_id: 71,
          payment_id: 81,
          bank_payment_order_id: 92,
          subscription_id: null,
          payment_status: "pending",
          order_status: "pending",
          provider_payment_url: "https://bank.example/pay/92",
        },
      });
    renderSheet(vi.fn(), {
      dropInBookingId: 71,
      requiredTariffId: 999,
      requiredDebtId: 11,
      exactPersonalDebtSettlement: {
        bookingId: 71,
        debtId: 11,
        tariffName: "Зафиксированная персоналка",
        amount: "2500.00",
        termsVersion: "complete_v1",
      },
    });

    expect(await screen.findByText("Зафиксированная персоналка")).toBeInTheDocument();
    await waitFor(() => {
      expect(get).toHaveBeenCalledWith("/billing/debts/?student_id=7");
    });
    fireEvent.click(screen.getByRole("button", { name: "СБП" }));
    const submit = screen.getByRole("button", { name: "Создать ссылку СБП" });
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    const firstPayload = post.mock.calls[0]?.[1] as Record<string, unknown>;
    expect(post).toHaveBeenLastCalledWith(
      "/personal-drop-in-bookings/71/bank-payment-orders/",
      {
        debt_id: 11,
        idempotency_key: expect.any(String),
      },
    );
    expect(firstPayload).not.toHaveProperty("tariff_id");
    expect(firstPayload).not.toHaveProperty("amount");
    expect(firstPayload).not.toHaveProperty("discount_ids");

    get.mockImplementation((url: string) => {
      if (url === "/billing/bank-payment-orders/92/") {
        return Promise.resolve({
          data: {
            id: 92,
            payment_id: 81,
            subscription_id: null,
            student_id: 7,
            tariff_id: 999,
            debt_ids: [11],
            source: "trainer",
            status: "pending",
            amount_snapshot: "2500.00",
            currency: "RUB",
            purpose_snapshot: "Зафиксированная персоналка",
            provider_payment_url: "https://bank.example/pay/92",
            expires_at: "2099-06-28T12:00:00Z",
            can_pay: false,
            can_share: true,
            can_copy: true,
            can_show_qr: true,
            can_request_refresh: true,
            can_cancel: true,
          },
        });
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });
    fireEvent.click(submit);
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect((post.mock.calls[1]?.[1] as Record<string, unknown>).idempotency_key).toBe(
      firstPayload.idempotency_key,
    );
    expect(await screen.findByText("Ссылка на оплату персоналки")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Открыть предпросмотр" })).toHaveAttribute(
      "href",
      "https://bank.example/pay/92",
    );
  });

  it.each([
    ["without a receipt debt id", { debtId: 0 }],
    ["without a receipt amount", { amount: "" }],
  ])("fails closed for an exact personal debt %s", async (_scenario, invalidFields) => {
    renderSheet(vi.fn(), {
      exactPersonalDebtSettlement: {
        bookingId: 71,
        debtId: 11,
        tariffName: "Зафиксированная персоналка",
        amount: "2500.00",
        ...invalidFields,
      } as ExactPersonalDebtSettlement,
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Не удалось подтвердить точный долг персоналки",
    );
    expect(screen.getByRole("button", { name: "Наличные" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "СБП" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Принять оплату" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Принять оплату" }));
    expect(post).not.toHaveBeenCalled();
  });

  it("blocks every payment action when a personal drop-in is missing its booking id", async () => {
    renderSheet(vi.fn(), { requiredTariffId: 3 });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "отсутствует идентификатор записи",
    );
    expect(await screen.findByRole("button", { name: /Base/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Принять оплату" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "СБП" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Принять оплату" }));
    expect(post).not.toHaveBeenCalled();
    expect(get).not.toHaveBeenCalledWith("/billing/bank-payment-orders/", expect.anything());
  });

  it("blocks every payment action when a personal drop-in is missing its required tariff", async () => {
    renderSheet(vi.fn(), { dropInBookingId: 71 });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "отсутствует обязательный тариф",
    );
    expect(await screen.findByRole("button", { name: /Base/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Принять оплату" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "СБП" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Принять оплату" }));
    expect(post).not.toHaveBeenCalled();
    expect(get).not.toHaveBeenCalledWith("/billing/bank-payment-orders/", expect.anything());
  });

  it("routes a drop-in debt away from the generic payment action", async () => {
    dropInDebt = true;
    renderSheet(vi.fn());

    expect(
      await screen.findByText("Долг за персоналку оплачивается из блока «Персоналки» в карточке ученика."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: /Долг #55/ })).not.toBeInTheDocument();
  });

  it("creates and displays a personal drop-in online link without querying generic bank orders", async () => {
    dropInDebt = true;
    post.mockResolvedValue({
      data: {
        id: 91,
        booking_id: 71,
        payment_id: 81,
        bank_payment_order_id: 92,
        subscription_id: 93,
        payment_status: "pending",
        order_status: "pending",
        provider_payment_url: "https://bank.example/pay/92",
        can_share: true,
        can_copy: true,
        can_show_qr: true,
        can_request_refresh: true,
      },
    });
    renderSheet(vi.fn(), {
      dropInBookingId: 71,
      requiredTariffId: 3,
      requiredDebtId: 11,
    });

    fireEvent.click(await screen.findByRole("button", { name: "СБП" }));
    get.mockImplementation((url: string) => {
      if (url === "/billing/bank-payment-orders/92/") {
        return Promise.resolve({
          data: {
            id: 92,
            payment_id: 81,
            subscription_id: 93,
            student_id: 7,
            tariff_id: 3,
            debt_ids: [11],
            source: "trainer",
            status: "pending",
            amount_snapshot: "1200.00",
            currency: "RUB",
            purpose_snapshot: "Разовая персоналка",
            provider_payment_url: "https://bank.example/pay/92",
            expires_at: "2099-06-28T12:00:00Z",
            can_pay: false,
            can_share: true,
            can_copy: true,
            can_show_qr: true,
            can_request_refresh: true,
            can_cancel: true,
          },
        });
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });
    fireEvent.click(screen.getByRole("button", { name: "Создать ссылку СБП" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/personal-drop-in-bookings/71/bank-payment-orders/",
        {},
      );
    });
    expect(await screen.findByText("Ссылка на оплату персоналки")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Открыть предпросмотр" })).toHaveAttribute(
      "href",
      "https://bank.example/pay/92",
    );
    expect(get).toHaveBeenCalledWith("/billing/bank-payment-orders/92/");
  });

  it("loads active discounts, previews percent and fixed discounts, and submits one selected discount", async () => {
    activeDiscounts = [...discountOptions];
    renderSheet();

    fireEvent.click(await screen.findByRole("button", { name: /Base/ }));

    await waitFor(() => {
      expect(get).toHaveBeenCalledWith("/billing/discounts/");
    });
    fireEvent.click(await screen.findByRole("radio", { name: /Семейная скидка/ }));
    const paymentSummary = screen.getByRole("region", { name: "Итог оплаты" });
    expect(within(paymentSummary).getByText(/−500\s₽/)).toBeInTheDocument();
    expect(within(paymentSummary).getByText(/4\s500\s₽/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("radio", { name: /Скидка на форму/ }));
    expect(within(paymentSummary).getByText(/−1\s000\s₽/)).toBeInTheDocument();
    expect(within(paymentSummary).getByText(/4\s000\s₽/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Принять оплату" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/billing/payments/",
        expect.objectContaining({
          tariff_id: 3,
          payment_method: "cash",
          discount_ids: [22],
        }),
      );
    });
  });

  it("rounds a fractional percent preview to the same cents as the backend", async () => {
    baseTariffPrice = 999.99;
    activeDiscounts = [discountOptions[3]];
    renderSheet();

    fireEvent.click(await screen.findByRole("button", { name: /Base/ }));
    fireEvent.click(await screen.findByRole("radio", { name: /Точная скидка/ }));

    const paymentSummary = screen.getByRole("region", { name: "Итог оплаты" });
    expect(within(paymentSummary).getByText(/−123,4\s₽/)).toBeInTheDocument();
    expect(within(paymentSummary).getByText(/876,59\s₽/)).toBeInTheDocument();
  });

  it("clears the selected discount for online payment and does not restore it", async () => {
    activeDiscounts = [...discountOptions];
    renderSheet();

    fireEvent.click(await screen.findByRole("button", { name: /Base/ }));
    fireEvent.click(await screen.findByRole("radio", { name: /Семейная скидка/ }));
    fireEvent.click(screen.getByRole("button", { name: "СБП" }));

    expect(screen.queryByRole("radio", { name: /Семейная скидка/ })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Наличные" }));
    expect(screen.getByRole("radio", { name: /Без скидки/ })).toBeChecked();
  });

  it("fails closed for manual payment when discounts cannot load and allows retry", async () => {
    rejectDiscounts = true;
    renderSheet();

    fireEvent.click(await screen.findByRole("button", { name: /Base/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Не удалось загрузить скидки");
    expect(screen.queryByRole("radiogroup", { name: "Скидка" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Принять оплату" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "СБП" }));
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Создать ссылку СБП" })).toBeEnabled();
    });

    rejectDiscounts = false;
    fireEvent.click(screen.getByRole("button", { name: "Наличные" }));
    fireEvent.click(screen.getByRole("button", { name: "Повторить загрузку" }));

    expect(await screen.findByRole("radio", { name: /Без скидки/ })).toBeChecked();
    expect(screen.getByRole("button", { name: "Принять оплату" })).toBeEnabled();
  });

  it("does not retain a selected discount when the student changes", async () => {
    activeDiscounts = [...discountOptions];
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });
    queryClient.setQueryData(getPaymentCapabilitiesQueryKey(), {
      ...ENABLED_SBP_PAYMENT_CAPABILITIES,
      training_group_rollout_mode: "off",
      training_group_payment_selection_mode: "legacy",
      canonical_group_selection_enabled: false,
    });
    const onOpenChange = vi.fn();
    const { rerender } = render(
      <QueryClientProvider client={queryClient}>
        <PaymentSheet
          open
          onOpenChange={onOpenChange}
          studentId={7}
          studentName="Masha Ivanova"
        />
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByRole("button", { name: /Base/ }));
    fireEvent.click(await screen.findByRole("radio", { name: /Семейная скидка/ }));
    expect(screen.getByRole("radio", { name: /Семейная скидка/ })).toBeChecked();

    rerender(
      <QueryClientProvider client={queryClient}>
        <PaymentSheet
          open
          onOpenChange={onOpenChange}
          studentId={8}
          studentName="Ivan Petrov"
        />
      </QueryClientProvider>,
    );

    expect(await screen.findByRole("radio", { name: /Без скидки/ })).toBeChecked();
  });

  it("disables manual payment when a selected discount reduces the amount to zero", async () => {
    activeDiscounts = [...discountOptions];
    renderSheet();

    fireEvent.click(await screen.findByRole("button", { name: /Base/ }));
    fireEvent.click(await screen.findByRole("radio", { name: /Полная скидка/ }));

    expect(screen.getByRole("button", { name: "Принять оплату" })).toBeDisabled();
    expect(
      screen.getByText("Сумма к оплате должна быть больше 0 — выберите другой тариф или скидку."),
    ).toBeInTheDocument();
  });

  it("shows who will own a personal package before accepting payment", async () => {
    renderSheet();

    fireEvent.click(await screen.findByRole("button", { name: /Base/ }));

    expect(
      await screen.findByText("Пакет закрепится за Current Trainer"),
    ).toBeInTheDocument();
  });

  it("shows the safe payout timing hint from tariff list response", async () => {
    renderSheet();

    fireEvent.click(await screen.findByRole("button", { name: /Group Plan/ }));

    expect(screen.getAllByText("Выплата после подтверждения")).toHaveLength(2);
    expect(screen.queryByText(/on_payment/)).not.toBeInTheDocument();
  });

  it("shows mixed payout timing for hybrid packages without raw policy names", async () => {
    renderSheet();

    fireEvent.click(await screen.findByRole("button", { name: /Hybrid Plan/ }));

    expect(screen.getAllByText("Смешанная выплата по пакету")).toHaveLength(2);
    expect(
      await screen.findByText("Пакет закрепится за Current Trainer"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/on_checkin/)).not.toBeInTheDocument();
  });

  it("submits current trainer as package owner for group-primary hybrid packages", async () => {
    renderSheet();

    fireEvent.click(await screen.findByRole("button", { name: /Hybrid Plan/ }));
    fireEvent.click(await screen.findByRole("radio", { name: /Tue Thu Group/ }));
    fireEvent.click(screen.getByRole("button", { name: "Принять оплату" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/billing/payments/",
        expect.objectContaining({
          tariff_id: 5,
          seller_trainer_id: 4,
          package_owner_trainer_id: 4,
          target_schedule_id: 31,
          target_start_date: "2030-01-08",
        }),
      );
    });
  });

  it("submits the selected permanent group and occurrence for manual payment", async () => {
    renderSheet();

    fireEvent.click(await screen.findByRole("button", { name: /Group Plan/ }));
    const group = await screen.findByRole("radio", { name: /Tue Thu Group/ });
    expect(group).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Принять оплату" })).toBeDisabled();
    fireEvent.click(group);
    expect(screen.getByText("Main Gym")).toBeInTheDocument();
    expect(screen.getByText("08.01.2030 · 18:00–19:00")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Первое занятие"), {
      target: { value: "2030-01-10" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Принять оплату" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/billing/payments/",
        expect.objectContaining({
          tariff_id: 4,
          target_schedule_id: 31,
          target_start_date: "2030-01-10",
        }),
      );
    });
  });

  it("uses date-effective occurrence details for a legacy schedule option", async () => {
    groupEnrollmentOptions = [
      {
        schedule_id: 31,
        group_name: "Legacy compatible group",
        trainer_id: 8,
        trainer_name: "Regular Trainer",
        location_id: 2,
        location_name: "Main Gym",
        training_type_id: 5,
        training_type_name: "Group Boxing",
        day_of_week: 1,
        start_time: "18:00:00",
        end_time: "19:00:00",
        next_occurrence_date: "2030-01-08",
        occurrence_dates: ["2030-01-08", "2030-01-10"],
        is_latest_trial_group: false,
        is_canonical_group_card: false,
        upcoming_occurrences: [
          {
            schedule_id: 31,
            date: "2030-01-08",
            start_time: "18:00:00",
            end_time: "19:00:00",
            trainer_id: 8,
            trainer_name: "Regular Trainer",
            location_id: 2,
            location_name: "Main Gym",
          },
          {
            schedule_id: 31,
            date: "2030-01-10",
            start_time: "20:30:00",
            end_time: "21:30:00",
            trainer_id: 9,
            trainer_name: "Substitute Trainer",
            location_id: 3,
            location_name: "North Gym",
          },
        ],
      },
    ];
    renderSheet();

    fireEvent.click(await screen.findByRole("button", { name: /Group Plan/ }));
    fireEvent.click(await screen.findByRole("radio", { name: /Legacy compatible group/ }));
    fireEvent.change(screen.getByLabelText("Первое занятие"), {
      target: { value: "2030-01-10" },
    });

    expect(screen.getByText("10.01.2030 · 20:30–21:30")).toBeInTheDocument();
    expect(screen.getByText("North Gym")).toBeInTheDocument();
    expect(screen.getByText("Substitute Trainer")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Принять оплату" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/billing/payments/",
        expect.objectContaining({
          target_schedule_id: 31,
          target_start_date: "2030-01-10",
        }),
      );
    });
  });

  it("requires an explicit canonical occurrence before submitting its group identity", async () => {
    groupEnrollmentOptions = [
      {
        schedule_id: 31,
        group_name: "Canonical evening group",
        trainer_id: 8,
        trainer_name: "Target Trainer",
        location_id: 2,
        location_name: "Main Gym",
        training_type_id: 5,
        training_type_name: "Group Boxing",
        day_of_week: 1,
        start_time: "18:00:00",
        end_time: "19:00:00",
        next_occurrence_date: "2030-01-08",
        occurrence_dates: ["2030-01-08", "2030-01-10"],
        is_latest_trial_group: false,
        training_group_id: 71,
        responsible_trainer_id: 4,
        responsible_trainer_name: "Responsible Trainer",
        target_group_membership_id: 81,
        slot_schedule_ids: [31, 32],
        is_canonical_group_card: true,
        upcoming_occurrences: [
          {
            schedule_id: 31,
            date: "2030-01-08",
            start_time: "18:00:00",
            end_time: "19:00:00",
            trainer_id: 8,
            trainer_name: "Target Trainer",
            location_id: 2,
            location_name: "Main Gym",
          },
          {
            schedule_id: 32,
            date: "2030-01-10",
            start_time: "19:00:00",
            end_time: "20:00:00",
            trainer_id: 9,
            trainer_name: "Occurrence Trainer",
            location_id: 3,
            location_name: "North Gym",
          },
        ],
      },
    ];
    renderSheet(
      vi.fn(),
      {},
      {
        ...ENABLED_SBP_PAYMENT_CAPABILITIES,
        training_group_rollout_mode: "active",
        training_group_payment_selection_mode: "canonical",
        canonical_group_selection_enabled: true,
      },
    );

    fireEvent.click(await screen.findByRole("button", { name: /Group Plan/ }));
    fireEvent.click(await screen.findByRole("radio", { name: /Canonical evening group/ }));

    expect(screen.getByRole("button", { name: "Принять оплату" })).toBeDisabled();
    expect(screen.getByRole("radiogroup", { name: "Выбор первого занятия" })).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("radio", { name: /10\.01\.2030.*North Gym.*Occurrence Trainer/ }),
    );
    expect(screen.getByText("Responsible Trainer")).toBeInTheDocument();
    expect(screen.getByText("Occurrence Trainer")).toBeInTheDocument();
    expect(screen.getByText("Current Trainer")).toBeInTheDocument();
    expect(screen.getByText("Ожидает подтверждения")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Принять оплату" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/billing/payments/",
        expect.objectContaining({
          target_training_group_id: 71,
          target_schedule_id: 32,
          target_start_date: "2030-01-10",
        }),
      );
    });
  });

  it("sends a stable command key with a canonical group manual payment", async () => {
    groupEnrollmentOptions = [
      {
        schedule_id: 31,
        group_name: "Canonical evening group",
        trainer_id: 8,
        trainer_name: "Target Trainer",
        location_id: 2,
        location_name: "Main Gym",
        training_type_id: 5,
        training_type_name: "Group Boxing",
        day_of_week: 1,
        start_time: "18:00:00",
        end_time: "19:00:00",
        next_occurrence_date: "2030-01-08",
        occurrence_dates: ["2030-01-08"],
        is_latest_trial_group: false,
        training_group_id: 71,
        responsible_trainer_id: 4,
        responsible_trainer_name: "Responsible Trainer",
        target_group_membership_id: 81,
        slot_schedule_ids: [31],
        is_canonical_group_card: true,
        upcoming_occurrences: [
          {
            schedule_id: 31,
            date: "2030-01-08",
            start_time: "18:00:00",
            end_time: "19:00:00",
            trainer_id: 8,
            trainer_name: "Target Trainer",
            location_id: 2,
            location_name: "Main Gym",
          },
        ],
      },
    ];
    renderSheet(
      vi.fn(),
      {},
      {
        ...ENABLED_SBP_PAYMENT_CAPABILITIES,
        training_group_rollout_mode: "active",
        training_group_payment_selection_mode: "canonical",
        canonical_group_selection_enabled: true,
      },
    );

    fireEvent.click(await screen.findByRole("button", { name: /Group Plan/ }));
    fireEvent.click(await screen.findByRole("radio", { name: /Canonical evening group/ }));
    fireEvent.click(screen.getByRole("radio", { name: /08\.01\.2030.*Target Trainer/ }));
    fireEvent.click(screen.getByRole("button", { name: "Принять оплату" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/billing/payments/",
        expect.objectContaining({
          target_training_group_id: 71,
          target_schedule_id: 31,
          target_start_date: "2030-01-08",
          idempotency_key: expect.any(String),
        }),
      );
    });
  });

  it("retains a canonical group manual key through a lost response and remount", async () => {
    groupEnrollmentOptions = [
      {
        schedule_id: 31,
        group_name: "Canonical evening group",
        trainer_id: 8,
        trainer_name: "Target Trainer",
        location_id: 2,
        location_name: "Main Gym",
        training_type_id: 5,
        training_type_name: "Group Boxing",
        day_of_week: 1,
        start_time: "18:00:00",
        end_time: "19:00:00",
        next_occurrence_date: "2030-01-08",
        occurrence_dates: ["2030-01-08"],
        is_latest_trial_group: false,
        training_group_id: 71,
        responsible_trainer_id: 4,
        responsible_trainer_name: "Responsible Trainer",
        target_group_membership_id: 81,
        slot_schedule_ids: [31],
        is_canonical_group_card: true,
        upcoming_occurrences: [
          {
            schedule_id: 31,
            date: "2030-01-08",
            start_time: "18:00:00",
            end_time: "19:00:00",
            trainer_id: 8,
            trainer_name: "Target Trainer",
            location_id: 2,
            location_name: "Main Gym",
          },
        ],
      },
    ];
    const capabilities = {
      ...ENABLED_SBP_PAYMENT_CAPABILITIES,
      training_group_rollout_mode: "active" as const,
      training_group_payment_selection_mode: "canonical" as const,
      canonical_group_selection_enabled: true,
    };
    const selectExactGroup = async () => {
      fireEvent.click(await screen.findByRole("button", { name: /Group Plan/ }));
      fireEvent.click(await screen.findByRole("radio", { name: /Canonical evening group/ }));
      fireEvent.click(screen.getByRole("radio", { name: /08\.01\.2030.*Target Trainer/ }));
    };
    post
      .mockRejectedValueOnce(new Error("connection lost"))
      .mockResolvedValueOnce({ data: { id: 99 } });

    const firstView = renderSheet(vi.fn(), {}, capabilities);
    await selectExactGroup();
    const submit = screen.getByRole("button", { name: "Принять оплату" });
    fireEvent.click(submit);
    fireEvent.click(submit);
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    const firstKey = (post.mock.calls[0]?.[1] as Record<string, unknown>).idempotency_key;
    firstView.unmount();

    renderSheet(vi.fn(), {}, capabilities);
    await selectExactGroup();
    fireEvent.click(screen.getByRole("button", { name: "Принять оплату" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect((post.mock.calls[1]?.[1] as Record<string, unknown>).idempotency_key).toBe(firstKey);
  });

  it("sends the canonical group and explicit occurrence when creating an online link", async () => {
    groupEnrollmentOptions = [
      {
        schedule_id: 31,
        group_name: "Canonical evening group",
        trainer_id: 8,
        trainer_name: "Target Trainer",
        location_id: 2,
        location_name: "Main Gym",
        training_type_id: 5,
        training_type_name: "Group Boxing",
        day_of_week: 1,
        start_time: "18:00:00",
        end_time: "19:00:00",
        next_occurrence_date: "2030-01-08",
        occurrence_dates: ["2030-01-08"],
        is_latest_trial_group: false,
        training_group_id: 71,
        responsible_trainer_id: 4,
        responsible_trainer_name: "Responsible Trainer",
        target_group_membership_id: 81,
        slot_schedule_ids: [31],
        is_canonical_group_card: true,
        upcoming_occurrences: [
          {
            schedule_id: 31,
            date: "2030-01-08",
            start_time: "18:00:00",
            end_time: "19:00:00",
            trainer_id: 8,
            trainer_name: "Target Trainer",
            location_id: 2,
            location_name: "Main Gym",
          },
        ],
      },
    ];
    renderSheet(
      vi.fn(),
      {},
      {
        ...ENABLED_SBP_PAYMENT_CAPABILITIES,
        training_group_rollout_mode: "active",
        training_group_payment_selection_mode: "canonical",
        canonical_group_selection_enabled: true,
      },
    );

    fireEvent.click(await screen.findByRole("button", { name: /Group Plan/ }));
    fireEvent.click(await screen.findByRole("radio", { name: /Canonical evening group/ }));
    fireEvent.click(screen.getByRole("radio", { name: /08\.01\.2030.*Target Trainer/ }));
    fireEvent.click(screen.getByRole("button", { name: "СБП" }));
    fireEvent.click(screen.getByRole("button", { name: "Создать ссылку СБП" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/billing/bank-payment-orders/",
        expect.objectContaining({
          target_training_group_id: 71,
          target_schedule_id: 31,
          target_start_date: "2030-01-08",
          idempotency_key: expect.any(String),
        }),
      );
    });
  });

  it("explains immediate operational admission while financial state remains pending", async () => {
    renderSheet();

    fireEvent.click(await screen.findByRole("button", { name: /Group Plan/ }));
    fireEvent.click(await screen.findByRole("radio", { name: /Tue Thu Group/ }));

    expect(
      screen.getByText(
        "Точный слот и дата будут повторно проверены сервером перед созданием оплаты или ссылки.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Ученик появится в постоянной группе после подтверждения оплаты."),
    ).not.toBeInTheDocument();
  });

  it("awaits every scoped admission invalidation before reporting success and closing", async () => {
    post.mockResolvedValueOnce({ data: { id: 99, student_id: 707 } });
    const onOpenChange = vi.fn();
    const onPaymentRecorded = vi.fn();
    const { queryClient } = renderSheet(
      onOpenChange,
      {},
      legacyGroupPaymentCapabilities,
      false,
      onPaymentRecorded,
    );
    const invalidatedKeys: unknown[][] = [];
    const resolveInvalidations: Array<() => void> = [];
    const invalidateSpy = vi
      .spyOn(queryClient, "invalidateQueries")
      .mockImplementation((filters) => {
        invalidatedKeys.push([...(filters?.queryKey ?? [])]);
        return new Promise<void>((resolve) => resolveInvalidations.push(resolve));
      });

    fireEvent.click(await screen.findByRole("button", { name: /Group Plan/ }));
    fireEvent.click(await screen.findByRole("radio", { name: /Tue Thu Group/ }));
    fireEvent.click(screen.getByRole("button", { name: "Принять оплату" }));

    await waitFor(() => expect(invalidateSpy).toHaveBeenCalledTimes(11));
    expect(invalidatedKeys).toEqual(
      expect.arrayContaining([
        ["billing", "payment-capabilities"],
        ["student", "7"],
        ["student", "7", "subscriptions"],
        ["billing", "debts", 7],
        ["student", "7", "commercial-context"],
        ["billing", "bank-payment-orders", 7, "live"],
        ["leads"],
        ["retention-tasks"],
        ["schedules"],
        ["billing", "group-enrollment-options", 7, 4],
        ["schedule", "31", "students"],
      ]),
    );
    expect(onPaymentRecorded).not.toHaveBeenCalled();
    expect(onOpenChange).not.toHaveBeenCalledWith(false);

    resolveInvalidations.forEach((resolve) => resolve());

    await waitFor(() => {
      expect(onPaymentRecorded).toHaveBeenCalledWith(
        expect.objectContaining({ studentId: 707, paymentId: 99 }),
      );
      expect(onOpenChange).toHaveBeenCalledWith(false);
    });
  });

  it("keeps the trainer payment sheet open after the admission switch rejects a manual group payment", async () => {
    post.mockRejectedValueOnce({ response: { data: { code: "manual_operational_admission_disabled" } } });
    const onOpenChange = vi.fn();
    renderSheet(onOpenChange);

    fireEvent.click(await screen.findByRole("button", { name: /Group Plan/ }));
    fireEvent.click(await screen.findByRole("radio", { name: /Tue Thu Group/ }));
    fireEvent.click(screen.getByRole("button", { name: "Принять оплату" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/billing/payments/",
        expect.objectContaining({ target_schedule_id: 31 }),
      );
    });
    expect(await screen.findByRole("radio", { name: /Tue Thu Group/ })).toBeChecked();
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
  });

  it("fails closed for online creation when the capability is unavailable", async () => {
    renderSheet(vi.fn(), {}, {
      ...legacyGroupPaymentCapabilities,
      online_payments_enabled: false,
    });

    fireEvent.click(await screen.findByRole("button", { name: /Group Plan/ }));
    fireEvent.click(await screen.findByRole("radio", { name: /Tue Thu Group/ }));

    const online = screen.getByRole("button", { name: "СБП" });
    expect(online).toBeDisabled();
    fireEvent.click(online);
    expect(post).not.toHaveBeenCalledWith(
      "/billing/bank-payment-orders/",
      expect.anything(),
    );
  });

  it("fails closed for group selection while the server capability is unavailable", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/billing/payment-capabilities/") {
        return new Promise(() => {});
      }
      if (url === "/billing/group-enrollment-options/") {
        return Promise.resolve({ data: groupEnrollmentOptions });
      }
      if (url === "/billing/discounts/") {
        return Promise.resolve({ data: [] });
      }
      if (url === "/billing/tariffs/") {
        return Promise.resolve({
          data: {
            items: [
              {
                id: 4,
                name: "Group Plan",
                price: 4500,
                training_type: {
                  id: 5,
                  name: "Group Boxing",
                  slug: "group-boxing",
                  kind: "group",
                  is_active: true,
                },
                trainings_limit: 8,
                duration_days: 30,
                is_active: true,
                payout_timing_hint: "after_payment_confirmation",
                requires_package_owner: false,
              },
            ],
          },
        });
      }
      if (url === "/trainers/me/") {
        return Promise.resolve({ data: { id: 4, first_name: "Current", last_name: "Trainer" } });
      }
      if (url === "/billing/debts/?student_id=7" || url === "/billing/bank-payment-orders/") {
        return Promise.resolve({ data: { items: [] } });
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderSheet(vi.fn(), {}, legacyGroupPaymentCapabilities, true);

    fireEvent.click(await screen.findByRole("button", { name: /Group Plan/ }));
    expect(
      await screen.findByText(/Не удалось подтвердить режим выбора группы/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("radio", { name: /Tue Thu Group/ })).not.toBeInTheDocument();
    const online = screen.getByRole("button", { name: "СБП" });
    expect(online).toBeVisible();
    expect(online).toBeDisabled();
    fireEvent.click(online);
    expect(post).not.toHaveBeenCalledWith(
      "/billing/bank-payment-orders/",
      expect.anything(),
    );
  });

  it("hides containment group selection but keeps a persisted group order readable and cancelable", async () => {
    pendingBankOrders = [
      {
        id: 77,
        tariff_id: 4,
        target_training_group_id: 71,
        target_schedule_id: 31,
        target_start_date: "2030-01-08",
        debt_ids: [],
        subscription_id: 70,
        status: "pending",
        amount_snapshot: "4500.00",
        currency: "RUB",
        provider_payment_link_id: "persisted-group-target",
        provider_payment_url: "https://pay.example/persisted-group-target",
        can_share: true,
        can_copy: true,
        can_show_qr: true,
        can_request_refresh: true,
        provider_status: "CREATED",
        expires_at: "2099-06-28T12:00:00Z",
        can_cancel: true,
      },
    ];
    renderSheet(vi.fn(), {}, {
      ...ENABLED_SBP_PAYMENT_CAPABILITIES,
      training_group_rollout_mode: "containment",
      training_group_payment_selection_mode: "disabled",
      canonical_group_selection_enabled: false,
    });

    fireEvent.click(await screen.findByRole("button", { name: /Group Plan/ }));

    expect(screen.getByText(/Новые оплаты в группу временно отключены на сервере/)).toBeInTheDocument();
    expect(screen.queryByRole("radio", { name: /Tue Thu Group/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Принять оплату" })).toBeDisabled();
    expect(await screen.findByText("Другая активная ссылка")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Открыть предпросмотр/ })).toHaveAttribute(
      "href",
      "https://pay.example/persisted-group-target",
    );

    fireEvent.click(screen.getByRole("button", { name: "Отменить" }));
    const cancelDialog = await screen.findByRole("dialog", { name: "Отменить оплату?" });
    fireEvent.click(within(cancelDialog).getByRole("button", { name: "Отменить" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/billing/bank-payment-orders/77/cancel/", {});
    });
  });

  it.each(["shadow", "active"])(
    "fails closed before a new group payment when %s is emergency-disabled",
    async (rolloutMode) => {
      renderSheet(vi.fn(), {}, {
        ...ENABLED_SBP_PAYMENT_CAPABILITIES,
        training_group_rollout_mode: rolloutMode,
        training_group_payment_selection_mode: "disabled",
        canonical_group_selection_enabled: false,
      });

      fireEvent.click(await screen.findByRole("button", { name: /Group Plan/ }));

      expect(screen.getByText(/Новые оплаты в группу временно отключены на сервере/)).toBeInTheDocument();
      expect(screen.queryByRole("radio", { name: /Tue Thu Group/ })).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Принять оплату" })).toBeDisabled();
      fireEvent.click(screen.getByRole("button", { name: "СБП" }));
      expect(screen.getByRole("button", { name: "Создать ссылку СБП" })).toBeDisabled();
      expect(post).not.toHaveBeenCalledWith("/billing/payments/", expect.anything());
      expect(post).not.toHaveBeenCalledWith("/billing/bank-payment-orders/", expect.anything());
    },
  );

  it("creates an online payment link without closing the sheet", async () => {
    post.mockResolvedValueOnce({
      data: {
        id: 41,
        tariff_id: 4,
        target_schedule_id: 31,
        target_start_date: "2030-01-08",
        debt_ids: [],
        payment_id: 91,
        subscription_id: 51,
        student_id: 7,
        provider: "mock",
        source: "trainer",
        status: "pending",
        amount_snapshot: "4500.00",
        currency: "RUB",
        purpose_snapshot: "Абонемент Group Plan",
        provider_payment_link_id: "jgr-41-test",
        provider_payment_url: "https://pay.example/jgr-41-test",
        can_share: true,
        can_copy: true,
        can_show_qr: true,
        can_request_refresh: true,
        provider_payment_modes: ["card"],
        provider_status: "CREATED",
        expires_at: "2099-06-28T12:00:00Z",
        paid_at: null,
        receipt_mode: "none",
        receipt_status: "not_required",
      },
    });
    const onOpenChange = vi.fn();
    renderSheet(onOpenChange);

    fireEvent.click(await screen.findByRole("button", { name: /Group Plan/ }));
    fireEvent.click(await screen.findByRole("radio", { name: /Tue Thu Group/ }));
    fireEvent.click(screen.getByRole("button", { name: "СБП" }));
    fireEvent.click(screen.getByRole("button", { name: "Создать ссылку СБП" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/billing/bank-payment-orders/",
        expect.objectContaining({
          student_id: 7,
          tariff_id: 4,
          discount_ids: [],
          debt_ids: [],
          seller_trainer_id: 4,
          target_schedule_id: 31,
          target_start_date: "2030-01-08",
        }),
      );
    });
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
    expect(await screen.findByText("Ссылка на оплату")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Открыть предпросмотр/ })).toHaveAttribute(
      "href",
      "https://pay.example/jgr-41-test",
    );
    expect(screen.getByRole("button", { name: "Показать QR" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.queryByRole("img", { name: "QR-код ссылки на оплату" })).not.toBeInTheDocument();
  });

  it("keeps a mismatched live group link visible and cancelable", async () => {
    pendingBankOrders = [
      {
        id: 77,
        tariff_id: 4,
        target_schedule_id: 99,
        target_start_date: "2030-01-09",
        debt_ids: [],
        subscription_id: 70,
        status: "pending",
        amount_snapshot: "4500.00",
        currency: "RUB",
        provider_payment_link_id: "old-target",
        provider_payment_url: "https://pay.example/old-target",
        can_share: true,
        can_copy: true,
        can_show_qr: true,
        can_request_refresh: true,
        provider_status: "CREATED",
        expires_at: "2099-06-28T12:00:00Z",
        can_cancel: true,
      },
    ];
    renderSheet();

    fireEvent.click(await screen.findByRole("button", { name: /Group Plan/ }));
    fireEvent.click(await screen.findByRole("radio", { name: /Tue Thu Group/ }));
    fireEvent.click(screen.getByRole("button", { name: "СБП" }));

    expect(await screen.findByText("Другая активная ссылка")).toBeInTheDocument();
    expect(
      screen.getByText(/Сначала отмените эту ссылку, затем выберите новую группу/),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Отменить" })).toBeEnabled();
    expect(
      screen.getByRole("button", { name: "Сначала отмените ссылку" }),
    ).toBeDisabled();
    expect(post).not.toHaveBeenCalledWith(
      "/billing/bank-payment-orders/",
      expect.anything(),
    );
  });

  it("keeps a created bank order without a provider URL visible and cancelable", async () => {
    pendingBankOrders = [
      {
        id: 78,
        tariff_id: 4,
        target_schedule_id: 31,
        target_start_date: "2030-01-08",
        debt_ids: [],
        subscription_id: 71,
        status: "created",
        amount_snapshot: "4500.00",
        currency: "RUB",
        provider_payment_link_id: "",
        provider_payment_url: "",
        can_share: true,
        can_copy: true,
        can_show_qr: true,
        can_request_refresh: true,
        provider_status: "",
        expires_at: "2099-06-28T12:00:00Z",
        can_cancel: true,
      },
    ];
    renderSheet();

    fireEvent.click(await screen.findByRole("button", { name: /Group Plan/ }));
    fireEvent.click(await screen.findByRole("radio", { name: /Tue Thu Group/ }));
    fireEvent.click(screen.getByRole("button", { name: "СБП" }));

    const panel = await screen.findByRole("region", { name: "Ссылка на оплату" });
    expect(
      within(panel).getByText(/Онлайн-оплата сейчас недоступна/),
    ).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: "Отменить" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Ссылка создана" })).toBeDisabled();
    expect(post).not.toHaveBeenCalledWith(
      "/billing/bank-payment-orders/",
      expect.anything(),
    );
  });

  it("shows an existing pending online payment link instead of creating a duplicate", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/billing/payment-capabilities/") {
        return Promise.resolve({ data: legacyGroupPaymentCapabilities });
      }

      if (url === "/billing/group-enrollment-options/") {
        return Promise.resolve({ data: groupEnrollmentOptions });
      }
      if (url === "/billing/discounts/") {
        return Promise.resolve({ data: activeDiscounts });
      }

      if (url === "/billing/tariffs/") {
        return Promise.resolve({
          data: {
            items: [
              {
                id: 4,
                name: "Group Plan",
                price: 4500,
                training_type: {
                  id: 5,
                  name: "Group Boxing",
                  slug: "group-boxing",
                  kind: "group",
                  is_active: true,
                },
                trainings_limit: 8,
                duration_days: 30,
                is_active: true,
                payout_timing_hint: "after_payment_confirmation",
                requires_package_owner: false,
              },
            ],
          },
        });
      }

      if (url === "/trainers/me/") {
        return Promise.resolve({ data: { id: 4, first_name: "Current", last_name: "Trainer" } });
      }

      if (url === "/billing/debts/?student_id=7") {
        return Promise.resolve({ data: { items: [] } });
      }

      if (url === "/billing/bank-payment-orders/") {
        return Promise.resolve({
          data: {
            items: [
              {
                id: 41,
                tariff_id: 4,
                target_schedule_id: 31,
                target_start_date: "2030-01-08",
                debt_ids: [],
                payment_id: 91,
                subscription_id: 51,
                student_id: 7,
                provider: "mock",
                source: "trainer",
                status: "pending",
                amount_snapshot: "4500.00",
                currency: "RUB",
                purpose_snapshot: "Абонемент Group Plan",
                provider_payment_link_id: "jgr-41-test",
                provider_payment_url: "https://pay.example/jgr-41-test",
        can_share: true,
        can_copy: true,
        can_show_qr: true,
        can_request_refresh: true,
                provider_payment_modes: ["card"],
                provider_status: "CREATED",
                expires_at: "2099-06-28T12:00:00Z",
                paid_at: null,
                receipt_mode: "none",
                receipt_status: "not_required",
              },
            ],
          },
        });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderSheet();

    fireEvent.click(await screen.findByRole("button", { name: /Group Plan/ }));
    fireEvent.click(await screen.findByRole("radio", { name: /Tue Thu Group/ }));
    fireEvent.click(await screen.findByRole("button", { name: "СБП" }));

    expect(await screen.findByText("Ссылка на оплату")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Открыть предпросмотр/ })).toHaveAttribute(
      "href",
      "https://pay.example/jgr-41-test",
    );
    expect(screen.getByRole("button", { name: "Показать QR" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.queryByRole("img", { name: "QR-код ссылки на оплату" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ссылка создана" })).toBeDisabled();
    expect(post).not.toHaveBeenCalledWith(
      "/billing/bank-payment-orders/",
      expect.anything(),
    );
  });

  it("shows an existing online link when its reserved debt is hidden from open debts", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/billing/group-enrollment-options/") {
        return Promise.resolve({ data: groupEnrollmentOptions });
      }
      if (url === "/billing/discounts/") {
        return Promise.resolve({ data: activeDiscounts });
      }

      if (url === "/billing/tariffs/") {
        return Promise.resolve({
          data: {
            items: [
              {
                id: 4,
                name: "Group Plan",
                price: 4500,
                training_type: {
                  id: 5,
                  name: "Group Boxing",
                  slug: "group-boxing",
                  kind: "group",
                  is_active: true,
                },
                trainings_limit: 8,
                duration_days: 30,
                is_active: true,
                payout_timing_hint: "after_payment_confirmation",
                requires_package_owner: false,
              },
            ],
          },
        });
      }

      if (url === "/trainers/me/") {
        return Promise.resolve({ data: { id: 4, first_name: "Current", last_name: "Trainer" } });
      }

      if (url === "/billing/debts/?student_id=7") {
        return Promise.resolve({ data: { items: [] } });
      }

      if (url === "/billing/bank-payment-orders/") {
        return Promise.resolve({
          data: {
            items: [
              {
                id: 41,
                tariff_id: 4,
                target_schedule_id: 31,
                target_start_date: "2030-01-08",
                debt_ids: [11],
                payment_id: 91,
                subscription_id: 51,
                student_id: 7,
                provider: "mock",
                source: "trainer",
                status: "pending",
                amount_snapshot: "5700.00",
                currency: "RUB",
                purpose_snapshot: "Абонемент Group Plan",
                provider_payment_link_id: "jgr-41-test",
                provider_payment_url: "https://pay.example/jgr-41-test",
        can_share: true,
        can_copy: true,
        can_show_qr: true,
        can_request_refresh: true,
                provider_payment_modes: ["card"],
                provider_status: "CREATED",
                expires_at: "2099-06-28T12:00:00Z",
                paid_at: null,
                receipt_mode: "none",
                receipt_status: "not_required",
              },
            ],
          },
        });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderSheet();

    fireEvent.click(await screen.findByRole("button", { name: /Group Plan/ }));
    fireEvent.click(await screen.findByRole("radio", { name: /Tue Thu Group/ }));
    fireEvent.click(await screen.findByRole("button", { name: "СБП" }));

    expect(await screen.findByText("Ссылка на оплату")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Открыть предпросмотр/ })).toHaveAttribute(
      "href",
      "https://pay.example/jgr-41-test",
    );
    expect(screen.getByRole("button", { name: "Ссылка создана" })).toBeDisabled();
    expect(post).not.toHaveBeenCalledWith(
      "/billing/bank-payment-orders/",
      expect.anything(),
    );
  });

  it("keeps online link creation disabled while cancellation is pending", async () => {
    const liveOrder = {
      id: 41,
      tariff_id: 4,
      target_schedule_id: 31,
      target_start_date: "2030-01-08",
      debt_ids: [],
      payment_id: 91,
      subscription_id: 51,
      student_id: 7,
      provider: "mock",
      source: "trainer",
      status: "pending",
      amount_snapshot: "4500.00",
      currency: "RUB",
      purpose_snapshot: "Абонемент Group Plan",
      provider_payment_link_id: "jgr-41-test",
      provider_payment_url: "https://pay.example/jgr-41-test",
        can_share: true,
        can_copy: true,
        can_show_qr: true,
        can_request_refresh: true,
      provider_payment_modes: ["card"],
      provider_status: "CREATED",
      expires_at: "2099-06-28T12:00:00Z",
      paid_at: null,
      receipt_mode: "none",
      receipt_status: "not_required",
      can_cancel: true,
    };
    let resolveCancel: (value: { data: typeof liveOrder }) => void = () => {};
    get.mockImplementation((url: string) => {
      if (url === "/billing/group-enrollment-options/") {
        return Promise.resolve({ data: groupEnrollmentOptions });
      }
      if (url === "/billing/discounts/") {
        return Promise.resolve({ data: activeDiscounts });
      }

      if (url === "/billing/tariffs/") {
        return Promise.resolve({
          data: {
            items: [
              {
                id: 4,
                name: "Group Plan",
                price: 4500,
                training_type: {
                  id: 5,
                  name: "Group Boxing",
                  slug: "group-boxing",
                  kind: "group",
                  is_active: true,
                },
                trainings_limit: 8,
                duration_days: 30,
                is_active: true,
                payout_timing_hint: "after_payment_confirmation",
                requires_package_owner: false,
              },
            ],
          },
        });
      }

      if (url === "/trainers/me/") {
        return Promise.resolve({ data: { id: 4, first_name: "Current", last_name: "Trainer" } });
      }

      if (url === "/billing/debts/?student_id=7") {
        return Promise.resolve({ data: { items: [] } });
      }

      if (url === "/billing/bank-payment-orders/") {
        return Promise.resolve({ data: { items: [liveOrder] } });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });
    post.mockImplementation((url: string) => {
      if (url === "/billing/bank-payment-orders/41/cancel/") {
        return new Promise((resolve) => {
          resolveCancel = resolve;
        });
      }
      return Promise.reject(new Error(`Unexpected POST URL: ${url}`));
    });

    renderSheet();

    fireEvent.click(await screen.findByRole("button", { name: /Group Plan/ }));
    fireEvent.click(await screen.findByRole("radio", { name: /Tue Thu Group/ }));
    fireEvent.click(screen.getByRole("button", { name: "СБП" }));
    expect(await screen.findByText("Ссылка на оплату")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Отменить" }));
    fireEvent.click(
      within(screen.getByRole("dialog", { name: "Отменить оплату?" })).getByRole(
        "button",
        { name: "Отменить" },
      ),
    );

    expect(await screen.findByRole("button", { name: "Отмена ссылки..." })).toBeDisabled();
    resolveCancel({ data: { ...liveOrder, status: "cancelled" } });
  });

  it("ignores an expired pending online payment link when creating a new link", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/billing/payment-capabilities/") {
        return Promise.resolve({ data: legacyGroupPaymentCapabilities });
      }

      if (url === "/billing/group-enrollment-options/") {
        return Promise.resolve({ data: groupEnrollmentOptions });
      }
      if (url === "/billing/discounts/") {
        return Promise.resolve({ data: activeDiscounts });
      }

      if (url === "/billing/tariffs/") {
        return Promise.resolve({
          data: {
            items: [
              {
                id: 4,
                name: "Group Plan",
                price: 4500,
                training_type: {
                  id: 5,
                  name: "Group Boxing",
                  slug: "group-boxing",
                  kind: "group",
                  is_active: true,
                },
                trainings_limit: 8,
                duration_days: 30,
                is_active: true,
                payout_timing_hint: "after_payment_confirmation",
                requires_package_owner: false,
              },
            ],
          },
        });
      }

      if (url === "/trainers/me/") {
        return Promise.resolve({ data: { id: 4, first_name: "Current", last_name: "Trainer" } });
      }

      if (url === "/billing/debts/?student_id=7") {
        return Promise.resolve({ data: { items: [] } });
      }

      if (url === "/billing/bank-payment-orders/") {
        return Promise.resolve({
          data: {
            items: [
              {
                id: 41,
                tariff_id: 4,
                target_schedule_id: 31,
                target_start_date: "2030-01-08",
                debt_ids: [],
                payment_id: 91,
                subscription_id: 51,
                student_id: 7,
                provider: "mock",
                source: "trainer",
                status: "pending",
                amount_snapshot: "4500.00",
                currency: "RUB",
                purpose_snapshot: "Абонемент Group Plan",
                provider_payment_link_id: "jgr-41-test",
                provider_payment_url: "https://pay.example/jgr-41-test",
        can_share: true,
        can_copy: true,
        can_show_qr: true,
        can_request_refresh: true,
                provider_payment_modes: ["card"],
                provider_status: "CREATED",
                expires_at: "2026-01-01T00:00:00Z",
                paid_at: null,
                receipt_mode: "none",
                receipt_status: "not_required",
              },
            ],
          },
        });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });
    post.mockResolvedValueOnce({
      data: {
        id: 42,
        tariff_id: 4,
        target_schedule_id: 31,
        target_start_date: "2030-01-08",
        debt_ids: [],
        payment_id: 92,
        subscription_id: 52,
        student_id: 7,
        provider: "mock",
        source: "trainer",
        status: "pending",
        amount_snapshot: "4500.00",
        currency: "RUB",
        purpose_snapshot: "Абонемент Group Plan",
        provider_payment_link_id: "jgr-42-test",
        provider_payment_url: "https://pay.example/jgr-42-test",
        can_share: true,
        can_copy: true,
        can_show_qr: true,
        can_request_refresh: true,
        provider_payment_modes: ["card"],
        provider_status: "CREATED",
        expires_at: "2099-06-28T12:00:00Z",
        paid_at: null,
        receipt_mode: "none",
        receipt_status: "not_required",
      },
    });

    renderSheet();

    fireEvent.click(await screen.findByRole("button", { name: /Group Plan/ }));
    fireEvent.click(await screen.findByRole("radio", { name: /Tue Thu Group/ }));
    fireEvent.click(await screen.findByRole("button", { name: "СБП" }));

    expect(screen.queryByText("Ссылка на оплату")).not.toBeInTheDocument();
    const createButton = screen.getByRole("button", { name: "Создать ссылку СБП" });
    expect(createButton).toBeEnabled();
    fireEvent.click(createButton);

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/billing/bank-payment-orders/",
        expect.objectContaining({
          student_id: 7,
          tariff_id: 4,
          debt_ids: [],
          seller_trainer_id: 4,
        }),
      );
    });
    expect(await screen.findByText("Ссылка на оплату")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Открыть предпросмотр/ })).toHaveAttribute(
      "href",
      "https://pay.example/jgr-42-test",
    );
  });

  it("does not show package owner hint for group tariffs", async () => {
    renderSheet();

    fireEvent.click(await screen.findByRole("button", { name: /Group Plan/ }));

    expect(screen.queryByText(/Пакет закрепится за/)).not.toBeInTheDocument();
  });

  it("does not turn a tariff request failure into an empty tariff list", async () => {
    rejectedQueries.add("tariffs");
    renderSheet();

    expect(await screen.findByText("Не удалось загрузить тарифы")).toBeInTheDocument();
    expect(screen.queryByText("Нет доступных тарифов")).not.toBeInTheDocument();

    rejectedQueries.delete("tariffs");
    fireEvent.click(
      screen.getByRole("button", { name: "Повторить загрузку тарифов" }),
    );

    expect(await screen.findByRole("button", { name: /Base/ })).toBeInTheDocument();
  });

  it("blocks manual payment until the student's debts are known", async () => {
    rejectedQueries.add("debts");
    renderSheet();

    fireEvent.click(await screen.findByRole("button", { name: /Base/ }));
    expect(await screen.findByText("Не удалось загрузить долги")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Принять оплату" })).toBeDisabled();

    rejectedQueries.delete("debts");
    fireEvent.click(
      screen.getByRole("button", { name: "Повторить загрузку долгов" }),
    );

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Принять оплату" })).toBeEnabled();
    });
  });

  it("blocks every payment method until trainer identity is known", async () => {
    rejectedQueries.add("trainer");
    renderSheet();

    fireEvent.click(await screen.findByRole("button", { name: /Group Plan/ }));
    fireEvent.click(await screen.findByRole("radio", { name: /Tue Thu Group/ }));
    expect(await screen.findByText("Не удалось подтвердить профиль тренера")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Принять оплату" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "СБП" }));
    expect(screen.getByRole("button", { name: "Создать ссылку СБП" })).toBeDisabled();

    rejectedQueries.delete("trainer");
    fireEvent.click(
      screen.getByRole("button", { name: "Повторить загрузку профиля" }),
    );

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Создать ссылку СБП" })).toBeEnabled();
    });
  });

  it("blocks a new online link until live payment orders are known", async () => {
    rejectedQueries.add("bank-orders");
    renderSheet();

    fireEvent.click(await screen.findByRole("button", { name: /Group Plan/ }));
    fireEvent.click(await screen.findByRole("radio", { name: /Tue Thu Group/ }));
    fireEvent.click(screen.getByRole("button", { name: "СБП" }));

    expect(await screen.findByText("Не удалось проверить активные ссылки")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Создать ссылку СБП" })).toBeDisabled();

    rejectedQueries.delete("bank-orders");
    fireEvent.click(
      screen.getByRole("button", { name: "Повторить проверку ссылок" }),
    );

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Создать ссылку СБП" })).toBeEnabled();
    });
  });

  it("keeps cached debt rows visible but blocks mutation after a failed refetch", async () => {
    const { queryClient } = renderSheet();
    fireEvent.click(await screen.findByRole("button", { name: /Base/ }));
    expect(await screen.findByRole("checkbox", { name: /Долг #55/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Принять оплату" })).toBeEnabled();

    rejectedQueries.add("debts");
    await queryClient.refetchQueries({ queryKey: ["billing", "debts", 7] });

    expect(await screen.findByText("Данные о долгах могли устареть")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /Долг #55/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Принять оплату" })).toBeDisabled();
  });
});
