import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "@/features/auth/auth-store";
import {
  ENABLED_SBP_PAYMENT_CAPABILITIES,
  getPaymentCapabilitiesQueryKey,
} from "@/api/payment-capabilities";
import {
  ContextualCommercialSheet,
  ContextualGroupSalePickerSheet,
} from "./contextual-commercial-sheet";
import { getOrCreateContextualCommercialCommandKey } from "@/api/contextual-commercial-command-key";

const { get, post } = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock("@/api/custom-fetch", () => ({ default: { get, post } }));

function renderSheet(
  context: Parameters<typeof ContextualCommercialSheet>[0]["context"],
  paymentCapabilities: unknown = ENABLED_SBP_PAYMENT_CAPABILITIES,
  props: Partial<Parameters<typeof ContextualCommercialSheet>[0]> = {},
) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  if (paymentCapabilities !== undefined) {
    queryClient.setQueryData(getPaymentCapabilitiesQueryKey(), paymentCapabilities);
  }
  return render(
    <QueryClientProvider client={queryClient}>
      <ContextualCommercialSheet open onOpenChange={vi.fn()} context={context} {...props} />
    </QueryClientProvider>,
  );
}

describe("ContextualCommercialSheet", () => {
  beforeEach(() => {
    localStorage.clear();
    get.mockReset();
    post.mockReset();
    useAuthStore.setState({
      accessToken: "header.eyJzdWIiOiJ0cmFpbmVyLTQifQ.signature",
      clubId: 4,
      role: "trainer",
      isAuthenticated: true,
    });
  });

  it("accepts a source-free new admission but fails closed for a marked group renewal without its exact source", async () => {
    let action: "new_admission" | "renewal" = "new_admission";
    get.mockImplementation((url: string) => {
      if (url === "/billing/tariffs/") {
        return Promise.resolve({
          data: [{ id: 7, name: "Месяц", is_active: true, training_type: { kind: "group" } }],
        });
      }
      if (url === "/billing/group-enrollment-options/") {
        return Promise.resolve({
          data: [
            {
              training_group_id: 25,
              schedule_id: 31,
              group_name: "Дети",
              is_canonical_group_card: true,
              next_occurrence_date: "2099-02-01",
              group_membership_action: action,
              renewed_from_subscription_id: null,
            },
          ],
        });
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });
    const renderPicker = () => {
      const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      return render(
        <QueryClientProvider client={queryClient}>
          <ContextualGroupSalePickerSheet
            open
            onOpenChange={vi.fn()}
            studentId={12}
            studentName="Клиент"
          />
        </QueryClientProvider>,
      );
    };

    const newAdmission = renderPicker();
    expect(await screen.findByRole("button", { name: "Дети" })).toBeInTheDocument();
    newAdmission.unmount();

    action = "renewal";
    renderPicker();
    expect(await screen.findByText(/Нет доступной группы с точной датой старта/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Дети" })).not.toBeInTheDocument();
  });

  it("defaults the nearest exact start, allows an alternate, then requests its signed review", async () => {
    get.mockImplementation((url: string, config?: { params?: Record<string, unknown> }) => {
      if (url === "/billing/tariffs/") {
        return Promise.resolve({
          data: [
            {
              id: 7,
              name: "Месяц",
              price: "5500.00",
              trainings_limit: 8,
              duration_days: 30,
              is_active: true,
              training_type: { kind: "group" },
            },
          ],
        });
      }
      if (url === "/billing/group-enrollment-options/") {
        return Promise.resolve({
          data: [
            {
              training_group_id: 25,
              schedule_id: 31,
              group_name: "Дети",
              trainer_name: "Тренер Группы",
              location_name: "Основной зал",
              next_occurrence_date: "2099-02-01",
              slot_schedule_ids: [31, 32],
              upcoming_occurrences: [
                { schedule_id: 31, date: "2099-02-01" },
                { schedule_id: 32, date: "2099-02-03" },
              ],
              group_membership_action: "new_admission",
              is_canonical_group_card: true,
              is_latest_trial_group: true,
            },
          ],
        });
      }
      if (url === "/billing/group-sale-offers/preview/") {
        return Promise.resolve({
          data: {
            protocol_version: "v2",
            student: { id: 12, display_name: "Клиент" },
            tariff: { id: 7, name: "Месяц", price: "5500.00", trainings_limit: 8, duration_days: 30 },
            group: {
              id: 25,
              name: "Дети",
              responsible_trainer_name: "Тренер Группы",
              location_name: "Основной зал",
              weekly_schedule: [],
            },
            selected_occurrence: {
              schedule_id: config?.params?.target_schedule_id,
              date: config?.params?.target_start_date,
            },
            expected_action: "new_admission",
            buyer_email_required: false,
            offer_digest: "v2.signed-offer",
          },
        });
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <ContextualGroupSalePickerSheet
          open
          onOpenChange={vi.fn()}
          studentId={12}
          studentName="Клиент"
        />
      </QueryClientProvider>,
    );

    expect(await screen.findByText("Оформляем: Клиент")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /Месяц.*5\s500/ })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /Дети.*Рекомендовано/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Дети.*2099-02-01/ })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Дети.*Рекомендовано/ }));
    const nearestStart = await screen.findByRole("button", { name: "2099-02-01" });
    expect(nearestStart).toHaveAttribute("aria-pressed", "true");
    expect(get).not.toHaveBeenCalledWith(
      "/billing/group-sale-offers/preview/",
      expect.anything(),
    );

    fireEvent.click(screen.getByRole("button", { name: "2099-02-03" }));
    expect(screen.getByRole("button", { name: "2099-02-03" })).toHaveAttribute("aria-pressed", "true");
    expect(get).not.toHaveBeenCalledWith(
      "/billing/group-sale-offers/preview/",
      expect.anything(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Проверить условия" }));
    await waitFor(() => {
      expect(get).toHaveBeenCalledWith("/billing/group-sale-offers/preview/", {
        params: {
          student_id: 12,
          tariff_id: 7,
          target_training_group_id: 25,
          target_schedule_id: 32,
          target_start_date: "2099-02-03",
        },
      });
    });

    expect(await screen.findByText("Проверка перед оформлением")).toBeInTheDocument();
    expect(await screen.findByText("5 500 ₽")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Зафиксировать наличные 5\s500 ₽ и оформить Клиент/ })).toBeInTheDocument();
  });

  it("keeps a large compatible group list bounded until the trainer opens other groups", async () => {
    const options = Array.from({ length: 7 }, (_, index) => ({
      training_group_id: index + 1,
      schedule_id: 31 + index,
      group_name: `Группа ${index + 1}`,
      next_occurrence_date: `2099-02-0${index + 1}`,
      upcoming_occurrences: [
        { schedule_id: 31 + index, date: `2099-02-0${index + 1}` },
      ],
      group_membership_action: "new_admission",
      is_canonical_group_card: true,
      is_latest_trial_group: index === 0,
    }));
    get.mockImplementation((url: string) => {
      if (url === "/billing/tariffs/") {
        return Promise.resolve({
          data: [{ id: 7, name: "Месяц", is_active: true, training_type: { kind: "group" } }],
        });
      }
      if (url === "/billing/group-enrollment-options/") return Promise.resolve({ data: options });
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <ContextualGroupSalePickerSheet
          open
          onOpenChange={vi.fn()}
          studentId={12}
          studentName="Клиент"
        />
      </QueryClientProvider>,
    );

    expect(await screen.findByRole("button", { name: /Группа 1.*Рекомендовано/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Группа 4" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Группа 5" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Другие группы (3)" }));

    const seventhGroup = await screen.findByRole("button", { name: "Группа 7" });
    fireEvent.click(seventhGroup);
    expect(seventhGroup).toHaveAttribute("aria-pressed", "true");
    expect(await screen.findByRole("button", { name: "2099-02-07" })).toBeInTheDocument();
  });

  it("surfaces canonical renewal as a typed conflict and hands off to student renewal", async () => {
    const onRenewalConflict = vi.fn();
    get.mockImplementation((url: string) => {
      if (url === "/billing/tariffs/") {
        return Promise.resolve({
          data: [{ id: 7, name: "Месяц", is_active: true, training_type: { kind: "group" } }],
        });
      }
      if (url === "/billing/group-enrollment-options/") {
        return Promise.resolve({
          data: [
            {
              training_group_id: 25,
              schedule_id: 31,
              group_name: "Дети",
              next_occurrence_date: "2099-02-01",
              group_membership_action: "renewal",
              renewed_from_subscription_id: 58,
              is_canonical_group_card: true,
            },
          ],
        });
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <ContextualGroupSalePickerSheet
          open
          onOpenChange={vi.fn()}
          studentId={12}
          studentName="Клиент"
          onRenewalConflict={onRenewalConflict}
        />
      </QueryClientProvider>,
    );

    expect(await screen.findByText("Для одной из групп уже есть действующее членство.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Дети" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Открыть продление ученика" }));
    expect(onRenewalConflict).toHaveBeenCalledTimes(1);
  });

  it("sends an exact new-admission group manual payload with no client amount or debt authority", async () => {
    post.mockResolvedValue({ data: { kind: "group_sale", payment_id: 91 } });
    renderSheet({
      kind: "group_sale",
      studentId: 12,
      studentName: "Клиент",
      tariffId: 7,
      tariffName: "Месяц",
      trainingGroupId: 25,
      scheduleId: 31,
      startDate: "2099-02-01",
      groupName: "Дети",
    });

    fireEvent.click(screen.getByRole("button", { name: "Зафиксировать оплату" }));
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(post).toHaveBeenCalledWith(
      "/billing/payments/",
      expect.objectContaining({
        student_id: 12,
        tariff_id: 7,
        payment_method: "cash",
        target_training_group_id: 25,
        target_schedule_id: 31,
        target_start_date: "2099-02-01",
        debt_ids: [],
      }),
    );
    expect(post.mock.calls[0]?.[1]).not.toHaveProperty("amount");
    expect(post.mock.calls[0]?.[1]).toHaveProperty("idempotency_key");
    expect(post.mock.calls[0]?.[1]).not.toHaveProperty("renewed_from_subscription_id");
  });

  it("creates a group SBP order with only the exact server-selected target", async () => {
    post.mockResolvedValue({ data: { id: 92, bank_payment_order_id: 92 } });
    renderSheet({
      kind: "group_sale",
      studentId: 12,
      studentName: "Клиент",
      tariffId: 7,
      tariffName: "Месяц",
      trainingGroupId: 25,
      scheduleId: 31,
      startDate: "2099-02-01",
      groupName: "Дети",
    });

    fireEvent.click(screen.getByRole("button", { name: "СБП" }));
    fireEvent.click(screen.getByRole("button", { name: "Создать ссылку СБП" }));
    await waitFor(() => expect(post).toHaveBeenCalled());

    expect(post).toHaveBeenCalledWith(
      "/billing/bank-payment-orders/",
      expect.objectContaining({
        student_id: 12,
        tariff_id: 7,
        target_training_group_id: 25,
        target_schedule_id: 31,
        target_start_date: "2099-02-01",
        idempotency_key: expect.any(String),
      }),
    );
    const payload = post.mock.calls[0]?.[1] as Record<string, unknown>;
    expect(payload).not.toHaveProperty("payment_method");
    expect(payload).not.toHaveProperty("amount");
    expect(payload).not.toHaveProperty("renewed_from_subscription_id");
  });

  it("submits a signed v2 group manual command only to its strict route", async () => {
    post.mockResolvedValue({
      data: {
        payment_id: 91,
        subscription_id: 101,
        workspace_state: "student",
        finance_state: "pending_manual",
      },
    });
    renderSheet({
      kind: "group_sale",
      protocolVersion: "v2",
      offerDigest: "v2.signed-offer",
      amount: "5500.00",
      slotScheduleIds: [31, 32],
      studentId: 12,
      studentName: "Клиент",
      tariffId: 7,
      tariffName: "Месяц",
      trainingGroupId: 25,
      scheduleId: 31,
      startDate: "2099-02-01",
      groupName: "Дети",
    });

    fireEvent.click(
      screen.getByRole("button", { name: /Зафиксировать наличные 5\s500 ₽ и оформить Клиент/ }),
    );
    await waitFor(() => expect(post).toHaveBeenCalled());

    expect(post).toHaveBeenCalledWith(
      "/billing/v2/group-sales/manual/",
      expect.objectContaining({
        protocol_version: "v2",
        student_id: 12,
        tariff_id: 7,
        payment_method: "cash",
        target_training_group_id: 25,
        target_schedule_id: 31,
        target_start_date: "2099-02-01",
        expected_offer_digest: "v2.signed-offer",
        idempotency_key: expect.any(String),
      }),
    );
    const payload = post.mock.calls[0]?.[1] as Record<string, unknown>;
    expect(payload).not.toHaveProperty("amount");
    expect(payload).not.toHaveProperty("discount_ids");
    expect(payload).not.toHaveProperty("debt_ids");
  });

  it("drops a stale signed-offer command key and returns selection to the server", async () => {
    const onOfferChanged = vi.fn();
    post.mockRejectedValue({
      response: { data: { code: "group_offer_changed", detail: "Offer changed" } },
    });
    const context = {
      kind: "group_sale" as const,
      protocolVersion: "v2" as const,
      offerDigest: "v2.stale-offer",
      amount: "5500.00",
      studentId: 12,
      studentName: "Клиент",
      tariffId: 7,
      tariffName: "Месяц",
      trainingGroupId: 25,
      scheduleId: 31,
      startDate: "2099-02-01",
      groupName: "Дети",
    };
    renderSheet(context, ENABLED_SBP_PAYMENT_CAPABILITIES, { onOfferChanged });

    fireEvent.click(
      screen.getByRole("button", { name: /Зафиксировать наличные 5\s500 ₽ и оформить Клиент/ }),
    );
    await waitFor(() => expect(onOfferChanged).toHaveBeenCalledTimes(1));
    const staleKey = (post.mock.calls[0]?.[1] as Record<string, unknown>).idempotency_key;
    expect(staleKey).toEqual(expect.any(String));

    const replacementKey = getOrCreateContextualCommercialCommandKey({
      clubId: 4,
      actorSubject: "trainer-4",
      audience: "staff",
      kind: "group_sale",
      protocolVersion: "v2",
      studentId: 12,
      paymentMethod: "cash",
      tariffId: 7,
      trainingGroupId: 25,
      scheduleId: 31,
      startDate: "2099-02-01",
      offerDigest: "v2.stale-offer",
    });
    expect(replacementKey).not.toBe(staleKey);
  });

  it("requires a fiscal email before creating a signed v2 SBP order", async () => {
    post.mockResolvedValue({
      data: {
        payment_id: 91,
        subscription_id: 101,
        bank_payment_order_id: 102,
        workspace_state: "lead",
        finance_state: "provider_pending",
      },
    });
    renderSheet({
      kind: "group_sale",
      protocolVersion: "v2",
      offerDigest: "v2.signed-fiscal-offer",
      amount: "5500.00",
      buyerEmailRequired: true,
      studentId: 12,
      studentName: "Клиент",
      tariffId: 7,
      tariffName: "Месяц",
      trainingGroupId: 25,
      scheduleId: 31,
      startDate: "2099-02-01",
      groupName: "Дети",
    });

    fireEvent.click(screen.getByRole("button", { name: "СБП" }));
    fireEvent.click(
      screen.getByRole("button", { name: /Создать ссылку СБП на 5\s500 ₽ для Клиент/ }),
    );
    expect(await screen.findByText("Укажите email для фискального чека.")).toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("Email для чека"), {
      target: { value: "buyer@example.test" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: /Создать ссылку СБП на 5\s500 ₽ для Клиент/ }),
    );
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(post).toHaveBeenCalledWith(
      "/billing/v2/group-sales/bank-orders/",
      expect.objectContaining({
        protocol_version: "v2",
        expected_offer_digest: "v2.signed-fiscal-offer",
        buyer_email: "buyer@example.test",
      }),
    );
  });

  it.each([
    ["cash", "/billing/payments/", "Зафиксировать оплату"],
    ["sbp", "/billing/bank-payment-orders/", "Создать ссылку СБП"],
  ] as const)(
    "carries the exact renewal source through group %s without client commercial authority",
    async (paymentMethod, endpoint, submitLabel) => {
      post.mockResolvedValue({ data: { payment_id: 91, bank_payment_order_id: 92 } });
      renderSheet({
        kind: "group_sale",
        studentId: 12,
        studentName: "Клиент",
        tariffId: 7,
        tariffName: "Месяц",
        trainingGroupId: 25,
        scheduleId: 31,
        startDate: "2099-02-01",
        groupName: "Дети",
        renewedFromSubscriptionId: 58,
      });

      if (paymentMethod === "sbp") fireEvent.click(screen.getByRole("button", { name: "СБП" }));
      fireEvent.click(screen.getByRole("button", { name: submitLabel }));
      await waitFor(() => expect(post).toHaveBeenCalled());

      expect(post).toHaveBeenCalledWith(
        endpoint,
        expect.objectContaining({
          student_id: 12,
          tariff_id: 7,
          target_training_group_id: 25,
          target_schedule_id: 31,
          target_start_date: "2099-02-01",
          renewed_from_subscription_id: 58,
          idempotency_key: expect.any(String),
        }),
      );
      const payload = post.mock.calls[0]?.[1] as Record<string, unknown>;
      expect(payload).not.toHaveProperty("amount");
      if (paymentMethod === "sbp") {
        expect(payload).not.toHaveProperty("payment_method");
        expect(payload).toHaveProperty("debt_ids", []);
      } else {
        expect(payload).toHaveProperty("payment_method", "cash");
        expect(payload).toHaveProperty("debt_ids", []);
      }
    },
  );

  it("sends the exact renewal source to the typed manual route", async () => {
    post.mockResolvedValue({ data: { kind: "subscription_renewal", payment_id: 91 } });
    renderSheet({
      kind: "subscription_renewal",
      studentId: 12,
      studentName: "Клиент",
      renewedFromSubscriptionId: 58,
      renewedFromSubscriptionName: "Абонемент #58",
      renewalTargetTariffId: 9,
      renewalTargetTariffName: "Абонемент 2026",
      renewalTargetPrice: "6500.00",
    });

    expect(screen.getByText(/Абонемент 2026.*6\s*500/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Перевод" }));
    fireEvent.click(screen.getByRole("button", { name: "Зафиксировать оплату" }));
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(post).toHaveBeenCalledWith(
      "/billing/payments/renewals/",
      expect.objectContaining({
        student_id: 12,
        renewed_from_subscription_id: 58,
        payment_method: "transfer",
        expected_target_tariff_id: 9,
        expected_target_price: "6500.00",
      }),
    );
    const payload = post.mock.calls[0]?.[1] as Record<string, unknown>;
    expect(payload).not.toHaveProperty("tariff_id");
    expect(payload).not.toHaveProperty("debt_ids");
    expect(payload).not.toHaveProperty("amount");
  });

  it("fails closed when a terminal renewal has no current target offer", () => {
    renderSheet({
      kind: "subscription_renewal",
      studentId: 12,
      studentName: "Клиент",
      renewedFromSubscriptionId: 58,
      renewedFromSubscriptionName: "Base A",
    });

    expect(
      screen.getByText("Актуальная цена продления недоступна. Обновите карточку клиента."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Зафиксировать оплату" })).toBeDisabled();
    expect(post).not.toHaveBeenCalled();
  });

  it("carries the expected target fingerprint to a staff SBP renewal", async () => {
    post.mockResolvedValue({ data: { kind: "subscription_renewal", bank_payment_order_id: 92 } });
    renderSheet(
      {
        kind: "subscription_renewal",
        studentId: 12,
        studentName: "Клиент",
        renewedFromSubscriptionId: 58,
        renewedFromSubscriptionName: "Абонемент #58",
        renewalTargetTariffId: 9,
        renewalTargetTariffName: "Абонемент 2026",
        renewalTargetPrice: "6500.00",
      },
      ENABLED_SBP_PAYMENT_CAPABILITIES,
      { initialPaymentMethod: "sbp" },
    );

    fireEvent.click(screen.getByRole("button", { name: "Создать ссылку СБП" }));
    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/billing/bank-payment-orders/",
        expect.objectContaining({
          student_id: 12,
          renewed_from_subscription_id: 58,
          expected_target_tariff_id: 9,
          expected_target_price: "6500.00",
        }),
      );
    });
  });

  it("refreshes a stale renewal offer and clears its command key", async () => {
    const onOfferChanged = vi.fn();
    post.mockRejectedValueOnce({
      response: { data: { code: "renewal_offer_stale", detail: "Offer changed" } },
    });
    renderSheet(
      {
        kind: "subscription_renewal",
        studentId: 12,
        studentName: "Клиент",
        renewedFromSubscriptionId: 58,
        renewedFromSubscriptionName: "Абонемент #58",
        renewalTargetTariffId: 9,
        renewalTargetTariffName: "Абонемент 2026",
        renewalTargetPrice: "6500.00",
      },
      ENABLED_SBP_PAYMENT_CAPABILITIES,
      { onOfferChanged },
    );

    fireEvent.click(screen.getByRole("button", { name: "Зафиксировать оплату" }));
    await waitFor(() => expect(onOfferChanged).toHaveBeenCalledTimes(1));
    expect(screen.getByRole("alert")).toHaveTextContent(/изменилась/);
    const staleKey = (post.mock.calls[0]?.[1] as Record<string, unknown>).idempotency_key;

    post.mockResolvedValueOnce({ data: { payment_id: 92 } });
    fireEvent.click(screen.getByRole("button", { name: "Зафиксировать оплату" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect((post.mock.calls[1]?.[1] as Record<string, unknown>).idempotency_key).not.toBe(
      staleKey,
    );
  });

  it("replays a lost-response command key and clears it only after the server response", async () => {
    post
      .mockRejectedValueOnce(new Error("connection lost"))
      .mockResolvedValueOnce({ data: { payment_id: 91 } })
      .mockResolvedValueOnce({ data: { payment_id: 92 } });
    renderSheet({
      kind: "subscription_renewal",
      studentId: 12,
      studentName: "Клиент",
      renewedFromSubscriptionId: 58,
      renewedFromSubscriptionName: "Абонемент #58",
      renewalTargetTariffId: 9,
      renewalTargetTariffName: "Абонемент 2026",
      renewalTargetPrice: "6500.00",
    });

    fireEvent.click(screen.getByRole("button", { name: "СБП" }));
    fireEvent.click(screen.getByRole("button", { name: "Создать ссылку СБП" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    const lostResponseKey = (post.mock.calls[0]?.[1] as Record<string, unknown>).idempotency_key;

    fireEvent.click(screen.getByRole("button", { name: "Создать ссылку СБП" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect((post.mock.calls[1]?.[1] as Record<string, unknown>).idempotency_key).toBe(
      lostResponseKey,
    );

    fireEvent.click(screen.getByRole("button", { name: "Создать ссылку СБП" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(3));
    expect((post.mock.calls[2]?.[1] as Record<string, unknown>).idempotency_key).not.toBe(
      lostResponseKey,
    );
  });

  it("starts a terminal SBP retry with a fresh key before retaining that new command", async () => {
    const previousKey = getOrCreateContextualCommercialCommandKey({
      clubId: 4,
      actorSubject: "trainer-4",
      audience: "staff",
      kind: "subscription_renewal",
      studentId: 12,
      paymentMethod: "sbp",
      renewedFromSubscriptionId: 58,
    });
    post.mockResolvedValue({ data: { bank_payment_order_id: 92 } });
    renderSheet(
      {
        kind: "subscription_renewal",
        studentId: 12,
        studentName: "Клиент",
        renewedFromSubscriptionId: 58,
        renewedFromSubscriptionName: "Абонемент #58",
        renewalTargetTariffId: 9,
        renewalTargetTariffName: "Абонемент 2026",
        renewalTargetPrice: "6500.00",
      },
      ENABLED_SBP_PAYMENT_CAPABILITIES,
      { initialPaymentMethod: "sbp", freshCommand: true },
    );

    fireEvent.click(screen.getByRole("button", { name: "Создать ссылку СБП" }));
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect((post.mock.calls[0]?.[1] as Record<string, unknown>).idempotency_key).not.toBe(
      previousKey,
    );
  });

  it.each([
    {
      kind: "group_sale" as const,
      studentId: 12,
      studentName: "Клиент",
      tariffId: 7,
      tariffName: "Месяц",
      trainingGroupId: 25,
      scheduleId: 31,
      startDate: "2099-02-01",
      groupName: "Дети",
    },
    {
      kind: "subscription_renewal" as const,
      studentId: 12,
      studentName: "Клиент",
      renewedFromSubscriptionId: 58,
      renewedFromSubscriptionName: "Абонемент #58",
    },
  ])("never exposes or posts SBP for unready $kind commercial payment capability", (context) => {
    renderSheet(context, { online_payments_enabled: true }, { initialPaymentMethod: "sbp" });

    expect(screen.queryByRole("button", { name: "СБП" })).not.toBeInTheDocument();
    const submit = screen.getByRole("button", { name: "Создать ссылку СБП" });
    expect(submit).toBeDisabled();
    fireEvent.click(submit);
    expect(post).not.toHaveBeenCalled();
  });
});
