import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "@/features/auth/auth-store";
import StudentDetail from "./student-detail";

const { get, post, deleteRequest } = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  deleteRequest: vi.fn(),
}));

vi.mock("@/api/custom-fetch", () => ({
  default: { get, post, delete: deleteRequest },
}));

function renderStudentDetail(initialEntry = "/trainer/students/12") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });

  const view = render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <QueryClientProvider client={queryClient}>
        <Routes>
          <Route path="/trainer/students/:studentId" element={<StudentDetail />} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  );

  return { queryClient, ...view };
}

function expectPageToContainCredential(value: string) {
  expect(document.body.textContent?.includes(value)).toBe(true);
}

describe("StudentDetail feedback block", () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
    deleteRequest.mockReset();
  });

  it("shows prior feedback responses and sends a survey", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/students/12/") {
        return Promise.resolve({
          data: {
            id: 12,
            first_name: "Mira",
            last_name: "Ivanova",
            phone: "",
            status: "active",
            contraindications: "",
            notes: [],
            can_manage_feedback: true,
          },
        });
      }
      if (url === "/billing/subscriptions/") {
        return Promise.resolve({ data: [] });
      }
      if (url === "/students/12/personal-bookings/") {
        return Promise.resolve({ data: [] });
      }
      if (url === "/grades/students/12/progress/") {
        return Promise.resolve({ data: [] });
      }
      if (url === "/students/12/checkins/") {
        return Promise.resolve({ data: [] });
      }
      if (url === "/feedback/students/12/responses/") {
        return Promise.resolve({
          data: [
            {
              id: 77,
              student_id: 12,
              form_id: 7,
              submitted_at: "2026-06-08T08:00:00Z",
              answers: [
                {
                  question_id: 701,
                  question_type: "rating",
                  question_text: "Оцените тренировку",
                  rating_value: 5,
                  bool_value: null,
                  text_value: "",
                },
                {
                  question_id: 702,
                  question_type: "text",
                  question_text: "Комментарий",
                  rating_value: null,
                  bool_value: null,
                  text_value: "Хочет продолжить",
                },
              ],
            },
          ],
        });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });
    post.mockResolvedValue({ data: { status: "scheduled" } });

    renderStudentDetail();

    expect(await screen.findByText("Mira Ivanova")).toBeInTheDocument();
    expect(await screen.findByText("Опросы")).toBeInTheDocument();
    expect(screen.getByText("Оцените тренировку")).toBeInTheDocument();
    expect(screen.getByText("Оценка 5/5")).toBeInTheDocument();
    expect(screen.getByText("Комментарий")).toBeInTheDocument();
    expect(screen.getByText("Хочет продолжить")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Отправить опрос" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/feedback/send-survey/12/");
    });
    expect(await screen.findByText("Опрос отправлен")).toBeInTheDocument();
  });

  it("hides manual survey send when trainer only has feedback read access", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/students/12/") {
        return Promise.resolve({
          data: {
            id: 12,
            first_name: "Mira",
            last_name: "Ivanova",
            phone: "",
            status: "active",
            contraindications: "",
            notes: [],
            can_manage_feedback: false,
          },
        });
      }
      if (url === "/billing/subscriptions/") {
        return Promise.resolve({ data: [] });
      }
      if (url === "/students/12/personal-bookings/") {
        return Promise.resolve({ data: [] });
      }
      if (url === "/grades/students/12/progress/") {
        return Promise.resolve({ data: [] });
      }
      if (url === "/students/12/checkins/") {
        return Promise.resolve({ data: [] });
      }
      if (url === "/feedback/students/12/responses/") {
        return Promise.resolve({
          data: [
            {
              id: 77,
              student_id: 12,
              form_id: 7,
              submitted_at: "2026-06-08T08:00:00Z",
              answers: [
                {
                  question_id: 701,
                  question_type: "rating",
                  question_text: "Оцените тренировку",
                  rating_value: 5,
                  bool_value: null,
                  text_value: "",
                },
              ],
            },
          ],
        });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderStudentDetail();

    expect(await screen.findByText("Mira Ivanova")).toBeInTheDocument();
    expect(await screen.findByText("Оценка 5/5")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Отправить опрос" })).not.toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });
});

