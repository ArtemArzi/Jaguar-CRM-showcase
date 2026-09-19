import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation, useParams } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "@/features/auth/auth-store";
import { useBrandingStore } from "@/features/branding/use-branding";
import Leads from "./leads";
import type { LeadData } from "../components/lead-card";

const { get, post } = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}));

vi.mock("@/api/custom-fetch", () => ({
  default: {
    get,
    post,
  },
}));

function StudentDestination() {
  const { studentId } = useParams();
  const { search, state } = useLocation();
  const manualAdmission = (state as { groupSaleManualAdmission?: unknown } | null)
    ?.groupSaleManualAdmission;
  return (
    <>
      <p>destination-student:{studentId}{search}</p>
      <p data-testid="student-route-state">{manualAdmission ? "manual-admission" : "none"}</p>
    </>
  );
}

function AvailabilityDestination() {
  const { search } = useLocation();
  return <p>destination-availability:{search}</p>;
}

function LeadsDestination() {
  const { search } = useLocation();
  return (
    <>
      <p data-testid="lead-location">{search}</p>
      <Leads />
    </>
  );
}

function buildLead(overrides: Partial<LeadData> = {}): LeadData {
  return {
    id: 11,
    first_name: "Анна",
    last_name: "Тестова",
    phone: "+70000000001",
    lead_status: "new",
    status: "lead",
    loss_reason: null,
    is_child: false,
    source: "other",
    assigned_trainer_id: 9,
    trial_date: null,
    created_at: "2026-06-01T10:00:00Z",
    ...overrides,
  };
}

function personalReceipt(overrides: Record<string, unknown> = {}) {
  return {
    kind: "personal_staff_intent",
    booking_id: 71,
    reservation_id: null,
    payment_id: null,
    subscription_id: null,
    bank_payment_order_id: null,
    debt_id: null,
    slot_id: 81,
    schedule_id: 101,
    enrollment_id: 102,
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
    status: "pending_payment",
    provider_payment_url: null,
    allowed_actions: [],
    resource_route: "/internal/not-a-route",
    ...overrides,
  };
}

function actionContextFor(lead: LeadData) {
  const primary = lead.primary_action ?? (lead.lead_status === "trial_done"
    ? {
        kind: "sell_training",
        label: "Оформить обучение",
        supporting_text: "Пробная подтверждена check-in, можно оформить обучение.",
        target_resource_type: "student",
        target_resource_id: lead.id,
        context: "trial_done",
      }
    : lead.lead_status === "trial_booked"
      ? {
          kind: "open_trial",
          label: "Открыть пробную",
          supporting_text: "Назначена точная пробная тренировка.",
          target_resource_type: "schedule_enrollment",
          target_resource_id: 91,
          context: "upcoming_trial",
        }
      : {
          kind: "contact_lead",
          label: "Связаться",
          supporting_text: "Нужно зафиксировать результат контакта.",
          target_resource_type: "student",
          target_resource_id: lead.id,
          context: "lead_task_or_stage",
        });
  return { primary_action: primary, active_context: null, secondary_capabilities: [] };
}

function canonicalGroupSaleFixture() {
  return {
    groupPaymentSelectionMode: "canonical" as const,
    groupEnrollmentOptions: [
      {
        training_group_id: 25,
        schedule_id: 31,
        group_name: "Дети",
        next_occurrence_date: "2030-01-08",
        upcoming_occurrences: [{ schedule_id: 31, date: "2030-01-08" }],
        group_membership_action: "new_admission",
        is_canonical_group_card: true,
        is_latest_trial_group: true,
      },
    ],
    groupSaleOfferPreview: {
      protocol_version: "v2",
      student: { id: 11, display_name: "Антон Тестов" },
      tariff: {
        id: 6,
        name: "Групповой абонемент",
        price: "5000.00",
        trainings_limit: 8,
        duration_days: 30,
      },
      group: {
        id: 25,
        name: "Дети",
        responsible_trainer_name: "Тренер",
        location_name: "Зал",
        weekly_schedule: [],
      },
      selected_occurrence: { schedule_id: 31, date: "2030-01-08" },
      expected_action: "new_admission",
      buyer_email_required: false,
      offer_digest: "v2.lead-sbp-offer",
    },
  };
}

