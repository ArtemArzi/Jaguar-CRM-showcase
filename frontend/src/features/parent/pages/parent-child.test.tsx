import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "@/features/auth/auth-store";
import ParentChild from "./parent-child";
import {
  ENABLED_SBP_PAYMENT_CAPABILITIES,
  getPaymentCapabilitiesQueryKey,
} from "@/api/payment-capabilities";
import {
  getPersonalAvailabilityCapabilityQueryKey,
  getUnifiedClientJourneyCapabilityQueryKey,
} from "@/api/unified-client-journey";
import { useBrandingStore } from "@/features/branding/use-branding";
import { privateQueryScope } from "@/api/private-query-cache";

const { get, post } = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}));

vi.mock("@/api/custom-fetch", () => ({
  default: { get, post },
}));

function renderParentChildPage(
  initialEntry = "/parent/child/1",
  paymentCapabilities: unknown = ENABLED_SBP_PAYMENT_CAPABILITIES,
  unifiedClientJourneyEnabled = false,
  skipPersonalAvailabilityCapabilitySeed = false,
  contextualRenewalEnabled: boolean | "error" = false,
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

  const rendered = render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <QueryClientProvider client={queryClient}>
        <Routes>
          <Route path="/parent/child/:childId" element={<ParentChild />} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return { queryClient, ...rendered };
}

function buildProfile(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    id: 1,
    first_name: "Masha",
    last_name: "Ivanova",
    status: "active",
    grade_progress: [
      {
        grade_system_name: "BJJ",
        current_grade: {
          id: 1,
          name: "Yellow belt",
          order: 1,
          min_trainings: 8,
        },
        trainings_since_last_grade: 5,
        next_grade: {
          id: 2,
          name: "Orange belt",
          order: 2,
          min_trainings: 10,
        },
        trainings_to_next: 5,
      },
    ],
    attendance_count: 12,
    active_subscription: {
      id: 5,
      tariff_name: "Base",
      trainings_used: 2,
      trainings_total: 8,
      trainings_left: 6,
      expires_at: "2026-05-01" as string | null,
      status: "active",
    },
    schedule: [],
    ...overrides,
  };
}

