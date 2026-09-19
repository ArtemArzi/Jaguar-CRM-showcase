import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useBrandingStore } from "@/features/branding/use-branding";
import { useAuthStore } from "@/features/auth/auth-store";
import { registerPrivateQueryClient } from "@/api/private-query-cache";
import {
  PersonalCommercialReceiptCard,
  PersonalCommercialReceiptList,
} from "./personal-commercial-context";
import {
  contextualRetryContextFromReceipt,
  getCommercialCacheScope,
  personalCommercialBankOrderQueryKey,
  personalCommercialContextQueryKey,
  type PersonalCommercialReceipt,
} from "./personal-commercial-context-api";

function testJwt(subject: string) {
  return `header.${btoa(JSON.stringify({ sub: subject }))}.signature`;
}

const { get, post } = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));

vi.mock("@/api/custom-fetch", () => ({ default: { get, post } }));

function receipt(
  overrides: Partial<PersonalCommercialReceipt> = {},
): PersonalCommercialReceipt {
  return {
    kind: "personal_staff_intent",
    booking_id: 71,
    reservation_id: null,
    payment_id: 91,
    subscription_id: 101,
    bank_payment_order_id: null,
    debt_id: null,
    slot_id: 81,
    schedule_id: 111,
    enrollment_id: 112,
    starts_at: "2099-07-07T10:00:00+05:00",
    ends_at: "2099-07-07T11:00:00+05:00",
    training_type_id: 22,
    training_type_name: "Персоналка",
    tariff_id: 44,
    tariff_name: "Разовая персоналка",
    trainer_id: 9,
    trainer_name: "Тренер",
    location_id: 11,
    location_name: "Основной зал",
    amount: "2500.00",
    payment_method: "cash",
    status: "pending",
    provider_payment_url: null,
    allowed_actions: ["review_payment"],
    resource_route: "/api/personal-drop-in-bookings/71/",
    ...overrides,
  };
}

function renderReceipt(value: PersonalCommercialReceipt, onSettleExactDebt = vi.fn()) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <PersonalCommercialReceiptCard receipt={value} onSettleExactDebt={onSettleExactDebt} />
    </QueryClientProvider>,
  );
}

