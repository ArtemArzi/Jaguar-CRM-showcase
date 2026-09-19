import { QueryClient, QueryClientProvider, onlineManager } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  ENABLED_SBP_PAYMENT_CAPABILITIES,
  getPaymentCapabilitiesQueryKey,
} from "@/api/payment-capabilities";
import { getPersonalAvailabilityCapabilityQueryKey } from "@/api/unified-client-journey";
import { useAuthStore } from "@/features/auth/auth-store";
import { useBrandingStore } from "@/features/branding/use-branding";
import type { StudentSubscription } from "../types";
import {
  directPersonalOfferQueryKey,
  fixedPersonalOfferQueryKey,
} from "../lib/personal-availability";
import {
  PersonalBookingSheet,
  type FixedPersonalSlotContext,
  type PersonalBookingSuccess,
} from "./personal-booking-sheet";
import {
  getCommercialCacheScope,
  personalCommercialContextQueryKey,
  type PersonalCommercialReceipt,
} from "./personal-commercial-context-api";

const { get, post } = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}));

let pendingPaymentReservations: unknown[] = [];
let directOfferResponses: Array<Promise<unknown> | unknown> = [];
let personalBookingDiscounts: unknown[] = [];
let personalBookingDiscountsShouldFail = false;
let fixedOfferResponse: Record<string, unknown> | null = null;
let fixedOfferPromise: Promise<Record<string, unknown>> | null = null;

vi.mock("@/api/custom-fetch", () => ({ default: { get, post } }));

const entitlement: StudentSubscription = {
  id: 55,
  tariff_name: "Персональный абонемент",
  training_type_id: 22,
  training_type_name: "Персоналка",
  trainings_used: 0,
  trainings_total: 8,
  trainings_left: 8,
  expires_at: "2099-08-01T00:00:00Z",
  status: "active",
  training_type_kind: "personal",
};

const hybridEntitlement: StudentSubscription = {
  ...entitlement,
  id: 57,
  tariff_name: "Группа + персоналки",
  training_type_id: 44,
  training_type_name: "Группа",
  training_type_kind: "group",
  trainings_left: 0,
  has_components: true,
  booking_entitlements: [
    {
      training_type_id: 22,
      training_type_name: "Персоналка",
      training_type_kind: "personal",
      credits_left: 1,
      weekly_limit: null,
      scope: "club",
      location_id: null,
    },
  ],
};

const wrongLocationHybridEntitlement: StudentSubscription = {
  ...hybridEntitlement,
  booking_entitlements: [
    {
      ...hybridEntitlement.booking_entitlements![0],
      scope: "location",
      location_id: 99,
    },
  ],
};

const exhaustedWeeklyHybridEntitlement: StudentSubscription = {
  ...hybridEntitlement,
  booking_date: "2099-07-07",
  booking_entitlements: [
    {
      ...hybridEntitlement.booking_entitlements![0],
      credits_left: null,
      weekly_limit: 1,
      weekly_used: 1,
    },
  ],
};

const inactiveOnlyComponentSubscription: StudentSubscription = {
  ...entitlement,
  has_components: true,
  booking_entitlements: [],
};

const hybridSubscriptionPayload = {
  id: 57,
  tariff: {
    name: "Группа + персоналки",
    trainings_limit: 12,
    training_type: { id: 44, name: "Группа", kind: "group" },
  },
  trainings_used: 12,
  trainings_left: 0,
  expires_at: "2099-08-01T00:00:00Z",
  status: "active",
  training_type_kind: "group",
  has_components: true,
  booking_entitlements: [
    {
      training_type_id: 22,
      training_type_name: "Персоналка",
      training_type_kind: "personal",
      credits_left: 1,
      weekly_limit: null,
      scope: "club",
      location_id: null,
    },
  ],
};

function renderSheet({
  subscriptions,
  fetchSubscriptions = false,
  fixedSlot,
  retryBankPaymentReceipt,
  onBooked = vi.fn(),
  paymentCapabilities = ENABLED_SBP_PAYMENT_CAPABILITIES,
  skipPaymentCapabilitiesSeed = false,
  skipPersonalAvailabilityCapabilitySeed = false,
  unifiedClientJourneyEnabled = false,
  staffCommandProtocol = "v1",
  onStaffIntentCreated = vi.fn(),
  queryClient: suppliedQueryClient,
}: {
  subscriptions?: StudentSubscription[];
  fetchSubscriptions?: boolean;
  fixedSlot?: FixedPersonalSlotContext;
  retryBankPaymentReceipt?: PersonalCommercialReceipt;
  onBooked?: ReturnType<typeof vi.fn>;
  paymentCapabilities?: unknown;
  skipPaymentCapabilitiesSeed?: boolean;
  skipPersonalAvailabilityCapabilitySeed?: boolean;
  unifiedClientJourneyEnabled?: boolean;
  staffCommandProtocol?: "v1" | "v2";
  onStaffIntentCreated?: (receipt: PersonalCommercialReceipt) => void;
  queryClient?: QueryClient;
} = {}) {
  const queryClient =
    suppliedQueryClient ??
    new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
  if (!skipPaymentCapabilitiesSeed && paymentCapabilities !== undefined) {
    queryClient.setQueryData(getPaymentCapabilitiesQueryKey(), paymentCapabilities);
  }
  if (!skipPersonalAvailabilityCapabilitySeed) {
    queryClient.setQueryData(getPersonalAvailabilityCapabilityQueryKey(
      1,
      useAuthStore.getState().accessToken,
      useAuthStore.getState().role,
    ), {
      enabled: unifiedClientJourneyEnabled,
      staff_command_protocol_version: staffCommandProtocol,
    });
  }
  const view = render(
    <QueryClientProvider client={queryClient}>
      <PersonalBookingSheet
        open
        onOpenChange={vi.fn()}
        studentId={7}
        studentName="Маша Иванова"
        subscriptions={fetchSubscriptions ? undefined : subscriptions ?? []}
        fixedSlot={fixedSlot}
        retryBankPaymentReceipt={retryBankPaymentReceipt}
        onBooked={onBooked as (success: PersonalBookingSuccess) => void}
        onStaffIntentCreated={onStaffIntentCreated}
      />
    </QueryClientProvider>,
  );
  return { queryClient, onBooked, onStaffIntentCreated, ...view };
}

function fillDirectContext() {
  fireEvent.change(screen.getByLabelText("Дата *"), { target: { value: "2099-07-07" } });
  fireEvent.change(screen.getByLabelText("Начало *"), { target: { value: "10:00" } });
  fireEvent.change(screen.getByLabelText("Конец *"), { target: { value: "11:00" } });
  fireEvent.change(screen.getByLabelText("Тип тренировки *"), { target: { value: "22" } });
  fireEvent.change(screen.getByLabelText("Зал *"), { target: { value: "11" } });
}

function selectPayAtClubAndFillDirectContext() {
  fireEvent.change(screen.getByLabelText("Разовая персоналка *"), { target: { value: "33" } });
  fillDirectContext();
}

const fixedSlot: FixedPersonalSlotContext = {
  slotId: 81,
  startsAt: "2099-07-07T10:00:00+05:00",
  endsAt: "2099-07-07T11:00:00+05:00",
  trainerId: 9,
  trainerName: "Тренер",
  locationId: 11,
  locationName: "Основной зал",
  trainingTypeId: 22,
  trainingTypeName: "Персоналка",
};

function staffReceipt(paymentMethod: "entitlement" | "cash" | "transfer" | "sbp" | "pay_at_visit") {
  return {
    kind: "personal_staff_intent" as const,
    booking_id: 71,
    reservation_id: paymentMethod === "sbp" ? 81 : null,
    payment_id: paymentMethod === "cash" || paymentMethod === "transfer" ? 91 : null,
    subscription_id: paymentMethod === "entitlement" ? 57 : null,
    bank_payment_order_id: paymentMethod === "sbp" ? 92 : null,
    debt_id: null,
    slot_id: 81,
    schedule_id: 101,
    enrollment_id: 102,
    starts_at: fixedSlot.startsAt,
    ends_at: fixedSlot.endsAt,
    training_type_id: 22,
    training_type_name: "Персоналка",
    tariff_id: 44,
    tariff_name: "Персоналка по умолчанию",
    trainer_id: 9,
    trainer_name: "Тренер",
    location_id: 11,
    location_name: "Основной зал",
    amount: paymentMethod === "entitlement" ? null : "2500.00",
    payment_method: paymentMethod,
    status: paymentMethod === "pay_at_visit" ? "scheduled" : "pending",
    provider_payment_url: paymentMethod === "sbp" ? "https://bank.example/pay/92" : null,
    allowed_actions: [],
    resource_route: "/api/personal-drop-in-bookings/71/",
  };
}

