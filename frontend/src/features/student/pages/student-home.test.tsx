import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import StudentHome from "./student-home";
import { useAuthStore } from "@/features/auth/auth-store";
import {
  ENABLED_SBP_PAYMENT_CAPABILITIES,
  getPaymentCapabilitiesQueryKey,
} from "@/api/payment-capabilities";
import { useBrandingStore } from "@/features/branding/use-branding";
import { todayInTimeZone } from "@/lib/club-date";
import { getMonday, toDateParam } from "../lib/student-schedule-utils";
import {
  getPersonalAvailabilityCapabilityQueryKey,
  getUnifiedClientJourneyCapabilityQueryKey,
} from "@/api/unified-client-journey";

const { get, post } = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}));

vi.mock("@/api/custom-fetch", () => ({
  default: { get, post },
}));

vi.mock("../components/subscription-card", () => ({
  SubscriptionCard: ({
    tariffName,
    freezeStatus,
    status,
  }: {
    tariffName: string;
    freezeStatus?: string | null;
    status: string;
  }) => (
    <div>
      subscription:{tariffName}
      <span>status:{status}</span>
      {freezeStatus ? <span>freeze:{freezeStatus}</span> : null}
    </div>
  ),
}));

vi.mock("../components/grade-card", () => ({
  GradeCard: ({
    systemName,
    variant,
  }: {
    systemName: string;
    variant?: string;
  }) => (
    <div>
      grade:{variant ?? "default"}:{systemName}
    </div>
  ),
}));

vi.mock("@/features/notifications/components/push-permission-banner", () => ({
  default: () => <div>push-banner</div>,
}));

function renderHomePage(
  paymentCapabilities: unknown = ENABLED_SBP_PAYMENT_CAPABILITIES,
  {
    unifiedClientJourneyEnabled = false,
    contextualRenewalEnabled = false,
    skipPersonalAvailabilityCapabilitySeed = false,
  }: {
    unifiedClientJourneyEnabled?: boolean;
    contextualRenewalEnabled?: boolean | "error";
    skipPersonalAvailabilityCapabilitySeed?: boolean;
  } = {},
) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });
  if (paymentCapabilities !== undefined) {
    queryClient.setQueryData(getPaymentCapabilitiesQueryKey(), paymentCapabilities);
  }
  if (!skipPersonalAvailabilityCapabilitySeed) {
    queryClient.setQueryData(getPersonalAvailabilityCapabilityQueryKey(
      1,
      useAuthStore.getState().accessToken,
      useAuthStore.getState().role,
    ), {
      enabled: unifiedClientJourneyEnabled,
      staff_command_protocol_version: "v1",
    });
  }
  if (contextualRenewalEnabled !== "error") {
    queryClient.setQueryData(getUnifiedClientJourneyCapabilityQueryKey(1), {
      enabled: contextualRenewalEnabled,
    });
  }

  return render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <StudentHome />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