describe("StudentDetail lead action context", () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
    deleteRequest.mockReset();
  });

  it("identifies and focuses an exact trial resource after a direct reload", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/students/12/") {
        return Promise.resolve({
          data: {
            id: 12,
            first_name: "Mira",
            last_name: "Ivanova",
            phone: "",
            status: "active",
            contraindications: "",
            notes: [],
            account_access: null,
          },
        });
      }
      if (url === "/leads/12/action-context") {
        return Promise.resolve({
          data: {
            primary_action: {
              kind: "open_trial",
              label: "Открыть пробную",
              supporting_text: "Пробная тренировка 15.08.2026 18:00.",
              target_resource_type: "schedule_enrollment",
              target_resource_id: 91,
              context: "upcoming_trial",
            },
            active_context: "trial",
            secondary_capabilities: [],
          },
        });
      }
      if (
        url === "/billing/subscriptions/" ||
        url === "/students/12/personal-bookings/" ||
        url === "/grades/students/12/progress/" ||
        url === "/students/12/checkins/" ||
        url === "/feedback/students/12/responses/"
      ) {
        return Promise.resolve({ data: [] });
      }
      if (url === "/billing/bank-payment-orders/") {
        return Promise.resolve({ data: { items: [] } });
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderStudentDetail(
      "/trainer/students/12?context=upcoming_trial&resource_type=schedule_enrollment&resource_id=91",
    );

    const context = await screen.findByRole("status", { name: "Точный контекст из заявки" });
    expect(context).toHaveTextContent("Пробная тренировка #91");
    expect(context).toHaveTextContent("Пробная тренировка 15.08.2026 18:00.");
    expect(context).toHaveTextContent("Следующее действие: Открыть пробную");
    expect(context).toHaveFocus();
  });

  it("rejects a forged or no-longer-live exact resource after a direct reload", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/students/12/") {
        return Promise.resolve({
          data: {
            id: 12,
            first_name: "Mira",
            last_name: "Ivanova",
            phone: "",
            status: "active",
            contraindications: "",
            notes: [],
            account_access: null,
          },
        });
      }
      if (url === "/leads/12/action-context") {
        return Promise.resolve({
          data: {
            primary_action: null,
            active_context: null,
            secondary_capabilities: [],
          },
        });
      }
      if (
        url === "/billing/subscriptions/" ||
        url === "/students/12/personal-bookings/" ||
        url === "/grades/students/12/progress/" ||
        url === "/students/12/checkins/" ||
        url === "/feedback/students/12/responses/"
      ) {
        return Promise.resolve({ data: [] });
      }
      if (url === "/billing/bank-payment-orders/") {
        return Promise.resolve({ data: { items: [] } });
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderStudentDetail(
      "/trainer/students/12?context=upcoming_trial&resource_type=schedule_enrollment&resource_id=999999",
    );

    expect(
      await screen.findByRole("alert"),
    ).toHaveTextContent("Точный контекст больше недоступен");
    expect(
      screen.queryByRole("status", { name: "Точный контекст из заявки" }),
    ).not.toBeInTheDocument();
  });
});

describe("StudentDetail subscription payment recovery", () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
    deleteRequest.mockReset();
  });

  function mockStudentCardWithLiveOrder({
    subscriptionOverrides = {},
    orderOverrides = {},
  }: {
    subscriptionOverrides?: Record<string, unknown>;
    orderOverrides?: Record<string, unknown>;
  } = {}) {
    get.mockImplementation((url: string) => {
      if (url === "/students/12/") {
        return Promise.resolve({
          data: {
            id: 12,
            first_name: "Mira",
            last_name: "Ivanova",
            phone: "",
            status: "active",
            contraindications: "",
            notes: [],
            can_manage_feedback: true,
          },
        });
      }
      if (url === "/billing/subscriptions/") {
        return Promise.resolve({
          data: [
            {
              id: 54,
              tariff_name: "Pending BJJ",
              trainings_used: 0,
              trainings_total: 8,
              trainings_left: 8,
              expires_at: null,
              status: "pending",
              ...subscriptionOverrides,
            },
          ],
        });
      }
      if (url === "/billing/bank-payment-orders/") {
        return Promise.resolve({
          data: {
            items: [
              {
                id: 901,
                subscription_id: 54,
                tariff_id: 3,
                debt_ids: [],
                status: "pending",
                amount_snapshot: "5000.00",
                currency: "RUB",
                provider_payment_link_id: "test-901",
                provider_payment_url: "https://bank.example/pay/901",
                provider_status: "CREATED",
                expires_at: "2099-06-28T12:00:00Z",
                can_pay: false,
                can_share: true,
                can_copy: true,
                can_show_qr: true,
                can_request_refresh: true,
                can_cancel: true,
                ...orderOverrides,
              },
            ],
          },
        });
      }
      if (url === "/students/12/personal-bookings/") return Promise.resolve({ data: [] });
      if (url === "/grades/students/12/progress/") return Promise.resolve({ data: [] });
      if (url === "/students/12/checkins/") return Promise.resolve({ data: [] });
      if (url === "/feedback/students/12/responses/") return Promise.resolve({ data: [] });
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });
  }

  it("cancels the exact linked order and awaits every related card invalidation", async () => {
    mockStudentCardWithLiveOrder();
    post.mockResolvedValue({ data: { id: 901, status: "cancelled" } });
    const { queryClient } = renderStudentDetail();
    let resolveSubscriptionsInvalidation: (() => void) | undefined;
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries").mockImplementation((filters) => {
      if (JSON.stringify(filters?.queryKey) === JSON.stringify(["student", "12", "subscriptions"])) {
        return new Promise<void>((resolve) => {
          resolveSubscriptionsInvalidation = () => resolve();
        });
      }
      return Promise.resolve();
    });

    const paymentPanel = await screen.findByRole("region", { name: "Онлайн-оплата" });
    expect(screen.getByText("Pending BJJ")).toBeInTheDocument();
    expect(within(paymentPanel).getByRole("link", { name: "Открыть предпросмотр" })).toHaveAttribute(
      "href",
      "https://bank.example/pay/901",
    );
    fireEvent.click(within(paymentPanel).getByRole("button", { name: "Отменить оплату" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Отменить оплату" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/billing/bank-payment-orders/901/cancel/", {});
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["student", "12"] });
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["student", "12", "subscriptions"] });
      expect(invalidateSpy).toHaveBeenCalledWith({
        queryKey: ["billing", "bank-payment-orders", 12],
      });
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["billing", "debts", 12] });
      expect(invalidateSpy).toHaveBeenCalledWith({
        queryKey: ["student", "12", "personal-bookings"],
      });
      expect(resolveSubscriptionsInvalidation).toEqual(expect.any(Function));
      expect(
        within(paymentPanel).getByRole("button", { name: "Отмена..." }),
      ).toBeDisabled();
    });

    resolveSubscriptionsInvalidation?.();
    await waitFor(() => {
      expect(
        within(paymentPanel).getByRole("button", { name: "Отменить оплату" }),
      ).toBeEnabled();
    });
  });

  it("retains the linked order when direct cancellation fails", async () => {
    mockStudentCardWithLiveOrder();
    post.mockRejectedValue(new Error("cancel failed"));
    renderStudentDetail();

    const paymentPanel = await screen.findByRole("region", { name: "Онлайн-оплата" });
    fireEvent.click(within(paymentPanel).getByRole("button", { name: "Отменить оплату" }));
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Отменить оплату" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent("Не удалось отменить оплату");
    expect(screen.getByRole("region", { name: "Онлайн-оплата" })).toBeInTheDocument();
    expect(post).toHaveBeenCalledWith("/billing/bank-payment-orders/901/cancel/", {});
  });

  it("keeps the accepted renewal target and amount after the current subscription offer changes", async () => {
    mockStudentCardWithLiveOrder({
      subscriptionOverrides: {
        tariff_name: "Base C",
        renewal_target_tariff_id: 10,
        renewal_target_tariff_name: "Base C",
        renewal_target_price: "7000.00",
      },
      orderOverrides: {
        renewed_from_subscription_id: 54,
        renewal_source_tariff_id: 3,
        renewal_source_tariff_name: "Base A",
        renewal_target_tariff_id: 9,
        renewal_target_tariff_name: "Base B",
        renewal_target_price: "6500.00",
        amount_snapshot: "6500.00",
      },
    });

    renderStudentDetail();

    const paymentPanel = await screen.findByRole("region", { name: "Онлайн-оплата" });
    expect(within(paymentPanel).getByText(/Base B.*6\s?500/)).toBeInTheDocument();
    expect(within(paymentPanel).getByText("6 500 ₽")).toBeInTheDocument();
    expect(within(paymentPanel).queryByText(/Base C/)).not.toBeInTheDocument();
  });
});

