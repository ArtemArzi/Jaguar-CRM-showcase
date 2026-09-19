import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import StudentSchedule from "./student-schedule";
import { useAuthStore } from "@/features/auth/auth-store";
import {
  ENABLED_SBP_PAYMENT_CAPABILITIES,
  getPaymentCapabilitiesQueryKey,
} from "@/api/payment-capabilities";
import { getPersonalAvailabilityCapabilityQueryKey } from "@/api/unified-client-journey";
import { useBrandingStore } from "@/features/branding/use-branding";

const { get, post } = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}));

vi.mock("@/api/custom-fetch", () => ({
  default: { get, post },
}));

function renderSchedulePage(
  paymentCapabilities: unknown = ENABLED_SBP_PAYMENT_CAPABILITIES,
  {
    skipPaymentCapabilitiesSeed = false,
    unifiedClientJourneyEnabled = false,
    skipPersonalAvailabilityCapabilitySeed = false,
  }: {
    skipPaymentCapabilitiesSeed?: boolean;
    unifiedClientJourneyEnabled?: boolean;
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
      staff_command_protocol_version: "v1",
    });
  }

  return render(
    <QueryClientProvider client={queryClient}>
      <StudentSchedule />
    </QueryClientProvider>,
  );
}

describe("StudentSchedule", () => {
  beforeEach(() => {
    localStorage.clear();
    get.mockReset();
    post.mockReset();
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
    useBrandingStore.setState({
      timeZone: "Asia/Yekaterinburg",
      timeZoneStatus: "ready",
      timeZoneClubId: 1,
      isTimeZoneAuthoritative: true,
    });
  });

  it("renders effective schedule cards without seconds and with badges", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/students/me/schedule-week/") {
        return Promise.resolve({
          data: [
            {
              schedule_id: 1,
              group_name: "Karate",
              effective_date: "2026-04-13",
              effective_start_time: "18:00:00",
              effective_end_time: "19:00:00",
              trainer_name: "Alex Backup",
              location_name: "Blue Hall",
              training_type_id: 3,
              training_type_name: "Muay Thai",
              is_rescheduled: true,
              is_substitute: true,
            },
          ],
        });
      }

      if (url === "/students/me/schedule/") {
        return Promise.resolve({ data: [{ id: 1 }] });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderSchedulePage();

    expect(await screen.findByText("Расписание")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /вернуться к текущей неделе/i })).toBeInTheDocument();
    expect(screen.getAllByText(/текущая неделя/i).length).toBeGreaterThan(0);
    expect(await screen.findByText("Karate")).toBeInTheDocument();
    expect(screen.getByText("Muay Thai")).toBeInTheDocument();
    expect(screen.getByText("18:00–19:00")).toBeInTheDocument();
    expect(screen.queryByText("18:00:00–19:00:00")).not.toBeInTheDocument();
    expect(screen.getByText("Перенос")).toBeInTheDocument();
    expect(screen.getByText("Замена тренера")).toBeInTheDocument();
    expect(screen.queryByText("перенос · замена тренера")).not.toBeInTheDocument();
  });

  it("shows onboarding empty state when student has no known schedule yet", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/students/me/schedule-week/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/students/me/schedule/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderSchedulePage();

    expect(
      await screen.findByText("Пока нет закреплённых занятий"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Когда тренер закрепит группу или появится персональная запись, расписание отобразится здесь."),
    ).toBeInTheDocument();
  });

  it("uses only the capability-on self-service personal routes", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/students/me/schedule-week/") return Promise.resolve({ data: [] });
      if (url === "/students/me/schedule/") return Promise.resolve({ data: [] });
      if (url === "/personal-availability/self-service/options/") {
        return Promise.resolve({
          data: [
            {
              slot_id: 77,
              date: "2026-08-13",
              starts_at: "2026-08-13T10:00:00+05:00",
              ends_at: "2026-08-13T11:00:00+05:00",
              trainer_id: 3,
              trainer_name: "Иван Петров",
              location_id: 2,
              location_name: "Зал",
              training_type_id: 8,
              training_type_name: "Персональная тренировка",
              capability: "can_book",
              offer_tariff_name: "",
              offer_price: "",
              offer_digest: "",
            },
          ],
        });
      }
      if (url === "/personal-availability/self-service/commands/") {
        return Promise.resolve({ data: { live: [], latest_terminal: [] } });
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderSchedulePage(ENABLED_SBP_PAYMENT_CAPABILITIES, {
      unifiedClientJourneyEnabled: true,
    });

    expect(await screen.findByText("Выберите свободный слот")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Записаться" })).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith(
      "/personal-availability/self-service/options/",
      expect.objectContaining({ params: expect.objectContaining({ date: expect.any(String) }) }),
    );
    expect(get).not.toHaveBeenCalledWith(
      "/personal-availability/payment-reservations/",
      expect.anything(),
    );
  });

  it("keeps terminal unified payment history visible after the club returns to legacy mode", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/students/me/schedule-week/" || url === "/students/me/schedule/") {
        return Promise.resolve({ data: [] });
      }
      if (url === "/personal-availability/payment-reservations/") {
        return Promise.resolve({ data: [] });
      }
      if (url === "/personal-availability/self-service/commands/") {
        return Promise.resolve({
          data: {
            live: [],
            latest_terminal: [
              {
                command_id: 51,
                slot_id: 77,
                capability: "can_pay",
                status: "cancelled",
                starts_at: "2099-08-13T10:00:00+05:00",
                ends_at: "2099-08-13T11:00:00+05:00",
                booking_id: null,
                reservation_id: 41,
                bank_payment_order_id: 123,
                provider_payment_url: "",
                amount_snapshot: "2700.00",
                order_status: "cancelled",
                allowed_actions: [],
              },
            ],
          },
        });
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderSchedulePage();

    expect(await screen.findByRole("button", { name: "Записаться" })).toBeInTheDocument();
    expect(await screen.findByText("Отменено")).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith(
      "/personal-availability/payment-reservations/",
      expect.objectContaining({ params: { status: "open_actionable" } }),
    );
    expect(get).toHaveBeenCalledWith("/personal-availability/self-service/commands/", {
      params: undefined,
    });
  });

  it.each([
    ["pending", () => new Promise<never>(() => {})],
    ["error", () => Promise.reject(new Error("capability offline"))],
  ] as const)("fails closed while the personal capability is %s", async (_state, capabilityResponse) => {
    get.mockImplementation((url: string) => {
      if (url === "/personal-availability/capability/") return capabilityResponse();
      if (url === "/students/me/schedule-week/" || url === "/students/me/schedule/") {
        return Promise.resolve({ data: [] });
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderSchedulePage(ENABLED_SBP_PAYMENT_CAPABILITIES, {
      skipPersonalAvailabilityCapabilitySeed: true,
    });

    expect(
      await screen.findByText(
        _state === "error"
          ? "Персональная запись временно недоступна"
          : "Проверяем доступность персональной записи",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Записаться" })).not.toBeInTheDocument();
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

  it("lets a student cancel their own group booking from the schedule", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/students/me/schedule-week/") {
        return Promise.resolve({
          data: [
            {
              schedule_id: 1,
              enrollment_id: 77,
              created_from: "student_self_booking",
              can_cancel: true,
              group_name: "Self Booked Group",
              effective_date: "2026-04-13",
              effective_start_time: "18:00:00",
              effective_end_time: "19:00:00",
              trainer_name: "Ivan Petrov",
              location_name: "Main hall",
            },
          ],
        });
      }

      if (url === "/students/me/schedule/") {
        return Promise.resolve({ data: [{ id: 1 }] });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });
    post.mockResolvedValue({
      data: {
        id: 77,
        status: "cancelled",
      },
    });

    renderSchedulePage();

    expect(await screen.findByText("Self Booked Group")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Отменить запись Self Booked Group" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/guest-bookings/77/cancel/", { reason: "" });
    });
  });

  it("lets a student cancel their own personal booking from the schedule", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/students/me/schedule-week/") {
        return Promise.resolve({
          data: [
            {
              schedule_id: 2,
              enrollment_id: 88,
              created_from: "personal_booking",
              can_cancel: true,
              group_name: "Персональная тренировка",
              effective_date: "2026-04-13",
              effective_start_time: "10:00:00",
              effective_end_time: "11:00:00",
              trainer_name: "Ivan Petrov",
              location_name: "Main hall",
            },
          ],
        });
      }

      if (url === "/students/me/schedule/") {
        return Promise.resolve({ data: [{ id: 2 }] });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });
    post.mockResolvedValue({
      data: {
        id: 88,
        status: "cancelled",
      },
    });

    renderSchedulePage();

    expect(await screen.findByText("Персональная тренировка")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Отменить запись Персональная тренировка" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/personal-bookings/88/cancel/", { reason: "" });
    });
  });

  it("shows stale cancel error and refreshes schedule when backend rejects past booking cancel", async () => {
    const invalidateSpy = vi.spyOn(QueryClient.prototype, "invalidateQueries");
    get.mockImplementation((url: string) => {
      if (url === "/students/me/schedule-week/") {
        return Promise.resolve({
          data: [
            {
              schedule_id: 1,
              enrollment_id: 77,
              created_from: "student_self_booking",
              can_cancel: true,
              group_name: "Past Self Booked Group",
              effective_date: "2026-04-13",
              effective_start_time: "18:00:00",
              effective_end_time: "19:00:00",
              trainer_name: "Ivan Petrov",
              location_name: "Main hall",
            },
          ],
        });
      }

      if (url === "/students/me/schedule/") {
        return Promise.resolve({ data: [{ id: 1 }] });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });
    post.mockRejectedValue({
      response: { data: { code: "booking_past_date" } },
    });

    renderSchedulePage();

    expect(await screen.findByText("Past Self Booked Group")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Отменить запись Past Self Booked Group" }));

    expect(await screen.findByText("Эту запись уже нельзя отменить, занятие прошло")).toBeInTheDocument();
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: ["student", "schedule-week", 7],
    });
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: ["student", "schedule-known", 7],
    });
    invalidateSpy.mockRestore();
  });

  it("lets a student book an available group guest option", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/students/me/schedule-week/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/students/me/schedule/") {
        return Promise.resolve({ data: [{ id: 1 }] });
      }

      if (url === "/schedules/guest-booking-options/") {
        return Promise.resolve({
          data: [
            {
              schedule_id: 42,
              date: "2026-04-13",
              start_time: "18:00:00",
              end_time: "19:00:00",
              group_name: "Kids Boxing",
              trainer_name: "Ivan Petrov",
              location_name: "Main hall",
              training_type_id: 3,
              training_type_name: "Boxing",
              booking_status: "can_book",
              reason_code: "",
              financial_status: "subscription",
              subscription_id: 9,
              drop_in_price: null,
            },
          ],
        });
      }

      if (url === "/personal-availability/options/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/personal-availability/payment-reservations/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });
    post.mockResolvedValue({
      data: {
        enrollment_id: 77,
        student_id: 7,
        display_name: "Student",
        schedule_id: 42,
        created_from: "student_self_booking",
        is_guest_visit: true,
        starts_on: "2026-04-13",
        ends_on: "2026-04-13",
        created: true,
        already_member: false,
        origin: "student_self_booking",
        financial_preview: {
          code: "subscription",
          message: "По абонементу",
        },
      },
    });

    renderSchedulePage();

    fireEvent.click(await screen.findByRole("button", { name: "Записаться" }));

    expect(screen.getByRole("tab", { name: "Группа" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Персоналка" })).toBeInTheDocument();

    const bookingSection = await screen.findByRole("region", {
      name: "Запись на групповую тренировку",
    });
    expect(await within(bookingSection).findByText("Kids Boxing")).toBeInTheDocument();

    fireEvent.click(within(bookingSection).getByRole("button", { name: "Записаться" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/schedules/42/guest-bookings/", {
        date: "2026-04-13",
        idempotency_key: "student-self-booking-7-42-2026-04-13",
      });
    });
    expect(await within(bookingSection).findByText("Запись создана")).toBeInTheDocument();
  });

  it("shows an already-booked group as a locked state, not a bookable action", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/students/me/schedule-week/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/students/me/schedule/") {
        return Promise.resolve({ data: [{ id: 1 }] });
      }

      if (url === "/schedules/guest-booking-options/") {
        return Promise.resolve({
          data: [
            {
              schedule_id: 43,
              date: "2026-04-13",
              start_time: "18:00:00",
              end_time: "19:00:00",
              group_name: "Already Booked Boxing",
              trainer_name: "Ivan Petrov",
              location_name: "Main hall",
              training_type_id: 3,
              training_type_name: "Boxing",
              booking_status: "already_booked",
              reason_code: "already_booked",
              financial_status: "subscription",
              subscription_id: 9,
              drop_in_price: null,
            },
          ],
        });
      }

      if (url === "/personal-availability/options/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/personal-availability/payment-reservations/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderSchedulePage();

    fireEvent.click(await screen.findByRole("button", { name: "Записаться" }));

    const bookingSection = await screen.findByRole("region", {
      name: "Запись на групповую тренировку",
    });
    expect(await within(bookingSection).findByText("Already Booked Boxing")).toBeInTheDocument();
    expect(within(bookingSection).getByRole("button", { name: "Уже записан" })).toBeDisabled();
    expect(
      within(bookingSection).queryByRole("button", { name: "Записаться" }),
    ).not.toBeInTheDocument();
  });

  it("lets a student book a published personal slot", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/students/me/schedule-week/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/students/me/schedule/") {
        return Promise.resolve({ data: [{ id: 1 }] });
      }

      if (url === "/schedules/guest-booking-options/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/personal-availability/options/") {
        return Promise.resolve({
          data: [
            {
              slot_id: 55,
              date: "2026-04-13",
              starts_at: "2026-04-13T10:00:00+05:00",
              ends_at: "2026-04-13T11:00:00+05:00",
              trainer_id: 3,
              trainer_name: "Ivan Petrov",
              location_id: 2,
              location_name: "Main hall",
              training_type_id: 8,
              training_type_name: "Персональная тренировка",
              booking_status: "can_book",
              reason_code: "",
              subscription_id: 19,
              payment_tariff_id: null,
              payment_tariff_name: "",
              payment_amount: "",
            },
          ],
        });
      }

      if (url === "/personal-availability/payment-reservations/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });
    post.mockResolvedValue({
      data: {
        schedule_id: 101,
        enrollment_id: 88,
        availability_slot_id: 55,
        student_id: 7,
        trainer_id: 3,
        trainer_name: "Ivan Petrov",
        location_id: 2,
        location_name: "Main hall",
        training_type_id: 8,
        training_type_name: "Персональная тренировка",
        starts_at: "2026-04-13T10:00:00+05:00",
        ends_at: "2026-04-13T11:00:00+05:00",
        created_from: "personal_booking",
        created: true,
      },
    });

    renderSchedulePage();

    fireEvent.click(await screen.findByRole("button", { name: "Записаться" }));
    fireEvent.click(screen.getByRole("tab", { name: "Персоналка" }));

    const personalSection = await screen.findByRole("region", {
      name: "Запись на персональную тренировку",
    });
    expect(
      await within(personalSection).findByText("Персональная тренировка"),
    ).toBeInTheDocument();

    fireEvent.click(within(personalSection).getByRole("button", { name: "Записаться" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/personal-availability/55/book/", {
        subscription_id: 19,
        idempotency_key: "student-personal-self-booking-7-55",
      });
    });
    expect(await within(personalSection).findByText("Запись создана")).toBeInTheDocument();
  });

  it("keeps personal online payment visible but disabled after a capability request error", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/students/me/schedule-week/") {
        return Promise.resolve({ data: [] });
      }
      if (url === "/students/me/schedule/") {
        return Promise.resolve({ data: [{ id: 1 }] });
      }
      if (url === "/schedules/guest-booking-options/") {
        return Promise.resolve({ data: [] });
      }
      if (url === "/personal-availability/options/") {
        return Promise.resolve({
          data: [
            {
              slot_id: 56,
              date: "2026-04-13",
              starts_at: "2026-04-13T10:00:00+05:00",
              ends_at: "2026-04-13T11:00:00+05:00",
              trainer_id: 3,
              trainer_name: "Ivan Petrov",
              location_id: 2,
              location_name: "Main hall",
              training_type_id: 8,
              training_type_name: "Персональная тренировка",
              booking_status: "can_pay",
              reason_code: "payment_required",
              subscription_id: null,
              payment_tariff_id: 12,
              payment_tariff_name: "Разовая персоналка",
              payment_amount: "2000.00",
            },
          ],
        });
      }
      if (url === "/personal-availability/payment-reservations/") {
        return Promise.resolve({ data: [] });
      }
      if (url === "/billing/payment-capabilities/") {
        return Promise.reject(new Error("capability unavailable"));
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderSchedulePage(undefined, { skipPaymentCapabilitiesSeed: true });

    fireEvent.click(await screen.findByRole("button", { name: "Записаться" }));
    fireEvent.click(screen.getByRole("tab", { name: "Персоналка" }));
    const personalSection = await screen.findByRole("region", {
      name: "Запись на персональную тренировку",
    });
    const pay = await within(personalSection).findByRole("button", { name: "Оплатить" });
    expect(pay).toBeVisible();
    expect(pay).toBeDisabled();
    fireEvent.click(pay);
    expect(post).not.toHaveBeenCalledWith(
      "/personal-availability/56/payment-reservations/",
      expect.anything(),
    );
  });

  it("creates and cancels a payment link for a personal slot without a subscription", async () => {
    const openedPaymentWindows: Array<{ closed: boolean; close: ReturnType<typeof vi.fn>; location: { href: string } }> = [];
    const openSpy = vi.spyOn(window, "open").mockImplementation(() => {
      const paymentWindow = {
        closed: false,
        close: vi.fn(),
        location: { href: "" },
      };
      openedPaymentWindows.push(paymentWindow);
      return paymentWindow as unknown as Window;
    });
    const reservation = {
      id: 31,
      student_id: 7,
      trainer_id: 3,
      trainer_name: "Ivan Petrov",
      location_id: 2,
      location_name: "Main hall",
      training_type_id: 8,
      training_type_name: "Персональная тренировка",
      tariff_id: 12,
      tariff_name: "Разовая персоналка",
      availability_slot_id: 56,
      starts_at: "2026-04-13T10:00:00+05:00",
      ends_at: "2026-04-13T11:00:00+05:00",
      status: "pending_payment",
      expires_at: "2099-04-13T09:45:00+05:00",
      payment_id: 91,
      bank_payment_order_id: 123,
      subscription_id: null,
      schedule_id: null,
      enrollment_id: null,
      provider_payment_url: "https://pay.example/personal-31",
      amount_snapshot: "2000.00",
      order_status: "pending",
      can_cancel: true,
      created_at: "2026-04-13T08:45:00+05:00",
    };
    const secondReservation = {
      ...reservation,
      id: 32,
      bank_payment_order_id: 124,
      provider_payment_url: "https://pay.example/personal-32",
    };
    let createdReservationCount = 0;
    let pendingReservations: typeof reservation[] = [];

    get.mockImplementation((url: string) => {
      if (url === "/students/me/schedule-week/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/students/me/schedule/") {
        return Promise.resolve({ data: [{ id: 1 }] });
      }

      if (url === "/schedules/guest-booking-options/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/personal-availability/options/") {
        return Promise.resolve({
          data: [
            {
              slot_id: 56,
              date: "2026-04-13",
              starts_at: "2026-04-13T03:00:00Z",
              ends_at: "2026-04-13T04:00:00Z",
              trainer_id: 3,
              trainer_name: "Ivan Petrov",
              location_id: 2,
              location_name: "Main hall",
              training_type_id: 8,
              training_type_name: "Персональная тренировка",
              booking_status: "can_pay",
              reason_code: "payment_required",
              subscription_id: null,
              payment_tariff_id: 12,
              payment_tariff_name: "Персоналка Максим Бранзовой по чек-ину",
              payment_amount: "3000.00",
              offer_tariff_id: 44,
              offer_tariff_name: "Персоналка по назначенной цене",
              offer_price: "2500.00",
              offer_digest: "student-slot-offer-v1",
              offer_error_code: "",
            },
          ],
        });
      }

      if (url === "/personal-availability/payment-reservations/") {
        return Promise.resolve({ data: pendingReservations });
      }

      if (url.startsWith("/students/me/bank-payment-orders/")) {
        const orderId = Number(url.split("/").at(-2));
        const paymentUrl = orderId === 124
          ? "https://pay.example/personal-32"
          : "https://pay.example/personal-31";
        return Promise.resolve({
          data: {
            id: orderId,
            payment_id: 91,
            subscription_id: null,
            student_id: 7,
            tariff_id: 12,
            debt_ids: [],
            source: "student",
            status: "pending",
            amount_snapshot: "2000.00",
            currency: "RUB",
            purpose_snapshot: "Разовая персоналка",
            provider_payment_url: paymentUrl,
            expires_at: "2099-04-13T09:45:00+05:00",
            can_pay: true,
            can_share: false,
            can_copy: false,
            can_show_qr: false,
            can_request_refresh: true,
            can_cancel: true,
          },
        });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });
    post.mockImplementation((url: string) => {
      if (url === "/personal-availability/56/payment-reservations/") {
        createdReservationCount += 1;
        pendingReservations = [
          createdReservationCount === 1 ? reservation : secondReservation,
        ];
        return Promise.resolve({
          data: createdReservationCount === 1 ? reservation : secondReservation,
        });
      }
      if (url === "/personal-availability/payment-reservations/31/cancel/") {
        pendingReservations = [];
        return Promise.resolve({ data: { ...reservation, status: "cancelled" } });
      }
      return Promise.reject(new Error(`Unexpected POST URL: ${url}`));
    });

    renderSchedulePage();

    fireEvent.click(await screen.findByRole("button", { name: "Записаться" }));
    fireEvent.click(screen.getByRole("tab", { name: "Персоналка" }));

    const personalSection = await screen.findByRole("region", {
      name: "Запись на персональную тренировку",
    });
    expect(await within(personalSection).findByText("Нужна оплата")).toBeInTheDocument();
    expect(await within(personalSection).findByText("08:00")).toBeInTheDocument();
    expect(within(personalSection).getByText("1ч")).toBeInTheDocument();
    expect(within(personalSection).queryByText("03:00")).not.toBeInTheDocument();
    const personalPaymentBadge = await within(personalSection).findByText(
      /Персоналка по назначенной цене · 2\s500\s₽/,
    );
    expect(personalPaymentBadge).toHaveClass("max-w-full");
    expect(personalPaymentBadge).toHaveClass("min-w-0");
    expect(personalPaymentBadge).toHaveClass("whitespace-normal");
    expect(personalPaymentBadge).toHaveClass("break-words");
    expect(personalPaymentBadge).not.toHaveClass("whitespace-nowrap");

    fireEvent.click(within(personalSection).getByRole("button", { name: "Оплатить" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/personal-availability/56/payment-reservations/", {
        tariff_id: 12,
        idempotency_key: "student-personal-payment-reservation-7-56-12",
      });
    });
    expect(await within(personalSection).findByText("Персоналка ожидает оплаты")).toBeInTheDocument();
    expect(within(personalSection).getByRole("button", { name: "Ссылка создана" })).toBeDisabled();
    expect(await within(personalSection).findByRole("link", { name: /Оплатить через СБП/ })).toHaveAttribute(
      "href",
      "https://pay.example/personal-31",
    );

    fireEvent.click(within(personalSection).getByRole("button", { name: "Отменить ссылку" }));
    const cancelDialog = screen.getByText("Отменить оплату?").closest('[role="dialog"]');
    expect(cancelDialog).not.toBeNull();
    fireEvent.click(
      within(cancelDialog as HTMLElement).getByRole("button", { name: "Отменить ссылку" }),
    );

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/personal-availability/payment-reservations/31/cancel/",
        {},
      );
    });
    await waitFor(() => {
      expect(within(personalSection).getByRole("button", { name: "Оплатить" })).toBeEnabled();
    });

    fireEvent.click(within(personalSection).getByRole("button", { name: "Оплатить" }));

    expect(openSpy).not.toHaveBeenCalled();
    openSpy.mockRestore();
  });

  it("blocks a second personal payment when pending payment lookup failed", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/students/me/schedule-week/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/students/me/schedule/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/schedules/guest-booking-options/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/personal-availability/options/") {
        return Promise.resolve({
          data: [
            {
              slot_id: 56,
              date: "2026-04-13",
              starts_at: "2026-04-13T10:00:00+05:00",
              ends_at: "2026-04-13T11:00:00+05:00",
              trainer_id: 3,
              trainer_name: "Ivan Petrov",
              location_id: 2,
              location_name: "Main hall",
              training_type_id: 8,
              training_type_name: "Персональная тренировка",
              booking_status: "can_pay",
              reason_code: "payment_required",
              subscription_id: null,
              payment_tariff_id: 12,
              payment_tariff_name: "Разовая персоналка",
              payment_amount: "2000.00",
            },
          ],
        });
      }

      if (url === "/personal-availability/payment-reservations/") {
        return Promise.reject(new Error("pending reservations unavailable"));
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderSchedulePage();

    fireEvent.click(await screen.findByRole("button", { name: "Записаться" }));
    fireEvent.click(screen.getByRole("tab", { name: "Персоналка" }));

    const personalSection = await screen.findByRole("region", {
      name: "Запись на персональную тренировку",
    });

    expect(
      await within(personalSection).findByText("Не удалось проверить ожидающие оплаты"),
    ).toBeInTheDocument();
    expect(within(personalSection).getByRole("button", { name: "Обновите экран" })).toBeDisabled();
    expect(within(personalSection).queryByRole("button", { name: "Оплатить" })).not.toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });

  it("drops a locally created personal payment after a fresh server snapshot excludes it", async () => {
    const openedPaymentWindows: Array<{
      closed: boolean;
      close: ReturnType<typeof vi.fn>;
      location: { href: string };
    }> = [];
    const openSpy = vi.spyOn(window, "open").mockImplementation(() => {
      const paymentWindow = {
        closed: false,
        close: vi.fn(),
        location: { href: "" },
      };
      openedPaymentWindows.push(paymentWindow);
      return paymentWindow as unknown as Window;
    });
    const reservation = {
      id: 31,
      student_id: 7,
      trainer_id: 3,
      trainer_name: "Ivan Petrov",
      location_id: 2,
      location_name: "Main hall",
      training_type_id: 8,
      training_type_name: "Персональная тренировка",
      tariff_id: 12,
      tariff_name: "Разовая персоналка",
      availability_slot_id: 56,
      starts_at: "2026-04-13T10:00:00+05:00",
      ends_at: "2026-04-13T11:00:00+05:00",
      status: "pending_payment",
      expires_at: "2099-04-13T11:15:00+05:00",
      payment_id: null,
      bank_payment_order_id: 91,
      subscription_id: null,
      schedule_id: null,
      enrollment_id: null,
      provider_payment_url: "https://pay.example/personal-31",
      amount_snapshot: "2000.00",
      order_status: "pending",
      can_cancel: true,
      created_at: "2026-04-13T08:45:00+05:00",
    };
    let pendingReservationRequests = 0;

    get.mockImplementation((url: string) => {
      if (url === "/students/me/schedule-week/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/students/me/schedule/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/schedules/guest-booking-options/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/personal-availability/options/") {
        return Promise.resolve({
          data: [
            {
              slot_id: 56,
              date: "2026-04-13",
              starts_at: "2026-04-13T10:00:00+05:00",
              ends_at: "2026-04-13T11:00:00+05:00",
              trainer_id: 3,
              trainer_name: "Ivan Petrov",
              location_id: 2,
              location_name: "Main hall",
              training_type_id: 8,
              training_type_name: "Персональная тренировка",
              booking_status: "can_pay",
              reason_code: "payment_required",
              subscription_id: null,
              payment_tariff_id: 12,
              payment_tariff_name: "Разовая персоналка",
              payment_amount: "2000.00",
            },
          ],
        });
      }

      if (url === "/personal-availability/payment-reservations/") {
        pendingReservationRequests += 1;
        return Promise.resolve({ data: [] });
      }

      if (url === "/students/me/bank-payment-orders/91/") {
        return Promise.resolve({
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
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });
    post.mockImplementation((url: string) => {
      if (url === "/personal-availability/56/payment-reservations/") {
        return Promise.resolve({ data: reservation });
      }
      return Promise.reject(new Error(`Unexpected POST URL: ${url}`));
    });

    renderSchedulePage();

    fireEvent.click(await screen.findByRole("button", { name: "Записаться" }));
    fireEvent.click(screen.getByRole("tab", { name: "Персоналка" }));

    const personalSection = await screen.findByRole("region", {
      name: "Запись на персональную тренировку",
    });
    expect(await within(personalSection).findByText("Нужна оплата")).toBeInTheDocument();

    fireEvent.click(within(personalSection).getByRole("button", { name: "Оплатить" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/personal-availability/56/payment-reservations/", {
        tariff_id: 12,
        idempotency_key: "student-personal-payment-reservation-7-56-12",
      });
    });
    await waitFor(() => {
      expect(pendingReservationRequests).toBeGreaterThanOrEqual(2);
    });
    await waitFor(() => {
      expect(within(personalSection).queryByText("Персоналка ожидает оплаты")).not.toBeInTheDocument();
      expect(within(personalSection).getByRole("button", { name: "Оплатить" })).toBeEnabled();
    });
    expect(openSpy).not.toHaveBeenCalled();
    openSpy.mockRestore();
  });

  it("attaches an existing pending payment reservation to its personal slot", async () => {
    const reservation = {
      id: 31,
      student_id: 7,
      trainer_id: 3,
      trainer_name: "Ivan Petrov",
      location_id: 2,
      location_name: "Main hall",
      training_type_id: 8,
      training_type_name: "Персональная тренировка",
      tariff_id: 12,
      tariff_name: "Разовая персоналка",
      availability_slot_id: 56,
      starts_at: "2026-04-13T10:00:00+05:00",
      ends_at: "2026-04-13T11:00:00+05:00",
      status: "pending_payment",
      expires_at: "2099-04-13T11:15:00+05:00",
      payment_id: null,
      bank_payment_order_id: 91,
      subscription_id: null,
      schedule_id: null,
      enrollment_id: null,
      provider_payment_url: "https://pay.example/personal-31",
      amount_snapshot: "2000.00",
      order_status: "pending",
      can_cancel: true,
      created_at: "2026-04-13T08:45:00+05:00",
    };

    get.mockImplementation((url: string) => {
      if (url === "/students/me/schedule-week/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/students/me/schedule/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/schedules/guest-booking-options/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/personal-availability/options/") {
        return Promise.resolve({
          data: [
            {
              slot_id: 56,
              date: "2026-04-13",
              starts_at: "2026-04-13T10:00:00+05:00",
              ends_at: "2026-04-13T11:00:00+05:00",
              trainer_id: 3,
              trainer_name: "Ivan Petrov",
              location_id: 2,
              location_name: "Main hall",
              training_type_id: 8,
              training_type_name: "Персональная тренировка",
              booking_status: "can_pay",
              reason_code: "payment_required",
              subscription_id: null,
              payment_tariff_id: 12,
              payment_tariff_name: "Разовая персоналка",
              payment_amount: "2000.00",
            },
          ],
        });
      }

      if (url === "/personal-availability/payment-reservations/") {
        return Promise.resolve({ data: [reservation] });
      }

      if (url === "/students/me/bank-payment-orders/91/") {
        return Promise.resolve({
          data: {
            id: 91,
            subscription_id: null,
            student_id: 7,
            tariff_id: 12,
            debt_ids: [],
            status: "pending",
            amount_snapshot: "2000.00",
            currency: "RUB",
            provider_payment_url: "https://pay.example/personal-31",
            expires_at: "2099-04-13T11:15:00+05:00",
            can_pay: true,
          },
        });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderSchedulePage();

    fireEvent.click(await screen.findByRole("button", { name: "Записаться" }));
    fireEvent.click(screen.getByRole("tab", { name: "Персоналка" }));

    await screen.findByRole("region", {
      name: "Запись на персональную тренировку",
    });
    await waitFor(() => {
      expect(
        within(
          screen.getByRole("region", {
            name: "Запись на персональную тренировку",
          }),
        ).getByText("Персоналка ожидает оплаты"),
      ).toBeInTheDocument();
    });

    const refreshedPersonalSection = screen.getByRole("region", {
      name: "Запись на персональную тренировку",
    });
    expect(
      within(refreshedPersonalSection).getByRole("button", { name: "Ссылка создана" }),
    ).toBeDisabled();
    expect(within(refreshedPersonalSection).getByRole("link", { name: /Оплатить через СБП/ })).toHaveAttribute(
      "href",
      "https://pay.example/personal-31",
    );
    expect(post).not.toHaveBeenCalled();
  });

  it("keeps an existing pending personal payment in the personal slot list when the held slot is absent from options", async () => {
    const reservation = {
      id: 31,
      student_id: 7,
      trainer_id: 3,
      trainer_name: "Ivan Petrov",
      location_id: 2,
      location_name: "Main hall",
      training_type_id: 8,
      training_type_name: "Персональная тренировка",
      tariff_id: 12,
      tariff_name: "Разовая персоналка",
      availability_slot_id: 56,
      starts_at: "2026-04-13T10:00:00+05:00",
      ends_at: "2026-04-13T11:00:00+05:00",
      status: "pending_payment",
      expires_at: "2099-04-13T11:15:00+05:00",
      payment_id: null,
      bank_payment_order_id: 91,
      subscription_id: null,
      schedule_id: null,
      enrollment_id: null,
      provider_payment_url: "https://pay.example/personal-31",
      amount_snapshot: "2000.00",
      order_status: "pending",
      can_cancel: true,
      created_at: "2026-04-13T08:45:00+05:00",
    };

    get.mockImplementation((url: string) => {
      if (url === "/students/me/schedule-week/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/students/me/schedule/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/schedules/guest-booking-options/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/personal-availability/options/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/personal-availability/payment-reservations/") {
        return Promise.resolve({ data: [reservation] });
      }

      if (url === "/students/me/bank-payment-orders/91/") {
        return Promise.resolve({
          data: {
            id: 91,
            subscription_id: null,
            student_id: 7,
            tariff_id: 12,
            debt_ids: [],
            status: "pending",
            amount_snapshot: "2000.00",
            currency: "RUB",
            provider_payment_url: "https://pay.example/personal-31",
            expires_at: "2099-04-13T11:15:00+05:00",
            can_pay: true,
          },
        });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderSchedulePage();

    fireEvent.click(await screen.findByRole("button", { name: "Записаться" }));
    fireEvent.click(screen.getByRole("tab", { name: "Персоналка" }));

    const personalSection = await screen.findByRole("region", {
      name: "Запись на персональную тренировку",
    });

    expect(await within(personalSection).findByText("Персоналка ожидает оплаты")).toBeInTheDocument();
    expect(within(personalSection).getAllByText("Ожидает оплаты").length).toBeGreaterThan(0);
    expect(within(personalSection).getAllByText("Ivan Petrov").length).toBeGreaterThan(0);
    expect(within(personalSection).getByText("Main hall")).toBeInTheDocument();
    expect(within(personalSection).getByRole("link", { name: /Оплатить через СБП/ })).toHaveAttribute(
      "href",
      "https://pay.example/personal-31",
    );
    expect(within(personalSection).queryByText("На этот день нет персональных слотов")).not.toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });
});