describe("PersonalCommercialReceiptCard", () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
    useBrandingStore.setState({ timeZone: "Asia/Yekaterinburg" });
  });

  it("renders an exact pending manual receipt without claiming lead conversion", () => {
    renderReceipt(receipt());

    expect(screen.getByText("Оплата ожидает подтверждения владельцем")).toBeInTheDocument();
    expect(screen.getByText("Персоналка")).toBeInTheDocument();
    expect(screen.getAllByText("Тренер")).toHaveLength(2);
    expect(screen.getByText("Основной зал")).toBeInTheDocument();
    expect(screen.queryByText(/стал учеником/i)).not.toBeInTheDocument();
  });

  it("renders pay-at-visit as payment at attendance, never a debt before check-in", () => {
    renderReceipt(receipt({ payment_method: "pay_at_visit", payment_id: null, status: "scheduled" }));

    expect(screen.getByText("К оплате при посещении")).toBeInTheDocument();
    expect(screen.queryByText(/долг/i)).not.toBeInTheDocument();
  });

  it("shows the frozen debt after a pay-at-visit attendance instead of a pre-attendance label", () => {
    renderReceipt(receipt({ payment_method: "pay_at_visit", payment_id: null, status: "debt_open" }));

    expect(screen.getByText("Долг ожидает оплаты")).toBeInTheDocument();
    expect(screen.queryByText("К оплате при посещении")).not.toBeInTheDocument();
  });

  it.each([
    ["debt_open", "cash", "Долг ожидает оплаты"],
    ["pending_payment", "cash", "Оплата ожидает подтверждения владельцем"],
    ["approved", "cash", "Оплата подтверждена"],
    ["manual_review", "cash", "Оплата на ручной проверке"],
    ["rejected", "cash", "Оплата отклонена"],
    ["failed", "cash", "Оплата не прошла"],
    ["cancelled", "cash", "Запись отменена"],
    ["expired", "cash", "Срок оплаты истёк"],
    ["confirmed", "cash", "Оплата подтверждена"],
  ] as const)("projects %s into Russian receipt text", (status, paymentMethod, expected) => {
    renderReceipt(receipt({ status, payment_method: paymentMethod }));

    expect(screen.getByText(expected)).toBeInTheDocument();
  });

  it("uses the club timezone for the exact session time rather than the timestamp offset", () => {
    useBrandingStore.setState({ timeZone: "UTC" });
    renderReceipt(receipt());

    expect(screen.getByText(/07\.07\.2099.*05:00.*06:00/)).toBeInTheDocument();
  });

  it("uses the configured non-Moscow club timezone for receipt session time", () => {
    useBrandingStore.setState({ timeZone: "Asia/Vladivostok" });
    renderReceipt(receipt());

    expect(screen.getByText(/07\.07\.2099.*15:00.*16:00/)).toBeInTheDocument();
  });

  it.each([
    ["cancelled", "Запись отменена"],
    ["no_show", "Клиент не пришёл"],
    ["rejected", "Оплата отклонена"],
  ] as const)("never renders terminal %s pay-at-visit receipt as due", (status, expected) => {
    renderReceipt(receipt({ payment_method: "pay_at_visit", status }));

    expect(screen.getByText(expected)).toBeInTheDocument();
    expect(screen.queryByText("К оплате при посещении")).not.toBeInTheDocument();
  });

  it("never leaks an unknown receipt status or a resource route into an action", () => {
    renderReceipt(
      receipt({
        status: "server_only_status",
        allowed_actions: [],
        resource_route: "/trainer/students/999",
      }),
    );

    expect(screen.getByText("Статус уточняется")).toBeInTheDocument();
    expect(screen.queryByText("server_only_status")).not.toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Принять оплату" })).not.toBeInTheDocument();
  });

  it("normalizes the server entitlement receipt into Russian coverage and booking states", () => {
    renderReceipt(
      receipt({
        amount: "",
        payment_method: "entitlement",
        status: "booked",
        payment_id: null,
        subscription_id: null,
      }),
    );

    expect(screen.getAllByText("По абонементу")).toHaveLength(2);
    expect(screen.getByText("Записан")).toBeInTheDocument();
    expect(screen.queryByText(/0\s*₽/)).not.toBeInTheDocument();
  });

  it("loads the existing safe SBP panel from the authoritative bank-order id", async () => {
    get.mockResolvedValue({
      data: {
        id: 92,
        subscription_id: 101,
        student_id: 7,
        tariff_id: 44,
        debt_ids: [],
        status: "pending",
        amount_snapshot: "2500.00",
        currency: "RUB",
        purpose_snapshot: "Персоналка",
        provider_payment_url: "https://bank.example/pay/92",
        expires_at: "2099-07-07T12:00:00Z",
        can_pay: false,
        can_copy: true,
        can_share: true,
        can_show_qr: true,
      },
    });
    renderReceipt(
      receipt({
        payment_method: "sbp",
        bank_payment_order_id: 92,
        allowed_actions: ["open_bank_payment_order"],
      }),
    );

    expect(await screen.findByRole("region", { name: "Ссылка на оплату СБП" })).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith("/billing/bank-payment-orders/92/");
  });

  it("does not open a bank order from a receipt without the server-declared action", () => {
    renderReceipt(
      receipt({
        payment_method: "sbp",
        bank_payment_order_id: 92,
        allowed_actions: ["view_booking"],
      }),
    );

    expect(get).not.toHaveBeenCalledWith("/billing/bank-payment-orders/92/");
    expect(screen.queryByRole("region", { name: "Ссылка на оплату СБП" })).not.toBeInTheDocument();
  });

  it("renders correction controls only when their server actions are declared", () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <PersonalCommercialReceiptCard
          studentId={7}
          receipt={receipt({
            payment_method: "sbp",
            reservation_id: 17,
            bank_payment_order_id: 92,
            allowed_actions: ["cancel_if_safe", "replace_payment_method"],
          })}
        />
      </QueryClientProvider>,
    );

    expect(screen.getByRole("button", { name: "Отменить попытку" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Заменить способ оплаты" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Обновить и сверить оплату" })).not.toBeInTheDocument();
    expect(screen.queryByText(/Нужна проверка владельца/)).not.toBeInTheDocument();
  });

  it("keeps reconciliation and owner review honest without offering replacement", () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <PersonalCommercialReceiptCard
          studentId={7}
          receipt={receipt({
            payment_method: "sbp",
            reservation_id: 17,
            bank_payment_order_id: 92,
            allowed_actions: ["refresh_or_reconcile", "owner_review"],
          })}
        />
      </QueryClientProvider>,
    );

    expect(screen.getByRole("button", { name: "Обновить и сверить оплату" })).toBeInTheDocument();
    expect(screen.getByText(/Нужна проверка владельца/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Отменить попытку" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Заменить способ оплаты" })).not.toBeInTheDocument();
  });

  it("posts only the immutable correction command and invalidates through the owner callback", async () => {
    post.mockResolvedValue({ data: receipt({ booking_id: 72, reservation_id: null, payment_id: 93 }) });
    const onReceiptChanged = vi.fn();
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <PersonalCommercialReceiptCard
          studentId={7}
          onReceiptChanged={onReceiptChanged}
          receipt={receipt({
            payment_method: "sbp",
            reservation_id: 17,
            bank_payment_order_id: 92,
            allowed_actions: ["replace_payment_method"],
          })}
        />
      </QueryClientProvider>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Заменить способ оплаты" }));
    fireEvent.click(screen.getByRole("button", { name: "Наличные" }));

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    const [path, payload] = post.mock.calls[0] as [string, Record<string, unknown>];
    expect(path).toBe("/students/7/personal-commercial-attempts/replace-payment-method/");
    expect(payload).toMatchObject({
      reservation_id: 17,
      replacement_payment_method: "cash",
      reason: "staff_payment_method_correction",
    });
    expect(payload).not.toHaveProperty("amount");
    expect(payload).not.toHaveProperty("discount_ids");
    await waitFor(() => expect(onReceiptChanged).toHaveBeenCalledTimes(1));
  });

  it("reuses the same correction idempotency key after a lost or failed response", async () => {
    post
      .mockRejectedValueOnce(new Error("response lost"))
      .mockResolvedValueOnce({ data: receipt({ booking_id: 72, reservation_id: null }) });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <PersonalCommercialReceiptCard
          studentId={7}
          receipt={receipt({
            payment_method: "sbp",
            reservation_id: 17,
            bank_payment_order_id: 92,
            allowed_actions: ["replace_payment_method"],
          })}
        />
      </QueryClientProvider>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Заменить способ оплаты" }));
    fireEvent.click(screen.getByRole("button", { name: "Наличные" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    await screen.findByText("Не удалось заменить способ оплаты. Исходная попытка сохранена для сверки.");
    fireEvent.click(screen.getByRole("button", { name: "Наличные" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));

    expect(post.mock.calls[1][1].idempotency_key).toBe(post.mock.calls[0][1].idempotency_key);
  });

  it("exposes only the server-declared exact debt-settlement action", () => {
    const onSettle = vi.fn();
    renderReceipt(
      receipt({
        debt_id: 73,
        payment_method: "pay_at_visit",
        allowed_actions: ["settle_exact_debt"],
      }),
      onSettle,
    );

    expect(screen.getByRole("button", { name: "Принять уже полученную оплату" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Подтвердить" })).not.toBeInTheDocument();
  });

  it("keeps a server-declared exact debt action when the old tariff is inactive or absent", () => {
    renderReceipt(
      receipt({
        debt_id: 73,
        tariff_id: null,
        tariff_name: null,
        allowed_actions: ["settle_exact_debt"],
      }),
    );

    expect(screen.getByRole("button", { name: "Принять уже полученную оплату" })).toBeInTheDocument();
  });

  it("puts a live attempt before the latest terminal receipt without dropping either on reload", () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <PersonalCommercialReceiptList
          attempts={[
            receipt({ payment_id: 1, status: "failed" }),
            receipt({ payment_id: 2, status: "pending_payment" }),
          ]}
        />
      </QueryClientProvider>,
    );

    const receipts = screen.getAllByRole("region", { name: "Коммерческий контекст персоналки" });
    expect(receipts).toHaveLength(2);
    expect(receipts[0]).toHaveTextContent("Оплата ожидает подтверждения владельцем");
    expect(receipts[1]).toHaveTextContent("Оплата не прошла");
    expect(screen.getByText("История попыток (1)").closest("details")).not.toBeNull();
  });

  it("keeps a terminal attempt active while owner review is still authorized", () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <PersonalCommercialReceiptList
          attempts={[receipt({ status: "failed", allowed_actions: ["owner_review"] })]}
        />
      </QueryClientProvider>,
    );

    expect(screen.getByText(/Нужна проверка владельца/)).toBeInTheDocument();
    expect(screen.queryByText(/История попыток/)).not.toBeInTheDocument();
  });

  it("orders a live attempt before an approved bank-order terminal receipt", () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <PersonalCommercialReceiptList
          attempts={[
            receipt({ kind: "renewal", payment_id: 1, payment_method: "sbp", status: "approved" }),
            receipt({ kind: "renewal", payment_id: 2, payment_method: "sbp", status: "pending" }),
          ]}
        />
      </QueryClientProvider>,
    );

    const receipts = screen.getAllByRole("region", { name: "Коммерческий контекст" });
    expect(receipts[0]).toHaveTextContent("Ожидает оплаты через СБП");
    expect(receipts[1]).toHaveTextContent("Оплата подтверждена");
  });

  it("retains a live entitlement and a latest terminal drop-in receipt together", () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <PersonalCommercialReceiptList
          attempts={[
            receipt({
              booking_id: 71,
              payment_id: 1,
              payment_method: "cash",
              status: "rejected",
            }),
            receipt({
              booking_id: null,
              payment_id: null,
              subscription_id: 101,
              payment_method: "entitlement",
              amount: "",
              status: "booked",
            }),
          ]}
        />
      </QueryClientProvider>,
    );

    const receipts = screen.getAllByRole("region", { name: "Коммерческий контекст персоналки" });
    expect(receipts).toHaveLength(2);
    expect(receipts[0]).toHaveTextContent("По абонементу");
    expect(receipts[0]).toHaveTextContent("Записан");
    expect(receipts[1]).toHaveTextContent("Оплата отклонена");
  });

  it("presents persistent group and renewal attempts from server-authorized context only", () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <PersonalCommercialReceiptList
          attempts={[
            receipt({
              kind: "group_sale",
              payment_id: 111,
              training_type_name: "",
              group_name: "Группа 7–9 лет",
              start_date: "2099-07-10",
              payment_method: "cash",
              status: "pending",
              allowed_actions: [],
            }),
            receipt({
              kind: "renewal",
              payment_id: 112,
              renewed_from_subscription_id: 44,
              renewed_from_subscription_name: "Летний абонемент",
              payment_method: "sbp",
              status: "failed",
              allowed_actions: ["retry_bank_payment"],
            }),
          ]}
        />
      </QueryClientProvider>,
    );

    expect(screen.getByText("Групповое обучение")).toBeInTheDocument();
    expect(screen.getByText("Группа 7–9 лет")).toBeInTheDocument();
    expect(screen.getByText("Продление абонемента")).toBeInTheDocument();
    expect(screen.getByText("Абонемент #44")).toBeInTheDocument();
    expect(screen.getByText("Оплата не прошла")).toBeInTheDocument();
    expect(screen.queryByText(/оплата подтверждена автоматически/i)).not.toBeInTheDocument();
  });
});