describe("StudentDetail personal bookings block", () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
    deleteRequest.mockReset();
  });

  function mockPersonalBookingPage(bookings: Array<Record<string, unknown>>) {
    get.mockImplementation((url: string) => {
      if (url === "/students/12/") {
        return Promise.resolve({
          data: {
            id: 12,
            first_name: "Mira",
            last_name: "Ivanova",
            phone: "",
            status: "active",
            contraindications: "",
            notes: [],
            can_manage_feedback: true,
          },
        });
      }
      if (url === "/billing/bank-payment-orders/901/") {
        return Promise.resolve({
          data: {
            id: 901,
            payment_id: 801,
            subscription_id: null,
            student_id: 12,
            tariff_id: 12,
            debt_ids: [],
            source: "trainer",
            status: "pending",
            amount_snapshot: "2000.00",
            currency: "RUB",
            purpose_snapshot: "Разовая персоналка",
            provider_payment_url: "https://bank.example/pay/901",
            expires_at: "2099-04-07T12:00:00Z",
            can_pay: false,
            can_share: true,
            can_copy: true,
            can_show_qr: true,
            can_request_refresh: true,
            can_cancel: true,
          },
        });
      }
      if (url === "/billing/subscriptions/") return Promise.resolve({ data: [] });
      if (url === "/students/12/personal-bookings/") return Promise.resolve({ data: bookings });
      if (url === "/grades/students/12/progress/") return Promise.resolve({ data: [] });
      if (url === "/students/12/checkins/") return Promise.resolve({ data: [] });
      if (url === "/feedback/students/12/responses/") return Promise.resolve({ data: [] });
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });
  }

  function dropInBooking(overrides: Record<string, unknown> = {}) {
    return {
      booking_id: 71,
      booking_kind: "drop_in",
      enrollment_id: 77,
      schedule_id: 51,
      student_id: 12,
      trainer_id: 3,
      trainer_name: "Coach Main",
      location_id: 5,
      location_name: "Central Hall",
      training_type_id: 9,
      training_type_name: "Персональная",
      starts_at: "2099-04-07T10:00:00",
      ends_at: "2099-04-07T11:00:00",
      created_from: "personal_drop_in",
      status: "active",
      attendance_state: "scheduled",
      financial_state: "pay_at_club",
      can_manage: true,
      can_cancel: false,
      can_mark_no_show: false,
      ...overrides,
    };
  }

  it("shows upcoming personal bookings and creates a trainer personal booking", async () => {
    useAuthStore.setState({ clubId: 1 });
    get.mockImplementation((url: string) => {
      if (url === "/students/12/") {
        return Promise.resolve({
          data: {
            id: 12,
            first_name: "Mira",
            last_name: "Ivanova",
            phone: "8 900 123 45 67",
            status: "active",
            is_child: false,
            contraindications: "",
            notes: [],
            account_access: null,
            has_parent_user: false,
            can_manage_account_access: true,
          },
        });
      }
      if (url === "/billing/subscriptions/") {
        return Promise.resolve({
          data: [
            {
              id: 44,
              tariff: {
                name: "Персональный пакет",
                trainings_limit: 8,
                training_type: {
                  id: 9,
                  name: "Персональная",
                  kind: "personal",
                },
              },
              trainings_used: 1,
              trainings_left: 7,
              expires_at: "2099-08-01T00:00:00Z",
              status: "active",
              paid_amount: "5000.00",
            },
          ],
        });
      }
      if (url === "/students/12/personal-bookings/") {
        return Promise.resolve({
          data: [
            {
              schedule_id: 51,
              enrollment_id: 77,
              student_id: 12,
              trainer_id: 3,
              trainer_name: "Coach Main",
              location_id: 5,
              location_name: "Central Hall",
              training_type_id: 9,
              training_type_name: "Персональная",
              starts_at: "2099-04-07T10:00:00",
              ends_at: "2099-04-07T11:00:00",
              created_from: "personal_booking",
              status: "active",
            },
          ],
        });
      }
      if (url === "/grades/students/12/progress/") {
        return Promise.resolve({ data: [] });
      }
      if (url === "/students/12/checkins/") {
        return Promise.resolve({ data: [] });
      }
      if (url === "/feedback/students/12/responses/") {
        return Promise.resolve({ data: [] });
      }
      if (url === "/clubs/locations/") {
        return Promise.resolve({ data: [{ id: 5, name: "Central Hall" }] });
      }
      if (url === "/billing/training-types/") {
        return Promise.resolve({
          data: [
            {
              id: 9,
              name: "Персональная",
              slug: "personal",
              kind: "personal",
              is_active: true,
            },
          ],
        });
      }
      if (url === "/personal-availability/capability/") {
        return Promise.resolve({ data: { enabled: false } });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });
    post.mockResolvedValue({ data: { enrollment_id: 88 } });

    renderStudentDetail();

    expect(await screen.findByText("Mira Ivanova")).toBeInTheDocument();
    expect(await screen.findByText("10:00 - 11:00")).toBeInTheDocument();
    expect(screen.getByText("Central Hall, Coach Main")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Записать" }));
    expect(await screen.findByText("Записать персоналку")).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "По абонементу" }));
    fireEvent.change(screen.getByLabelText(/Абонемент/), {
      target: { value: "44" },
    });
    fireEvent.change(screen.getByLabelText(/Дата/), {
      target: { value: "2099-04-08" },
    });
    fireEvent.change(screen.getByLabelText(/Начало/), {
      target: { value: "12:00" },
    });
    fireEvent.change(screen.getByLabelText(/Конец/), {
      target: { value: "13:00" },
    });
    fireEvent.change(screen.getByLabelText(/Зал/), {
      target: { value: "5" },
    });

    const submitButton = screen.getAllByRole("button", { name: "Записать" }).at(-1);
    await waitFor(() => {
      expect(submitButton).toBeEnabled();
    });
    fireEvent.submit(submitButton!.closest("form")!);

    await waitFor(() => {
      expect(post).toHaveBeenCalled();
    });
    const [url, payload] = post.mock.calls[0];
    expect(url).toBe("/students/12/personal-bookings/");
    expect(payload).toMatchObject({
      starts_at: "2099-04-08T12:00:00",
      ends_at: "2099-04-08T13:00:00",
      location_id: 5,
      training_type_id: 9,
      subscription_id: 44,
    });
    expect(payload).not.toHaveProperty("trainer_id");
  });

  it("sends explicit audit reasons for drop-in cancellation and no-show", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/students/12/") {
        return Promise.resolve({
          data: {
            id: 12,
            first_name: "Mira",
            last_name: "Ivanova",
            phone: "",
            status: "active",
            contraindications: "",
            notes: [],
            can_manage_feedback: true,
          },
        });
      }
      if (url === "/billing/subscriptions/") return Promise.resolve({ data: [] });
      if (url === "/students/12/personal-bookings/") {
        return Promise.resolve({
          data: [
            {
              booking_id: 71,
              booking_kind: "drop_in",
              enrollment_id: 77,
              schedule_id: 51,
              student_id: 12,
              trainer_id: 3,
              trainer_name: "Coach Main",
              location_id: 5,
              location_name: "Central Hall",
              training_type_id: 9,
              training_type_name: "Персональная",
              starts_at: "2099-04-07T10:00:00",
              ends_at: "2099-04-07T11:00:00",
              created_from: "personal_drop_in",
              status: "active",
              attendance_state: "scheduled",
              financial_state: "pay_at_club",
              can_manage: true,
              can_cancel: true,
              can_mark_no_show: false,
            },
            {
              booking_id: 72,
              booking_kind: "drop_in",
              enrollment_id: 78,
              schedule_id: 52,
              student_id: 12,
              trainer_id: 3,
              trainer_name: "Coach Main",
              location_id: 5,
              location_name: "Central Hall",
              training_type_id: 9,
              training_type_name: "Персональная",
              starts_at: "2099-04-08T10:00:00",
              ends_at: "2099-04-08T11:00:00",
              created_from: "personal_drop_in",
              status: "active",
              attendance_state: "scheduled",
              financial_state: "pay_at_club",
              can_manage: true,
              can_cancel: false,
              can_mark_no_show: true,
            },
          ],
        });
      }
      if (url === "/grades/students/12/progress/") return Promise.resolve({ data: [] });
      if (url === "/students/12/checkins/") return Promise.resolve({ data: [] });
      if (url === "/feedback/students/12/responses/") return Promise.resolve({ data: [] });
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });
    post.mockResolvedValue({ data: {} });

    renderStudentDetail();

    expect(
      await screen.findAllByText("Оплата в клубе: долг появится после check-in."),
    ).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "Отменить запись" }));
    fireEvent.click(screen.getByRole("button", { name: "Не пришёл" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/personal-drop-in-bookings/71/cancel/", {
        reason: "Отменено тренером в карточке ученика",
      });
      expect(post).toHaveBeenCalledWith("/personal-drop-in-bookings/72/no-show/", {
        reason: "Клиент не пришёл",
      });
    });
  });

  it("shows and cancels only the exact linked online payment attempt", async () => {
    mockPersonalBookingPage([
      dropInBooking({
        booking_id: 71,
        financial_state: "payment_pending",
        payment_id: 801,
        bank_payment_order_id: 901,
        payment_status: "pending",
        order_status: "pending",
        provider_payment_url: "https://bank.example/pay/901",
        can_cancel_payment: true,
        next_action_label: "Оплата ожидает подтверждения",
      }),
      dropInBooking({
        booking_id: 72,
        enrollment_id: 78,
        schedule_id: 52,
        financial_state: "payment_pending",
        payment_id: 802,
        bank_payment_order_id: null,
        provider_payment_url: "",
        can_cancel_payment: false,
      }),
    ]);
    post.mockResolvedValue({ data: {} });
    const { queryClient } = renderStudentDetail();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    expect(await screen.findByText("Онлайн-оплата ожидает подтверждения владельцем.")).toBeInTheDocument();
    expect(screen.getByText("Ручная оплата ожидает подтверждения владельцем.")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Отправить ссылку" })).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith("/billing/bank-payment-orders/901/");
    expect(screen.getByRole("link", { name: "Открыть предпросмотр" })).toHaveAttribute(
      "href",
      "https://bank.example/pay/901",
    );
    expect(screen.queryByText("https://bank.example/pay/901")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Отменить ссылку на оплату" }));
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "Отменить ссылку на оплату",
      }),
    );

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/billing/bank-payment-orders/901/cancel/", {});
    });
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: ["student", "12", "personal-bookings"],
    });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["billing", "debts", 12] });
    expect(screen.getAllByRole("link", { name: "Открыть предпросмотр" })).toHaveLength(1);
  });

  it("keeps another trainer's drop-ins readable while hiding every booking action", async () => {
    mockPersonalBookingPage([
      dropInBooking({
        booking_id: 71,
        starts_at: "2099-04-07T10:00:00",
        ends_at: "2099-04-07T11:00:00",
        financial_state: "pay_at_club",
        can_manage: false,
        can_mark_no_show: true,
        can_cancel: true,
      }),
      dropInBooking({
        booking_id: 72,
        enrollment_id: 78,
        schedule_id: 52,
        starts_at: "2099-04-08T10:00:00",
        ends_at: "2099-04-08T11:00:00",
        financial_state: "debt_open",
        can_manage: false,
      }),
      dropInBooking({
        booking_id: 73,
        enrollment_id: 79,
        schedule_id: 53,
        starts_at: "2099-04-09T10:00:00",
        ends_at: "2099-04-09T11:00:00",
        financial_state: "payment_pending",
        bank_payment_order_id: 901,
        order_status: "pending",
        provider_payment_url: "https://bank.example/pay/901",
        can_cancel_payment: true,
        can_manage: false,
      }),
      dropInBooking({
        booking_id: 74,
        enrollment_id: 80,
        schedule_id: 54,
        starts_at: "2099-04-10T10:00:00",
        ends_at: "2099-04-10T11:00:00",
        financial_state: "pay_at_club",
        can_manage: true,
        can_cancel: true,
      }),
    ]);

    renderStudentDetail();

    const readOnlyPrepayment = await screen.findByRole("article", {
      name: "Персоналка 2099-04-07 10:00 - 11:00",
    });
    expect(within(readOnlyPrepayment).getByText("Оплата в клубе: долг появится после check-in.")).toBeInTheDocument();
    expect(within(readOnlyPrepayment).queryByRole("button", { name: "Предоплата" })).not.toBeInTheDocument();
    expect(within(readOnlyPrepayment).queryByRole("button", { name: "Не пришёл" })).not.toBeInTheDocument();
    expect(within(readOnlyPrepayment).queryByRole("button", { name: "Отменить запись" })).not.toBeInTheDocument();

    const readOnlyDebt = screen.getByRole("article", {
      name: "Персоналка 2099-04-08 10:00 - 11:00",
    });
    expect(within(readOnlyDebt).getByText("Долг за персоналку")).toBeInTheDocument();
    expect(within(readOnlyDebt).queryByRole("button", { name: "Принять оплату" })).not.toBeInTheDocument();

    const readOnlyPayment = screen.getByRole("article", {
      name: "Персоналка 2099-04-09 10:00 - 11:00",
    });
    expect(within(readOnlyPayment).getByText("Оплата ожидает подтверждения владельцем.")).toBeInTheDocument();
    expect(within(readOnlyPayment).queryByText(/Онлайн-оплата|Ручная оплата/)).not.toBeInTheDocument();
    expect(within(readOnlyPayment).queryByRole("link", { name: "Открыть предпросмотр" })).not.toBeInTheDocument();
    expect(within(readOnlyPayment).queryByRole("button", { name: "Отменить ссылку на оплату" })).not.toBeInTheDocument();

    const ownedBooking = screen.getByRole("article", {
      name: "Персоналка 2099-04-10 10:00 - 11:00",
    });
    expect(within(ownedBooking).getByRole("button", { name: "Предоплата" })).toBeInTheDocument();
    expect(within(ownedBooking).getByRole("button", { name: "Отменить запись" })).toBeInTheDocument();
  });

  it("prioritizes no-show over payment actions for a past unattended personal booking", async () => {
    mockPersonalBookingPage([
      dropInBooking({
        can_mark_no_show: true,
        financial_state: "payment_pending",
        bank_payment_order_id: 901,
        provider_payment_url: "https://bank.example/pay/901",
        can_cancel_payment: true,
        next_action_label: "Не пришёл",
      }),
    ]);

    renderStudentDetail();

    expect(await screen.findByRole("button", { name: "Не пришёл" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Предоплата" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Открыть предпросмотр" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Отменить ссылку на оплату" })).not.toBeInTheDocument();
  });

  it("shows terminal financial truth and no payment CTA for cancelled or no-show drop-ins", async () => {
    mockPersonalBookingPage([
      dropInBooking({
        attendance_state: "cancelled",
        status: "cancelled",
        financial_state: "pay_at_club",
        can_cancel: true,
      }),
      dropInBooking({
        booking_id: 72,
        enrollment_id: 78,
        attendance_state: "no_show",
        status: "cancelled",
        financial_state: "payment_pending",
        bank_payment_order_id: 901,
        provider_payment_url: "https://bank.example/pay/901",
        can_cancel_payment: true,
        can_cancel: true,
      }),
    ]);

    renderStudentDetail();

    expect(await screen.findByText("Запись отменена. Долг за непосещённую персоналку не создан.")).toBeInTheDocument();
    expect(screen.getByText("Клиент не пришёл. Связанная оплата ожидает решения владельца.")).toBeInTheDocument();
    expect(screen.queryByText(/долг появится после check-in/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Предоплата" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Открыть предпросмотр" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Отменить ссылку на оплату" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Отменить запись" })).not.toBeInTheDocument();
  });

  it("shows a visible Russian error when cancellation or no-show is rejected", async () => {
    mockPersonalBookingPage([
      dropInBooking({ booking_id: 71, can_cancel: true }),
      dropInBooking({
        booking_id: 72,
        enrollment_id: 78,
        schedule_id: 52,
        can_mark_no_show: true,
        next_action_label: "Не пришёл",
      }),
    ]);
    post
      .mockRejectedValueOnce({ response: { data: { detail: "Отменить запись уже нельзя" } } })
      .mockRejectedValueOnce({ response: { data: { detail: "Неявка уже подтверждена" } } });

    renderStudentDetail();
    await screen.findByRole("button", { name: "Отменить запись" });
    fireEvent.click(screen.getByRole("button", { name: "Отменить запись" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Отменить запись уже нельзя");

    fireEvent.click(screen.getByRole("button", { name: "Не пришёл" }));
    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent("Неявка уже подтверждена");
    });
  });
});

describe("StudentDetail account access block", () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
    deleteRequest.mockReset();
  });

  function mockDetailRequests(
    student: Record<string, unknown>,
    subscriptions: Array<Record<string, unknown>> = [
      {
        id: 44,
        tariff_name: "Муай тай",
        trainings_used: 0,
        trainings_total: 8,
        trainings_left: 8,
        expires_at: "2099-01-01T00:00:00Z",
        status: "active",
        paid_amount: "5000.00",
      },
    ],
    grades: Array<Record<string, unknown>> = [],
    options: { omitSensitiveActionsPermission?: boolean } = {},
  ) {
    get.mockImplementation((url: string) => {
      if (url === "/students/12/") {
        const data: Record<string, unknown> = {
            id: 12,
            first_name: "Mira",
            last_name: "Ivanova",
            phone: "8 900 123 45 67",
            status: "active",
            is_child: false,
            contraindications: "",
            notes: [],
            account_access: null,
            has_parent_user: false,
            can_manage_account_access: true,
            account_access_eligible: true,
            can_manage_sensitive_actions: true,
            can_manage_feedback: true,
            ...student,
        };
        if (options.omitSensitiveActionsPermission) {
          delete data.can_manage_sensitive_actions;
        }
        return Promise.resolve({ data });
      }
      if (url === "/billing/subscriptions/") {
        return Promise.resolve({ data: subscriptions });
      }
      if (url === "/students/12/personal-bookings/") {
        return Promise.resolve({ data: [] });
      }
      if (url === "/grades/students/12/progress/") {
        return Promise.resolve({ data: grades });
      }
      if (url === "/students/12/checkins/") {
        return Promise.resolve({ data: [] });
      }
      if (url === "/feedback/students/12/responses/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });
  }

  it("shows the permitted generic payment action first and opens only the generic sheet", async () => {
    mockDetailRequests({ can_manage_sensitive_actions: true });

    renderStudentDetail();

    expect(await screen.findByText("Mira Ivanova")).toBeInTheDocument();
    expect(screen.queryByText("Другие действия")).not.toBeInTheDocument();
    const accountAccessButton = screen.getByRole("button", { name: "Открыть кабинет" });
    expect(accountAccessButton).toBeInTheDocument();

    const paymentAction = screen.getByRole("button", { name: "Принять оплату" });
    expect(paymentAction).toHaveAttribute("type", "button");
    expect(paymentAction).toHaveClass("min-h-[44px]", "whitespace-normal");
    expect(paymentAction.parentElement).toHaveClass("flex-wrap");

    get.mockClear();
    fireEvent.click(paymentAction);

    expect(post).not.toHaveBeenCalled();
    expect(await screen.findByRole("dialog", { name: "Принять оплату" })).toBeInTheDocument();
    await waitFor(() => {
      expect(get).toHaveBeenCalledWith("/billing/tariffs/");
    });
    expect(
      get.mock.calls.some(([url]) => String(url).startsWith("/personal-drop-in-bookings/")),
    ).toBe(false);
    expect(accountAccessButton).toBeInTheDocument();
  });

  it.each([
    ["false", false],
    ["null", null],
  ])("hides the generic payment action and does not mount its queries when permission is %s", async (_case, permission) => {
    mockDetailRequests({ can_manage_sensitive_actions: permission });

    renderStudentDetail();

    expect(await screen.findByText("Mira Ivanova")).toBeInTheDocument();
    await waitFor(() => {
      expect(get).toHaveBeenCalledWith("/feedback/students/12/responses/");
    });
    expect(screen.queryByRole("button", { name: "Принять оплату" })).not.toBeInTheDocument();
    expect(get).not.toHaveBeenCalledWith("/billing/tariffs/");
    expect(get).not.toHaveBeenCalledWith("/billing/discounts/");
    expect(get).not.toHaveBeenCalledWith("/billing/debts/?student_id=12");
  });

  it("fails closed when the sensitive-action permission is missing", async () => {
    mockDetailRequests({}, undefined, undefined, { omitSensitiveActionsPermission: true });

    renderStudentDetail();

    expect(await screen.findByText("Mira Ivanova")).toBeInTheDocument();
    await waitFor(() => {
      expect(get).toHaveBeenCalledWith("/feedback/students/12/responses/");
    });
    expect(screen.queryByRole("button", { name: "Принять оплату" })).not.toBeInTheDocument();
    expect(get).not.toHaveBeenCalledWith("/billing/tariffs/");
    expect(get).not.toHaveBeenCalledWith("/billing/discounts/");
    expect(get).not.toHaveBeenCalledWith("/billing/debts/?student_id=12");
  });

  it("renders pending check-in readiness from the server contract", async () => {
    mockDetailRequests({
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
        covered_visit_count: 0,
      },
    });

    renderStudentDetail();

    expect(await screen.findByText("Оплата ожидает подтверждения")).toBeInTheDocument();
    expect(
      screen.getByText("Записан в Tue Thu Group · доступен для чекина с 2026-07-22"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/чекин сейчас недоступен/)).not.toBeInTheDocument();
  });

  it("renders a personal v2 admission without a group label", async () => {
    mockDetailRequests({
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
    });

    renderStudentDetail();

    expect(await screen.findByText("Оплата ожидает подтверждения")).toBeInTheDocument();
    expect(screen.getByText("Персональная запись · доступна для чекина 2026-07-24")).toBeInTheDocument();
    expect(screen.queryByText(/Записан в/)).not.toBeInTheDocument();
  });

  it("renders pending check-in unavailability from the server contract", async () => {
    mockDetailRequests({
      operational_admission: {
        payment_id: 92,
        payment_status: "pending",
        payment_method: "transfer",
        subscription_status: "pending",
        enrollment_status: "active",
        group_label: "Future Group",
        start_date: "2026-07-29",
        checkin_ready: false,
        account_access_eligible: true,
        covered_visit_count: 0,
      },
    });

    renderStudentDetail();

    expect(await screen.findByText("Оплата ожидает подтверждения")).toBeInTheDocument();
    expect(
      screen.getByText("Записан в Future Group · чекин сейчас недоступен (старт 2026-07-29)"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/доступен для чекина с/)).not.toBeInTheDocument();
  });

  it("opens adult account access and shows the one-time password", async () => {
    const issuedLogin = ["+7", "900", "123", "45", "67"].join("");
    const temporaryPassword = ["temporary", "pass", "visible", "once"].join("-");
    mockDetailRequests({});
    post.mockResolvedValue({
      data: {
        student_id: 12,
        role: "student",
        status: "open",
        username: issuedLogin,
        must_change_password: true,
        issued_at: "2026-06-19T08:00:00Z",
        reset_at: null,
        temporary_password: temporaryPassword,
        created_user: true,
        created_membership: true,
        created_access: true,
      },
    });

    renderStudentDetail();

    expect(await screen.findByText("Mira Ivanova")).toBeInTheDocument();
    const openButton = await screen.findByRole("button", { name: "Открыть кабинет" });
    await waitFor(() => {
      expect(openButton).toBeEnabled();
    });
    fireEvent.click(openButton);

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/students/12/account-access/open/", {});
    });
    expectPageToContainCredential(issuedLogin);
    expectPageToContainCredential(temporaryPassword);
  });

  it("sends explicit parent phone for child account access", async () => {
    const parentPhone = ["8 901", "222", "33", "44"].join(" ");
    const issuedParentLogin = ["+7", "901", "222", "33", "44"].join("");
    mockDetailRequests({
      is_child: true,
      phone: "8 900 123 45 67",
    });
    post.mockResolvedValue({
      data: {
        student_id: 12,
        role: "parent",
        status: "open",
        username: issuedParentLogin,
        must_change_password: true,
        issued_at: "2026-06-19T08:00:00Z",
        reset_at: null,
        temporary_password: "temporary-parent-pass",
        created_user: true,
        created_membership: true,
        created_access: true,
      },
    });

    renderStudentDetail();

    expect(await screen.findByText("Mira Ivanova")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Телефон родителя"), {
      target: { value: parentPhone },
    });
    const openButton = screen.getByRole("button", { name: "Открыть кабинет" });
    await waitFor(() => {
      expect(openButton).toBeEnabled();
    });
    fireEvent.click(openButton);

    await waitFor(() => {
      expect(post).toHaveBeenCalled();
    });
    const submittedParentPhone = post.mock.calls.some(([url, payload]) => {
      const body = payload as Record<string, unknown>;
      return url === "/students/12/account-access/open/" && body.parent_phone === parentPhone;
    });
    expect(submittedParentPhone).toBe(true);
    expectPageToContainCredential(issuedParentLogin);
  });

  it("keeps child parent phone blank and disables open until parent phone is entered", async () => {
    mockDetailRequests({
      is_child: true,
      phone: "8 900 123 45 67",
      has_parent_user: false,
    });

    renderStudentDetail();

    expect(await screen.findByText("Mira Ivanova")).toBeInTheDocument();
    const input = screen.getByLabelText("Телефон родителя") as HTMLInputElement;
    expect(input.value).toBe("");
    expect(screen.getByRole("button", { name: "Открыть кабинет" })).toBeDisabled();
  });

  it("disables account access actions when trainer cannot manage this student", async () => {
    mockDetailRequests({
      can_manage_account_access: false,
    });

    renderStudentDetail();

    expect(await screen.findByText("Mira Ivanova")).toBeInTheDocument();
    expect(
      screen.getByText("Доступ может открыть ответственный тренер или администратор"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Открыть кабинет" }));
    expect(post).not.toHaveBeenCalled();
  });

  it("hides sensitive management actions for a read-only trainer scope", async () => {
    mockDetailRequests(
      {
        can_manage_sensitive_actions: false,
        can_manage_account_access: false,
        can_manage_feedback: false,
      },
      [
        {
          id: 44,
          tariff_name: "Муай тай",
          trainings_used: 0,
          trainings_total: 8,
          trainings_left: 8,
          expires_at: "2099-01-01T00:00:00Z",
          status: "active",
          paid_amount: "5000.00",
        },
      ],
      [
        {
          student_grade_id: 91,
          grade_system_id: 7,
          grade_system_name: "Муай тай",
          current_grade: {
            id: 3,
            name: "Белый",
            order: 1,
            min_trainings: 0,
          },
          trainings_since_last_grade: 2,
          next_grade: {
            id: 4,
            name: "Желтый",
            order: 2,
            min_trainings: 10,
          },
          trainings_to_next: 8,
        },
      ],
    );

    renderStudentDetail();

    expect(await screen.findByText("Mira Ivanova")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Записать" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Принять оплату" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Заморозить" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Повысить грейд" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "+ Дисциплина" })).not.toBeInTheDocument();
    expect(screen.queryByTitle("Убрать дисциплину")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Отправить опрос" })).not.toBeInTheDocument();
  });

  it("disables freeze action when an active subscription already has a pending freeze", async () => {
    mockDetailRequests(
      {
        can_manage_sensitive_actions: true,
      },
      [
        {
          id: 44,
          tariff_name: "Муай тай",
          trainings_used: 0,
          trainings_total: 8,
          trainings_left: 8,
          expires_at: "2099-01-01T00:00:00Z",
          status: "active",
          paid_amount: "5000.00",
          freeze_status: "pending",
        },
      ],
    );

    renderStudentDetail();

    expect(await screen.findByText("Mira Ivanova")).toBeInTheDocument();
    expect(screen.getByText("Заявка на заморозку ждёт")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Заявка ждёт" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Заморозить" })).not.toBeInTheDocument();
  });

  it("opens child account access for an already linked parent without sending child phone", async () => {
    mockDetailRequests({
      is_child: true,
      phone: "8 900 123 45 67",
      has_parent_user: true,
    });
    post.mockResolvedValue({
      data: {
        student_id: 12,
        role: "parent",
        status: "open",
        username: "+79012223344",
        must_change_password: true,
        issued_at: "2026-06-19T08:00:00Z",
        reset_at: null,
        temporary_password: "temporary-parent-pass",
        created_user: false,
        created_membership: true,
        created_access: true,
      },
    });

    renderStudentDetail();

    expect(await screen.findByText("Mira Ivanova")).toBeInTheDocument();
    expect(screen.queryByLabelText("Телефон родителя")).not.toBeInTheDocument();
    const openButton = screen.getByRole("button", { name: "Открыть кабинет" });
    await waitFor(() => {
      expect(openButton).toBeEnabled();
    });
    fireEvent.click(openButton);

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/students/12/account-access/open/", {});
    });
  });

  it("resets existing account access and shows the new one-time password", async () => {
    const issuedLogin = ["+7", "900", "123", "45", "67"].join("");
    const temporaryPassword = ["new", "reset", "password", "visible", "once"].join("-");
    mockDetailRequests({
      account_access: {
        role: "student",
        status: "open",
        username: issuedLogin,
        must_change_password: false,
        issued_at: "2026-06-19T08:00:00Z",
        reset_at: null,
      },
    });
    post.mockResolvedValue({
      data: {
        student_id: 12,
        role: "student",
        status: "reset",
        username: issuedLogin,
        must_change_password: true,
        issued_at: "2026-06-19T08:00:00Z",
        reset_at: "2026-06-19T09:00:00Z",
        temporary_password: temporaryPassword,
        created_user: false,
        created_membership: false,
        created_access: false,
      },
    });

    renderStudentDetail();

    await waitFor(() => {
      expectPageToContainCredential(issuedLogin);
    });
    fireEvent.click(screen.getByRole("button", { name: "Сбросить пароль" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/students/12/account-access/reset/");
    });
    expectPageToContainCredential(temporaryPassword);
  });
});
