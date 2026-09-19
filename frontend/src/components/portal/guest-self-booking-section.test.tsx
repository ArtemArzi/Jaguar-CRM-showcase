import { fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "@/features/auth/auth-store";
import {
  GuestSelfBookingSheet,
  type GuestBookingDateOption,
  type PersonalAvailabilityOption,
  type PersonalBookingPaymentReservation,
} from "./guest-self-booking-section";

const { get } = vi.hoisted(() => ({ get: vi.fn() }));

vi.mock("@/api/custom-fetch", () => ({ default: { get } }));

function renderGuest(children: ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={queryClient}>{children}</QueryClientProvider>);
}

const dateOptions: GuestBookingDateOption[] = [
  {
    value: "2026-04-13",
    label: "13 апреля",
    shortLabel: "Пн",
  },
];

function personalReservation(
  overrides: Partial<PersonalBookingPaymentReservation> = {},
): PersonalBookingPaymentReservation {
  return {
    id: 31,
    student_id: 7,
    trainer_id: 3,
    trainer_name: "Иван Петров",
    location_id: 2,
    location_name: "Основной зал",
    training_type_id: 8,
    training_type_name: "Персональная тренировка",
    tariff_id: 12,
    tariff_name: "Разовая персоналка",
    availability_slot_id: null,
    starts_at: "2026-04-13T10:00:00+05:00",
    ends_at: "2026-04-13T11:00:00+05:00",
    status: "manual_review",
    expires_at: "2099-04-13T11:15:00+05:00",
    payment_id: null,
    bank_payment_order_id: 91,
    subscription_id: null,
    schedule_id: null,
    enrollment_id: null,
    provider_payment_url: "https://pay.example/personal-31",
    amount_snapshot: "2000.00",
    order_status: "approved",
    can_cancel: false,
    created_at: "2026-04-13T08:45:00+05:00",
    ...overrides,
  };
}

function personalOption(overrides: Partial<PersonalAvailabilityOption> = {}): PersonalAvailabilityOption {
  return {
    slot_id: 56,
    date: "2026-04-13",
    starts_at: "2026-04-13T10:00:00+05:00",
    ends_at: "2026-04-13T11:00:00+05:00",
    trainer_id: 3,
    trainer_name: "Иван Петров",
    location_id: 2,
    location_name: "Основной зал",
    training_type_id: 8,
    training_type_name: "Персональная тренировка",
    booking_status: "can_pay",
    reason_code: "payment_required",
    subscription_id: null,
    payment_tariff_id: 12,
    payment_tariff_name: "Разовая персоналка",
    payment_amount: "2000.00",
    ...overrides,
  };
}

describe("GuestSelfBookingSheet", () => {
  beforeEach(() => {
    useAuthStore.setState({
      accessToken: "header.eyJzdWIiOiJzdHVkZW50LTcifQ.signature",
      clubId: 1,
      role: "student",
      isAuthenticated: true,
    });
  });

  it("shows manual-review reservations with the exact refreshable order", async () => {
    get.mockReset();
    get.mockResolvedValue({
      data: {
        id: 91,
        provider: "mock",
        subscription_id: 22,
        student_id: 7,
        tariff_id: 12,
        debt_ids: [],
        status: "manual_review",
        amount_snapshot: "2000.00",
        currency: "RUB",
        purpose_snapshot: "Персональная тренировка",
        expires_at: "2099-04-13T11:15:00+05:00",
        payment_status: "pending",
        subscription_status: "pending",
        can_request_refresh: true,
      },
    });
    renderGuest(
      <GuestSelfBookingSheet
        open
        onOpenChange={vi.fn()}
        ariaLabel="Самозапись"
        eyebrow="Группа"
        title="Выберите группу"
        description="Выберите доступное занятие"
        dateOptions={dateOptions}
        selectedDate="2026-04-13"
        onDateChange={vi.fn()}
        options={[]}
        onBook={vi.fn()}
        personalOptions={[]}
        onPersonalBook={vi.fn()}
        personalPaymentReservations={[personalReservation()]}
      />,
    );

    fireEvent.click(screen.getByText("Персоналка"));

    const fallbackCard = screen.getByRole("group", {
      name: /Персональный слот Персональная тренировка/,
    });

    expect(within(fallbackCard).getByText("На проверке")).toBeInTheDocument();
    expect(within(fallbackCard).getByText("Проверяем оплату")).toBeInTheDocument();
    expect(within(fallbackCard).getByText("Оплата под защищённой проверкой")).toBeInTheDocument();
    expect(within(fallbackCard).getByText(/Не повторяйте оплату/)).toBeInTheDocument();
    expect((await within(fallbackCard).findAllByText("Проверка")).length).toBeGreaterThan(0);
    expect(within(fallbackCard).queryByText("Оплата получена")).not.toBeInTheDocument();
    expect(get).toHaveBeenCalledWith("/students/me/bank-payment-orders/91/");
  });

  it("attaches a reservation to the matching personal slot without blocking other slots", () => {
    const matchingOption = personalOption();
    const otherOption = personalOption({
      slot_id: 57,
      starts_at: "2026-04-13T12:00:00+05:00",
      ends_at: "2026-04-13T13:00:00+05:00",
    });

    get.mockResolvedValue({
      data: {
        id: 91,
        subscription_id: null,
        student_id: 7,
        tariff_id: 12,
        debt_ids: [],
        status: "pending",
        amount_snapshot: "2000.00",
        currency: "RUB",
        expires_at: "2099-04-13T11:15:00+05:00",
        can_pay: true,
      },
    });
    renderGuest(
      <GuestSelfBookingSheet
        open
        onOpenChange={vi.fn()}
        ariaLabel="Самозапись"
        eyebrow="Группа"
        title="Выберите группу"
        description="Выберите доступное занятие"
        dateOptions={dateOptions}
        selectedDate="2026-04-13"
        onDateChange={vi.fn()}
        options={[]}
        onBook={vi.fn()}
        personalOptions={[matchingOption, otherOption]}
        onPersonalBook={vi.fn()}
        onlinePaymentsEnabled
        personalPaymentReservations={[
          personalReservation({
            availability_slot_id: null,
            status: "pending_payment",
            order_status: "pending",
            can_cancel: true,
          }),
        ]}
      />,
    );

    fireEvent.click(screen.getByText("Персоналка"));

    const matchingCard = screen.getByRole("group", {
      name: /Персональный слот Персональная тренировка 10:00 Иван Петров/,
    });
    const otherCard = screen.getByRole("group", {
      name: /Персональный слот Персональная тренировка 12:00 Иван Петров/,
    });

    expect(within(matchingCard).getByText("Ссылка уже создана")).toBeInTheDocument();
    expect(within(matchingCard).getByRole("button", { name: /Ссылка создана/ })).toBeDisabled();
    expect(within(otherCard).getByRole("button", { name: /Оплатить/ })).toBeEnabled();
  });
});