describe("commercial context cache scope", () => {
  it("does not reuse a previous staff subject's cached receipt after an auth switch", () => {
    const queryClient = new QueryClient();
    const firstStaffKey = personalCommercialContextQueryKey(
      12,
      getCommercialCacheScope(4, testJwt("staff-a")),
    );
    const nextStaffKey = personalCommercialContextQueryKey(
      12,
      getCommercialCacheScope(4, testJwt("staff-b")),
    );
    queryClient.setQueryData(firstStaffKey, { attempts: [{ payment_id: 91 }] });

    expect(queryClient.getQueryData(firstStaffKey)).toEqual({ attempts: [{ payment_id: 91 }] });
    expect(queryClient.getQueryData(nextStaffKey)).toBeUndefined();
    expect(nextStaffKey).not.toEqual(firstStaffKey);
  });

  it("does not reuse an actor's nested provider-link data after an auth switch", () => {
    const queryClient = new QueryClient();
    const firstStaffKey = personalCommercialBankOrderQueryKey(
      92,
      getCommercialCacheScope(4, testJwt("staff-a")),
    );
    const nextStaffKey = personalCommercialBankOrderQueryKey(
      92,
      getCommercialCacheScope(4, testJwt("staff-b")),
    );
    queryClient.setQueryData(firstStaffKey, { provider_payment_url: "https://bank.example/first" });

    expect(queryClient.getQueryData(nextStaffKey)).toBeUndefined();
  });

  it("removes owner-only receipt and provider-link data synchronously on a trainer downgrade", () => {
    const queryClient = new QueryClient();
    const unregister = registerPrivateQueryClient(queryClient);
    const token = testJwt("same-staff");
    const ownerScope = getCommercialCacheScope(4, token, "owner");
    const receiptKey = personalCommercialContextQueryKey(12, ownerScope);
    const providerLinkKey = personalCommercialBankOrderQueryKey(92, ownerScope);
    queryClient.setQueryData(receiptKey, { attempts: [{ payment_id: 91 }] });
    queryClient.setQueryData(providerLinkKey, { provider_payment_url: "https://bank.example/owner" });

    useAuthStore.setState({
      accessToken: token,
      role: "owner",
      clubId: 4,
      isAuthenticated: true,
    });
    useAuthStore.getState().setUserInfo("trainer", 4);

    expect(queryClient.getQueryData(receiptKey)).toBeUndefined();
    expect(queryClient.getQueryData(providerLinkKey)).toBeUndefined();
    unregister();
  });
});