describe("ParentChild redesign contract", () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
    useAuthStore.setState({
      accessToken: "header.eyJzdWIiOiJwYXJlbnQtMSJ9.signature",
      role: "parent",
      clubId: 1,
      isAuthenticated: true,
    });
    useBrandingStore.setState({
      timeZone: "Asia/Yekaterinburg",
      timeZoneStatus: "ready",
      timeZoneClubId: 1,
      isTimeZoneAuthoritative: true,
    });
  });

  it("removes Parent A child and payment-link data before Parent B can load the same child route", async () => {
    const parentAToken = "header.eyJzdWIiOiJwYXJlbnQtYSJ9.signature";
    const parentBToken = "header.eyJzdWIiOiJwYXJlbnQtYiJ9.signature";
    let resolveParentB: ((value: { data: ReturnType<typeof buildProfile> }) => void) | null = null;

    useAuthStore.setState({ accessToken: parentAToken, role: "parent", clubId: 1, isAuthenticated: true });
    get.mockImplementation((url: string) => {
      const isParentB = useAuthStore.getState().accessToken === parentBToken;
      if (url === "/parents/children/1/") {
        if (!isParentB) return Promise.resolve({ data: buildProfile({ first_name: "Анна A" }) });
        return new Promise((resolve) => {
          resolveParentB = resolve;
        });
      }
      if (url === "/parents/children/1/attendance/") return Promise.resolve({ data: [] });
      if (url === "/parents/children/1/bank-payment-orders/") {
        return Promise.resolve({
          data: [
            {
              id: 45,
              status: "pending",
              provider_payment_url: isParentB
                ? "https://pay.example/parent-b"
                : "https://pay.example/parent-a",
              amount_snapshot: "2000.00",
              currency: "RUB",
              purpose_snapshot: "Абонемент",
              can_pay: true,
              can_share: false,
              can_copy: false,
              can_show_qr: false,
              can_request_refresh: false,
              can_cancel: false,
            },
          ],
        });
      }
      if (url === "/personal-availability/self-service/commands/") {
        return Promise.resolve({ data: { live: [], latest_terminal: [] } });
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    const { queryClient } = renderParentChildPage();
    expect(await screen.findByText("Анна A Ivanova")).toBeInTheDocument();
    const parentAOrderKey = [
      "parent",
      "child",
      "1",
      "bank-payment-orders",
      "recent",
      ...privateQueryScope({
        clubId: 1,
        actorSubject: "parent-a",
        audience: "parent",
        role: "parent",
      }),
    ] as const;
    queryClient.setQueryData(parentAOrderKey, {
      provider_payment_url: "https://pay.example/parent-a",
    });

    act(() => {
      useAuthStore.getState().logout();
      useAuthStore.getState().setTokens(parentBToken);
      useAuthStore.getState().setUserInfo("parent", 1);
    });

    expect(screen.queryByText("Анна A Ivanova")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Оплатить через СБП/ })?.getAttribute("href")).not.toBe(
      "https://pay.example/parent-a",
    );
    expect(queryClient.getQueryData(parentAOrderKey)).toBeUndefined();

    await waitFor(() => expect(resolveParentB).not.toBeNull());
    await act(async () => resolveParentB?.({ data: buildProfile({ first_name: "Борис B" }) }));
    expect(await screen.findByText("Борис B Ivanova")).toBeInTheDocument();
  });

  it("uses only child-scoped capability-on personal routes in the child portal", async () => {
    get.mockImplementation((url: string, config?: { params?: Record<string, unknown> }) => {
      if (url === "/parents/children/1/") return Promise.resolve({ data: buildProfile() });
      if (url === "/parents/children/1/attendance/") return Promise.resolve({ data: [] });
      if (url === "/parents/children/1/bank-payment-orders/") return Promise.resolve({ data: [] });
      if (url === "/personal-availability/self-service/commands/") {
        expect(config?.params).toEqual({ child_student_id: 1 });
        return Promise.resolve({ data: { live: [], latest_terminal: [] } });
      }
      if (url === "/personal-availability/self-service/options/") {
        expect(config?.params).toMatchObject({ child_student_id: 1, date: expect.any(String) });
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
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentChildPage("/parent/child/1", ENABLED_SBP_PAYMENT_CAPABILITIES, true);

    expect(await screen.findByText("Персональные тренировки: Masha Ivanova")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Записаться" })).toBeInTheDocument();
    expect(get).not.toHaveBeenCalledWith("/personal-availability/options/", expect.anything());
    expect(get).not.toHaveBeenCalledWith(
      "/personal-availability/payment-reservations/",
      expect.anything(),
    );
  });

  it("keeps the child terminal personal receipt visible after flag-off", async () => {
    get.mockImplementation((url: string, config?: { params?: Record<string, unknown> }) => {
      if (url === "/parents/children/1/") return Promise.resolve({ data: buildProfile() });
      if (url === "/parents/children/1/attendance/") return Promise.resolve({ data: [] });
      if (url === "/parents/children/1/bank-payment-orders/") return Promise.resolve({ data: [] });
      if (url === "/personal-availability/payment-reservations/") return Promise.resolve({ data: [] });
      if (url === "/personal-availability/self-service/commands/") {
        expect(config?.params).toEqual({ child_student_id: 1 });
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

    renderParentChildPage();

    expect(await screen.findByText("Отменено")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Персональные тренировки: Masha Ivanova" })).toBeInTheDocument();
  });

  it("keeps the child personal flow unavailable when the capability request fails", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/personal-availability/capability/") return Promise.reject(new Error("offline"));
      if (url === "/parents/children/1/") return Promise.resolve({ data: buildProfile() });
      if (url === "/parents/children/1/attendance/") return Promise.resolve({ data: [] });
      if (url === "/parents/children/1/bank-payment-orders/") return Promise.resolve({ data: [] });
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentChildPage(
      "/parent/child/1",
      ENABLED_SBP_PAYMENT_CAPABILITIES,
      false,
      true,
    );

    expect(
      await screen.findByText("Персональная запись временно недоступна"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Записать ребёнка" })).not.toBeInTheDocument();
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

  it("renders the subscription hero before the grade summary", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/1/") {
        return Promise.resolve({ data: buildProfile() });
      }

      if (url === "/parents/children/1/attendance/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/parents/children/1/bank-payment-orders/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentChildPage();

    const subscriptionSection = await screen.findByRole("region", {
      name: "Абонемент ребёнка",
    });
    const gradeSection = screen.getByRole("region", {
      name: "Прогресс по грейду",
    });
    expect(screen.getByRole("link", { name: /открыть опрос/i })).toHaveAttribute(
      "href",
      "/parent/child/1/feedback",
    );

    expect(
      subscriptionSection.compareDocumentPosition(gradeSection) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("surfaces the last-training subscription warning in the child profile", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/1/") {
        return Promise.resolve({
          data: buildProfile({
            active_subscription: {
              id: 5,
              tariff_name: "Base",
              trainings_used: 7,
              trainings_total: 8,
              trainings_left: 1,
              expires_at: "2026-05-01",
              status: "active",
            },
          }),
        });
      }

      if (url === "/parents/children/1/attendance/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentChildPage();

    const subscriptionSection = await screen.findByRole("region", {
      name: "Абонемент ребёнка",
    });

    expect(
      within(subscriptionSection).getByText("Последняя тренировка"),
    ).toBeInTheDocument();
    expect(
      within(subscriptionSection).getByText("После неё абонемент нужно продлить."),
    ).toBeInTheDocument();
  });

  it("renders a pending child admission visit as covered and non-payable", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/1/") {
        return Promise.resolve({
          data: buildProfile({
            open_debts: [],
            financial_state: {
              operational_admission: {
                payment_id: 91,
                payment_status: "pending",
                payment_method: "transfer",
                subscription_status: "pending",
                enrollment_status: "active",
                group_label: "Kids Group",
                start_date: "2026-07-22",
                checkin_ready: true,
                account_access_eligible: true,
                covered_visit_count: 1,
              },
              covered_visits: [
                {
                  debt_id: 55,
                  checkin_id: 66,
                  training_type_name: "Karate",
                  checkin_date: "2026-07-22",
                  coverage_state: "covered_awaiting_confirmation",
                  is_payable: false,
                },
              ],
            },
          }),
        });
      }
      if (
        url === "/parents/children/1/attendance/" ||
        url === "/parents/children/1/bank-payment-orders/"
      ) {
        return Promise.resolve({ data: [] });
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentChildPage();

    expect(await screen.findByText("Оплата ожидает подтверждения")).toBeInTheDocument();
    expect(screen.getByText(/Kids Group.*старт/)).toBeInTheDocument();
    expect(screen.getByText(/Karate.*покрыто оплатой/)).toBeInTheDocument();
    expect(
      screen.getByText("Отдельная оплата и ссылка не нужны до подтверждения."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Есть задолженность")).not.toBeInTheDocument();
  });

  it("renders a personal v2 child admission without group copy", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/1/") {
        return Promise.resolve({
          data: buildProfile({
            open_debts: [],
            financial_state: {
              operational_admission_v2: {
                kind: "personal",
                payment_id: 93,
                payment_status: "pending",
                payment_method: "transfer",
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
          }),
        });
      }
      if (
        url === "/parents/children/1/attendance/" ||
        url === "/parents/children/1/bank-payment-orders/"
      ) {
        return Promise.resolve({ data: [] });
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentChildPage();

    expect(await screen.findByText("Оплата ожидает подтверждения")).toBeInTheDocument();
    expect(screen.getByText("Персональная запись · 24.07.2026")).toBeInTheDocument();
    expect(screen.queryByText(/Запись в группу/)).not.toBeInTheDocument();
  });

  it("renders every concurrent child admission with only its payment-owned covered visit", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/1/") {
        return Promise.resolve({
          data: buildProfile({
            open_debts: [],
            financial_state: {
              operational_admission: {
                payment_id: 92,
                payment_status: "pending",
                payment_method: "cash",
                subscription_status: "pending",
                enrollment_status: "active",
                group_label: "Kids BJJ",
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
                  group_label: "Kids BJJ",
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
                  group_label: "Kids boxing",
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
                  training_type_name: "Kids BJJ visit",
                  checkin_date: "2026-07-23",
                  coverage_state: "covered_awaiting_confirmation",
                  is_payable: false,
                },
                {
                  payment_id: 91,
                  debt_id: 55,
                  checkin_id: 66,
                  training_type_name: "Kids boxing visit",
                  checkin_date: "2026-07-22",
                  coverage_state: "covered_awaiting_confirmation",
                  is_payable: false,
                },
              ],
            },
          }),
        });
      }
      if (
        url === "/parents/children/1/attendance/" ||
        url === "/parents/children/1/bank-payment-orders/"
      ) {
        return Promise.resolve({ data: [] });
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentChildPage();

    expect(await screen.findByText(/Kids BJJ.*старт/)).toBeInTheDocument();
    expect(screen.getByText(/Kids boxing.*старт/)).toBeInTheDocument();
    expect(screen.getByText(/Kids BJJ visit.*покрыто оплатой/)).toBeInTheDocument();
    expect(screen.getByText(/Kids boxing visit.*покрыто оплатой/)).toBeInTheDocument();
    expect(screen.queryByText("Есть задолженность")).not.toBeInTheDocument();
  });

  it("renders rejected child admission as one payable debt without future readiness", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/1/") {
        return Promise.resolve({
          data: buildProfile({
            open_debts: [
              {
                id: 55,
                checkin_id: 66,
                tariff_price: "1200.00",
                reason: "no_subscription",
                training_type_name: "Karate",
                checkin_date: "2026-07-22",
                created_at: "2026-07-22T10:00:00Z",
              },
            ],
            financial_state: {
              operational_admission: {
                payment_id: 91,
                payment_status: "rejected",
                payment_method: "transfer",
                subscription_status: "cancelled",
                enrollment_status: "cancelled",
                group_label: "Kids Group",
                start_date: "2026-07-22",
                checkin_ready: false,
                account_access_eligible: false,
                covered_visit_count: 0,
              },
              covered_visits: [],
            },
          }),
        });
      }
      if (
        url === "/parents/children/1/attendance/" ||
        url === "/parents/children/1/bank-payment-orders/"
      ) {
        return Promise.resolve({ data: [] });
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentChildPage();

    expect(await screen.findByText("Оплата отклонена")).toBeInTheDocument();
    expect(
      screen.getByText("Запись отменена; будущая готовность к чекину снята."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Kids Group.*старт/)).not.toBeInTheDocument();
    expect(screen.queryByText(/покрыто оплатой/)).not.toBeInTheDocument();
    expect(screen.getByText("Есть задолженность")).toBeInTheDocument();
  });

  it("creates a parent child-scoped online renewal link", async () => {
    const renewalOrder = {
      id: 45,
      tariff_id: 3,
      debt_ids: [],
      payment_id: 95,
      subscription_id: 55,
      student_id: 1,
      provider: "mock",
      source: "parent",
      status: "pending",
      amount_snapshot: "5000.00",
      currency: "RUB",
      purpose_snapshot: "Абонемент Base",
      provider_payment_link_id: "jgr-45-test",
      provider_payment_url: "https://pay.example/jgr-45-test",
        can_pay: true,
        can_request_refresh: true,
      provider_payment_modes: ["card"],
      provider_status: "CREATED",
      expires_at: "2099-06-28T12:00:00Z",
      paid_at: null,
    };
    let pendingOrders: Array<typeof renewalOrder> = [];
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/1/") {
        return Promise.resolve({
          data: buildProfile({
            active_subscription: {
              id: 5,
              tariff_id: 3,
              tariff_name: "Base",
              trainings_used: 2,
              trainings_total: 8,
              trainings_left: 6,
              expires_at: "2026-05-01",
              status: "active",
            },
          }),
        });
      }

      if (url === "/parents/children/1/attendance/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/parents/children/1/bank-payment-orders/") {
        return Promise.resolve({ data: pendingOrders });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });
    post.mockImplementation((url: string) => {
      if (url === "/parents/children/1/bank-payment-orders/") {
        pendingOrders = [renewalOrder];
        return Promise.resolve({ data: renewalOrder });
      }
      return Promise.reject(new Error(`Unexpected POST URL: ${url}`));
    });

    renderParentChildPage();

    fireEvent.click(await screen.findByRole("button", { name: "Продлить через СБП" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/parents/children/1/bank-payment-orders/",
        {
          tariff_id: 3,
          debt_ids: [],
        },
      );
    });
    expect(await screen.findByText("Продление ожидает оплаты")).toBeInTheDocument();
  });

  it("uses an exact child renewal source and no client tariff or debt in unified mode", async () => {
    post.mockResolvedValue({ data: { id: 45, renewed_from_subscription_id: 5 } });
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/1/") {
        return Promise.resolve({
          data: buildProfile({
            active_subscription: {
              id: 5,
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
          }),
        });
      }
      if (
        url === "/parents/children/1/attendance/" ||
        url === "/parents/children/1/bank-payment-orders/"
      ) {
        return Promise.resolve({ data: [] });
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentChildPage(
      "/parent/child/1",
      ENABLED_SBP_PAYMENT_CAPABILITIES,
      false,
      false,
      true,
    );

    expect(await screen.findByText(/Продление: Base 2026 · 6\s?500/)).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "Продлить через СБП" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/parents/children/1/bank-payment-orders/",
        expect.objectContaining({
          renewed_from_subscription_id: 5,
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

  it("does not fall back to a tariff child renewal when unified capability is unavailable", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/students/intakes/capability") return Promise.reject(new Error("offline"));
      if (url === "/parents/children/1/") {
        return Promise.resolve({
          data: buildProfile({
            active_subscription: {
              id: 5,
              tariff_id: 3,
              tariff_name: "Base",
              trainings_used: 2,
              trainings_total: 8,
              trainings_left: 6,
              expires_at: "2099-05-01",
              status: "active",
            },
          }),
        });
      }
      if (
        url === "/parents/children/1/attendance/" ||
        url === "/parents/children/1/bank-payment-orders/"
      ) {
        return Promise.resolve({ data: [] });
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentChildPage(
      "/parent/child/1",
      ENABLED_SBP_PAYMENT_CAPABILITIES,
      false,
      false,
      "error",
    );

    expect(
      await screen.findByText(/Продление через СБП станет доступно после проверки настроек клуба/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Продлить через СБП" })).not.toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });

  it("keeps parent renewal and personal payment controls disabled for malformed capability data", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/1/") {
        return Promise.resolve({
          data: buildProfile({
            active_subscription: {
              id: 5,
              tariff_id: 3,
              tariff_name: "Base",
              trainings_used: 2,
              trainings_total: 8,
              trainings_left: 6,
              expires_at: "2026-05-01",
              status: "active",
            },
          }),
        });
      }
      if (
        url === "/parents/children/1/attendance/" ||
        url === "/parents/children/1/bank-payment-orders/" ||
        url === "/schedules/guest-booking-options/" ||
        url === "/personal-availability/payment-reservations/"
      ) {
        return Promise.resolve({ data: [] });
      }
      if (url === "/personal-availability/options/") {
        return Promise.resolve({
          data: [
            {
              slot_id: 67,
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
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentChildPage("/parent/child/1", { online_payments_enabled: "true" });

    const renewal = await screen.findByRole("button", { name: "Продлить через СБП" });
    expect(renewal).toBeVisible();
    expect(renewal).toBeDisabled();
    fireEvent.click(renewal);

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
      "/parents/children/1/bank-payment-orders/",
      expect.anything(),
    );
    expect(post).not.toHaveBeenCalledWith(
      "/personal-availability/67/payment-reservations/",
      expect.anything(),
    );
  });

  it("submits the child-visible designated offer digest for a personal payment", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/1/") return Promise.resolve({ data: buildProfile() });
      if (
        url === "/parents/children/1/attendance/" ||
        url === "/parents/children/1/bank-payment-orders/" ||
        url === "/schedules/guest-booking-options/" ||
        url === "/personal-availability/payment-reservations/"
      ) {
        return Promise.resolve({ data: [] });
      }
      if (url === "/personal-availability/options/") {
        return Promise.resolve({
          data: [
            {
              slot_id: 68,
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
              payment_tariff_name: "Старый тариф",
              payment_amount: "2000.00",
              offer_tariff_id: 45,
              offer_tariff_name: "Назначенная персоналка",
              offer_price: "2500.00",
              offer_digest: "parent-slot-offer-v1",
              offer_error_code: "",
            },
          ],
        });
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });
    post.mockResolvedValue({
      data: {
        id: 301,
        trainer_id: 3,
        trainer_name: "Иван Петров",
        location_id: 2,
        location_name: "Основной зал",
        training_type_id: 8,
        training_type_name: "Персональная тренировка",
        tariff_id: 45,
        tariff_name: "Назначенная персоналка",
        starts_at: "2026-04-13T10:00:00+05:00",
        ends_at: "2026-04-13T11:00:00+05:00",
        status: "pending_payment",
        expires_at: "2026-04-13T10:15:00+05:00",
        bank_payment_order_id: null,
        subscription_id: null,
        provider_payment_url: "",
        amount_snapshot: "2500.00",
        order_status: "pending",
        can_cancel: true,
      },
    });

    renderParentChildPage();
    fireEvent.click(await screen.findByRole("button", { name: "Записаться" }));
    fireEvent.click(screen.getByRole("tab", { name: "Персоналка" }));
    const personalSection = await screen.findByRole("region", {
      name: "Запись на персональную тренировку",
    });
    await within(personalSection).findByText("Нужна оплата");
    fireEvent.click(within(personalSection).getByRole("button", { name: "Оплатить" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/personal-availability/68/payment-reservations/", {
        child_student_id: 1,
        tariff_id: 12,
        idempotency_key: "parent-personal-payment-reservation-1-68-12",
      });
    });
  });

  it("shows an existing pending child payment link instead of a second renewal button", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/1/") {
        return Promise.resolve({
          data: buildProfile({
            active_subscription: {
              id: 5,
              tariff_id: 3,
              tariff_name: "Base",
              trainings_used: 2,
              trainings_total: 8,
              trainings_left: 6,
              expires_at: "2026-05-01",
              status: "active",
            },
          }),
        });
      }

      if (url === "/parents/children/1/attendance/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/parents/children/1/bank-payment-orders/") {
        return Promise.resolve({
          data: [
            {
              id: 45,
              tariff_id: 3,
              debt_ids: [],
              payment_id: 95,
              subscription_id: 55,
              student_id: 1,
              provider: "mock",
              source: "trainer",
              status: "pending",
              amount_snapshot: "5000.00",
              currency: "RUB",
              purpose_snapshot: "Абонемент Base",
              provider_payment_link_id: "jgr-45-test",
              provider_payment_url: "https://pay.example/jgr-45-test",
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

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentChildPage();

    expect(await screen.findByText("Продление ожидает оплаты")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Оплатить через СБП/ })).toHaveAttribute(
      "href",
      "https://pay.example/jgr-45-test",
    );
    expect(screen.queryByRole("button", { name: "Продлить через СБП" })).not.toBeInTheDocument();
  });

  it("cancels a child renewal through the in-app confirmation and hides it", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    const renewalOrder = {
      id: 45,
      tariff_id: 3,
      debt_ids: [],
      payment_id: 95,
      subscription_id: 55,
      student_id: 1,
      provider: "mock",
      source: "parent",
      status: "pending",
      amount_snapshot: "5000.00",
      currency: "RUB",
      purpose_snapshot: "Абонемент Base",
      provider_payment_link_id: "jgr-45-test",
      provider_payment_url: "https://pay.example/jgr-45-test",
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
      if (url === "/parents/children/1/") {
        return Promise.resolve({
          data: buildProfile({
            active_subscription: {
              id: 5,
              tariff_id: 3,
              tariff_name: "Base",
              trainings_used: 2,
              trainings_total: 8,
              trainings_left: 6,
              expires_at: "2026-05-01",
              status: "active",
            },
          }),
        });
      }

      if (url === "/parents/children/1/attendance/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/parents/children/1/bank-payment-orders/") {
        return Promise.resolve({ data: pendingOrders });
      }

      if (url === "/personal-availability/payment-reservations/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });
    post.mockImplementation((url: string) => {
      if (url === "/parents/children/1/bank-payment-orders/45/cancel/") {
        pendingOrders = [];
        return Promise.resolve({ data: { ...renewalOrder, status: "cancelled" } });
      }
      return Promise.reject(new Error(`Unexpected POST URL: ${url}`));
    });

    renderParentChildPage();

    expect(await screen.findByText("Продление ожидает оплаты")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Отменить продление" }));

    expect(confirmSpy).not.toHaveBeenCalled();
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("Отменить оплату?")).toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole("button", { name: "Отменить продление" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/parents/children/1/bank-payment-orders/45/cancel/",
        {},
      );
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
      if (url === "/parents/children/1/") {
        return Promise.resolve({
          data: buildProfile({
            active_subscription: {
              id: 5,
              tariff_id: 3,
              tariff_name: "Base",
              trainings_used: 2,
              trainings_total: 8,
              trainings_left: 6,
              expires_at: "2026-09-01",
              status: "active",
            },
          }),
        });
      }
      if (url === "/parents/children/1/attendance/") return Promise.resolve({ data: [] });
      if (url === "/parents/children/1/bank-payment-orders/") {
        bankOrderReads += 1;
        return Promise.resolve({
          data: [
            {
              id: 45,
              tariff_id: 3,
              debt_ids: [],
              subscription_id: 55,
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
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentChildPage();

    expect(await screen.findByText("Статус продления")).toBeInTheDocument();
    expect(screen.getAllByText("Проверка")).not.toHaveLength(0);
    expect(screen.queryByRole("link", { name: /Оплатить через СБП/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Обновить статус" }));

    await waitFor(() => expect(bankOrderReads).toBe(2));
  });

  it("shows grade system names in the progress section", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/1/") {
        return Promise.resolve({ data: buildProfile() });
      }

      if (url === "/parents/children/1/attendance/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentChildPage();

    const gradeSection = await screen.findByRole("region", {
      name: "Прогресс по грейду",
    });

    expect(within(gradeSection).getByText("BJJ")).toBeInTheDocument();
  });

  it("shows an explicit retryable attendance error state without hiding the loaded profile", async () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    let attendanceCalls = 0;

    get.mockImplementation((url: string) => {
      if (url === "/parents/children/1/") {
        return Promise.resolve({ data: buildProfile() });
      }

      if (url === "/parents/children/1/attendance/") {
        attendanceCalls += 1;
        if (attendanceCalls === 1) {
          return Promise.reject(new Error("attendance failed"));
        }
        return Promise.resolve({
          data: [
            {
              date: "2026-04-15",
              group_name: "Kids",
              trainer_name: "Ivan Petrov",
              training_type_name: "Muay Thai",
              start_time: "17:30",
            },
          ],
        });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentChildPage();

    expect(await screen.findByText("Masha Ivanova")).toBeInTheDocument();
    expect(
      await screen.findByText("Не удалось загрузить активность."),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Повторить" }));

    expect(await screen.findByText("Muay Thai")).toBeInTheDocument();

    consoleError.mockRestore();
  });

  it("shows a retry action when the child profile fails to load", async () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    let profileCalls = 0;
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/1/") {
        profileCalls += 1;
        if (profileCalls === 1) {
          return Promise.reject(new Error("profile failed"));
        }
        return Promise.resolve({ data: buildProfile() });
      }

      if (url === "/parents/children/1/attendance/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentChildPage();

    const retryButton = await screen.findByRole("button", { name: "Повторить" });
    expect(screen.getByText("Не удалось загрузить профиль")).toBeInTheDocument();

    fireEvent.click(retryButton);

    expect(await screen.findByText("Masha Ivanova")).toBeInTheDocument();

    consoleError.mockRestore();
  });

  it("shows attendance_count in the activity section once the profile is loaded", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/1/") {
        return Promise.resolve({ data: buildProfile({ attendance_count: 12 }) });
      }

      if (url === "/parents/children/1/attendance/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentChildPage();

    const activitySection = await screen.findByRole("region", {
      name: "Активность",
    });

    expect(within(activitySection).getByText("12 посещений")).toBeInTheDocument();
  });

  it("scrolls to a requested child detail section from a read-only quick action anchor", async () => {
    const scrollIntoView = vi.fn();
    const originalScrollIntoView = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = scrollIntoView;

    get.mockImplementation((url: string) => {
      if (url === "/parents/children/1/") {
        return Promise.resolve({ data: buildProfile() });
      }

      if (url === "/parents/children/1/attendance/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentChildPage("/parent/child/1#groups");

    await screen.findByRole("region", {
      name: "Занятия ребёнка",
    });

    await waitFor(() => {
      expect(scrollIntoView).toHaveBeenCalledWith({ block: "start" });
    });

    Element.prototype.scrollIntoView = originalScrollIntoView;
  });

  it("shows the child's training groups and trainer context", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/1/") {
        return Promise.resolve({
          data: buildProfile({
            schedule: [
              {
                id: 8,
                day_of_week: 2,
                start_time: "17:30",
                end_time: "18:30",
                group_name: "Kids Muay Thai",
                training_type_name: "Muay Thai",
                trainer_name: "Ivan Petrov",
                location_name: "Main hall",
              },
            ],
          }),
        });
      }

      if (url === "/parents/children/1/attendance/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentChildPage();

    const trainingSection = await screen.findByRole("region", {
      name: "Занятия ребёнка",
    });

    expect(within(trainingSection).getByText("Kids Muay Thai")).toBeInTheDocument();
    expect(within(trainingSection).getByText("Muay Thai")).toBeInTheDocument();
    expect(within(trainingSection).getByText("Группа")).toBeInTheDocument();
    expect(within(trainingSection).getByText(/Ivan Petrov/)).toBeInTheDocument();
    expect(within(trainingSection).getByText(/Main hall/)).toBeInTheDocument();
  });

  it("shows one-time personal bookings as personal lessons in the child schedule", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/1/") {
        return Promise.resolve({
          data: buildProfile({
            schedule: [
              {
                id: 18,
                day_of_week: 1,
                start_time: "12:00",
                end_time: "13:00",
                group_name: "Персоналка",
                training_type_name: "Personal Boxing",
                training_type_kind: "personal",
                one_time_date: "2026-04-14",
                trainer_name: "Ivan Petrov",
                location_name: "Small hall",
              },
            ],
          }),
        });
      }

      if (url === "/parents/children/1/attendance/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentChildPage();

    const trainingSection = await screen.findByRole("region", {
      name: "Занятия ребёнка",
    });

    expect(within(trainingSection).getAllByText("Персоналка").length).toBeGreaterThan(0);
    expect(within(trainingSection).getByText("Personal Boxing")).toBeInTheDocument();
    expect(within(trainingSection).getByText("14 апреля")).toBeInTheDocument();
  });

  it("shows upcoming cancel, reschedule, and substitute schedule states to parents", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/1/") {
        return Promise.resolve({
          data: buildProfile({
            schedule: [
              {
                id: 8,
                day_of_week: 2,
                start_time: "17:30",
                end_time: "18:30",
                group_name: "Kids Muay Thai",
                trainer_name: "Ivan Petrov",
                location_name: "Main hall",
                upcoming_exceptions: [
                  {
                    exception_type: "cancelled",
                    date: "2026-06-06",
                    reason: "Турнир",
                  },
                ],
                upcoming_occurrences: [],
              },
              {
                id: 9,
                day_of_week: 3,
                start_time: "18:00",
                end_time: "19:00",
                group_name: "Kids Boxing",
                trainer_name: "Ivan Petrov",
                location_name: "Small hall",
                upcoming_exceptions: [
                  {
                    exception_type: "rescheduled",
                    date: "2026-06-07",
                    new_date: "2026-06-08",
                    new_start_time: "19:00:00",
                    new_end_time: "20:00:00",
                  },
                ],
                upcoming_occurrences: [
                  {
                    effective_date: "2026-06-08",
                    effective_start_time: "19:00:00",
                    effective_end_time: "20:00:00",
                    trainer_name: "Ivan Petrov",
                    is_rescheduled: true,
                    is_substitute: false,
                  },
                ],
              },
              {
                id: 10,
                day_of_week: 4,
                start_time: "16:00",
                end_time: "17:00",
                group_name: "Kids BJJ",
                trainer_name: "Ivan Petrov",
                location_name: "Blue hall",
                upcoming_exceptions: [
                  {
                    exception_type: "substitute",
                    date: "2026-06-09",
                    substitute_trainer_name: "Alex Backup",
                  },
                ],
                upcoming_occurrences: [
                  {
                    effective_date: "2026-06-09",
                    effective_start_time: "16:00:00",
                    effective_end_time: "17:00:00",
                    trainer_name: "Alex Backup",
                    is_rescheduled: false,
                    is_substitute: true,
                  },
                ],
              },
            ],
          }),
        });
      }

      if (url === "/parents/children/1/attendance/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentChildPage();

    const trainingSection = await screen.findByRole("region", {
      name: "Занятия ребёнка",
    });

    expect(within(trainingSection).getByText(/Отменено/)).toBeInTheDocument();
    expect(within(trainingSection).getByText(/Турнир/)).toBeInTheDocument();
    expect(within(trainingSection).getByText(/Перенос/)).toBeInTheDocument();
    expect(within(trainingSection).getByText(/8 июня, 19:00-20:00/)).toBeInTheDocument();
    expect(within(trainingSection).getByText(/Замена тренера/)).toBeInTheDocument();
    expect(within(trainingSection).getAllByText(/Alex Backup/).length).toBeGreaterThan(0);
  });

  it("renders each reschedule row from its own exception payload", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/1/") {
        return Promise.resolve({
          data: buildProfile({
            schedule: [
              {
                id: 31,
                day_of_week: 1,
                start_time: "17:00",
                end_time: "18:00",
                group_name: "Kids Boxing",
                trainer_name: "Coach One",
                location_name: "Main Hall",
                upcoming_exceptions: [
                  {
                    exception_type: "rescheduled",
                    date: "2026-06-08",
                    new_date: "2026-06-09",
                    new_start_time: "19:00:00",
                    new_end_time: "20:00:00",
                  },
                  {
                    exception_type: "rescheduled",
                    date: "2026-06-15",
                    new_date: "2026-06-16",
                    new_start_time: "20:00:00",
                    new_end_time: "21:00:00",
                  },
                ],
                upcoming_occurrences: [
                  {
                    effective_date: "2026-06-09",
                    effective_start_time: "19:00:00",
                    effective_end_time: "20:00:00",
                    trainer_name: "Coach One",
                    is_rescheduled: true,
                    is_substitute: false,
                  },
                ],
              },
            ],
          }),
        });
      }

      if (url === "/parents/children/1/attendance/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentChildPage();

    const trainingSection = await screen.findByRole("region", {
      name: "Занятия ребёнка",
    });

    expect(within(trainingSection).getByText(/9 июня, 19:00-20:00/)).toBeInTheDocument();
    expect(within(trainingSection).getByText(/16 июня, 20:00-21:00/)).toBeInTheDocument();
  });

  it("does not show churned children with an active subscription as only left", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/1/") {
        return Promise.resolve({
          data: buildProfile({
            status: "churned",
            active_subscription: {
              id: 5,
              tariff_name: "Base",
              trainings_used: 2,
              trainings_total: 8,
              trainings_left: 6,
              expires_at: "2026-05-01",
              status: "active",
            },
          }),
        });
      }

      if (url === "/parents/children/1/attendance/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentChildPage();

    expect(await screen.findByText("Не посещает 30+ дней")).toBeInTheDocument();
    expect(screen.getByText("Абонемент активен")).toBeInTheDocument();
    expect(screen.queryByText("Ушёл")).not.toBeInTheDocument();
  });

  it("explains the grade empty state instead of hiding progress completely", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/1/") {
        return Promise.resolve({
          data: buildProfile({ grade_progress: [] }),
        });
      }

      if (url === "/parents/children/1/attendance/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentChildPage();

    const gradeSection = await screen.findByRole("region", {
      name: "Прогресс по грейду",
    });

    expect(
      within(gradeSection).getByText("Прогресс пока не назначен"),
    ).toBeInTheDocument();
  });

  it("renders an unlimited active subscription with no expiry date", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/1/") {
        return Promise.resolve({
          data: buildProfile({
            active_subscription: {
              id: 5,
              tariff_name: "Family unlimited",
              trainings_used: 2,
              trainings_total: null,
              trainings_left: null,
              expires_at: null,
              status: "active",
            },
          }),
        });
      }

      if (url === "/parents/children/1/attendance/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentChildPage();

    expect(await screen.findByText("Family unlimited")).toBeInTheDocument();
    expect(screen.getByText("Без срока окончания")).toBeInTheDocument();
  });

  it("renders every active subscription returned by the parent profile", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/1/") {
        const primarySubscription = {
          id: 5,
          tariff_name: "Kids BJJ",
          trainings_used: 9,
          trainings_total: 12,
          trainings_left: 3,
          expires_at: "2026-05-20",
          status: "active",
        };

        return Promise.resolve({
          data: buildProfile({
            active_subscription: primarySubscription,
            active_subscriptions: [
              primarySubscription,
              {
                id: 6,
                tariff_name: "Kids boxing",
                trainings_used: 2,
                trainings_total: 8,
                trainings_left: 6,
                expires_at: "2026-05-10",
                status: "active",
              },
            ],
          }),
        });
      }

      if (url === "/parents/children/1/attendance/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentChildPage();

    const subscriptionSection = await screen.findByRole("region", {
      name: "Абонемент ребёнка",
    });

    expect(within(subscriptionSection).getByText("Kids BJJ")).toBeInTheDocument();
    expect(within(subscriptionSection).getByText("Kids boxing")).toBeInTheDocument();
  });

  it("renders the parent-safe child document checklist read-only", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/1/") {
        return Promise.resolve({
          data: buildProfile({
            document_checklist: [
              {
                document_type: {
                  id: 7,
                  name: "Медицинская справка",
                  description: "Нужна для допуска к тренировкам",
                  is_required: true,
                  is_active: true,
                },
                is_provided: false,
                has_file: false,
              },
              {
                document_type: {
                  id: 8,
                  name: "Согласие родителя",
                  description: "",
                  is_required: false,
                  is_active: true,
                },
                is_provided: true,
                has_file: true,
              },
            ],
          }),
        });
      }

      if (url === "/parents/children/1/attendance/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentChildPage();

    const documentsSection = await screen.findByRole("region", {
      name: "Документы ребёнка",
    });

    expect(
      within(documentsSection).getByText("Медицинская справка"),
    ).toBeInTheDocument();
    expect(within(documentsSection).getByText("Не предоставлен")).toBeInTheDocument();
    expect(
      within(documentsSection).getByText("Согласие родителя"),
    ).toBeInTheDocument();
    expect(within(documentsSection).getByText("Файл загружен")).toBeInTheDocument();
    expect(within(documentsSection).queryByRole("button")).not.toBeInTheDocument();
  });

  it("shows open debts in the child subscription section", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/1/") {
        return Promise.resolve({
          data: buildProfile({
            active_subscription: null,
            active_subscriptions: [],
            open_debts: [
              {
                id: 11,
                checkin_id: 21,
                tariff_price: "1200.00",
                reason: "no_subscription",
                training_type_name: "Kids Muay Thai",
                checkin_date: "2026-03-01",
                created_at: "2026-03-01T12:00:00Z",
              },
              {
                id: 12,
                checkin_id: 22,
                tariff_price: "900.00",
                reason: "no_subscription",
                training_type_name: "Kids Boxing",
                checkin_date: "2026-03-03",
                created_at: "2026-03-03T12:00:00Z",
              },
            ],
          }),
        });
      }

      if (url === "/parents/children/1/attendance/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentChildPage();

    const subscriptionSection = await screen.findByRole("region", {
      name: "Абонемент ребёнка",
    });

    expect(within(subscriptionSection).getByText("Есть задолженность")).toBeInTheDocument();
    expect(within(subscriptionSection).getByText("Kids Muay Thai")).toBeInTheDocument();
    expect(
      within(subscriptionSection).getAllByText("Причина: нет подходящего абонемента").length,
    ).toBe(2);
    expect(within(subscriptionSection).getByText("Тренировка: 01.03.2026")).toBeInTheDocument();
    expect(within(subscriptionSection).getByText("Создано: 01.03.2026")).toBeInTheDocument();
    expect(within(subscriptionSection).getByText("Тренировка: 03.03.2026")).toBeInTheDocument();
    expect(within(subscriptionSection).getByText("Создано: 03.03.2026")).toBeInTheDocument();
    expect(within(subscriptionSection).getByText(/1200/)).toBeInTheDocument();
    expect(within(subscriptionSection).getByText("Kids Boxing")).toBeInTheDocument();
    expect(within(subscriptionSection).getByText(/900/)).toBeInTheDocument();
    expect(within(subscriptionSection).getByText("Открытых долгов: 2")).toBeInTheDocument();
  });

  it("renders frozen subscriptions as current without showing pending as active", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/1/") {
        return Promise.resolve({
          data: buildProfile({
            active_subscription: {
              id: 7,
              tariff_name: "Paused package",
              trainings_used: 2,
              trainings_total: 8,
              trainings_left: 6,
              expires_at: "2026-05-20",
              status: "frozen",
            },
            active_subscriptions: [
              {
                id: 7,
                tariff_name: "Paused package",
                trainings_used: 2,
                trainings_total: 8,
                trainings_left: 6,
                expires_at: "2026-05-20",
                status: "frozen",
              },
              {
                id: 8,
                tariff_name: "Waiting package",
                trainings_used: 0,
                trainings_total: 8,
                trainings_left: 8,
                expires_at: null,
                status: "pending",
              },
              {
                id: 9,
                tariff_name: "Old package",
                trainings_used: 8,
                trainings_total: 8,
                trainings_left: 0,
                expires_at: "2026-04-01",
                status: "expired",
              },
            ],
          }),
        });
      }

      if (url === "/parents/children/1/attendance/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentChildPage();

    const subscriptionSection = await screen.findByRole("region", {
      name: "Абонемент ребёнка",
    });

    expect(within(subscriptionSection).getByText("Paused package")).toBeInTheDocument();
    expect(within(subscriptionSection).getAllByText("Абонемент на паузе").length).toBeGreaterThan(0);
    expect(within(subscriptionSection).queryByText("Waiting package")).not.toBeInTheDocument();
    expect(
      within(subscriptionSection).queryByText("Абонемент ожидает активации"),
    ).not.toBeInTheDocument();
    expect(within(subscriptionSection).queryByText("Old package")).not.toBeInTheDocument();
    expect(within(subscriptionSection).queryByText("Абонемент истек")).not.toBeInTheDocument();
  });

  it("renders pending freeze requests on active parent subscriptions", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/1/") {
        return Promise.resolve({
          data: buildProfile({
            active_subscription: {
              id: 7,
              tariff_name: "Active package",
              trainings_used: 3,
              trainings_total: 8,
              trainings_left: 5,
              expires_at: "2026-05-20",
              status: "active",
              freeze_status: "pending",
            },
            active_subscriptions: [
              {
                id: 7,
                tariff_name: "Active package",
                trainings_used: 3,
                trainings_total: 8,
                trainings_left: 5,
                expires_at: "2026-05-20",
                status: "active",
                freeze_status: "pending",
              },
            ],
          }),
        });
      }

      if (url === "/parents/children/1/attendance/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentChildPage();

    const subscriptionSection = await screen.findByRole("region", {
      name: "Абонемент ребёнка",
    });

    expect(within(subscriptionSection).getByText("Active package")).toBeInTheDocument();
    expect(
      within(subscriptionSection).getAllByText("Заморозка ожидает подтверждения").length,
    ).toBeGreaterThan(0);
    expect(
      within(subscriptionSection).getByText("5 тренировок из пакета ещё доступны"),
    ).toBeInTheDocument();
  });

  it("loads more attendance history with bounded parent query params", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/1/") {
        return Promise.resolve({ data: buildProfile({ attendance_count: 12 }) });
      }

      if (url === "/parents/children/1/attendance/") {
        return Promise.resolve({
          data: [
            {
              date: "2026-04-15",
              group_name: "Kids",
              trainer_name: "Ivan Petrov",
              training_type_name: "Muay Thai",
              start_time: "17:30",
            },
          ],
        });
      }

      if (url === "/parents/children/1/attendance/?limit=10&offset=1") {
        return Promise.resolve({
          data: [
            {
              date: "2026-04-01",
              group_name: "Kids",
              trainer_name: "Ivan Petrov",
              training_type_name: "Boxing",
              start_time: "18:30",
            },
          ],
        });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentChildPage();

    expect(await screen.findByText("Muay Thai")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Показать ещё" }));

    expect(await screen.findByText("Boxing")).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith("/parents/children/1/attendance/?limit=10&offset=1");
  });

  it("shows a visible error when attendance load-more fails", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/1/") {
        return Promise.resolve({ data: buildProfile({ attendance_count: 12 }) });
      }

      if (url === "/parents/children/1/attendance/") {
        return Promise.resolve({
          data: [
            {
              date: "2026-04-15",
              group_name: "Kids",
              trainer_name: "Ivan Petrov",
              training_type_name: "Muay Thai",
              start_time: "17:30",
            },
          ],
        });
      }

      if (url === "/parents/children/1/attendance/?limit=10&offset=1") {
        return Promise.reject(new Error("network"));
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentChildPage();

    expect(await screen.findByText("Muay Thai")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Показать ещё" }));

    expect(
      await screen.findByText("Не удалось загрузить ещё посещения."),
    ).toBeInTheDocument();
  });

  it("uses the canonical trainings_left value from the parent profile", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/1/") {
        return Promise.resolve({
          data: buildProfile({
            active_subscription: {
              id: 5,
              tariff_name: "Adjusted package",
              trainings_used: 2,
              trainings_total: 8,
              trainings_left: 3,
              expires_at: "2026-05-01",
              status: "active",
            },
          }),
        });
      }

      if (url === "/parents/children/1/attendance/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderParentChildPage();

    const subscriptionSection = await screen.findByRole("region", {
      name: "Абонемент ребёнка",
    });

    expect(within(subscriptionSection).getByText("3")).toBeInTheDocument();
    expect(within(subscriptionSection).queryByText("6")).not.toBeInTheDocument();
  });

  it("lets a parent book the current child into an available group guest option", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/1/") {
        return Promise.resolve({ data: buildProfile() });
      }

      if (url === "/parents/children/1/attendance/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/schedules/guest-booking-options/") {
        return Promise.resolve({
          data: [
            {
              schedule_id: 44,
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
        enrollment_id: 78,
        student_id: 1,
        display_name: "Masha Ivanova",
        schedule_id: 44,
        created_from: "student_self_booking",
        is_guest_visit: true,
        starts_on: "2026-04-13",
        ends_on: "2026-04-13",
        created: true,
        already_member: false,
        origin: "parent_self_booking",
        financial_preview: {
          code: "subscription",
          message: "По абонементу",
        },
      },
    });

    renderParentChildPage();

    fireEvent.click(await screen.findByRole("button", { name: "Записаться" }));

    expect(screen.getByText("Запись для: Masha Ivanova")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Группа" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Персоналка" })).toBeInTheDocument();

    const bookingSection = await screen.findByRole("region", {
      name: "Запись ребёнка на групповую тренировку",
    });
    expect(await within(bookingSection).findByText("Kids Boxing")).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith("/schedules/guest-booking-options/", {
      params: expect.objectContaining({ child_student_id: 1 }),
    });

    fireEvent.click(within(bookingSection).getByRole("button", { name: "Записаться" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/schedules/44/guest-bookings/", {
        date: "2026-04-13",
        child_student_id: 1,
        idempotency_key: "parent-self-booking-1-44-2026-04-13",
      });
    });
    expect(await within(bookingSection).findByText("Запись создана")).toBeInTheDocument();
  });

  it("lets a parent cancel the current child's group booking from the child profile", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/1/") {
        return Promise.resolve({
          data: buildProfile({
            schedule: [
              {
                id: 44,
                day_of_week: 0,
                start_time: "18:00:00",
                end_time: "19:00:00",
                group_name: "Self Booked Kids",
                training_type_id: 3,
                training_type_name: "Boxing",
                trainer_name: "Ivan Petrov",
                location_name: "Main hall",
                upcoming_occurrences: [
                  {
                    schedule_id: 44,
                    enrollment_id: 78,
                    created_from: "student_self_booking",
                    can_cancel: true,
                    effective_date: "2026-04-13",
                    effective_start_time: "18:00:00",
                    effective_end_time: "19:00:00",
                    trainer_name: "Ivan Petrov",
                  },
                ],
              },
            ],
          }),
        });
      }

      if (url === "/parents/children/1/attendance/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });
    post.mockResolvedValue({
      data: {
        id: 78,
        status: "cancelled",
      },
    });

    renderParentChildPage();

    expect(await screen.findByText("Self Booked Kids")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Отменить запись Self Booked Kids" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/guest-bookings/78/cancel/", { reason: "" });
    });
  });

  it("shows stale cancel error and refreshes child profile when backend rejects past booking cancel", async () => {
    const invalidateSpy = vi.spyOn(QueryClient.prototype, "invalidateQueries");
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/1/") {
        return Promise.resolve({
          data: buildProfile({
            schedule: [
              {
                id: 44,
                day_of_week: 0,
                start_time: "18:00:00",
                end_time: "19:00:00",
                group_name: "Past Self Booked Kids",
                training_type_id: 3,
                training_type_name: "Boxing",
                trainer_name: "Ivan Petrov",
                location_name: "Main hall",
                upcoming_occurrences: [
                  {
                    schedule_id: 44,
                    enrollment_id: 78,
                    created_from: "student_self_booking",
                    can_cancel: true,
                    effective_date: "2026-04-13",
                    effective_start_time: "18:00:00",
                    effective_end_time: "19:00:00",
                    trainer_name: "Ivan Petrov",
                  },
                ],
              },
            ],
          }),
        });
      }

      if (url === "/parents/children/1/attendance/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });
    post.mockRejectedValue({
      response: { data: { code: "booking_past_date" } },
    });

    renderParentChildPage();

    expect(await screen.findByText("Past Self Booked Kids")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Отменить запись Past Self Booked Kids" }));

    expect(await screen.findByText("Эту запись уже нельзя отменить, занятие прошло")).toBeInTheDocument();
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: ["parent", "child", "1"],
    });
    invalidateSpy.mockRestore();
  });

  it("lets a parent cancel the current child's personal booking from the child profile", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/1/") {
        return Promise.resolve({
          data: buildProfile({
            schedule: [
              {
                id: 45,
                day_of_week: 0,
                start_time: "10:00:00",
                end_time: "11:00:00",
                group_name: "Персональная тренировка",
                training_type_id: 8,
                training_type_name: "Персональная тренировка",
                trainer_name: "Ivan Petrov",
                location_name: "Main hall",
                upcoming_occurrences: [
                  {
                    schedule_id: 45,
                    enrollment_id: 89,
                    created_from: "personal_booking",
                    can_cancel: true,
                    effective_date: "2026-04-13",
                    effective_start_time: "10:00:00",
                    effective_end_time: "11:00:00",
                    trainer_name: "Ivan Petrov",
                  },
                ],
              },
            ],
          }),
        });
      }

      if (url === "/parents/children/1/attendance/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });
    post.mockResolvedValue({
      data: {
        id: 89,
        status: "cancelled",
      },
    });

    renderParentChildPage();

    const cancelButton = await screen.findByRole("button", {
      name: "Отменить запись Персональная тренировка",
    });
    fireEvent.click(cancelButton);

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/personal-bookings/89/cancel/", { reason: "" });
    });
  });

  it("lets a parent book the current child into a personal slot", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/parents/children/1/") {
        return Promise.resolve({ data: buildProfile() });
      }

      if (url === "/parents/children/1/attendance/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/schedules/guest-booking-options/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/personal-availability/options/") {
        return Promise.resolve({
          data: [
            {
              slot_id: 66,
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
        schedule_id: 102,
        enrollment_id: 89,
        availability_slot_id: 66,
        student_id: 1,
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

    renderParentChildPage();

    fireEvent.click(await screen.findByRole("button", { name: "Записаться" }));
    fireEvent.click(screen.getByRole("tab", { name: "Персоналка" }));

    const personalSection = await screen.findByRole("region", {
      name: "Запись на персональную тренировку",
    });
    expect(
      await within(personalSection).findByText("Персональная тренировка"),
    ).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith("/personal-availability/options/", {
      params: expect.objectContaining({ child_student_id: 1 }),
    });

    fireEvent.click(within(personalSection).getByRole("button", { name: "Записаться" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/personal-availability/66/book/", {
        child_student_id: 1,
        subscription_id: 19,
        idempotency_key: "parent-personal-self-booking-1-66",
      });
    });
    expect(await within(personalSection).findByText("Запись создана")).toBeInTheDocument();
  });

  it("lets a parent create and cancel a child-scoped payment link for a personal slot", async () => {
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
      id: 41,
      student_id: 1,
      trainer_id: 3,
      trainer_name: "Ivan Petrov",
      location_id: 2,
      location_name: "Main hall",
      training_type_id: 8,
      training_type_name: "Персональная тренировка",
      tariff_id: 12,
      tariff_name: "Разовая персоналка",
      availability_slot_id: 67,
      starts_at: "2026-04-13T10:00:00+05:00",
      ends_at: "2026-04-13T11:00:00+05:00",
      status: "pending_payment",
      expires_at: "2099-04-13T09:45:00+05:00",
      payment_id: 92,
      bank_payment_order_id: 124,
      subscription_id: null,
      schedule_id: null,
      enrollment_id: null,
      provider_payment_url: "https://pay.example/child-personal-41",
        can_pay: true,
        can_request_refresh: true,
      amount_snapshot: "2000.00",
      order_status: "pending",
      can_cancel: true,
      created_at: "2026-04-13T08:45:00+05:00",
    };
    let pendingReservations: Array<typeof reservation> = [];

    get.mockImplementation((url: string) => {
      if (url === "/parents/children/1/") {
        return Promise.resolve({ data: buildProfile() });
      }

      if (url === "/parents/children/1/attendance/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/schedules/guest-booking-options/") {
        return Promise.resolve({ data: [] });
      }

      if (url === "/personal-availability/options/") {
        return Promise.resolve({
          data: [
            {
              slot_id: 67,
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
        return Promise.resolve({ data: pendingReservations });
      }

      if (url === "/parents/children/1/bank-payment-orders/124/") {
        return Promise.resolve({
          data: {
            id: 124,
            payment_id: 92,
            subscription_id: null,
            student_id: 1,
            tariff_id: 12,
            debt_ids: [],
            source: "parent",
            status: "pending",
            amount_snapshot: "2000.00",
            currency: "RUB",
            purpose_snapshot: "Разовая персоналка",
            provider_payment_url: "https://pay.example/child-personal-41",
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
      if (url === "/personal-availability/67/payment-reservations/") {
        pendingReservations = [reservation];
        return Promise.resolve({ data: reservation });
      }
      if (url === "/personal-availability/payment-reservations/41/cancel/") {
        pendingReservations = [];
        return Promise.resolve({ data: { ...reservation, status: "cancelled" } });
      }
      return Promise.reject(new Error(`Unexpected POST URL: ${url}`));
    });

    renderParentChildPage();

    fireEvent.click(await screen.findByRole("button", { name: "Записаться" }));
    fireEvent.click(screen.getByRole("tab", { name: "Персоналка" }));

    const personalSection = await screen.findByRole("region", {
      name: "Запись на персональную тренировку",
    });
    expect(await within(personalSection).findByText("Нужна оплата")).toBeInTheDocument();

    fireEvent.click(within(personalSection).getByRole("button", { name: "Оплатить" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/personal-availability/67/payment-reservations/", {
        child_student_id: 1,
        tariff_id: 12,
        idempotency_key: "parent-personal-payment-reservation-1-67-12",
      });
    });
    expect(await within(personalSection).findByText("Персоналка ожидает оплаты")).toBeInTheDocument();
    expect(await within(personalSection).findByRole("link", { name: /Оплатить через СБП/ })).toHaveAttribute(
      "href",
      "https://pay.example/child-personal-41",
    );

    fireEvent.click(within(personalSection).getByRole("button", { name: "Отменить ссылку" }));
    const cancelDialog = screen.getByText("Отменить оплату?").closest('[role="dialog"]');
    expect(cancelDialog).not.toBeNull();
    fireEvent.click(
      within(cancelDialog as HTMLElement).getByRole("button", { name: "Отменить ссылку" }),
    );

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/personal-availability/payment-reservations/41/cancel/",
        { child_student_id: 1 },
      );
    });
    expect(openSpy).not.toHaveBeenCalled();
    openSpy.mockRestore();
  });
});