describe("PersonalBookingSheet", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    onlineManager.setOnline(true);
    pendingPaymentReservations = [];
    directOfferResponses = [];
    personalBookingDiscounts = [];
    personalBookingDiscountsShouldFail = false;
    fixedOfferResponse = null;
    fixedOfferPromise = null;
    useAuthStore.setState({
      accessToken: "header.eyJzdWIiOiJ0cmFpbmVyLTkifQ.signature",
      role: "trainer",
      clubId: 1,
      trainerId: 9,
      isAuthenticated: true,
    });
    useBrandingStore.setState({
      timeZone: "Asia/Yekaterinburg",
      timeZoneStatus: "ready",
      timeZoneClubId: 1,
      isTimeZoneAuthoritative: true,
    });
    get.mockImplementation((url: string, config?: { params?: { discount_id?: number } }) => {
      if (url === "/trainers/me/") {
        return Promise.resolve({ data: { id: 9, first_name: "Тренер", last_name: "" } });
      }
      if (url === "/billing/subscriptions/") {
        return Promise.resolve({ data: [hybridSubscriptionPayload] });
      }
      if (url === "/clubs/locations/") return Promise.resolve({ data: [{ id: 11, name: "Основной зал" }] });
      if (url === "/billing/training-types/") {
        return Promise.resolve({
          data: [
            { id: 22, name: "Персоналка", slug: "personal", kind: "personal", is_active: true },
            { id: 23, name: "Другая персоналка", slug: "personal-extra", kind: "personal", is_active: true },
          ],
        });
      }
      if (url === "/personal-availability/direct-offer/") {
        const nextResponse = directOfferResponses.shift();
        if (nextResponse !== undefined) {
          return nextResponse;
        }
        if (config?.params?.discount_id === 22) {
          return Promise.resolve({
            data: {
              offer_tariff_id: 44,
              offer_tariff_name: "Разовая персоналка",
              offer_price: "2500.00",
              offer_base_amount: "2500.00",
              offer_discount_id: 22,
              offer_discount_name: "Семейная скидка",
              offer_discount_type: "fixed",
              offer_discount_value: "500.00",
              offer_discount_amount: "500.00",
              offer_payable_amount: "2000.00",
              offer_trainer_id: 9,
              offer_digest: "direct-discount-v1",
              offer_error_code: null,
            },
          });
        }
        return Promise.resolve({
          data: {
            offer_tariff_id: 44,
            offer_tariff_name: "Разовая персоналка",
            offer_price: "2500.00",
            offer_duration_days: 1,
            offer_scope: "club",
            offer_location_id: null,
            offer_digest: "direct-offer-v1",
            offer_error_code: null,
          },
        });
      }
      if (url === "/personal-availability/offers/") {
        if (fixedOfferPromise !== null) {
          return fixedOfferPromise.then((data) => ({ data }));
        }
        if (fixedOfferResponse !== null) {
          return Promise.resolve({ data: fixedOfferResponse });
        }
        const selectedDiscount = config?.params?.discount_id === 22;
        return Promise.resolve({
          data: {
            offer_tariff_id: 44,
            offer_tariff_name: "Разовая персоналка",
            offer_price: "2500.00",
            offer_base_amount: "2500.00",
            offer_discount_id: selectedDiscount ? 22 : null,
            offer_discount_name: selectedDiscount ? "Семейная скидка" : "",
            offer_discount_type: selectedDiscount ? "fixed" : "",
            offer_discount_value: selectedDiscount ? "500.00" : "",
            offer_discount_amount: selectedDiscount ? "500.00" : "0.00",
            offer_payable_amount: selectedDiscount ? "2000.00" : "2500.00",
            offer_trainer_id: 9,
            offer_digest: selectedDiscount ? "slot-discount-v1" : "slot-offer-v1",
            offer_error_code: "",
          },
        });
      }
      if (url === "/billing/discounts/") {
        return personalBookingDiscountsShouldFail
          ? Promise.reject(new Error("Discounts unavailable"))
          : Promise.resolve({ data: personalBookingDiscounts });
      }
      if (url === "/personal-availability/slots/") {
        return Promise.resolve({
          data: [
            {
              id: 81,
              starts_at: "2099-07-07T23:00:00Z",
              ends_at: "2099-07-08T00:00:00Z",
              trainer_id: 9,
              trainer_name: "Тренер",
              location_id: 11,
              location_name: "Основной зал",
              training_type_id: 22,
              training_type_name: "Персоналка",
              offer_tariff_id: 44,
              offer_tariff_name: "Разовая персоналка",
              offer_price: "2500.00",
              offer_digest: "slot-offer-v1",
            },
          ],
        });
      }
      if (url === "/billing/tariffs/") {
        return Promise.resolve({
          data: { items: [{ id: 33, name: "Разовая персоналка", price: 2000, training_type: { id: 22, name: "Персоналка", slug: "personal", kind: "personal", is_active: true }, trainings_limit: 1, duration_days: 1, scope: "club", location_id: null, is_active: true }] },
        });
      }
      if (url === "/students/7/personal-booking-payment-reservations/") {
        return Promise.resolve({ data: pendingPaymentReservations });
      }
      if (url === "/billing/bank-payment-orders/92/") {
        return Promise.resolve({
          data: {
            id: 92,
            subscription_id: null,
            student_id: 7,
            tariff_id: 33,
            debt_ids: [],
            source: "trainer",
            status: "pending",
            amount_snapshot: "2000.00",
            currency: "RUB",
            purpose_snapshot: "Разовая персоналка",
            provider_payment_url: "https://bank.example/pay/92",
            expires_at: "2099-07-07T12:00:00Z",
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
    post.mockResolvedValue({ data: { id: 9, price_snapshot: "2000.00" } });
  });

  it("keeps the mobile sheet anchored while only its content scrolls vertically", async () => {
    renderSheet();

    const dialog = screen.getByRole("dialog", { name: "Записать персоналку" });
    const scrollRegion = dialog.querySelector('[data-slot="personal-booking-scroll"]');

    expect(dialog).toHaveClass("w-full", "max-w-[100vw]", "overflow-x-hidden", "overscroll-contain");
    expect(scrollRegion).toHaveClass(
      "min-w-0",
      "max-w-full",
      "overflow-x-hidden",
      "overflow-y-auto",
      "overscroll-contain",
      "touch-pan-y",
    );
    expect(await screen.findByRole("button", { name: "Оплата в клубе" })).toBeVisible();
  });

  it("shows honest pay-at-club and online modes, then creates the exact drop-in payload", async () => {
    const { queryClient, onBooked } = renderSheet();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    expect(await screen.findByRole("button", { name: "Оплата в клубе" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Оплата через СБП" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "По абонементу" })).not.toBeInTheDocument();
    expect(screen.getByText(/Долг появится только после check-in/)).toBeInTheDocument();

    selectPayAtClubAndFillDirectContext();
    fireEvent.click(screen.getByRole("button", { name: "Записать с оплатой в клубе" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/students/7/personal-drop-in-bookings/",
        expect.objectContaining({
          starts_at: "2099-07-07T10:00:00",
          ends_at: "2099-07-07T11:00:00",
          location_id: 11,
          training_type_id: 22,
          tariff_id: 33,
        }),
      );
    });
    expect(onBooked).toHaveBeenCalledWith(expect.objectContaining({ mode: "pay_at_club", price: "2000.00" }));
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["trainer", "availability"] });
  });

  it("fails closed when the online-payment capability request errors", async () => {
    renderSheet({ skipPaymentCapabilitiesSeed: true });

    const online = await screen.findByRole("button", { name: "Оплата через СБП" });
    expect(online).toBeVisible();
    expect(online).toBeDisabled();
    await waitFor(() => {
      expect(get).toHaveBeenCalledWith("/billing/payment-capabilities/");
    });
    fireEvent.click(online);
    expect(post).not.toHaveBeenCalled();
  });

  it("keeps the entitlement path explicit when the client has a usable personal subscription", async () => {
    renderSheet({ subscriptions: [entitlement] });
    expect(await screen.findByRole("button", { name: "По абонементу" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByRole("button", { name: "Оплата в клубе" })).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Абонемент *"), { target: { value: "55" } });
    fillDirectContext();
    fireEvent.click(screen.getByRole("button", { name: "Записать" }));
    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/students/7/personal-bookings/",
        expect.objectContaining({ subscription_id: 55, training_type_id: 22 }),
      );
    });
  });

  it("normalizes and uses a hybrid subscription's matching personal component for a direct booking", async () => {
    renderSheet({ fetchSubscriptions: true });

    expect(await screen.findByRole("button", { name: "По абонементу" })).toHaveAttribute("aria-pressed", "true");
    expect(get).toHaveBeenCalledWith(
      "/billing/subscriptions/",
      expect.objectContaining({
        params: expect.objectContaining({ student_id: 7, booking_date: expect.any(String) }),
      }),
    );
    fireEvent.change(screen.getByLabelText("Абонемент *"), { target: { value: "57" } });
    expect(screen.getByLabelText("Тип тренировки *")).toHaveValue("22");
    fillDirectContext();
    fireEvent.click(await screen.findByRole("button", { name: "Записать" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/students/7/personal-bookings/",
        expect.objectContaining({ subscription_id: 57, training_type_id: 22 }),
      );
    });
  });

  it("clears an incompatible component selection when the direct training type changes", async () => {
    renderSheet({ subscriptions: [hybridEntitlement] });
    await screen.findByRole("button", { name: "По абонементу" });
    fireEvent.change(screen.getByLabelText("Абонемент *"), { target: { value: "57" } });
    expect(screen.getByLabelText("Абонемент *")).toHaveValue("57");

    fireEvent.change(screen.getByLabelText("Тип тренировки *"), { target: { value: "23" } });
    expect(screen.queryByRole("button", { name: "По абонементу" })).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Тип тренировки *"), { target: { value: "22" } });
    expect(await screen.findByLabelText("Абонемент *")).toHaveValue("");
  });

  it("uses slot endpoints without allowing the fixed trainer, time, type, or location to drift", async () => {
    renderSheet({ fixedSlot });
    await screen.findByText("Слот зафиксирован");
    await screen.findByRole("option", { name: /Разовая персоналка/ });
    expect(screen.queryByLabelText("Дата *")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Разовая персоналка *"), { target: { value: "33" } });
    fireEvent.click(screen.getByRole("button", { name: "Записать с оплатой в клубе" }));
    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/personal-availability/slots/81/drop-in-bookings/",
        expect.objectContaining({ student_id: 7, tariff_id: 33 }),
      );
    });
    expect(post.mock.calls[0][1]).not.toHaveProperty("starts_at");
    expect(post.mock.calls[0][1]).not.toHaveProperty("trainer_id");
  });

  it("uses the exact direct-time server offer and staff intent when the flag is on", async () => {
    renderSheet({ unifiedClientJourneyEnabled: true });
    await screen.findByLabelText("Дата *");
    fillDirectContext();
    expect((await screen.findAllByText(/2\s*500\s*₽/)).length).toBeGreaterThan(1);
    expect(screen.queryByLabelText("Разовая персоналка *")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Наличные" })).toBeEnabled();
    post.mockResolvedValueOnce({ data: staffReceipt("cash") });
    fireEvent.click(screen.getByRole("button", { name: "Наличные" }));
    fireEvent.click(screen.getByRole("button", { name: /Записать Маша Иванова, наличные,/ }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/personal-availability/staff-intents/direct/",
        expect.objectContaining({
          student_id: 7,
          trainer_id: 9,
          starts_at: "2099-07-07T10:00:00",
          ends_at: "2099-07-07T11:00:00",
          location_id: 11,
          training_type_id: 22,
          payment_method: "cash",
          offer_digest: "direct-offer-v1",
          idempotency_key: expect.any(String),
        }),
      );
    });
    expect(post.mock.calls[0]?.[1]).not.toHaveProperty("tariff_id");
    expect(post.mock.calls[0]?.[1]).not.toHaveProperty("discount_ids");
  });

  it("keeps direct paid methods available when the client also has a usable entitlement", async () => {
    post.mockResolvedValueOnce({ data: staffReceipt("cash") });
    renderSheet({
      subscriptions: [entitlement],
      unifiedClientJourneyEnabled: true,
    });
    await screen.findByRole("option", { name: "Персоналка" });
    await screen.findByRole("option", { name: "Основной зал" });
    fillDirectContext();

    expect(await screen.findByRole("button", { name: "По абонементу" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    expect(screen.getByRole("button", { name: "Выберите способ оплаты" })).toBeDisabled();
    await waitFor(() => {
      expect(get).toHaveBeenCalledWith(
        "/personal-availability/direct-offer/",
        expect.objectContaining({ params: expect.objectContaining({ trainer_id: 9 }) }),
      );
      expect(get).toHaveBeenCalledWith("/billing/discounts/");
    });
    expect(await screen.findByRole("region", { name: "Итог персональной записи" })).toHaveTextContent(
      /2\s*500\s*₽/,
    );
    const cash = await screen.findByRole("button", { name: "Наличные" });
    await waitFor(() => expect(cash).toBeEnabled());
    fireEvent.click(cash);
    fireEvent.click(screen.getByRole("button", { name: /Записать Маша Иванова, наличные,/ }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/personal-availability/staff-intents/direct/",
        expect.objectContaining({
          student_id: 7,
          trainer_id: 9,
          starts_at: "2099-07-07T10:00:00",
          ends_at: "2099-07-07T11:00:00",
          payment_method: "cash",
          offer_digest: "direct-offer-v1",
        }),
      );
    });
  });

  it("shows the selected client, trainer, club time, exact discount and payable offer for a fixed slot", async () => {
    personalBookingDiscounts = [
      {
        id: 22,
        name: "Семейная скидка",
        discount_type: "fixed",
        value: "500.00",
        is_active: true,
      },
    ];
    const { queryClient } = renderSheet({ fixedSlot, unifiedClientJourneyEnabled: true });

    const bookingSummary = await screen.findByRole("region", {
      name: "Детали персональной записи",
    });
    expect(bookingSummary).toHaveTextContent("Маша Иванова");
    expect(bookingSummary).toHaveTextContent("Тренер");
    expect(bookingSummary).toHaveTextContent("2099-07-07 · 10:00–11:00 · Основной зал");

    fireEvent.click(await screen.findByRole("radio", { name: /Семейная скидка/ }));

    const commercialCacheScope = getCommercialCacheScope(
      1,
      useAuthStore.getState().accessToken,
      useAuthStore.getState().role,
    );
    expect(
      queryClient.getQueryState(["billing", "discounts", ...commercialCacheScope]),
    ).toBeDefined();
    expect(queryClient.getQueryState(["billing", "discounts"])).toBeUndefined();

    const offerSummary = await screen.findByRole("region", {
      name: "Итог персональной записи",
    });
    expect(offerSummary).toHaveTextContent("Обычная цена");
    expect(offerSummary).toHaveTextContent(/2\s*500\s*₽/);
    expect(offerSummary).toHaveTextContent("Скидка: Семейная скидка");
    expect(offerSummary).toHaveTextContent(/500\s*₽/);
    expect(offerSummary).toHaveTextContent("К оплате");
    expect(offerSummary).toHaveTextContent(/2\s*000\s*₽/);
    expect(screen.getByRole("button", { name: "Выберите способ оплаты" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Оплата при посещении" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    fireEvent.click(screen.getByRole("button", { name: "Оплата при посещении" }));
    expect(
      screen.getByRole("button", {
        name: /Записать Маша Иванова, оплата при посещении, на 2\s*000\s*₽/,
      }),
    ).toBeEnabled();
  });

  it("binds the selected discount to exact slot and direct staff commands without raw amount authority", async () => {
    personalBookingDiscounts = [
      {
        id: 22,
        name: "Семейная скидка",
        discount_type: "fixed",
        value: "500.00",
        is_active: true,
      },
    ];
    post.mockResolvedValue({ data: staffReceipt("cash") });
    const fixed = renderSheet({
      fixedSlot: { ...fixedSlot, trainerId: 71 },
      unifiedClientJourneyEnabled: true,
    });

    fireEvent.click(await screen.findByRole("radio", { name: /Семейная скидка/ }));
    await waitFor(() => {
      expect(screen.getByRole("region", { name: "Итог персональной записи" })).toHaveTextContent(
        /2\s*000\s*₽/,
      );
    });
    expect(get).toHaveBeenCalledWith(
      "/personal-availability/offers/",
      expect.objectContaining({
        params: expect.objectContaining({ slot_id: 81, trainer_id: 71, discount_id: 22 }),
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Наличные" }));
    fireEvent.click(screen.getByRole("button", { name: /Записать Маша Иванова, наличные, на 2\s*000\s*₽/ }));
    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/personal-availability/slots/81/staff-intents/",
        expect.objectContaining({
          student_id: 7,
          payment_method: "cash",
          offer_digest: "slot-discount-v1",
          discount_id: 22,
        }),
      );
    });
    expect(post.mock.calls[0]?.[1]).not.toHaveProperty("amount");
    expect(post.mock.calls[0]?.[1]).not.toHaveProperty("discount_ids");

    fixed.unmount();
    post.mockClear();
    renderSheet({ unifiedClientJourneyEnabled: true });
    await screen.findByLabelText("Дата *");
    fillDirectContext();
    fireEvent.click(await screen.findByRole("radio", { name: /Семейная скидка/ }));
    await waitFor(() => {
      expect(screen.getByRole("region", { name: "Итог персональной записи" })).toHaveTextContent(
        /2\s*000\s*₽/,
      );
    });
    expect(get).toHaveBeenCalledWith(
      "/personal-availability/direct-offer/",
      expect.objectContaining({ params: expect.objectContaining({ discount_id: 22 }) }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Перевод" }));
    fireEvent.click(screen.getByRole("button", { name: /Записать Маша Иванова, перевод, на 2\s*000\s*₽/ }));
    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/personal-availability/staff-intents/direct/",
        expect.objectContaining({
          trainer_id: 9,
          starts_at: "2099-07-07T10:00:00",
          ends_at: "2099-07-07T11:00:00",
          location_id: 11,
          training_type_id: 22,
          payment_method: "transfer",
          offer_digest: "direct-discount-v1",
          discount_id: 22,
        }),
      );
    });
    expect(post.mock.calls[0]?.[1]).not.toHaveProperty("amount");
    expect(post.mock.calls[0]?.[1]).not.toHaveProperty("discount_ids");
  });

  it("blocks a fresh paid booking when the discount catalog cannot be loaded", async () => {
    personalBookingDiscountsShouldFail = true;
    renderSheet({ fixedSlot, unifiedClientJourneyEnabled: true });

    expect(
      await screen.findByText("Не удалось загрузить скидки. Запись заблокирована до повторной проверки."),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Выберите способ оплаты" }),
    ).toBeDisabled();
    expect(screen.getByRole("button", { name: "Оплата при посещении" })).toBeDisabled();
    expect(post).not.toHaveBeenCalled();
  });

  it("blocks unknown capability instead of rendering legacy payment actions", async () => {
    renderSheet({ skipPersonalAvailabilityCapabilitySeed: true });

    expect(
      await screen.findByText("Не удалось подтвердить доступный способ записи."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Оплата в клубе" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Наличные" })).not.toBeInTheDocument();
  });

  it("dispatches direct cash immediately instead of pausing the command at Отправка", async () => {
    post.mockResolvedValueOnce({ data: staffReceipt("cash") });
    renderSheet({ unifiedClientJourneyEnabled: true });
    await screen.findByLabelText("Дата *");
    fillDirectContext();
    expect((await screen.findAllByText(/2\s*500\s*₽/)).length).toBeGreaterThan(1);

    onlineManager.setOnline(false);
    fireEvent.click(screen.getByRole("button", { name: "Наличные" }));
    fireEvent.click(screen.getByRole("button", { name: /Записать Маша Иванова, наличные,/ }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/personal-availability/staff-intents/direct/",
        expect.objectContaining({ payment_method: "cash" }),
      );
    });
    expect(
      await screen.findByRole("region", { name: "Коммерческий контекст персоналки" }),
    ).toBeInTheDocument();
  });

  it("retries a direct SBP receipt with its persisted context and a fresh offer digest", async () => {
    pendingPaymentReservations = [
      {
        id: 81,
        tariff_id: 44,
        tariff_name: "Разовая персоналка",
        starts_at: "2099-07-07T10:00:00",
        ends_at: "2099-07-07T11:00:00",
        status: "pending_payment",
        expires_at: "2099-07-07T12:00:00Z",
        bank_payment_order_id: 92,
        subscription_id: null,
        provider_payment_url: "",
        amount_snapshot: "2500.00",
        order_status: "cancelled",
        can_cancel: false,
      },
    ];
    post.mockResolvedValueOnce({ data: staffReceipt("sbp") });
    renderSheet({
      unifiedClientJourneyEnabled: true,
      retryBankPaymentReceipt: {
        ...staffReceipt("sbp"),
        slot_id: null,
        trainer_id: 9,
        starts_at: "2099-07-07T05:00:00Z",
        ends_at: "2099-07-07T06:00:00Z",
        status: "cancelled",
        allowed_actions: ["retry_bank_payment"],
      },
    });

    expect(await screen.findByText(/Будет создана новая попытка СБП для этой же персоналки/)).toBeInTheDocument();
    expect(screen.getByLabelText("Дата *")).toHaveValue("2099-07-07");
    expect(screen.getByLabelText("Начало *")).toHaveValue("10:00");
    expect(screen.getByLabelText("Конец *")).toHaveValue("11:00");
    expect(screen.getByLabelText("Дата *")).toBeDisabled();
    expect(screen.getByLabelText("Начало *")).toBeDisabled();
    expect(screen.getByLabelText("Тип тренировки *")).toBeDisabled();
    expect(screen.getByLabelText("Зал *")).toBeDisabled();
    expect(get).not.toHaveBeenCalledWith("/billing/discounts/");
    const submit = await screen.findByRole("button", {
      name: /Повторить оплату СБП для Маша Иванова на 2\s*500\s*₽/,
    });
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/personal-availability/staff-intents/direct/",
        expect.objectContaining({
          student_id: 7,
          trainer_id: 9,
          starts_at: "2099-07-07T10:00:00",
          ends_at: "2099-07-07T11:00:00",
          location_id: 11,
          training_type_id: 22,
          payment_method: "sbp",
          offer_digest: "direct-offer-v1",
          idempotency_key: expect.any(String),
        }),
      );
    });
  });

  it("keeps a different live reservation blocking an otherwise authorized terminal retry", async () => {
    pendingPaymentReservations = [
      {
        id: 81,
        tariff_id: 44,
        tariff_name: "Отменённая персоналка",
        starts_at: "2099-07-07T10:00:00",
        ends_at: "2099-07-07T11:00:00",
        status: "pending_payment",
        expires_at: "2099-07-07T12:00:00Z",
        bank_payment_order_id: 92,
        subscription_id: null,
        provider_payment_url: "",
        amount_snapshot: "2500.00",
        order_status: "cancelled",
        can_cancel: false,
      },
      {
        id: 82,
        tariff_id: 44,
        tariff_name: "Другая активная персоналка",
        starts_at: "2099-07-07T10:00:00",
        ends_at: "2099-07-07T11:00:00",
        status: "pending_payment",
        expires_at: "2099-07-07T12:00:00Z",
        bank_payment_order_id: 93,
        subscription_id: null,
        provider_payment_url: "",
        amount_snapshot: "2500.00",
        order_status: "pending",
        can_cancel: true,
      },
    ];
    renderSheet({
      unifiedClientJourneyEnabled: true,
      retryBankPaymentReceipt: {
        ...staffReceipt("sbp"),
        slot_id: null,
        trainer_id: 9,
        starts_at: "2099-07-07T05:00:00Z",
        ends_at: "2099-07-07T06:00:00Z",
        status: "cancelled",
        allowed_actions: ["retry_bank_payment"],
      },
    });

    const submit = await screen.findByRole("button", { name: /Повторить оплату СБП для Маша Иванова/ });
    await waitFor(() => {
      expect(get).toHaveBeenCalledWith("/personal-availability/direct-offer/", expect.anything());
    });
    expect(submit).toBeDisabled();
    expect(post).not.toHaveBeenCalled();
  });

  it("keeps a repeated direct SBP retry disabled until its cached digest is refreshed", async () => {
    const sharedQueryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    sharedQueryClient.setQueryData(
      [
        ...directPersonalOfferQueryKey({
          trainerId: 9,
          startsAt: "2099-07-07T10:00:00",
          endsAt: "2099-07-07T11:00:00",
          locationId: 11,
          trainingTypeId: 22,
          discountId: null,
        }),
        "retry-bank-payment",
      ],
      {
        tariffId: 44,
        tariffName: "Старая разовая персоналка",
        price: "2500.00",
        payableAmount: "2500.00",
        trainerId: 9,
        digest: "cached-direct-retry-digest",
      },
    );

    let resolveRefreshedOffer: ((value: unknown) => void) | undefined;
    directOfferResponses = [
      new Promise((resolve) => {
        resolveRefreshedOffer = resolve;
      }),
    ];
    renderSheet({
      unifiedClientJourneyEnabled: true,
      queryClient: sharedQueryClient,
      retryBankPaymentReceipt: {
        ...staffReceipt("sbp"),
        slot_id: null,
        trainer_id: 9,
        starts_at: "2099-07-07T05:00:00Z",
        ends_at: "2099-07-07T06:00:00Z",
        allowed_actions: ["retry_bank_payment"],
      },
    });

    const submit = await screen.findByRole("button", {
      name: /Повторить оплату СБП для Маша Иванова на 2\s*500\s*₽/,
    });
    await waitFor(() => {
      expect(
        get.mock.calls.filter(([url]) => url === "/personal-availability/direct-offer/"),
      ).toHaveLength(1);
    });
    await waitFor(() => expect(submit).toBeDisabled());

    resolveRefreshedOffer?.({
      data: {
        offer_tariff_id: 44,
        offer_tariff_name: "Обновлённая разовая персоналка",
        offer_price: "2700.00",
        offer_duration_days: 1,
        offer_scope: "club",
        offer_location_id: null,
        offer_digest: "direct-offer-retry-v2",
        offer_error_code: null,
      },
    });

    await waitFor(() => expect(submit).toBeEnabled());
    expect(submit).toHaveAccessibleName(/Повторить оплату СБП для Маша Иванова на 2\s*700\s*₽/);
    fireEvent.click(submit);
    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/personal-availability/staff-intents/direct/",
        expect.objectContaining({
          payment_method: "sbp",
          offer_digest: "direct-offer-retry-v2",
          idempotency_key: expect.any(String),
        }),
      );
    });
  });

  it("waits for the authoritative club timezone before rebuilding a direct SBP retry", async () => {
    useBrandingStore.setState({
      timeZone: "Asia/Yekaterinburg",
      timeZoneStatus: "pending",
      timeZoneClubId: 1,
      isTimeZoneAuthoritative: false,
    });
    post.mockResolvedValueOnce({ data: staffReceipt("sbp") });
    renderSheet({
      unifiedClientJourneyEnabled: true,
      retryBankPaymentReceipt: {
        ...staffReceipt("sbp"),
        slot_id: null,
        trainer_id: 9,
        starts_at: "2099-07-07T05:00:00Z",
        ends_at: "2099-07-07T06:00:00Z",
        allowed_actions: ["retry_bank_payment"],
      },
    });

    expect(await screen.findByText("Проверяем часовой пояс клуба для повторной оплаты...")).toBeInTheDocument();
    expect(screen.queryByLabelText("Начало *")).not.toBeInTheDocument();
    expect(get).not.toHaveBeenCalledWith(
      "/personal-availability/direct-offer/",
      expect.anything(),
    );

    act(() => {
      useBrandingStore.getState().setBranding({
        primary_color: "#000000",
        accent_color: "#FF6B00",
        club_name_display: "CRM Jaguar",
        logo_url: "",
        timezone: "Europe/Moscow",
      }, 1);
    });

    expect(await screen.findByLabelText("Начало *")).toHaveValue("08:00");
    expect(screen.getByLabelText("Конец *")).toHaveValue("09:00");
    expect(screen.getByLabelText("Начало *")).toBeDisabled();
    const submit = await screen.findByRole("button", { name: /Повторить оплату СБП для Маша Иванова на/ });
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/personal-availability/staff-intents/direct/",
        expect.objectContaining({
          starts_at: "2099-07-07T08:00:00",
          ends_at: "2099-07-07T09:00:00",
        }),
      );
    });
  });

  it("does not reset editable direct-booking fields when branding timezone changes", async () => {
    renderSheet({ unifiedClientJourneyEnabled: true });
    expect(await screen.findByRole("button", { name: "Оплата при посещении" })).toBeInTheDocument();
    fillDirectContext();

    act(() => {
      useBrandingStore.getState().setBranding({
        primary_color: "#000000",
        accent_color: "#FF6B00",
        club_name_display: "CRM Jaguar",
        logo_url: "",
        timezone: "Europe/Moscow",
      }, 1);
    });

    expect(screen.getByLabelText("Дата *")).toHaveValue("2099-07-07");
    expect(screen.getByLabelText("Начало *")).toHaveValue("10:00");
    expect(screen.getByLabelText("Конец *")).toHaveValue("11:00");
  });

  it("keeps a direct SBP retry unavailable when branding timezone loading fails", async () => {
    useBrandingStore.setState({
      timeZone: "Asia/Yekaterinburg",
      timeZoneStatus: "failed",
      timeZoneClubId: 1,
      isTimeZoneAuthoritative: false,
    });
    renderSheet({
      unifiedClientJourneyEnabled: true,
      retryBankPaymentReceipt: {
        ...staffReceipt("sbp"),
        slot_id: null,
        trainer_id: 9,
        starts_at: "2099-07-07T05:00:00Z",
        ends_at: "2099-07-07T06:00:00Z",
        allowed_actions: ["retry_bank_payment"],
      },
    });

    expect(
      await screen.findByText("Не удалось получить часовой пояс клуба для повторной оплаты. Обновите страницу."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Повторить оплату СБП/ })).not.toBeInTheDocument();
    expect(get).not.toHaveBeenCalledWith(
      "/personal-availability/direct-offer/",
      expect.anything(),
    );
  });

  it("uses the club-local date when refreshing a fixed-slot SBP retry across UTC midnight", async () => {
    useBrandingStore.setState({
      timeZone: "Asia/Vladivostok",
      timeZoneStatus: "ready",
      timeZoneClubId: 1,
      isTimeZoneAuthoritative: true,
    });
    renderSheet({
      unifiedClientJourneyEnabled: true,
      retryBankPaymentReceipt: {
        ...staffReceipt("sbp"),
        slot_id: 81,
        starts_at: "2099-07-07T23:00:00Z",
        ends_at: "2099-07-08T00:00:00Z",
        allowed_actions: ["retry_bank_payment"],
      },
    });

    expect(await screen.findByText("Слот зафиксирован")).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith("/personal-availability/slots/", {
      params: { date_from: "2099-07-08", date_to: "2099-07-08" },
    });
    expect(screen.getAllByText("2099-07-08 · 09:00–10:00 · Основной зал")).toHaveLength(2);
    await waitFor(() => {
      expect(get).toHaveBeenCalledWith(
        "/personal-availability/offers/",
        expect.objectContaining({ params: expect.objectContaining({ slot_id: 81 }) }),
      );
    });
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Повторить оплату СБП для Маша Иванова на/ })).toBeEnabled();
    });
  });

  it("refetches a warm fixed-slot offer before retrying terminal SBP", async () => {
    const sharedQueryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    fixedOfferResponse = {
      offer_tariff_id: 44,
      offer_tariff_name: "Разовая персоналка",
      offer_price: "2500.00",
      offer_payable_amount: "2500.00",
      offer_trainer_id: 9,
      offer_digest: "slot-offer-before-cancel",
      offer_error_code: "",
    };
    const warm = renderSheet({
      fixedSlot,
      unifiedClientJourneyEnabled: true,
      queryClient: sharedQueryClient,
    });
    await waitFor(() => {
      expect(
        get.mock.calls.filter(([url]) => url === "/personal-availability/offers/"),
      ).toHaveLength(1);
    });
    warm.unmount();

    fixedOfferResponse = {
      ...fixedOfferResponse,
      offer_price: "2700.00",
      offer_payable_amount: "2700.00",
      offer_digest: "slot-offer-retry-v2",
    };
    post.mockResolvedValueOnce({ data: staffReceipt("sbp") });
    renderSheet({
      unifiedClientJourneyEnabled: true,
      queryClient: sharedQueryClient,
      retryBankPaymentReceipt: {
        ...staffReceipt("sbp"),
        status: "cancelled",
        allowed_actions: ["retry_bank_payment"],
      },
    });

    const submit = await screen.findByRole("button", {
      name: /Повторить оплату СБП для Маша Иванова на 2\s*700\s*₽/,
    });
    await waitFor(() => {
      expect(
        get.mock.calls.filter(([url]) => url === "/personal-availability/offers/"),
      ).toHaveLength(2);
    });
    expect(submit).toBeEnabled();
    fireEvent.click(submit);
    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/personal-availability/slots/81/staff-intents/",
        expect.objectContaining({
          payment_method: "sbp",
          offer_digest: "slot-offer-retry-v2",
          idempotency_key: expect.any(String),
        }),
      );
    });
  });

  it("keeps a repeated fixed-slot SBP retry disabled until its cached digest is refreshed", async () => {
    const sharedQueryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const retryQueryKey = [
      ...fixedPersonalOfferQueryKey({
        slotId: fixedSlot.slotId,
        trainerId: fixedSlot.trainerId,
        locationId: fixedSlot.locationId,
        trainingTypeId: fixedSlot.trainingTypeId,
        discountId: null,
      }),
      "retry-bank-payment",
    ];
    sharedQueryClient.setQueryData(retryQueryKey, {
      tariffId: 44,
      tariffName: "Разовая персоналка",
      price: "2500.00",
      payableAmount: "2500.00",
      trainerId: 9,
      digest: "cached-retry-digest",
    });
    let resolveOffer!: (value: Record<string, unknown>) => void;
    fixedOfferPromise = new Promise((resolve) => {
      resolveOffer = resolve;
    });

    renderSheet({
      unifiedClientJourneyEnabled: true,
      queryClient: sharedQueryClient,
      retryBankPaymentReceipt: {
        ...staffReceipt("sbp"),
        status: "cancelled",
        allowed_actions: ["retry_bank_payment"],
      },
    });

    const cachedSubmit = await screen.findByRole("button", {
      name: /Повторить оплату СБП для Маша Иванова на 2\s*500\s*₽/,
    });
    expect(cachedSubmit).toBeDisabled();
    fireEvent.click(cachedSubmit);
    expect(post).not.toHaveBeenCalled();

    resolveOffer({
      offer_tariff_id: 44,
      offer_tariff_name: "Разовая персоналка",
      offer_price: "2700.00",
      offer_payable_amount: "2700.00",
      offer_trainer_id: 9,
      offer_digest: "fresh-retry-digest",
      offer_error_code: "",
    });
    const freshSubmit = await screen.findByRole("button", {
      name: /Повторить оплату СБП для Маша Иванова на 2\s*700\s*₽/,
    });
    await waitFor(() => expect(freshSubmit).toBeEnabled());
    fireEvent.click(freshSubmit);
    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/personal-availability/slots/81/staff-intents/",
        expect.objectContaining({ offer_digest: "fresh-retry-digest" }),
      );
    });
  });

  it("fails closed instead of substituting the current trainer for an incomplete direct SBP retry", async () => {
    renderSheet({
      unifiedClientJourneyEnabled: true,
      retryBankPaymentReceipt: {
        ...staffReceipt("sbp"),
        slot_id: null,
        trainer_id: 0,
        allowed_actions: ["retry_bank_payment"],
      },
    });

    expect(
      await screen.findByText(
        "Не удалось подтвердить точный контекст персоналки для повторной оплаты. Обновите карточку клиента.",
      ),
    ).toBeInTheDocument();
    expect(get).not.toHaveBeenCalledWith(
      "/personal-availability/direct-offer/",
      expect.anything(),
    );
    expect(post).not.toHaveBeenCalled();
  });

  it("submits each flag-on personal intent through the fixed-slot coordinator without tariff or discount authority", async () => {
    const { queryClient } = renderSheet({
      fixedSlot: {
        ...fixedSlot,
        offerTariffId: 44,
        offerTariffName: "Персоналка по умолчанию",
        offerPrice: "2500.00",
        offerDigest: "slot-offer-v1",
      },
      unifiedClientJourneyEnabled: true,
    });
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    post.mockResolvedValueOnce({ data: staffReceipt("cash") });
    expect((await screen.findAllByText(/2\s*500\s*₽/)).length).toBeGreaterThan(1);
    expect(screen.queryByLabelText("Разовая персоналка *")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Дата *")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Наличные" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Перевод" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Оплата через СБП" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Оплата при посещении" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Наличные" }));
    fireEvent.click(screen.getByRole("button", { name: /Записать Маша Иванова, наличные,/ }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/personal-availability/slots/81/staff-intents/",
        expect.objectContaining({
          student_id: 7,
          payment_method: "cash",
          offer_digest: "slot-offer-v1",
        }),
      );
    });
    expect(post.mock.calls[0][1]).not.toHaveProperty("tariff_id");
    expect(post.mock.calls[0][1]).not.toHaveProperty("discount_ids");
    expect(await screen.findByText("Оплата ожидает подтверждения владельцем")).toBeInTheDocument();
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: personalCommercialContextQueryKey(
        7,
        getCommercialCacheScope(
          1,
          useAuthStore.getState().accessToken,
          useAuthStore.getState().role,
        ),
      ),
    });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["leads", "mine"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["leads"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["students"] });
  });

  it("uses the v2 manual route, invalidates lead and student lists, and returns student state to its caller", async () => {
    const onStaffIntentCreated = vi.fn();
    const { queryClient } = renderSheet({
      fixedSlot: {
        ...fixedSlot,
        offerTariffId: 44,
        offerTariffName: "Персоналка по умолчанию",
        offerPrice: "2500.00",
        offerDigest: "slot-offer-v2",
      },
      unifiedClientJourneyEnabled: true,
      staffCommandProtocol: "v2",
      onStaffIntentCreated,
    });
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    post.mockResolvedValueOnce({
      data: {
        ...staffReceipt("cash"),
        workspace_state: "student",
        finance_state: "pending_manual",
        command_replayed: false,
      },
    });

    expect(await screen.findByRole("button", { name: "Наличные" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.queryByRole("button", { name: "Оплата при посещении" })).not.toBeInTheDocument();
    expect(
      screen.getByText(
        "После успешной записи клиент перейдёт в ученики, а оплата останется на финансовой проверке владельца.",
      ),
    ).toBeInTheDocument();
    const submit = screen.getByRole("button", { name: /Записать Маша Иванова, наличные,/ });
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/personal-availability/v2/slots/81/staff-intents/",
        expect.objectContaining({
          payment_method: "cash",
          offer_digest: "slot-offer-v1",
          protocol_version: "v2",
        }),
      );
    });
    expect(onStaffIntentCreated).toHaveBeenCalledWith(
      expect.objectContaining({
        workspace_state: "student",
        finance_state: "pending_manual",
        command_replayed: false,
      }),
    );
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["leads"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["students"] });
  });

  it.each([
    ["cash", "Наличные", /Записать Маша Иванова, наличные,/],
    ["transfer", "Перевод", /Записать Маша Иванова, перевод,/],
    ["sbp", "Оплата через СБП", /Создать ссылку СБП для Маша Иванова/],
    ["pay_at_visit", "Оплата при посещении", /Записать Маша Иванова, оплата при посещении,/],
  ] as const)("sends the flag-on %s mode as one digest-bound coordinator command", async (paymentMethod, modeLabel, submitLabel) => {
    post.mockResolvedValueOnce({ data: staffReceipt(paymentMethod) });
    renderSheet({
      fixedSlot: {
        ...fixedSlot,
        offerTariffId: 44,
        offerTariffName: "Персоналка по умолчанию",
        offerPrice: "2500.00",
        offerDigest: "slot-offer-v1",
      },
      unifiedClientJourneyEnabled: true,
    });

    await screen.findByRole("region", { name: "Итог персональной записи" });
    await waitFor(() => expect(screen.getByRole("button", { name: modeLabel })).toBeEnabled());
    fireEvent.click(await screen.findByRole("button", { name: modeLabel }));
    fireEvent.click(screen.getByRole("button", { name: submitLabel }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/personal-availability/slots/81/staff-intents/",
        expect.objectContaining({
          student_id: 7,
          payment_method: paymentMethod,
          offer_digest: "slot-offer-v1",
        }),
      );
    });
    expect(post.mock.calls[0][1]).not.toHaveProperty("tariff_id");
    expect(post.mock.calls[0][1]).not.toHaveProperty("discount_ids");
  });

  it("sends entitlement through the coordinator without a paid-offer digest", async () => {
    post.mockResolvedValueOnce({ data: staffReceipt("entitlement") });
    renderSheet({
      fixedSlot: {
        ...fixedSlot,
        offerTariffId: 44,
        offerTariffName: "Персоналка по умолчанию",
        offerPrice: "2500.00",
        offerDigest: "slot-offer-v1",
      },
      subscriptions: [hybridEntitlement],
      unifiedClientJourneyEnabled: true,
    });

    fireEvent.click(await screen.findByRole("button", { name: "По абонементу" }));
    fireEvent.change(screen.getByLabelText("Абонемент *"), { target: { value: "57" } });
    fireEvent.click(screen.getByRole("button", { name: "Записать Маша Иванова по абонементу" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/personal-availability/slots/81/staff-intents/",
        expect.objectContaining({
          student_id: 7,
          payment_method: "entitlement",
          subscription_id: 57,
        }),
      );
    });
    expect(post.mock.calls[0][1]).not.toHaveProperty("offer_digest");
    expect(post.mock.calls[0][1]).not.toHaveProperty("discount_id");
  });

  it("replays an unchanged flag-on intent with one key and rotates it after the offer changes", async () => {
    fixedOfferResponse = {
      offer_tariff_id: 44,
      offer_tariff_name: "Персоналка по умолчанию",
      offer_price: "2500.00",
      offer_digest: "old-slot-offer-v1",
    };
    post
      .mockRejectedValueOnce({ response: { data: { code: "idempotency_conflict" } } })
      .mockRejectedValueOnce({
        response: {
          data: {
            code: "personal_offer_changed",
            current_offer: {
              offer_tariff_id: 45,
              offer_tariff_name: "Обновлённая персоналка",
              offer_price: "2700.00",
              offer_digest: "new-slot-offer-v1",
            },
          },
        },
      });
    renderSheet({
      fixedSlot: {
        ...fixedSlot,
        offerTariffId: 44,
        offerTariffName: "Персоналка по умолчанию",
        offerPrice: "2500.00",
        offerDigest: "old-slot-offer-v1",
      },
      unifiedClientJourneyEnabled: true,
    });

    await screen.findByRole("region", { name: "Итог персональной записи" });
    await waitFor(() => expect(screen.getByRole("button", { name: "Перевод" })).toBeEnabled());
    fireEvent.click(await screen.findByRole("button", { name: "Перевод" }));
    fireEvent.click(screen.getByRole("button", { name: /Записать Маша Иванова, перевод,/ }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: /Записать Маша Иванова, перевод,/ }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));

    const sameFingerprintKey = post.mock.calls[0][1].idempotency_key;
    expect(post.mock.calls[1][1].idempotency_key).toBe(sameFingerprintKey);
    expect(post.mock.calls[1][1].offer_digest).toBe("old-slot-offer-v1");

    expect((await screen.findAllByText(/2\s*700\s*₽/)).length).toBeGreaterThan(1);
    post.mockResolvedValueOnce({ data: staffReceipt("transfer") });
    fireEvent.click(screen.getByRole("button", { name: /Записать Маша Иванова, перевод,/ }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(3));
    expect(post.mock.calls[2][1].idempotency_key).not.toBe(sameFingerprintKey);
    expect(post.mock.calls[2][1].offer_digest).toBe("new-slot-offer-v1");
  });

  it("shows owner setup guidance instead of publishing a commercially unusable fixed slot", async () => {
    fixedOfferResponse = { offer_error_code: "personal_booking_tariff_not_configured" };
    renderSheet({
      fixedSlot: {
        ...fixedSlot,
        offerErrorCode: "personal_booking_tariff_not_configured",
      },
      unifiedClientJourneyEnabled: true,
    });

    expect(await screen.findByText("Нет корректной цены для персоналки. Попросите владельца настроить тариф.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Оплата через СБП" })).toBeDisabled();
  });

  it("keeps the sheet open and asks for confirmation again when a displayed offer becomes stale", async () => {
    fixedOfferResponse = {
      offer_tariff_id: 44,
      offer_tariff_name: "Персоналка по назначенной цене",
      offer_price: "2500.00",
      offer_digest: "old-slot-offer-v1",
    };
    post.mockRejectedValueOnce({
      response: {
        data: {
          code: "personal_offer_changed",
          current_offer: {
            offer_tariff_id: 45,
            offer_tariff_name: "Обновлённая персоналка",
            offer_price: "2700.00",
            offer_digest: "new-slot-offer-v1",
          },
        },
      },
    });
    const onBooked = vi.fn();
    renderSheet({
      fixedSlot: {
        ...fixedSlot,
        offerTariffId: 44,
        offerTariffName: "Персоналка по назначенной цене",
        offerPrice: "2500.00",
        offerDigest: "old-slot-offer-v1",
      },
      onBooked,
      unifiedClientJourneyEnabled: true,
    });

    await screen.findByRole("region", { name: "Итог персональной записи" });
    await waitFor(() => expect(screen.getByRole("button", { name: "Оплата при посещении" })).toBeEnabled());
    fireEvent.click(await screen.findByRole("button", { name: "Оплата при посещении" }));
    fireEvent.click(screen.getByRole("button", { name: /Записать Маша Иванова, оплата при посещении,/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Цена или условия персоналки изменились",
    );
    expect(onBooked).not.toHaveBeenCalled();

    expect((await screen.findAllByText(/2\s*700\s*₽/)).length).toBeGreaterThan(1);
    post.mockResolvedValueOnce({ data: staffReceipt("pay_at_visit") });
    fireEvent.click(screen.getByRole("button", { name: /Записать Маша Иванова, оплата при посещении,/ }));

    await waitFor(() => {
      expect(post).toHaveBeenLastCalledWith(
        "/personal-availability/slots/81/staff-intents/",
        expect.objectContaining({
          payment_method: "pay_at_visit",
          offer_digest: "new-slot-offer-v1",
        }),
      );
    });
  });

  it("does not let an entitlement for another personal type hide pay-at-club for a fixed slot", async () => {
    renderSheet({
      fixedSlot,
      subscriptions: [
        {
          ...entitlement,
          id: 56,
          training_type_id: 99,
          training_type_name: "Другая персоналка",
        },
      ],
    });

    expect(await screen.findByRole("button", { name: "Оплата в клубе" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.queryByRole("button", { name: "По абонементу" })).not.toBeInTheDocument();
  });

  it("shows pay-at-club when the selected booking date is after subscription expiry", async () => {
    renderSheet({
      subscriptions: [
        {
          ...entitlement,
          expires_at: "2099-07-06T00:00:00Z",
        },
      ],
    });
    await screen.findByRole("button", { name: "По абонементу" });

    fireEvent.change(screen.getByLabelText("Дата *"), { target: { value: "2099-07-07" } });

    expect(screen.queryByRole("button", { name: "По абонементу" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Оплата в клубе" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("shows pay-at-club when a hybrid personal component is scoped to another location", async () => {
    renderSheet({ fixedSlot, subscriptions: [wrongLocationHybridEntitlement] });

    expect(await screen.findByRole("button", { name: "Оплата в клубе" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.queryByRole("button", { name: "По абонементу" })).not.toBeInTheDocument();
  });

  it("shows pay-at-club when the selected week's component limit is exhausted", async () => {
    renderSheet({ fixedSlot, subscriptions: [exhaustedWeeklyHybridEntitlement] });

    expect(await screen.findByRole("button", { name: "Оплата в клубе" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: "Оплата через СБП" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "По абонементу" })).not.toBeInTheDocument();
  });

  it("does not fall back to a top-level entitlement when only inactive components remain", async () => {
    renderSheet({ subscriptions: [inactiveOnlyComponentSubscription] });

    expect(await screen.findByRole("button", { name: "Оплата в клубе" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: "Оплата через СБП" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "По абонементу" })).not.toBeInTheDocument();
  });

  it("accepts a hybrid personal component only when it matches the fixed slot type", async () => {
    renderSheet({ fixedSlot, subscriptions: [hybridEntitlement] });

    expect(await screen.findByRole("button", { name: "По абонементу" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.change(screen.getByLabelText("Абонемент *"), { target: { value: "57" } });
    fireEvent.click(screen.getByRole("button", { name: "Записать" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/personal-availability/slots/81/book-client/",
        expect.objectContaining({ student_id: 7, subscription_id: 57 }),
      );
    });
  });

  it("uses one non-deterministic idempotency key for retries, then creates a new key after reopening", async () => {
    const first = renderSheet();
    await screen.findByRole("button", { name: "Оплата в клубе" });
    selectPayAtClubAndFillDirectContext();
    fireEvent.click(screen.getByRole("button", { name: "Записать с оплатой в клубе" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: "Записать с оплатой в клубе" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    const firstKey = post.mock.calls[0][1].idempotency_key;
    expect(post.mock.calls[1][1].idempotency_key).toBe(firstKey);

    first.unmount();
    post.mockClear();
    renderSheet();
    await screen.findByRole("button", { name: "Оплата в клубе" });
    selectPayAtClubAndFillDirectContext();
    fireEvent.click(screen.getByRole("button", { name: "Записать с оплатой в клубе" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(post.mock.calls[0][1].idempotency_key).not.toBe(firstKey);
  });

  it("keeps an online retry key stable after a pending conflict and maps the server error", async () => {
    post
      .mockRejectedValueOnce({
        response: { data: { code: "bank_payment_order_pending_exists" } },
      })
      .mockResolvedValueOnce({
        data: {
          id: 91,
          tariff_id: 33,
          tariff_name: "Разовая персоналка",
          starts_at: "2099-07-07T10:00:00",
          ends_at: "2099-07-07T11:00:00",
          status: "pending_payment",
          expires_at: "2099-07-07T12:00:00Z",
          bank_payment_order_id: 92,
          subscription_id: null,
          provider_payment_url: "https://bank.example/pay/92",
          amount_snapshot: "2000.00",
          order_status: "pending",
          can_cancel: true,
        },
      });
    renderSheet();
    await screen.findByRole("button", { name: "Оплата через СБП" });
    fireEvent.click(screen.getByRole("button", { name: "Оплата через СБП" }));
    selectPayAtClubAndFillDirectContext();
    fireEvent.click(screen.getByRole("button", { name: "Создать ссылку СБП" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Оплата ожидает подтверждения. Завершите или отмените текущую попытку.",
    );
    const firstKey = post.mock.calls[0][1].idempotency_key;
    fireEvent.click(screen.getByRole("button", { name: "Создать ссылку СБП" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(post.mock.calls[1][1].idempotency_key).toBe(firstKey);
  });

  it("shows one pending-payment panel when the created reservation is returned by the refetch", async () => {
    const reservation = {
      id: 91,
      tariff_id: 33,
      tariff_name: "Разовая персоналка",
      starts_at: "2099-07-07T10:00:00",
      ends_at: "2099-07-07T11:00:00",
      status: "pending_payment",
      expires_at: "2099-07-07T12:00:00Z",
      bank_payment_order_id: 92,
      subscription_id: null,
      provider_payment_url: "https://bank.example/pay/92",
      amount_snapshot: "2000.00",
      order_status: "pending",
      can_cancel: true,
    };
    post.mockImplementation((url: string) => {
      if (url === "/students/7/personal-booking-payment-reservations/") {
        pendingPaymentReservations = [reservation];
        return Promise.resolve({ data: reservation });
      }
      return Promise.resolve({ data: { id: 9, price_snapshot: "2000.00" } });
    });

    renderSheet();
    await screen.findByRole("button", { name: "Оплата через СБП" });
    fireEvent.click(screen.getByRole("button", { name: "Оплата через СБП" }));
    await waitFor(() => {
      expect(get).toHaveBeenCalledWith(
        "/students/7/personal-booking-payment-reservations/",
        { params: { status: "open_actionable" } },
      );
    });
    selectPayAtClubAndFillDirectContext();
    fireEvent.click(screen.getByRole("button", { name: "Создать ссылку СБП" }));

    await waitFor(() => {
      expect(
        get.mock.calls.filter(
          ([url]) => url === "/students/7/personal-booking-payment-reservations/",
        ),
      ).toHaveLength(2);
    });
    expect(
      await screen.findAllByRole("region", { name: "Оплата ожидает подтверждения" }),
    ).toHaveLength(1);
  });

  it("shows manual review without looking up a missing payment order", async () => {
    pendingPaymentReservations = [
      {
        id: 93,
        tariff_id: 33,
        tariff_name: "Разовая персоналка",
        starts_at: "2099-07-07T10:00:00",
        ends_at: "2099-07-07T11:00:00",
        status: "manual_review",
        expires_at: "2099-07-07T12:00:00Z",
        bank_payment_order_id: null,
        subscription_id: null,
        provider_payment_url: "",
        amount_snapshot: "2000.00",
        order_status: "approved",
        can_cancel: false,
      },
    ];
    renderSheet();

    expect(await screen.findByText("Оплата под защищённой проверкой")).toBeInTheDocument();
    expect(screen.getByText(/Не создавайте новую ссылку/)).toBeInTheDocument();
    expect(get).not.toHaveBeenCalledWith("/billing/bank-payment-orders/null/");
  });

  it("looks up the exact manual-review order and keeps refresh available", async () => {
    pendingPaymentReservations = [
      {
        id: 94,
        tariff_id: 33,
        tariff_name: "Разовая персоналка",
        starts_at: "2099-07-07T10:00:00",
        ends_at: "2099-07-07T11:00:00",
        status: "manual_review",
        expires_at: "2099-07-07T12:00:00Z",
        bank_payment_order_id: 92,
        subscription_id: null,
        provider_payment_url: "",
        amount_snapshot: "2000.00",
        order_status: "manual_review",
        can_cancel: false,
      },
    ];
    get.mockImplementation((url: string) => {
      if (url === "/students/7/personal-booking-payment-reservations/") {
        return Promise.resolve({ data: pendingPaymentReservations });
      }
      if (url === "/billing/bank-payment-orders/92/") {
        return Promise.resolve({
          data: {
            id: 92,
            subscription_id: 54,
            student_id: 7,
            tariff_id: 33,
            debt_ids: [],
            source: "trainer",
            status: "manual_review",
            amount_snapshot: "2000.00",
            currency: "RUB",
            purpose_snapshot: "Разовая персоналка",
            provider_payment_url: "",
            expires_at: "2099-07-07T12:00:00Z",
            can_pay: false,
            can_share: false,
            can_copy: false,
            can_show_qr: false,
            can_request_refresh: true,
            can_cancel: false,
          },
        });
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });
    renderSheet();

    expect(
      await screen.findByRole("region", { name: "Оплата под защищённой проверкой" }),
    ).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith("/billing/bank-payment-orders/92/");
    fireEvent.click(screen.getByRole("button", { name: "Обновить статус" }));
    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/billing/bank-payment-orders/92/refresh/", {});
    });
  });

  it("maps the drop-in tariff contract error to the configuration guidance", async () => {
    post.mockRejectedValueOnce({
      response: { data: { code: "personal_drop_in_tariff_invalid" } },
    });
    renderSheet();
    await screen.findByRole("button", { name: "Оплата в клубе" });
    selectPayAtClubAndFillDirectContext();
    fireEvent.click(screen.getByRole("button", { name: "Записать с оплатой в клубе" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Разовая персоналка настроена не полностью. Попросите владельца проверить тариф, цену и ставку тренера.",
    );
  });
});