describe("contextual bank-payment retries", () => {
  it("reconstructs only complete exact group and renewal contexts", () => {
    const groupReceipt = receipt({
      kind: "group_sale",
      payment_method: "sbp",
      status: "failed",
      allowed_actions: ["retry_bank_payment"],
      tariff_id: 44,
      tariff_name: "Группа",
      training_group_id: 25,
      schedule_id: 31,
      target_start_date: "2099-07-10",
      group_name: "Дети",
    });
    const renewalReceipt = receipt({
      kind: "renewal",
      payment_method: "sbp",
      status: "failed",
      allowed_actions: ["retry_bank_payment"],
      renewed_from_subscription_id: 55,
      renewed_from_subscription_name: "Base A",
      amount: "5000.00",
      renewal_target_tariff_id: 9,
      renewal_target_tariff_name: "Base B",
      renewal_target_price: "6500.00",
    });

    expect(
      contextualRetryContextFromReceipt({
        receipt: groupReceipt,
        studentId: 12,
        studentName: "Клиент",
      }),
    ).toMatchObject({ kind: "group_sale", trainingGroupId: 25, scheduleId: 31 });
    expect(
      contextualRetryContextFromReceipt({
        receipt: renewalReceipt,
        studentId: 12,
        studentName: "Клиент",
      }),
    ).toMatchObject({
      kind: "subscription_renewal",
      renewedFromSubscriptionId: 55,
      renewedFromSubscriptionName: "Base A",
      renewalTargetTariffId: 9,
      renewalTargetTariffName: "Base B",
      renewalTargetPrice: "6500.00",
    });
    const unavailableRenewal = contextualRetryContextFromReceipt({
      receipt: receipt({
        kind: "renewal",
        payment_method: "sbp",
        status: "cancelled",
        allowed_actions: ["retry_bank_payment"],
        renewed_from_subscription_id: 55,
        renewed_from_subscription_name: "Base A",
        amount: "5000.00",
      }),
      studentId: 12,
      studentName: "Клиент",
    });
    expect(unavailableRenewal).toMatchObject({
      kind: "subscription_renewal",
      renewedFromSubscriptionName: "Base A",
    });
    expect(unavailableRenewal).not.toMatchObject({ renewalTargetPrice: "5000.00" });
    expect(
      contextualRetryContextFromReceipt({
        receipt: receipt({ ...groupReceipt, schedule_id: null }),
        studentId: 12,
        studentName: "Клиент",
      }),
    ).toBeNull();
  });

  it("routes terminal group and renewal SBP retries only through the contextual handler", () => {
    const onPersonalRetry = vi.fn();
    const onContextualRetry = vi.fn();
    const groupReceipt = receipt({
      kind: "group_sale",
      payment_method: "sbp",
      status: "failed",
      allowed_actions: ["retry_bank_payment"],
      tariff_id: 44,
      tariff_name: "Группа",
      training_group_id: 25,
      schedule_id: 31,
      target_start_date: "2099-07-10",
      group_name: "Дети",
    });
    const renewalReceipt = receipt({
      kind: "renewal",
      payment_method: "sbp",
      status: "failed",
      allowed_actions: ["retry_bank_payment"],
      renewed_from_subscription_id: 55,
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <PersonalCommercialReceiptList
          attempts={[groupReceipt, renewalReceipt]}
          onRetryPersonalBankPayment={onPersonalRetry}
          onRetryContextualBankPayment={onContextualRetry}
          canRetryContextualBankPayment={(value) =>
            contextualRetryContextFromReceipt({
              receipt: value,
              studentId: 12,
              studentName: "Клиент",
            }) !== null
          }
        />
      </QueryClientProvider>,
    );

    for (const button of screen.getAllByRole("button", { name: "Повторить оплату СБП" })) {
      fireEvent.click(button);
    }

    expect(onContextualRetry).toHaveBeenCalledTimes(2);
    expect(onPersonalRetry).not.toHaveBeenCalled();
  });

  it("does not offer a contextual retry when an exact group reference is missing", () => {
    const onContextualRetry = vi.fn();
    const incompleteGroupReceipt = receipt({
      kind: "group_sale",
      payment_method: "sbp",
      status: "failed",
      allowed_actions: ["retry_bank_payment"],
      tariff_id: 44,
      tariff_name: "Группа",
      training_group_id: 25,
      schedule_id: null,
      target_start_date: "2099-07-10",
      group_name: "Дети",
    });
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <PersonalCommercialReceiptList
          attempts={[incompleteGroupReceipt]}
          onRetryContextualBankPayment={onContextualRetry}
          canRetryContextualBankPayment={(value) =>
            contextualRetryContextFromReceipt({
              receipt: value,
              studentId: 12,
              studentName: "Клиент",
            }) !== null
          }
        />
      </QueryClientProvider>,
    );

    expect(screen.queryByRole("button", { name: "Повторить оплату СБП" })).not.toBeInTheDocument();
  });
});