function renderLeads({
  leads,
  poolLeads = [],
  archivedLeads = [],
  schedules = [],
  locations = [],
  trainingTypes = [],
  leadsError = false,
  poolError = false,
  personalAvailabilityEnabled = false,
  unifiedJourneyCapability: unifiedJourneyCapabilityValue = true,
  groupSaleCommandProtocol = "v2",
  groupPaymentSelectionMode = "legacy",
  commercialAttempts = [],
  commercialContextError = false,
  availabilitySlots = [],
  groupEnrollmentOptions,
  groupSaleOfferPreview,
  initialEntry = "/trainer/leads",
}: {
  leads: LeadData[];
  poolLeads?: LeadData[];
  archivedLeads?: LeadData[];
  leadsError?: boolean;
  poolError?: boolean;
  personalAvailabilityEnabled?: boolean;
  unifiedJourneyCapability?: boolean | "error";
  groupSaleCommandProtocol?: "v1" | "v2" | "invalid";
  groupPaymentSelectionMode?: "canonical" | "legacy" | "disabled" | "error";
  commercialAttempts?: Array<Record<string, unknown>>;
  commercialContextError?: boolean;
  availabilitySlots?: Array<Record<string, unknown>>;
  groupEnrollmentOptions?: Array<Record<string, unknown>>;
  groupSaleOfferPreview?: Record<string, unknown>;
  schedules?: Array<{
    schedule_id: number;
    group_name: string;
    effective_date: string;
    effective_start_time: string;
    effective_end_time: string;
    trainer_id: number;
    trainer_name: string;
    location_id: number;
    location_name: string;
    training_type_name?: string;
  }>;
  locations?: Array<{ id: number; name: string }>;
  trainingTypes?: Array<{
    id: number;
    name: string;
    slug: string;
    kind: string;
    is_active: boolean;
  }>;
  initialEntry?: string;
}) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  get.mockImplementation((url: string, config?: { params?: Record<string, unknown> }) => {
    if (url === "/trainers/me/") {
      return Promise.resolve({ data: { id: 9 } });
    }

    if (url === "/billing/payment-capabilities/") {
      if (groupPaymentSelectionMode === "error") {
        return Promise.reject(new Error("payment capability unavailable"));
      }
      return Promise.resolve({
        data: {
          online_payments_enabled: true,
          payment_mode: "sbp",
          payment_modes: ["sbp"],
          payment_creation_enabled: true,
          can_create_payment_order: true,
          training_group_rollout_mode: "off",
          training_group_payment_selection_mode: groupPaymentSelectionMode,
          canonical_group_selection_enabled: groupPaymentSelectionMode === "canonical",
        },
      });
    }
    if (url === "/students/intakes/capability") {
      return unifiedJourneyCapabilityValue === "error"
        ? Promise.reject(new Error("unified capability unavailable"))
        : Promise.resolve({
            data: {
              enabled: unifiedJourneyCapabilityValue,
              group_sale_command_protocol_version: groupSaleCommandProtocol,
            },
          });
    }
    if (url === "/personal-availability/capability/") {
      return Promise.resolve({
        data: {
          enabled: personalAvailabilityEnabled,
          staff_command_protocol_version: "v1",
        },
      });
    }

    if (url === "/leads/") {
      const offset = Number(config?.params?.offset ?? 0);
      const limit = Number(config?.params?.limit ?? 50);
      if (config?.params?.workspace === "archived") {
        return Promise.resolve({
          data: {
            items: archivedLeads.slice(offset, offset + limit),
            count: archivedLeads.length,
          },
        });
      }
      if (config?.params?.scope === "pool") {
        if (poolError) {
          return Promise.reject(new Error("Pool leads failed"));
        }
        return Promise.resolve({
          data: { items: poolLeads.slice(offset, offset + limit), count: poolLeads.length },
        });
      }
      if (leadsError) {
        return Promise.reject(new Error("Leads failed"));
      }
      return Promise.resolve({
        data: { items: leads.slice(offset, offset + limit), count: leads.length },
      });
    }

    if (url.endsWith("/action-context")) {
      const leadId = Number(url.split("/")[2]);
      const lead = [...leads, ...poolLeads, ...archivedLeads].find(
        (item) => item.id === leadId,
      );
      return lead
        ? Promise.resolve({ data: actionContextFor(lead) })
        : Promise.reject({ response: { status: 404 } });
    }

    if (url.endsWith("/commercial-context/")) {
      if (commercialContextError) {
        return Promise.reject({ response: { status: 403 } });
      }
      return Promise.resolve({
        data: { lead_id: Number(url.split("/")[2]), attempts: commercialAttempts },
      });
    }

    if (url.startsWith("/leads/")) {
      const leadId = Number(url.split("/")[2]);
      const lead = [...leads, ...poolLeads, ...archivedLeads].find(
        (item) => item.id === leadId,
      );
      return lead ? Promise.resolve({ data: lead }) : Promise.reject({ response: { status: 404 } });
    }

    if (url === "/schedules/by-date/") {
      return Promise.resolve({ data: schedules });
    }

    if (url === "/personal-availability/slots/") {
      return Promise.resolve({ data: availabilitySlots });
    }

    if (url === "/personal-availability/offers/") {
      const matchingSlot = availabilitySlots.find(
        (item) => item.id === Number(config?.params?.slot_id),
      );
      return matchingSlot
        ? Promise.resolve({ data: matchingSlot })
        : Promise.reject(new Error("Personal availability offer not found"));
    }

    if (url === "/clubs/locations/") {
      return Promise.resolve({ data: locations });
    }

    if (url === "/billing/training-types/") {
      return Promise.resolve({ data: trainingTypes });
    }

    if (url === "/billing/subscriptions/") {
      return Promise.resolve({ data: [] });
    }

    if (url === "/students/11/personal-booking-payment-reservations/") {
      return Promise.resolve({ data: [] });
    }

    if (url === "/billing/tariffs/") {
      return Promise.resolve({
        data: {
          items: [
            {
              id: 6,
              name: "Групповой абонемент",
              price: 5000,
              training_type: {
                id: 5,
                name: "Групповой бокс",
                slug: "group-boxing",
                kind: "group",
                is_active: true,
              },
              trainings_limit: 8,
              duration_days: 30,
              is_active: true,
              requires_package_owner: false,
            },
          ],
        },
      });
    }

    if (url === "/billing/discounts/") {
      return Promise.resolve({ data: [] });
    }

    if (url === "/billing/debts/?student_id=11") {
      return Promise.resolve({ data: { items: [] } });
    }

    if (url === "/billing/bank-payment-orders/") {
      return Promise.resolve({ data: { items: [] } });
    }

    if (url === "/billing/group-sale-offers/preview/") {
      return groupSaleOfferPreview
        ? Promise.resolve({ data: groupSaleOfferPreview })
        : Promise.reject(new Error("Group sale preview is not configured"));
    }

    if (url === "/billing/group-enrollment-options/") {
      return Promise.resolve({
        data: groupEnrollmentOptions ?? [
          {
            schedule_id: 31,
            group_name: "Вечерняя группа",
            trainer_id: 12,
            trainer_name: "Другой Тренер",
            location_id: 3,
            location_name: "Главный зал",
            training_type_id: 5,
            training_type_name: "Групповой бокс",
            day_of_week: 1,
            start_time: "18:00:00",
            end_time: "19:00:00",
            next_occurrence_date: "2030-01-08",
            occurrence_dates: ["2030-01-08", "2030-01-10"],
            is_latest_trial_group: false,
          },
        ],
      });
    }

    return Promise.reject(new Error(`Unexpected GET ${url}`));
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route path="/trainer/leads" element={<LeadsDestination />} />
          <Route path="/trainer/students/:studentId" element={<StudentDestination />} />
          <Route path="/trainer/availability" element={<AvailabilityDestination />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function openPendingCanonicalGroupSbpNotice() {
  renderLeads({
    leads: [
      buildLead({
        first_name: "Антон",
        last_name: "Тестов",
        lead_status: "trial_done",
        trial_date: "2030-01-07",
      }),
    ],
    ...canonicalGroupSaleFixture(),
  });
  fireEvent.click(await screen.findByRole("button", { name: /Антон Тестов/ }));
  fireEvent.click(await screen.findByRole("button", { name: "Оформить обучение" }));
  fireEvent.click(await screen.findByRole("button", { name: /Дети.*Рекомендовано/ }));
  fireEvent.click(screen.getByRole("button", { name: "Проверить условия" }));
  expect(await screen.findByText("Проверка перед оформлением")).toBeInTheDocument();
  fireEvent.click(await screen.findByRole("button", { name: "СБП" }));
  fireEvent.click(
    screen.getByRole("button", { name: /Создать ссылку СБП на 5\s000 ₽ для Антон Тестов/ }),
  );
  await screen.findByRole("button", { name: "Сверить оплату" });
}

describe("Trainer leads page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    post.mockReset();
    post.mockResolvedValue({ data: {} });
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
  });

  it("opens lead details from an accessible card button and shows status actions", async () => {
    renderLeads({ leads: [buildLead()] });

    fireEvent.click(await screen.findByRole("button", { name: /Анна Тестова/ }));

    expect(screen.getByRole("heading", { name: "Анна Тестова" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Связаться" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Записать на пробную" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Потерян" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Ещё" }));
    expect(screen.getByRole("button", { name: "Потерян" })).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Конвертировать|Оформить сделку/ }),
    ).not.toBeInTheDocument();
  });

  it("re-authorizes and opens an intake deep link after a direct reload", async () => {
    renderLeads({ leads: [buildLead()], initialEntry: "/trainer/leads?lead=11" });

    expect(await screen.findByRole("heading", { name: "Анна Тестова" })).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith("/leads/11");
  });

  it("restores trainer-safe pending personal payment context on the canonical lead deep link", async () => {
    renderLeads({
      leads: [buildLead()],
      initialEntry: "/trainer/leads?lead=11",
      commercialAttempts: [personalReceipt()],
    });

    expect(await screen.findByText("Персональная запись и оплата")).toBeInTheDocument();
    expect(screen.getByText("Оплата ожидает подтверждения владельцем")).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith("/leads/11/commercial-context/");
    expect(screen.queryByText(/internal\/not-a-route/)).not.toBeInTheDocument();
  });

  it("shows a role-safe state when lead commercial context is unavailable", async () => {
    renderLeads({
      leads: [buildLead()],
      initialEntry: "/trainer/leads?lead=11",
      commercialContextError: true,
    });

    expect(
      await screen.findByText("Коммерческий контекст сейчас недоступен. Обновите карточку заявки."),
    ).toBeInTheDocument();
  });

  it("retries a terminal SBP attempt with a new slot command key and keeps terminal plus live context", async () => {
    const attempts = [
      personalReceipt({
        payment_method: "sbp",
        status: "failed",
        reservation_id: 81,
        bank_payment_order_id: 92,
        allowed_actions: ["retry_bank_payment"],
      }),
    ];
    const liveReceipt = personalReceipt({
      payment_method: "sbp",
      status: "pending_payment",
      reservation_id: 82,
      bank_payment_order_id: 93,
      allowed_actions: [],
    });
    post.mockImplementation((url: string) => {
      if (/\/personal-availability\/slots\/81\/staff-intents\/$/.test(url)) {
        attempts.push(liveReceipt);
        return Promise.resolve({ data: liveReceipt });
      }
      return Promise.resolve({ data: {} });
    });
    renderLeads({
      leads: [buildLead()],
      initialEntry: "/trainer/leads?lead=11",
      personalAvailabilityEnabled: true,
      commercialAttempts: attempts,
      availabilitySlots: [
        {
          id: 81,
          starts_at: "2099-07-07T10:00:00+05:00",
          ends_at: "2099-07-07T11:00:00+05:00",
          trainer_id: 9,
          trainer_name: "Тренер",
          location_id: 11,
          location_name: "Основной зал",
          training_type_id: 22,
          training_type_name: "Персоналка",
          offer_tariff_id: 44,
          offer_tariff_name: "Разовая персоналка",
          offer_price: "2500.00",
          offer_digest: "refreshed-slot-offer",
          offer_error_code: null,
        },
      ],
    });

    fireEvent.click(await screen.findByRole("button", { name: "Повторить оплату СБП" }));
    const retrySubmit = await screen.findByRole("button", {
      name: /Повторить оплату СБП для Анна Тестова на 2\s*500\s*₽/,
    });
    await waitFor(() => expect(retrySubmit).toBeEnabled());
    fireEvent.click(retrySubmit);
    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/personal-availability/slots/81/staff-intents/",
        expect.objectContaining({
          student_id: 11,
          payment_method: "sbp",
          offer_digest: "refreshed-slot-offer",
          idempotency_key: expect.any(String),
        }),
      );
    });
    expect(screen.getAllByRole("region", { name: "Коммерческий контекст персоналки" })).toHaveLength(2);
    expect(screen.getByText("Оплата не прошла")).toBeInTheDocument();
    expect(screen.getByText("Ожидает оплаты через СБП")).toBeInTheDocument();
  });

  it("refreshes a lead renewal retry from the receipt offer after a stale target", async () => {
    const attempts = [
      personalReceipt({
        kind: "renewal",
        payment_method: "cash",
        status: "failed",
        amount: "5000.00",
        tariff_name: "Base A",
        renewed_from_subscription_id: 55,
        renewed_from_subscription_name: "Base A",
        renewal_target_tariff_id: 9,
        renewal_target_tariff_name: "Base B",
        renewal_target_price: "6500.00",
        allowed_actions: ["create_renewal"],
      }),
    ];
    post.mockImplementationOnce(() => {
      attempts.splice(0, 1, personalReceipt({
        kind: "renewal",
        payment_method: "cash",
        status: "failed",
        amount: "5000.00",
        tariff_name: "Base A",
        renewed_from_subscription_id: 55,
        renewed_from_subscription_name: "Base A",
        renewal_target_tariff_id: 10,
        renewal_target_tariff_name: "Base C",
        renewal_target_price: "7000.00",
        allowed_actions: ["create_renewal"],
      }));
      return Promise.reject({
        response: { data: { code: "renewal_offer_stale", detail: "Offer changed" } },
      });
    });
    post.mockResolvedValueOnce({ data: { payment_id: 94 } });

    renderLeads({
      leads: [buildLead()],
      initialEntry: "/trainer/leads?lead=11",
      commercialAttempts: attempts,
    });

    fireEvent.click(await screen.findByRole("button", { name: "Создать оплату снова" }));
    expect(await screen.findByText(/Base B.*6\s?500/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Зафиксировать оплату" }));

    await waitFor(() => {
      expect(get.mock.calls.filter(([url]) => url === "/leads/11/commercial-context/")).toHaveLength(2);
    });
    fireEvent.click(screen.getByRole("button", { name: "Создать оплату снова" }));
    expect(await screen.findByText(/Base C.*7\s?000/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Зафиксировать оплату" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/billing/payments/renewals/",
        expect.objectContaining({
          student_id: 11,
          renewed_from_subscription_id: 55,
          expected_target_tariff_id: 10,
          expected_target_price: "7000.00",
        }),
      );
    });
  });

  it("shows archived leads separately and reopens the same card", async () => {
    const archived = buildLead({
      id: 19,
      lead_status: null,
      status: "lost",
      workspace: "leads_archived",
    });
    renderLeads({ leads: [], archivedLeads: [archived] });

    fireEvent.click(await screen.findByRole("tab", { name: "Завершённые" }));
    fireEvent.click(await screen.findByRole("button", { name: /Анна Тестова/ }));
    fireEvent.click(screen.getByRole("button", { name: "Вернуть в работу" }));

    await waitFor(() => expect(post).toHaveBeenCalledWith("/leads/19/reopen"));
  });

  it("renders one primary CTA from the server action context", async () => {
    renderLeads({
      leads: [
        buildLead({
          primary_action: {
            kind: "open_payment",
            label: "Продолжить оплату",
            supporting_text: "Есть незавершённая оплата по точному контексту услуги.",
            target_resource_type: "bank_payment_order",
            target_resource_id: 88,
            context: "payment_action_required",
          },
        }),
      ],
    });

    fireEvent.click(await screen.findByRole("button", { name: /Анна Тестова/ }));

    expect(await screen.findByText(/незавершённая оплата/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Продолжить оплату" })).toBeInTheDocument();
    expect(document.querySelectorAll(".ui-brand-action")).toHaveLength(1);
  });

  it.each([
    ["payment", "Продолжить оплату", "payment", 701, "pending_payment"],
    [
      "personal booking",
      "Открыть запись",
      "personal_booking_reservation",
      702,
      "upcoming_personal_booking",
    ],
    ["trial", "Открыть пробную", "schedule_enrollment", 703, "upcoming_trial"],
  ] as const)(
    "keeps the %s action on the canonical lead deep link instead of inventing a student route",
    async (_kind, label, resourceType, resourceId, context) => {
      renderLeads({
        leads: [
          buildLead({
            primary_action: {
              kind: "open_existing_context",
              label,
              supporting_text: "Откройте точный сохранённый контекст.",
              target_resource_type: resourceType,
              target_resource_id: resourceId,
              context,
            },
          }),
        ],
      });

      fireEvent.click(await screen.findByRole("button", { name: /Анна Тестова/ }));
      fireEvent.click(await screen.findByRole("button", { name: label }));

      await waitFor(() => {
        expect(screen.getByTestId("lead-location")).toHaveTextContent("?lead=11");
      });
      expect(await screen.findByRole("heading", { name: "Анна Тестова" })).toBeInTheDocument();
      expect(screen.queryByText(/destination-student:/)).not.toBeInTheDocument();
    },
  );

  it("fails closed instead of falling back to a keyless group payment while unified mode lacks canonical selection", async () => {
    renderLeads({ leads: [buildLead()] });

    fireEvent.click(await screen.findByRole("button", { name: /Анна Тестова/ }));
    fireEvent.click(screen.getByRole("button", { name: "Ещё" }));
    fireEvent.click(screen.getByRole("button", { name: "Оформить сразу в группу" }));

    expect(
      await screen.findByText(/Оплата не будет создана без точной группы/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Оформить в группу" })).not.toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });

  it.each([
    { unifiedJourneyCapability: false, groupPaymentSelectionMode: "legacy", expected: "legacy" },
    { unifiedJourneyCapability: true, groupPaymentSelectionMode: "canonical", expected: "contextual" },
    {
      unifiedJourneyCapability: true,
      groupPaymentSelectionMode: "canonical",
      groupSaleCommandProtocol: "v1",
      expected: "legacy",
    },
    {
      unifiedJourneyCapability: true,
      groupPaymentSelectionMode: "canonical",
      groupSaleCommandProtocol: "invalid",
      expected: "blocked",
    },
    { unifiedJourneyCapability: true, groupPaymentSelectionMode: "legacy", expected: "blocked" },
    { unifiedJourneyCapability: true, groupPaymentSelectionMode: "disabled", expected: "blocked" },
    { unifiedJourneyCapability: true, groupPaymentSelectionMode: "error", expected: "blocked" },
    { unifiedJourneyCapability: "error", groupPaymentSelectionMode: "canonical", expected: "blocked" },
  ] as const)(
    "uses generic group payment only for the authoritative legacy capability state",
    async ({ unifiedJourneyCapability, groupPaymentSelectionMode, groupSaleCommandProtocol, expected }) => {
      renderLeads({
        leads: [buildLead()],
        unifiedJourneyCapability,
        groupPaymentSelectionMode,
        ...(groupSaleCommandProtocol ? { groupSaleCommandProtocol } : {}),
      });

      fireEvent.click(await screen.findByRole("button", { name: /Анна Тестова/ }));
      const moreActions = screen.queryByRole("button", { name: "Ещё" });
      if (moreActions) fireEvent.click(moreActions);
      fireEvent.click(screen.getByRole("button", { name: "Оформить сразу в группу" }));

      if (expected === "blocked") {
        expect(
          await screen.findByText(/Оплата не будет создана без точной группы/),
        ).toBeInTheDocument();
        expect(post).not.toHaveBeenCalled();
        return;
      }

      if (expected === "legacy") {
        expect(await screen.findByRole("heading", { name: "Оформить в группу" })).toBeInTheDocument();
      } else {
        expect(await screen.findByRole("heading", { name: "Оформить обучение" })).toBeInTheDocument();
        expect(screen.queryByRole("heading", { name: "Оформить в группу" })).not.toBeInTheDocument();
      }
      expect(post).not.toHaveBeenCalled();
    },
  );

  it("carries the authoritative trial hint through manual group admission and hands off its receipt", async () => {
    post.mockResolvedValueOnce({
      data: {
        payment_id: 91,
        subscription_id: 101,
        workspace_state: "student",
        finance_state: "pending_manual",
      },
    });
    renderLeads({
      leads: [buildLead({ first_name: "Антон", last_name: "Тестов", lead_status: "trial_done", trial_date: "2030-01-07" })],
      groupPaymentSelectionMode: "canonical",
      groupEnrollmentOptions: [
        {
          training_group_id: 25,
          schedule_id: 31,
          group_name: "Дети",
          next_occurrence_date: "2030-01-08",
          upcoming_occurrences: [{ schedule_id: 31, date: "2030-01-08" }],
          group_membership_action: "new_admission",
          is_canonical_group_card: true,
          is_latest_trial_group: true,
        },
      ],
      groupSaleOfferPreview: {
        protocol_version: "v2",
        student: { id: 11, display_name: "Антон Тестов" },
        tariff: { id: 6, name: "Групповой абонемент", price: "5000.00", trainings_limit: 8, duration_days: 30 },
        group: { id: 25, name: "Дети", responsible_trainer_name: "Тренер", location_name: "Зал", weekly_schedule: [] },
        selected_occurrence: { schedule_id: 31, date: "2030-01-08" },
        expected_action: "new_admission",
        buyer_email_required: false,
        offer_digest: "v2.lead-manual-offer",
      },
    });

    fireEvent.click(await screen.findByRole("button", { name: /Антон Тестов/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Оформить обучение" }));
    expect(await screen.findByText("Пробная проведена · 2030-01-07")).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: /Дети.*Рекомендовано/ }));
    fireEvent.click(screen.getByRole("button", { name: "Проверить условия" }));
    expect(await screen.findByText("Проверка перед оформлением")).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: /Зафиксировать наличные 5\s000 ₽ и оформить Антон Тестов/ }),
    );

    expect(await screen.findByText("destination-student:11")).toBeInTheDocument();
    expect(screen.getByTestId("student-route-state")).toHaveTextContent("manual-admission");
  });

  it("shows a terminal group SBP result instead of pending copy and allows dismiss", async () => {
    post
      .mockResolvedValueOnce({
        data: {
          payment_id: 91,
          bank_payment_order_id: 92,
          workspace_state: "lead",
          finance_state: "provider_pending",
          bank_payment_order_status: "pending",
          provider_payment_url: "https://payments.example.test/order/92",
          allowed_actions: ["cancel", "refresh"],
        },
      })
      .mockResolvedValueOnce({
        data: {
          status: "cancelled",
          fulfillment_state: "not_fulfilled",
          can_share: false,
          can_copy: false,
          can_cancel: false,
          can_request_refresh: false,
        },
      });
    renderLeads({
      leads: [
        buildLead({
          first_name: "Антон",
          last_name: "Тестов",
          lead_status: "trial_done",
          trial_date: "2030-01-07",
        }),
      ],
      ...canonicalGroupSaleFixture(),
    });

    fireEvent.click(await screen.findByRole("button", { name: /Антон Тестов/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Оформить обучение" }));
    fireEvent.click(await screen.findByRole("button", { name: /Дети.*Рекомендовано/ }));
    fireEvent.click(screen.getByRole("button", { name: "Проверить условия" }));
    expect(await screen.findByText("Проверка перед оформлением")).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "СБП" }));
    fireEvent.click(
      screen.getByRole("button", { name: /Создать ссылку СБП на 5\s000 ₽ для Антон Тестов/ }),
    );

    fireEvent.click(await screen.findByRole("button", { name: "Отменить попытку" }));
    expect(
      await screen.findByText(/Попытка оплаты завершена.*снова откройте оформление обучения/),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Ссылка создана. Ученик будет оформлен после подтверждения оплаты."),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Закрыть уведомление" }));
    expect(screen.queryByText(/Попытка оплаты завершена/)).not.toBeInTheDocument();
  });

  it("invalidates commercial projections before fulfilled group SBP handoff", async () => {
    const invalidateSpy = vi.spyOn(QueryClient.prototype, "invalidateQueries");
    post
      .mockResolvedValueOnce({
        data: {
          payment_id: 91,
          bank_payment_order_id: 92,
          workspace_state: "lead",
          finance_state: "provider_pending",
          bank_payment_order_status: "pending",
          provider_payment_url: "https://payments.example.test/order/92",
          allowed_actions: ["cancel", "refresh"],
        },
      })
      .mockResolvedValueOnce({
        data: {
          status: "approved",
          fulfillment_state: "fulfilled",
          can_share: false,
          can_copy: false,
          can_cancel: false,
          can_request_refresh: false,
        },
      });
    renderLeads({
      leads: [
        buildLead({
          first_name: "Антон",
          last_name: "Тестов",
          lead_status: "trial_done",
          trial_date: "2030-01-07",
        }),
      ],
      ...canonicalGroupSaleFixture(),
    });

    fireEvent.click(await screen.findByRole("button", { name: /Антон Тестов/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Оформить обучение" }));
    fireEvent.click(await screen.findByRole("button", { name: /Дети.*Рекомендовано/ }));
    fireEvent.click(screen.getByRole("button", { name: "Проверить условия" }));
    expect(await screen.findByText("Проверка перед оформлением")).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "СБП" }));
    fireEvent.click(
      screen.getByRole("button", { name: /Создать ссылку СБП на 5\s000 ₽ для Антон Тестов/ }),
    );

    fireEvent.click(await screen.findByRole("button", { name: "Сверить оплату" }));
    expect(await screen.findByText("destination-student:11")).toBeInTheDocument();
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["student"] });
  });

  it("keeps an approved but not yet fulfilled group SBP lead out of student UI", async () => {
    post
      .mockResolvedValueOnce({
        data: {
          payment_id: 91,
          bank_payment_order_id: 92,
          workspace_state: "lead",
          finance_state: "provider_pending",
          bank_payment_order_status: "pending",
          provider_payment_url: "https://payments.example.test/order/92",
          allowed_actions: ["cancel", "refresh"],
        },
      })
      .mockResolvedValueOnce({
        data: {
          status: "approved",
          fulfillment_state: "fulfillment_pending",
          can_share: false,
          can_copy: false,
          can_cancel: false,
          can_request_refresh: true,
        },
      });

    await openPendingCanonicalGroupSbpNotice();
    fireEvent.click(screen.getByRole("button", { name: "Сверить оплату" }));

    expect(
      await screen.findByText("Оплата подтверждена. Завершаем оформление ученика."),
    ).toBeInTheDocument();
    expect(screen.queryByText("destination-student:11")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Сверить оплату" })).toBeInTheDocument();
  });

  it("routes a canonical renewal conflict to the existing student detail instead of creating a sale", async () => {
    renderLeads({
      leads: [buildLead()],
      groupPaymentSelectionMode: "canonical",
      groupEnrollmentOptions: [
        {
          training_group_id: 25,
          schedule_id: 31,
          group_name: "Дети",
          next_occurrence_date: "2030-01-08",
          group_membership_action: "renewal",
          renewed_from_subscription_id: 58,
          is_canonical_group_card: true,
        },
      ],
    });

    fireEvent.click(await screen.findByRole("button", { name: /Анна Тестова/ }));
    fireEvent.click(screen.getByRole("button", { name: "Ещё" }));
    fireEvent.click(screen.getByRole("button", { name: "Оформить сразу в группу" }));
    fireEvent.click(await screen.findByRole("button", { name: "Открыть продление ученика" }));

    expect(await screen.findByText("destination-student:11")).toBeInTheDocument();
    expect(screen.getByTestId("student-route-state")).toHaveTextContent("none");
    expect(post).not.toHaveBeenCalled();
  });

  it("creates a new request from the unified lead intake sheet", async () => {
    renderLeads({ leads: [] });

    fireEvent.click(await screen.findByRole("button", { name: "Добавить ученика" }));

    expect(screen.getByRole("heading", { name: "Добавить ученика" })).toBeInTheDocument();
    expect(screen.queryByText("Новый лид")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Создать лид" })).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Имя клиента *"), {
      target: { value: "Иван" },
    });
    fireEvent.change(screen.getByLabelText("Телефон клиента *"), {
      target: { value: "+7 (900) 123-45-67" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Создать заявку" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/students/intakes/", {
        idempotency_key: expect.any(String),
        intake_kind: "new_contact",
        first_name: "Иван",
        last_name: "",
        phone: "+79001234567",
        guardian_phone: "",
        is_child: false,
        date_of_birth: null,
        source: "other",
        confirm_distinct_child: false,
      });
    });
    expect(post).not.toHaveBeenCalledWith("/students/", expect.anything());
  });

  it("creates a child request with guardian phone instead of child personal phone", async () => {
    renderLeads({ leads: [] });

    fireEvent.click(await screen.findByRole("button", { name: "Добавить ученика" }));
    fireEvent.click(screen.getByRole("switch"));

    expect(screen.getByText("Один номер родителя можно использовать для нескольких детей.")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Имя ребёнка *"), {
      target: { value: "Маша" },
    });
    fireEvent.change(screen.getByLabelText("Телефон родителя *"), {
      target: { value: "+7 (900) 123-45-67" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Создать заявку" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/students/intakes/", {
        idempotency_key: expect.any(String),
        intake_kind: "new_contact",
        first_name: "Маша",
        last_name: "",
        phone: "",
        guardian_phone: "+79001234567",
        is_child: true,
        date_of_birth: null,
        source: "other",
        confirm_distinct_child: false,
      });
    });
  });

  it("shows the server-owned privacy-safe duplicate result", async () => {
    post.mockRejectedValueOnce({
      response: {
        data: {
          code: "duplicate_phone",
          detail: "Такая заявка уже есть в свободных",
          result_kind: "duplicate",
          identity_visibility: "masked",
          allowed_action: "claim_pool_lead",
          route: null,
        },
      },
    });
    renderLeads({ leads: [] });

    fireEvent.click(await screen.findByRole("button", { name: "Добавить ученика" }));
    fireEvent.change(screen.getByLabelText("Имя клиента *"), {
      target: { value: "Иван" },
    });
    fireEvent.change(screen.getByLabelText("Телефон клиента *"), {
      target: { value: "+7 (900) 123-45-67" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Создать заявку" }));

    expect(await screen.findByText("Такая заявка уже есть в свободных")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Открыть карточку" })).not.toBeInTheDocument();
  });

  it("shows a loading error instead of an empty state when mine leads fail", async () => {
    renderLeads({ leads: [], leadsError: true });

    expect(await screen.findByText("Не удалось загрузить заявки")).toBeInTheDocument();
    expect(screen.queryByText("Нет активных заявок")).not.toBeInTheDocument();
  });

  it("does not show terminal history buckets in the active trainer lead view", async () => {
    renderLeads({
      leads: [
        buildLead({ id: 91, first_name: "Converted", lead_status: "converted" }),
        buildLead({ id: 92, first_name: "Lost", lead_status: "lost" }),
        buildLead({ id: 93, first_name: "Active", lead_status: "new" }),
      ],
    });

    expect(await screen.findByText("НОВЫЕ")).toBeInTheDocument();
    expect(screen.queryByText("СДЕЛКА")).not.toBeInTheDocument();
    expect(screen.queryByText("ПОТЕРЯНЫ")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Converted/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Lost/ })).not.toBeInTheDocument();
  });

  it("queries mine and pool leads through segmented scoped modes", async () => {
    renderLeads({
      leads: [buildLead({ id: 12, first_name: "Анна" })],
      poolLeads: [
        buildLead({
          id: 13,
          first_name: "Олег",
          phone: undefined,
          masked_phone: "+7 *** ***-00-13",
          assigned_trainer_id: null,
        }),
      ],
    });

    await screen.findByRole("button", { name: /Анна Тестова/ });
    expect(get).toHaveBeenCalledWith("/leads/", {
      params: { scope: "mine", limit: 50, offset: 0 },
    });

    fireEvent.click(screen.getByRole("tab", { name: "Свободные заявки" }));

    expect(await screen.findByText("+7 *** ***-00-13")).toBeInTheDocument();
    expect(screen.getByText("Новый")).toBeInTheDocument();
    expect(screen.getByText(/в CRM/)).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith("/leads/", {
      params: { scope: "pool", limit: 50, offset: 0 },
    });
  });

  it("loads additional mine leads instead of hiding records after the first page", async () => {
    const leads = Array.from({ length: 51 }, (_, index) =>
      buildLead({
        id: index + 1,
        first_name: `Лид${index + 1}`,
        last_name: "Тестовый",
      }),
    );
    renderLeads({ leads });

    expect(await screen.findByRole("button", { name: /Лид1 Тестовый/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Лид51 Тестовый/ })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Загрузить ещё/ }));

    expect(await screen.findByRole("button", { name: /Лид51 Тестовый/ })).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith("/leads/", {
      params: { scope: "mine", limit: 50, offset: 50 },
    });
  }, 10_000);

  it("renders pool cards with masked phone and no raw phone requirement", async () => {
    renderLeads({
      leads: [],
      poolLeads: [
        buildLead({
          id: 61,
          first_name: "Павел",
          last_name: "Пулов",
          phone: undefined,
          masked_phone: "+7 *** ***-44-55",
          assigned_trainer_id: null,
        }),
      ],
    });

    fireEvent.click(await screen.findByRole("tab", { name: "Свободные заявки" }));

    expect(await screen.findByText("+7 *** ***-44-55")).toBeInTheDocument();
    expect(screen.queryByText("+79995554433")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Забрать" })).toBeInTheDocument();
  });

  it("claims a pool lead and shows it in mine", async () => {
    const mineLeads: LeadData[] = [];
    const claimedLead = buildLead({
      id: 71,
      first_name: "Мария",
      last_name: "Заявка",
      phone: "+70000000071",
      assigned_trainer_id: 9,
    });
    post.mockImplementationOnce(() => {
      mineLeads.push(claimedLead);
      return Promise.resolve({ data: claimedLead });
    });

    renderLeads({
      leads: mineLeads,
      poolLeads: [
        buildLead({
          ...claimedLead,
          phone: undefined,
          masked_phone: "+7 *** ***-00-71",
          assigned_trainer_id: null,
        }),
      ],
    });

    fireEvent.click(await screen.findByRole("tab", { name: "Свободные заявки" }));
    fireEvent.click(await screen.findByRole("button", { name: "Забрать" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/leads/71/claim");
    });
    expect(await screen.findByRole("button", { name: /Мария Заявка/ })).toBeInTheDocument();
    expect(screen.getByText("+70000000071")).toBeInTheDocument();
  });

  it("shows conflict copy when a pool lead was already claimed", async () => {
    post.mockRejectedValueOnce({ response: { status: 409 } });

    renderLeads({
      leads: [],
      poolLeads: [
        buildLead({
          id: 81,
          first_name: "Сергей",
          phone: undefined,
          masked_phone: "+7 *** ***-00-81",
          assigned_trainer_id: null,
        }),
      ],
    });

    fireEvent.click(await screen.findByRole("tab", { name: "Свободные заявки" }));
    fireEvent.click(await screen.findByRole("button", { name: "Забрать" }));

    expect(await screen.findByText("Заявку уже забрали")).toBeInTheDocument();
  });

  it("records a new lead contact through the typed outcome command", async () => {
    renderLeads({ leads: [buildLead({ id: 21 })] });

    fireEvent.click(await screen.findByRole("button", { name: /Анна Тестова/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Связаться" }));
    fireEvent.click(screen.getByRole("button", { name: "Сохранить результат" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/leads/21/contact-outcomes/", {
        outcome: "contacted",
        due_date: null,
        loss_reason: "",
        notes: "",
      });
    });
  });

  it("requires a user-selected due date before recording no answer", async () => {
    renderLeads({ leads: [buildLead({ id: 22 })] });

    fireEvent.click(await screen.findByRole("button", { name: /Анна Тестова/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Связаться" }));
    fireEvent.change(screen.getByLabelText("Результат контакта"), {
      target: { value: "no_answer" },
    });

    const submit = screen.getByRole("button", { name: "Сохранить результат" });
    expect(submit).toBeDisabled();
    fireEvent.click(submit);
    expect(post).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("Дата следующего контакта"), {
      target: { value: "2099-06-10" },
    });
    expect(submit).toBeEnabled();
    fireEvent.click(submit);

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/leads/22/contact-outcomes/", {
        outcome: "no_answer",
        due_date: "2099-06-10",
        loss_reason: "",
        notes: "",
      });
    });
  });

  it("hands an assigned lead from its detail sheet to one sibling personal-booking sheet", async () => {
    renderLeads({ leads: [buildLead({ id: 11, lead_status: "contacted" })] });

    fireEvent.click(await screen.findByRole("button", { name: /Анна Тестова/ }));
    expect(screen.getByRole("dialog", { name: "Анна Тестова" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Ещё" }));
    fireEvent.click(screen.getByRole("button", { name: "Записать персоналку" }));

    expect(screen.getByRole("dialog", { name: "Записать персоналку" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "Анна Тестова" })).not.toBeInTheDocument();
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
  });

  it("hands a flag-on lead to availability with only the scoped person id preselected", async () => {
    renderLeads({
      leads: [buildLead({ id: 11, lead_status: "contacted" })],
      personalAvailabilityEnabled: true,
    });

    fireEvent.click(await screen.findByRole("button", { name: /Анна Тестова/ }));
    fireEvent.click(screen.getByRole("button", { name: "Ещё" }));
    fireEvent.click(screen.getByRole("button", { name: "Записать персоналку" }));

    expect(await screen.findByText("destination-availability:?student_id=11")).toBeInTheDocument();
  });

  it("books a trial with schedule_id through book-trial instead of status trial_booked", async () => {
    renderLeads({
      leads: [
        buildLead({
          id: 31,
          first_name: "Борис",
          last_name: "Пробный",
          lead_status: "contacted",
        }),
      ],
      schedules: [
        {
          schedule_id: 41,
          group_name: "Утро",
          effective_date: "2099-06-10",
          effective_start_time: "10:00:00",
          effective_end_time: "11:00:00",
          trainer_id: 9,
          trainer_name: "Тренер",
          location_id: 3,
          location_name: "Зал",
          training_type_name: "Муай тай",
        },
      ],
    });

    fireEvent.click(await screen.findByRole("button", { name: /Борис Пробный/ }));
    fireEvent.click(screen.getByRole("button", { name: "Ещё" }));
    fireEvent.click(screen.getByRole("button", { name: "Записать на пробную" }));

    const submit = screen.getByRole("button", { name: "Сохранить пробную" });
    expect(submit).toBeDisabled();

    fireEvent.change(screen.getByLabelText("Дата пробной *"), {
      target: { value: "2099-06-10" },
    });

    await waitFor(() => {
      expect(get).toHaveBeenCalledWith("/schedules/by-date/", {
        params: { date: "2099-06-10" },
      });
    });
    expect(submit).toBeDisabled();

    fireEvent.change(await screen.findByLabelText("Тренировка *"), {
      target: { value: "41" },
    });
    expect(submit).toBeEnabled();

    fireEvent.click(submit);

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/leads/31/book-trial", {
        mode: "group",
        schedule_id: 41,
        occurrence_date: "2099-06-10",
      });
    });
    expect(post).not.toHaveBeenCalledWith(
      "/leads/31/status",
      expect.objectContaining({ status: "trial_booked" }),
    );
  });

  it("allows thinking leads to book a trial through book-trial", async () => {
    renderLeads({
      leads: [buildLead({ id: 42, lead_status: "thinking" })],
      schedules: [
        {
          schedule_id: 43,
          group_name: "Вечер",
          effective_date: "2099-06-11",
          effective_start_time: "19:00:00",
          effective_end_time: "20:00:00",
          trainer_id: 9,
          trainer_name: "Тренер",
          location_id: 3,
          location_name: "Зал",
          training_type_name: "Муай тай",
        },
      ],
    });

    fireEvent.click(await screen.findByRole("button", { name: /Анна Тестова/ }));
    fireEvent.click(screen.getByRole("button", { name: "Ещё" }));
    fireEvent.click(screen.getByRole("button", { name: "Записать на пробную" }));

    fireEvent.change(screen.getByLabelText("Дата пробной *"), {
      target: { value: "2099-06-11" },
    });

    await waitFor(() => {
      expect(get).toHaveBeenCalledWith("/schedules/by-date/", {
        params: { date: "2099-06-11" },
      });
    });
    await screen.findByRole("option", { name: /Вечер/ });
    const submit = screen.getByRole("button", { name: "Сохранить пробную" });
    expect(submit).toBeDisabled();

    fireEvent.change(screen.getByLabelText("Тренировка *"), {
      target: { value: "43" },
    });
    expect(submit).toBeEnabled();

    fireEvent.click(submit);

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/leads/42/book-trial", {
        mode: "group",
        schedule_id: 43,
        occurrence_date: "2099-06-11",
      });
    });
    expect(screen.queryByText(/передайте администратору/i)).not.toBeInTheDocument();
  });

  it("does not offer a new personal trial", async () => {
    renderLeads({
      leads: [
        buildLead({
          id: 52,
          first_name: "Лид",
          last_name: "Персональный",
          lead_status: "contacted",
        }),
      ],
    });

    fireEvent.click(await screen.findByRole("button", { name: /Лид Персональный/ }));
    fireEvent.click(screen.getByRole("button", { name: "Ещё" }));
    fireEvent.click(screen.getByRole("button", { name: "Записать на пробную" }));
    expect(screen.queryByRole("button", { name: "Персонально" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Начало *")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Тренировка *")).toBeInTheDocument();
  });

  it("requires a loss reason before losing a lead", async () => {
    renderLeads({
      leads: [buildLead({ id: 51, lead_status: "thinking" })],
    });

    fireEvent.click(await screen.findByRole("button", { name: /Анна Тестова/ }));
    fireEvent.click(screen.getByRole("button", { name: "Ещё" }));
    fireEvent.click(screen.getByRole("button", { name: "Потерян" }));

    const submit = screen.getByRole("button", { name: "Сохранить потерю" });
    fireEvent.click(submit);

    expect(await screen.findByText("Выберите причину потери")).toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("Причина потери *"), {
      target: { value: "expensive" },
    });

    fireEvent.click(submit);

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/leads/51/contact-outcomes/", {
        outcome: "lost",
        due_date: null,
        loss_reason: "expensive",
        notes: "",
      });
    });
  });

  it("shows trainer-safe post-trial copy without owner-only convert action", async () => {
    renderLeads({
      leads: [buildLead({ lead_status: "trial_done" })],
    });

    fireEvent.click(await screen.findByRole("button", { name: /Анна Тестова/ }));

    expect(
      await screen.findByText(/Пробная подтверждена check-in/),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Оформить обучение" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Пробная прошла" })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Конвертировать|Оформить сделку/ }),
    ).not.toBeInTheDocument();
    expect(post).not.toHaveBeenCalledWith(
      expect.stringContaining("/convert"),
      expect.anything(),
    );
  });
});