describe("StudentHome", () => {
  beforeEach(() => {
    localStorage.clear();
    get.mockReset();
    post.mockReset();
    post.mockResolvedValue({
      data: {
        id: 44,
        tariff_id: 3,
        debt_ids: [],
        payment_id: 94,
        subscription_id: 54,
        student_id: 7,
        provider: "mock",
        source: "student",
        status: "pending",
        amount_snapshot: "5000.00",
        currency: "RUB",
        purpose_snapshot: "Абонемент Base",
        provider_payment_link_id: "jgr-44-test",
        provider_payment_url: "https://pay.example/jgr-44-test",
        can_pay: true,
        can_request_refresh: true,
        provider_payment_modes: ["card"],
        provider_status: "CREATED",
        expires_at: "2099-06-28T12:00:00Z",
        paid_at: null,
      },
    });
    useBrandingStore.setState({
      timeZone: "Asia/Yekaterinburg",
      timeZoneStatus: "ready",
      timeZoneClubId: 1,
      isTimeZoneAuthoritative: true,
    });
    useAuthStore.setState({
      accessToken: "header.eyJzdWIiOiJzdHVkZW50LTEifQ.signature",
      refreshToken: null,
      role: "student",
      clubId: 1,
      trainerId: null,
      studentId: 7,
      isAuthenticated: true,
      studentBootstrapStatus: "resolved",
      studentBootstrapError: null,
    });
  });

  it("loads persistent self-service personal history from the capability-on source", async () => {
    get.mockImplementation((url: string) => {
      if (
        url === "/students/me/subscriptions/" ||
        url === "/students/me/debts/" ||
        url === "/students/me/financial-state/" ||
        url === "/grades/my-progress/" ||
        url === "/students/me/schedule-week/" ||
        url === "/students/me/bank-payment-orders/"
      ) {
        return Promise.resolve({ data: [] });
      }
      if (url === "/personal-availability/self-service/commands/") {
        return Promise.resolve({
          data: {
            live: [],
            latest_terminal: [
              {
                command_id: 12,
                slot_id: 77,
                capability: "can_pay",
                status: "expired",
                starts_at: "2026-08-20T10:00:00+05:00",
                ends_at: "2026-08-20T11:00:00+05:00",
                booking_id: null,
                reservation_id: 11,
                bank_payment_order_id: 22,
                provider_payment_url: "",
                amount_snapshot: "2500.00",
                order_status: "expired",
                allowed_actions: [],
              },
            ],
          },
        });
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    renderHomePage(ENABLED_SBP_PAYMENT_CAPABILITIES, { unifiedClientJourneyEnabled: true });

    expect(await screen.findByText("Срок ссылки истёк")).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith("/personal-availability/self-service/commands/", {
      params: undefined,
    });
    expect(get).not.toHaveBeenCalledWith(
      "/personal-availability/payment-reservations/",
      expect.anything(),
    );
  });

  it("fails closed on a capability error without starting a personal payment flow", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/personal-availability/capability/") return Promise.reject(new Error("offline"));
      if (
        url === "/students/me/subscriptions/" ||
        url === "/students/me/debts/" ||
        url === "/grades/my-progress/" ||
        url === "/students/me/schedule-week/" ||
        url === "/students/me/bank-payment-orders/"
      ) {
        return Promise.resolve({ data: [] });
      }
      if (url === "/students/me/financial-state/") return Promise.resolve({ data: {} });
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    renderHomePage(ENABLED_SBP_PAYMENT_CAPABILITIES, {
      skipPersonalAvailabilityCapabilitySeed: true,
    });

    expect(
      await screen.findByText("Персональная запись временно недоступна"),
    ).toBeInTheDocument();
    expect(
      get.mock.calls.filter(([url]) =>
        [
          "/personal-availability/options/",
          "/personal-availability/payment-reservations/",
          "/personal-availability/self-service/options/",
          "/personal-availability/self-service/commands/",
        ].includes(url),
      ),
    ).toEqual([]);
    expect(post).not.toHaveBeenCalled();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("uses the club time zone when choosing home schedule weeks", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date("2026-06-28T20:30:00.000Z"));
    const requestedWeekStarts: string[] = [];
    get.mockImplementation((url: string, config?: { params?: { week_start?: string } }) => {
      if (url === "/students/me/subscriptions/") {
        return Promise.resolve({ data: [] });
      }
      if (url === "/students/me/debts/") {
        return Promise.resolve({ data: [] });
      }
      if (url === "/grades/my-progress/") {
        return Promise.resolve({ data: [] });
      }
      if (url === "/students/me/schedule-week/") {
        requestedWeekStarts.push(config?.params?.week_start ?? "");
        return Promise.resolve({ data: [] });
      }
      if (url === "/personal-availability/payment-reservations/") {
        return Promise.resolve({ data: [] });
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    renderHomePage();

    await waitFor(() => {
      expect(requestedWeekStarts).toEqual(expect.arrayContaining(["2026-06-29", "2026-07-06"]));
    });
    expect(requestedWeekStarts).not.toContain("2026-06-22");
  });

  it("renders the next effective occurrence from weekly schedule data", async () => {
    const currentMonday = getMonday(todayInTimeZone("Asia/Yekaterinburg"));
    const nextMonday = new Date(currentMonday);
    nextMonday.setDate(currentMonday.getDate() + 7);
    const nextMondayParam = toDateParam(nextMonday);

    get.mockImplementation((url: string, config?: { params?: { week_start?: string } }) => {
      if (url === "/students/me/subscriptions/") {
        return Promise.resolve({
          data: [
            {
              id: 1,
              tariff_name: "Base",
              trainings_used: 2,
              trainings_total: 8,
              trainings_left: 6,
              expires_at: "2026-05-01",
              status: "active",
            },
          ],
        });
      }

      if (url === "/students/me/bank-payment-orders/44/") {
        return Promise.resolve({
          data: {
            id: 44,
            payment_id: 5,
            subscription_id: null,
            student_id: 7,
            tariff_id: 12,
            debt_ids: [],
            source: "student",
            status: "pending",
            amount_snapshot: "2000.00",
            currency: "RUB",
            purpose_snapshot: "Разовая персоналка",
            provider_payment_url: "https://pay.example/personal",
            expires_at: "2099-04-13T12:00:00Z",
            can_pay: true,
            can_share: false,
            can_copy: false,
            can_show_qr: false,
            can_request_refresh: true,
            can_cancel: true,
          },
        });
      }

      if (url === "/students/me/debts/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/grades/my-progress/") {
        return Promise.resolve({ data: [] });
      }

      if (
        url === "/students/me/schedule-week/" &&
        config?.params?.week_start === toDateParam(currentMonday)
      ) {
        return Promise.resolve({ data: [] });
      }

      if (
        url === "/students/me/schedule-week/" &&
        config?.params?.week_start === nextMondayParam
      ) {
        return Promise.resolve({
          data: [
            {
              schedule_id: 10,
              group_name: "Karate",
              effective_date: nextMondayParam,
              effective_start_time: "18:00:00",
              effective_end_time: "19:00:00",
              trainer_name: "Alex Backup",
              location_name: "Blue Hall",
              is_substitute: true,
              is_rescheduled: false,
            },
          ],
        });
      }

      return Promise.reject(
        new Error(`Unexpected request: ${url} ${config?.params?.week_start ?? ""}`),
      );
    });

    renderHomePage();

    expect(await screen.findByText("Личный кабинет")).toBeInTheDocument();
    expect(await screen.findByText("grade:hero:")).toBeInTheDocument();
    expect(screen.getByText("subscription:Base")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /ближайшие тренировки и переносы/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /отметки и журнал занятий/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /обратная связь после тренировки/i })).toHaveAttribute(
      "href",
      "/student/feedback",
    );
    expect(screen.getByRole("link", { name: /данные, документы и уведомления/i })).toBeInTheDocument();
    expect(await screen.findByText("Karate")).toBeInTheDocument();
    expect(screen.getByText(/18:00–19:00/)).toBeInTheDocument();
    expect(screen.getByText("Alex Backup")).toBeInTheDocument();
    expect(screen.getByText("Blue Hall")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /открыть расписание/i })).toBeInTheDocument();
  });

  it("shows pending personal payment before next group and personal cards", async () => {
    const currentMonday = getMonday(todayInTimeZone("Asia/Yekaterinburg"));
    const nextMonday = new Date(currentMonday);
    nextMonday.setDate(currentMonday.getDate() + 7);
    const nextMondayParam = toDateParam(nextMonday);

    get.mockImplementation((url: string, config?: { params?: { week_start?: string } }) => {
      if (url === "/students/me/subscriptions/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/students/me/debts/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/grades/my-progress/") {
        return Promise.resolve({ data: [] });
      }

      if (
        url === "/students/me/schedule-week/" &&
        config?.params?.week_start === toDateParam(currentMonday)
      ) {
        return Promise.resolve({ data: [] });
      }

      if (
        url === "/students/me/schedule-week/" &&
        config?.params?.week_start === nextMondayParam
      ) {
        return Promise.resolve({
          data: [
            {
              schedule_id: 10,
              group_name: "Kids Group",
              effective_date: nextMondayParam,
              effective_start_time: "18:00:00",
              effective_end_time: "19:00:00",
              trainer_name: "Group Coach",
              location_name: "Blue Hall",
              training_type_kind: "group",
            },
            {
              schedule_id: 11,
              group_name: "Personal Session",
              effective_date: nextMondayParam,
              effective_start_time: "12:00:00",
              effective_end_time: "13:00:00",
              trainer_name: "Personal Coach",
              location_name: "Small Hall",
              training_type_kind: "personal",
              created_from: "personal_booking",
            },
          ],
        });
      }

      if (url === "/personal-availability/payment-reservations/") {
        return Promise.resolve({
          data: [
            {
              id: 71,
              student_id: 7,
              trainer_id: 8,
              trainer_name: "Personal Coach",
              location_id: 3,
              location_name: "Small Hall",
              training_type_id: 4,
              training_type_name: "Personal Boxing",
              tariff_id: 12,
              tariff_name: "Разовая персоналка",
              availability_slot_id: 99,
              starts_at: "2026-04-14T12:00:00Z",
              ends_at: "2026-04-14T13:00:00Z",
              status: "pending_payment",
              expires_at: "2099-04-13T12:00:00Z",
              payment_id: 5,
              bank_payment_order_id: 44,
              subscription_id: null,
              schedule_id: null,
              enrollment_id: null,
              provider_payment_url: "https://pay.example/personal",
        can_pay: true,
        can_request_refresh: true,
              amount_snapshot: "2000.00",
              order_status: "pending",
              can_cancel: true,
              created_at: "2026-04-13T10:00:00Z",
            },
            {
              id: 72,
              student_id: 7,
              trainer_id: 8,
              trainer_name: "Personal Coach",
              location_id: 3,
              location_name: "Small Hall",
              training_type_id: 4,
              training_type_name: "Paid Personal Boxing",
              tariff_id: 12,
              tariff_name: "Разовая персоналка",
              availability_slot_id: 100,
              starts_at: "2026-04-15T12:00:00Z",
              ends_at: "2026-04-15T13:00:00Z",
              status: "pending_payment",
              expires_at: "2026-04-14T12:00:00Z",
              payment_id: 6,
              bank_payment_order_id: 45,
              subscription_id: null,
              schedule_id: null,
              enrollment_id: null,
              provider_payment_url: "https://pay.example/paid-personal",
        can_pay: true,
        can_request_refresh: true,
              amount_snapshot: "2000.00",
              order_status: "approved",
              can_cancel: false,
              created_at: "2026-04-14T10:00:00Z",
            },
          ],
        });
      }

      if (url === "/students/me/bank-payment-orders/44/") {
        return Promise.resolve({
          data: {
            id: 44,
            payment_id: 5,
            subscription_id: null,
            student_id: 7,
            tariff_id: 12,
            debt_ids: [],
            source: "student",
            status: "pending",
            amount_snapshot: "2000.00",
            currency: "RUB",
            purpose_snapshot: "Разовая персоналка",
            provider_payment_url: "https://pay.example/personal",
            expires_at: "2099-04-13T12:00:00Z",
            can_pay: true,
            can_share: false,
            can_copy: false,
            can_show_qr: false,
            can_request_refresh: true,
            can_cancel: true,
          },
        });
      }

      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    renderHomePage();

    expect(await screen.findByText("Персоналка ожидает оплаты")).toBeInTheDocument();
    expect(screen.getByText(/Personal Boxing/)).toBeInTheDocument();
    expect(screen.queryByText(/Paid Personal Boxing/)).not.toBeInTheDocument();
    expect(await screen.findByRole("link", { name: /Оплатить через СБП/ })).toHaveAttribute(
      "href",
      "https://pay.example/personal",
    );
    expect(screen.getByText("Следующая группа")).toBeInTheDocument();
    expect(screen.getByText("Kids Group")).toBeInTheDocument();
    expect(screen.getByText("Следующая персоналка")).toBeInTheDocument();
    expect(screen.getByText("Personal Session")).toBeInTheDocument();
  });

  it("renders a non-empty home state even when the student has no grade or subscription", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/students/me/subscriptions/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/students/me/debts/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/grades/my-progress/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/students/me/schedule-week/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/students/me/bank-payment-orders/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    renderHomePage();

    expect(await screen.findByText("Личный кабинет")).toBeInTheDocument();
    expect(await screen.findByText("grade:hero:")).toBeInTheDocument();
    expect(screen.getByText("Нет активного абонемента")).toBeInTheDocument();
    expect(
      screen.getByText("Пока нет ближайшего занятия"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Как только появится ближайшее занятие, карточка обновится автоматически."),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /открыть профиль/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /проверить расписание/i })).toBeInTheDocument();
  });

  it("shows open debt details from no-subscription check-ins", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/students/me/subscriptions/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/students/me/debts/") {
        return Promise.resolve({
          data: [
            {
              id: 11,
              checkin_id: 21,
              tariff_price: "1000.00",
              reason: "no_subscription",
              training_type_name: "Muay Thai",
              checkin_date: "2026-03-01",
              created_at: "2026-03-01T12:00:00Z",
            },
            {
              id: 12,
              checkin_id: 22,
              tariff_price: null,
              reason: "subscription_exhausted",
              training_type_name: "Boxing",
              checkin_date: "2026-03-03",
              created_at: "2026-03-03T12:00:00Z",
            },
          ],
        });
      }

      if (url === "/grades/my-progress/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/students/me/schedule-week/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/students/me/bank-payment-orders/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    renderHomePage();

    expect(await screen.findByText("Есть задолженность")).toBeInTheDocument();
    expect(screen.getByText("Muay Thai")).toBeInTheDocument();
    expect(screen.getByText(/1000/)).toBeInTheDocument();
    expect(screen.getByText("Нет подходящего абонемента")).toBeInTheDocument();
    expect(screen.getByText("Закончились тренировки по абонементу")).toBeInTheDocument();
    expect(screen.getByText("Тренировка: 01.03.2026")).toBeInTheDocument();
    expect(screen.getByText("Создано: 01.03.2026")).toBeInTheDocument();
    expect(screen.getByText("Boxing")).toBeInTheDocument();
    expect(screen.getByText("Сумма уточняется")).toBeInTheDocument();
    expect(screen.getByText("Тренировка: 03.03.2026")).toBeInTheDocument();
    expect(screen.getByText("Создано: 03.03.2026")).toBeInTheDocument();
    expect(screen.getByText("Открытых долгов: 2")).toBeInTheDocument();
  });

  it("renders a pending manual admission visit as covered and non-payable", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/students/me/financial-state/") {
        return Promise.resolve({
          data: {
            operational_admission: {
              payment_id: 91,
              payment_status: "pending",
              payment_method: "cash",
              subscription_status: "pending",
              enrollment_status: "active",
              group_label: "Tue Thu Group",
              start_date: "2026-07-22",
              checkin_ready: true,
              account_access_eligible: true,
              covered_visit_count: 1,
            },
            covered_visits: [
              {
                debt_id: 55,
                checkin_id: 66,
                training_type_name: "Group Boxing",
                checkin_date: "2026-07-22",
                coverage_state: "covered_awaiting_confirmation",
                is_payable: false,
              },
            ],
          },
        });
      }
      if (
        url === "/students/me/subscriptions/" ||
        url === "/students/me/debts/" ||
        url === "/grades/my-progress/" ||
        url === "/students/me/schedule-week/" ||
        url === "/students/me/bank-payment-orders/" ||
        url === "/personal-availability/payment-reservations/"
      ) {
        return Promise.resolve({ data: [] });
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    renderHomePage();

    expect(await screen.findByText("Оплата ожидает подтверждения")).toBeInTheDocument();
    expect(screen.getByText(/Tue Thu Group.*старт/)).toBeInTheDocument();
    expect(screen.getByText(/Group Boxing.*покрыто оплатой/)).toBeInTheDocument();
    expect(
      screen.getByText("Отдельная оплата и ссылка не нужны до подтверждения."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Есть задолженность")).not.toBeInTheDocument();
  });

  it("renders a personal v2 admission without treating it as a group", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/students/me/financial-state/") {
        return Promise.resolve({
          data: {
            operational_admission_v2: {
              kind: "personal",
              payment_id: 93,
              payment_status: "pending",
              payment_method: "cash",
              subscription_status: "pending",
              start_date: "2026-07-24",
              checkin_ready: true,
              account_access_eligible: true,
              is_qualifying: true,
              booking_id: 71,
              session_id: 81,
              booking_state: "scheduled",
            },
            covered_visits: [],
          },
        });
      }
      if (
        url === "/students/me/subscriptions/" ||
        url === "/students/me/debts/" ||
        url === "/grades/my-progress/" ||
        url === "/students/me/schedule-week/" ||
        url === "/students/me/bank-payment-orders/" ||
        url === "/personal-availability/payment-reservations/"
      ) {
        return Promise.resolve({ data: [] });
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    renderHomePage();

    expect(await screen.findByText("Оплата ожидает подтверждения")).toBeInTheDocument();
    expect(screen.getByText("Персональная запись · 24.07.2026")).toBeInTheDocument();
    expect(screen.queryByText(/Запись в группу/)).not.toBeInTheDocument();
  });

  it("renders every concurrent admission with only its payment-owned covered visit", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/students/me/financial-state/") {
        return Promise.resolve({
          data: {
            operational_admission: {
              payment_id: 92,
              payment_status: "pending",
              payment_method: "cash",
              subscription_status: "pending",
              enrollment_status: "active",
              group_label: "BJJ group",
              start_date: "2026-07-23",
              checkin_ready: true,
              account_access_eligible: true,
              covered_visit_count: 1,
            },
            operational_admissions: [
              {
                payment_id: 92,
                payment_status: "pending",
                payment_method: "cash",
                subscription_status: "pending",
                enrollment_status: "active",
                group_label: "BJJ group",
                start_date: "2026-07-23",
                checkin_ready: true,
                account_access_eligible: true,
                covered_visit_count: 1,
              },
              {
                payment_id: 91,
                payment_status: "pending",
                payment_method: "transfer",
                subscription_status: "pending",
                enrollment_status: "active",
                group_label: "Boxing group",
                start_date: "2026-07-22",
                checkin_ready: true,
                account_access_eligible: true,
                covered_visit_count: 1,
              },
            ],
            covered_visits: [
              {
                payment_id: 92,
                debt_id: 56,
                checkin_id: 67,
                training_type_name: "BJJ visit",
                checkin_date: "2026-07-23",
                coverage_state: "covered_awaiting_confirmation",
                is_payable: false,
              },
              {
                payment_id: 91,
                debt_id: 55,
                checkin_id: 66,
                training_type_name: "Boxing visit",
                checkin_date: "2026-07-22",
                coverage_state: "covered_awaiting_confirmation",
                is_payable: false,
              },
            ],
          },
        });
      }
      if (
        url === "/students/me/subscriptions/" ||
        url === "/students/me/debts/" ||
        url === "/grades/my-progress/" ||
        url === "/students/me/schedule-week/" ||
        url === "/students/me/bank-payment-orders/" ||
        url === "/personal-availability/payment-reservations/"
      ) {
        return Promise.resolve({ data: [] });
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    renderHomePage();

    expect(await screen.findByText(/BJJ group.*старт/)).toBeInTheDocument();
    expect(screen.getByText(/Boxing group.*старт/)).toBeInTheDocument();
    expect(screen.getByText(/BJJ visit.*покрыто оплатой/)).toBeInTheDocument();
    expect(screen.getByText(/Boxing visit.*покрыто оплатой/)).toBeInTheDocument();
    expect(screen.queryByText("Есть задолженность")).not.toBeInTheDocument();
  });

  it("renders rejection as released payable debt without future readiness", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/students/me/financial-state/") {
        return Promise.resolve({
          data: {
            operational_admission: {
              payment_id: 91,
              payment_status: "rejected",
              payment_method: "cash",
              subscription_status: "cancelled",
              enrollment_status: "cancelled",
              group_label: "Tue Thu Group",
              start_date: "2026-07-22",
              checkin_ready: false,
              account_access_eligible: false,
              covered_visit_count: 0,
            },
            covered_visits: [],
          },
        });
      }
      if (url === "/students/me/debts/") {
        return Promise.resolve({
          data: [
            {
              id: 55,
              checkin_id: 66,
              tariff_price: "1200.00",
              reason: "no_subscription",
              training_type_name: "Group Boxing",
              checkin_date: "2026-07-22",
              created_at: "2026-07-22T10:00:00Z",
            },
          ],
        });
      }
      if (
        url === "/students/me/subscriptions/" ||
        url === "/grades/my-progress/" ||
        url === "/students/me/schedule-week/" ||
        url === "/students/me/bank-payment-orders/" ||
        url === "/personal-availability/payment-reservations/"
      ) {
        return Promise.resolve({ data: [] });
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    renderHomePage();

    expect(await screen.findByText("Оплата отклонена")).toBeInTheDocument();
    expect(
      screen.getByText("Запись отменена; будущая готовность к чекину снята."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Tue Thu Group.*старт/)).not.toBeInTheDocument();
    expect(screen.queryByText(/покрыто оплатой/)).not.toBeInTheDocument();
    expect(screen.getByText("Есть задолженность")).toBeInTheDocument();
    expect(screen.getByText("Group Boxing")).toBeInTheDocument();
  });

  it("creates a student online renewal link without self-service debt settlement", async () => {
    const renewalOrder = {
      id: 44,
      tariff_id: 3,
      debt_ids: [],
      payment_id: 94,
      subscription_id: 54,
      student_id: 7,
      provider: "mock",
      source: "student",
      status: "pending",
      amount_snapshot: "5000.00",
      currency: "RUB",
      purpose_snapshot: "Абонемент Base",
      provider_payment_link_id: "jgr-44-test",
      provider_payment_url: "https://pay.example/jgr-44-test",
        can_pay: true,
        can_request_refresh: true,
      provider_payment_modes: ["card"],
      provider_status: "CREATED",
      expires_at: "2099-06-28T12:00:00Z",
      paid_at: null,
    };
    let pendingOrders: Array<typeof renewalOrder> = [];
    get.mockImplementation((url: string) => {
      if (url === "/students/me/subscriptions/") {
        return Promise.resolve({
          data: [
            {
              id: 14,
              tariff_id: 3,
              tariff_name: "Base",
              trainings_used: 2,
              trainings_total: 8,
              trainings_left: 6,
              expires_at: "2026-05-01",
              status: "active",
            },
          ],
        });
      }

      if (url === "/students/me/debts/") {
        return Promise.resolve({
          data: [
            {
              id: 11,
              checkin_id: 21,
              tariff_price: "1000.00",
              reason: "no_subscription",
              training_type_name: "Muay Thai",
              checkin_date: "2026-03-01",
              created_at: "2026-03-01T12:00:00Z",
            },
            {
              id: 12,
              checkin_id: 22,
              tariff_price: "1200.00",
              reason: "subscription_exhausted",
              training_type_name: "Boxing",
              checkin_date: "2026-03-03",
              created_at: "2026-03-03T12:00:00Z",
            },
          ],
        });
      }

      if (url === "/grades/my-progress/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/students/me/schedule-week/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/students/me/bank-payment-orders/") {
        return Promise.resolve({ data: pendingOrders });
      }

      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });
    post.mockImplementation((url: string) => {
      if (url === "/students/me/bank-payment-orders/") {
        pendingOrders = [renewalOrder];
        return Promise.resolve({ data: renewalOrder });
      }
      return Promise.reject(new Error(`Unexpected POST request: ${url}`));
    });

    renderHomePage();

    expect(await screen.findByText("Есть задолженность")).toBeInTheDocument();
    expect(
      screen.getByText("Для закрытия долга тренер сформирует отдельную ссылку."),
    ).toBeInTheDocument();

    fireEvent.click(await screen.findByRole("button", { name: "Продлить через СБП" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/students/me/bank-payment-orders/", {
        tariff_id: 3,
        debt_ids: [],
      });
    });
    expect(await screen.findByText("Продление ожидает оплаты")).toBeInTheDocument();
  });

  it("uses the exact student renewal source and no client tariff or debt in unified mode", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/students/me/subscriptions/") {
        return Promise.resolve({
          data: [
            {
              id: 14,
              tariff_id: 3,
              tariff_name: "Base",
              renewal_target_tariff_id: 9,
              renewal_target_tariff_name: "Base 2026",
              renewal_target_price: "6500.00",
              trainings_used: 2,
              trainings_total: 8,
              trainings_left: 6,
              expires_at: "2099-05-01",
              status: "active",
            },
          ],
        });
      }
      if (
        url === "/students/me/debts/" ||
        url === "/students/me/financial-state/" ||
        url === "/grades/my-progress/" ||
        url === "/students/me/schedule-week/" ||
        url === "/students/me/bank-payment-orders/" ||
        url === "/personal-availability/payment-reservations/"
      ) {
        return Promise.resolve({ data: [] });
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    renderHomePage(undefined, { contextualRenewalEnabled: true });

    expect(await screen.findByText(/Продление: Base 2026 · 6\s?500/)).toBeInTheDocument();
    expect(screen.getByText("subscription:Base")).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "Продлить через СБП" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/students/me/bank-payment-orders/",
        expect.objectContaining({
          renewed_from_subscription_id: 14,
          idempotency_key: expect.any(String),
        }),
      );
    });
    const payload = post.mock.calls[0]?.[1] as Record<string, unknown>;
    expect(payload).not.toHaveProperty("tariff_id");
    expect(payload).not.toHaveProperty("debt_ids");
    expect(payload).toMatchObject({
      expected_target_tariff_id: 9,
      expected_target_price: "6500.00",
    });
  });

  it("does not fall back to a tariff renewal when unified capability is unavailable", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/students/intakes/capability") return Promise.reject(new Error("offline"));
      if (url === "/students/me/subscriptions/") {
        return Promise.resolve({
          data: [
            {
              id: 14,
              tariff_id: 3,
              tariff_name: "Base",
              trainings_used: 2,
              trainings_total: 8,
              trainings_left: 6,
              expires_at: "2099-05-01",
              status: "active",
            },
          ],
        });
      }
      if (
        url === "/students/me/debts/" ||
        url === "/students/me/financial-state/" ||
        url === "/grades/my-progress/" ||
        url === "/students/me/schedule-week/" ||
        url === "/students/me/bank-payment-orders/" ||
        url === "/personal-availability/payment-reservations/"
      ) {
        return Promise.resolve({ data: [] });
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    renderHomePage(undefined, { contextualRenewalEnabled: "error" });

    expect(
      await screen.findByText(/Продление через СБП станет доступно после проверки настроек клуба/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Продлить через СБП" })).not.toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });

  it("keeps renewal visible but disabled for malformed payment capability data", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/students/me/subscriptions/") {
        return Promise.resolve({
          data: [
            {
              id: 14,
              tariff_id: 3,
              tariff_name: "Base",
              trainings_used: 2,
              trainings_total: 8,
              trainings_left: 6,
              expires_at: "2026-05-01",
              status: "active",
            },
          ],
        });
      }
      if (
        url === "/students/me/debts/" ||
        url === "/grades/my-progress/" ||
        url === "/students/me/schedule-week/" ||
        url === "/students/me/bank-payment-orders/" ||
        url === "/personal-availability/payment-reservations/"
      ) {
        return Promise.resolve({ data: [] });
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    renderHomePage({ online_payments_enabled: "true" });

    const renewal = await screen.findByRole("button", { name: "Продлить через СБП" });
    expect(renewal).toBeVisible();
    expect(renewal).toBeDisabled();
    fireEvent.click(renewal);
    expect(post).not.toHaveBeenCalledWith(
      "/students/me/bank-payment-orders/",
      expect.anything(),
    );
  });

  it("shows an existing pending online payment link for renewal", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/students/me/subscriptions/") {
        return Promise.resolve({
          data: [
            {
              id: 5,
              tariff_id: 3,
              tariff_name: "Base",
              trainings_used: 2,
              trainings_total: 8,
              trainings_left: 6,
              expires_at: "2026-05-01",
              status: "active",
            },
          ],
        });
      }

      if (url === "/students/me/debts/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/grades/my-progress/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/students/me/schedule-week/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/students/me/bank-payment-orders/") {
        return Promise.resolve({
          data: [
            {
              id: 44,
              tariff_id: 3,
              debt_ids: [],
              payment_id: 94,
              subscription_id: 54,
              student_id: 7,
              provider: "mock",
              source: "trainer",
              status: "pending",
              amount_snapshot: "6500.00",
              currency: "RUB",
              purpose_snapshot: "Абонемент Base",
              renewed_from_subscription_id: 5,
              renewal_source_tariff_id: 3,
              renewal_source_tariff_name: "Base",
              renewal_target_tariff_id: 9,
              renewal_target_tariff_name: "Base 2026",
              renewal_target_price: "6500.00",
              provider_payment_link_id: "jgr-44-test",
              provider_payment_url: "https://pay.example/jgr-44-test",
        can_pay: true,
        can_request_refresh: true,
              provider_payment_modes: ["card"],
              provider_status: "CREATED",
              expires_at: "2099-06-28T12:00:00Z",
              paid_at: null,
            },
          ],
        });
      }

      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    renderHomePage();

    expect(await screen.findByText("Продление ожидает оплаты")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Оплатить через СБП/ })).toHaveAttribute(
      "href",
      "https://pay.example/jgr-44-test",
    );
    expect(await screen.findByText(/Base 2026/)).toBeInTheDocument();
    expect(await screen.findByText(/Base 2026 · 6\s?500/)).toBeInTheDocument();
    expect(await screen.findByText(/^6\s?500\s?₽$/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Продлить через СБП" })).not.toBeInTheDocument();
  });

  it("cancels a pending renewal through the in-app confirmation and hides it", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    const renewalOrder = {
      id: 44,
      tariff_id: 3,
      debt_ids: [],
      payment_id: 94,
      subscription_id: 54,
      student_id: 7,
      provider: "mock",
      source: "student",
      status: "pending",
      amount_snapshot: "5000.00",
      currency: "RUB",
      purpose_snapshot: "Абонемент Base",
      provider_payment_link_id: "jgr-44-test",
      provider_payment_url: "https://pay.example/jgr-44-test",
        can_pay: true,
        can_request_refresh: true,
      provider_payment_modes: ["card"],
      provider_status: "CREATED",
      expires_at: "2099-06-28T12:00:00Z",
      paid_at: null,
      can_cancel: true,
    };
    let pendingOrders: Array<typeof renewalOrder> = [renewalOrder];
    get.mockImplementation((url: string) => {
      if (url === "/students/me/subscriptions/") {
        return Promise.resolve({
          data: [
            {
              id: 5,
              tariff_id: 3,
              tariff_name: "Base",
              trainings_used: 2,
              trainings_total: 8,
              trainings_left: 6,
              expires_at: "2026-05-01",
              status: "active",
            },
          ],
        });
      }

      if (url === "/students/me/debts/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/grades/my-progress/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/students/me/schedule-week/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/students/me/bank-payment-orders/") {
        return Promise.resolve({ data: pendingOrders });
      }

      if (url === "/personal-availability/payment-reservations/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });
    post.mockImplementation((url: string) => {
      if (url === "/students/me/bank-payment-orders/44/cancel/") {
        pendingOrders = [];
        return Promise.resolve({ data: { ...renewalOrder, status: "cancelled" } });
      }
      return Promise.reject(new Error(`Unexpected POST request: ${url}`));
    });

    renderHomePage();

    expect(await screen.findByText("Продление ожидает оплаты")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Отменить продление" }));

    expect(confirmSpy).not.toHaveBeenCalled();
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("Отменить оплату?")).toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole("button", { name: "Отменить продление" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/students/me/bank-payment-orders/44/cancel/", {});
    });
    await waitFor(() => {
      expect(screen.queryByText("Продление ожидает оплаты")).not.toBeInTheDocument();
    });
    expect(confirmSpy).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });

  it("refreshes a reopened manual-review renewal without exposing another payment action", async () => {
    let bankOrderReads = 0;
    get.mockImplementation((url: string) => {
      if (url === "/students/me/subscriptions/") {
        return Promise.resolve({
          data: [
            {
              id: 5,
              tariff_id: 3,
              tariff_name: "Base",
              trainings_used: 2,
              trainings_total: 8,
              trainings_left: 6,
              expires_at: "2026-09-01",
              status: "active",
            },
          ],
        });
      }
      if (url === "/students/me/debts/") return Promise.resolve({ data: [] });
      if (url === "/students/me/financial-state/") return Promise.resolve({ data: {} });
      if (url === "/grades/my-progress/") return Promise.resolve({ data: [] });
      if (url === "/students/me/schedule-week/") return Promise.resolve({ data: [] });
      if (url === "/personal-availability/payment-reservations/") {
        return Promise.resolve({ data: [] });
      }
      if (url === "/students/me/bank-payment-orders/") {
        bankOrderReads += 1;
        return Promise.resolve({
          data: [
            {
              id: 44,
              tariff_id: 3,
              debt_ids: [],
              subscription_id: 54,
              status: "manual_review",
              amount_snapshot: "5000.00",
              currency: "RUB",
              purpose_snapshot: "Абонемент Base",
              expires_at: "2099-06-28T12:00:00Z",
              can_request_refresh: true,
            },
          ],
        });
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    renderHomePage();

    expect(await screen.findByText("Последняя онлайн-оплата")).toBeInTheDocument();
    expect(screen.getAllByText("Проверка")).not.toHaveLength(0);
    expect(screen.queryByRole("link", { name: /Оплатить через СБП/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Обновить статус" }));

    await waitFor(() => expect(bankOrderReads).toBe(2));
  });

  it("shows a frozen subscription as the current subscription state", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/students/me/subscriptions/") {
        return Promise.resolve({
          data: [
            {
              id: 3,
              tariff_name: "Frozen Plan",
              trainings_used: 2,
              trainings_total: 8,
              trainings_left: 6,
              expires_at: "2026-05-01",
              status: "frozen",
            },
          ],
        });
      }

      if (url === "/students/me/debts/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/grades/my-progress/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/students/me/schedule-week/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    renderHomePage();

    expect(await screen.findByText("subscription:Frozen Plan")).toBeInTheDocument();
    expect(screen.getByText("status:frozen")).toBeInTheDocument();
  });

  it("does not treat pending subscriptions as current entitlement cards", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/students/me/subscriptions/") {
        return Promise.resolve({
          data: [
            {
              id: 4,
              tariff_name: "Pending Plan",
              trainings_used: 0,
              trainings_total: null,
              trainings_left: null,
              expires_at: null,
              status: "pending",
            },
          ],
        });
      }

      if (url === "/students/me/debts/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/grades/my-progress/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/students/me/schedule-week/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    renderHomePage();

    expect(await screen.findByText("Нет активного абонемента")).toBeInTheDocument();
    expect(screen.queryByText("subscription:Pending Plan")).not.toBeInTheDocument();
    expect(screen.queryByText("status:pending")).not.toBeInTheDocument();
  });

  it("shows active and frozen subscriptions when both are present", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/students/me/subscriptions/") {
        return Promise.resolve({
          data: [
            {
              id: 8,
              tariff_name: "Frozen Plan",
              trainings_used: 2,
              trainings_total: 8,
              trainings_left: 6,
              expires_at: "2026-05-01",
              status: "frozen",
            },
            {
              id: 9,
              tariff_name: "Active Plan",
              trainings_used: 1,
              trainings_total: 12,
              trainings_left: 11,
              expires_at: "2026-06-01",
              status: "active",
            },
          ],
        });
      }

      if (url === "/students/me/debts/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/grades/my-progress/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/students/me/schedule-week/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    renderHomePage();

    expect(await screen.findByText("subscription:Active Plan")).toBeInTheDocument();
    expect(screen.getByText("subscription:Frozen Plan")).toBeInTheDocument();
  });

  it("passes pending freeze status to the subscription card", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/students/me/subscriptions/") {
        return Promise.resolve({
          data: [
            {
              id: 11,
              tariff_name: "Active With Freeze",
              trainings_used: 1,
              trainings_total: 12,
              trainings_left: 11,
              expires_at: "2026-06-01",
              status: "active",
              freeze_status: "pending",
            },
          ],
        });
      }

      if (url === "/students/me/debts/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/grades/my-progress/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/students/me/schedule-week/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    renderHomePage();

    expect(await screen.findByText("subscription:Active With Freeze")).toBeInTheDocument();
    expect(screen.getByText("freeze:pending")).toBeInTheDocument();
  });

  it("renders every active subscription for multi-discipline students", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/students/me/subscriptions/") {
        return Promise.resolve({
          data: [
            {
              id: 9,
              tariff_name: "BJJ Plan",
              trainings_used: 1,
              trainings_total: 12,
              trainings_left: 11,
              expires_at: "2026-06-01",
              status: "active",
            },
            {
              id: 10,
              tariff_name: "Boxing Plan",
              trainings_used: 3,
              trainings_total: 8,
              trainings_left: 5,
              expires_at: "2026-06-15",
              status: "active",
            },
          ],
        });
      }

      if (url === "/students/me/debts/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/grades/my-progress/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/students/me/schedule-week/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    renderHomePage();

    expect(await screen.findByText("subscription:BJJ Plan")).toBeInTheDocument();
    expect(screen.getByText("subscription:Boxing Plan")).toBeInTheDocument();
  });
});
