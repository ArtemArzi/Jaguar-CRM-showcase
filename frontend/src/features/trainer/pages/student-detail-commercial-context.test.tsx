import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "@/features/auth/auth-store";
import { useBrandingStore } from "@/features/branding/use-branding";
import StudentDetail from "./student-detail";

const { get, post, deleteRequest } = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  deleteRequest: vi.fn(),
}));

vi.mock("@/api/custom-fetch", () => ({
  default: { get, post, delete: deleteRequest },
}));

const student = {
  id: 12,
  first_name: "Mira",
  last_name: "Ivanova",
  phone: "",
  status: "lead",
  contraindications: "",
  notes: [],
  account_access: null,
};
let personalAvailabilityEnabled = true;
let commercialAttempts: readonly Record<string, unknown>[] = [];
let commercialSubscriptions: readonly Record<string, unknown>[] = [];

function manualReceipt(status = "pending") {
  return {
    kind: "personal_staff_intent" as const,
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
    payment_method: "cash" as const,
    status,
    provider_payment_url: null,
    allowed_actions: ["review_payment"],
    resource_route: "/api/personal-drop-in-bookings/71/",
  };
}

function retryableSbpReceipt() {
  return {
    ...manualReceipt("failed"),
    slot_id: null,
    payment_method: "sbp" as const,
    allowed_actions: ["retry_bank_payment"],
  };
}

function renewalReceipt(
  status: "failed" | "cancelled",
  target: { id: number; name: string; price: string } | null = {
    id: 9,
    name: "Base B",
    price: "6500.00",
  },
) {
  return {
    ...manualReceipt(status),
    kind: "renewal" as const,
    booking_id: null,
    payment_id: 92,
    subscription_id: null,
    bank_payment_order_id: null,
    slot_id: null,
    schedule_id: null,
    enrollment_id: null,
    starts_at: null,
    ends_at: null,
    tariff_id: 4,
    tariff_name: "Base A",
    amount: "5000.00",
    payment_method: "cash" as const,
    allowed_actions: ["create_renewal"],
    renewed_from_subscription_id: 55,
    renewed_from_subscription_name: "Base A",
    renewal_target_tariff_id: target?.id ?? null,
    renewal_target_tariff_name: target?.name ?? null,
    renewal_target_price: target?.price ?? null,
  };
}

function LocationStateProbe() {
  const location = useLocation();
  return <output data-testid="student-detail-route-state">{JSON.stringify(location.state)}</output>;
}

function renderDetail(
  initialEntry: string | { pathname: string; state?: unknown } = "/trainer/students/12",
) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <QueryClientProvider client={queryClient}>
        <Routes>
          <Route path="/trainer/students/:studentId" element={<StudentDetail />} />
        </Routes>
        <LocationStateProbe />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

describe("StudentDetail commercial context", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    personalAvailabilityEnabled = true;
    commercialAttempts = [manualReceipt(), manualReceipt("rejected")];
    commercialSubscriptions = [];
    useAuthStore.setState({ accessToken: null, role: "trainer", clubId: 1, trainerId: 9 });
    useBrandingStore.setState({
      timeZone: "Asia/Yekaterinburg",
      timeZoneStatus: "ready",
      timeZoneClubId: 1,
      isTimeZoneAuthoritative: true,
    });
    get.mockImplementation((url: string) => {
      if (url === "/personal-availability/capability/") {
        return Promise.resolve({
          data: {
            enabled: personalAvailabilityEnabled,
            staff_command_protocol_version: "v2",
          },
        });
      }
      if (url === "/students/12/") return Promise.resolve({ data: student });
      if (url === "/billing/subscriptions/") {
        return Promise.resolve({ data: commercialSubscriptions });
      }
      if (url === "/billing/bank-payment-orders/") return Promise.resolve({ data: { items: [] } });
      if (url === "/students/12/personal-bookings/") return Promise.resolve({ data: [] });
      if (url === "/grades/students/12/progress/") return Promise.resolve({ data: [] });
      if (url === "/students/12/checkins/") return Promise.resolve({ data: [] });
      if (url === "/feedback/students/12/responses/") return Promise.resolve({ data: [] });
      if (url === "/students/12/commercial-context/") {
        return Promise.resolve({ data: { student_id: 12, attempts: commercialAttempts } });
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });
  });

  it("restores live and latest terminal authoritative receipts after a reload", async () => {
    const first = renderDetail();
    expect(await screen.findByText("Персональная запись и оплата")).toBeInTheDocument();
    expect(await screen.findAllByRole("region", { name: "Коммерческий контекст персоналки" })).toHaveLength(2);
    expect(await screen.findByText("Оплата ожидает подтверждения владельцем")).toBeInTheDocument();
    expect(screen.queryByText(/стал учеником/i)).not.toBeInTheDocument();

    first.unmount();
    renderDetail();
    expect(await screen.findAllByRole("region", { name: "Коммерческий контекст персоналки" })).toHaveLength(2);
    expect(get.mock.calls.filter(([url]) => url === "/students/12/commercial-context/")).toHaveLength(2);
  });

  it("shows a safe state when the scoped commercial context is unavailable", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/personal-availability/capability/") return Promise.resolve({ data: { enabled: true } });
      if (url === "/students/12/") return Promise.resolve({ data: student });
      if (url === "/billing/subscriptions/" || url === "/students/12/personal-bookings/" || url === "/grades/students/12/progress/" || url === "/students/12/checkins/" || url === "/feedback/students/12/responses/") return Promise.resolve({ data: [] });
      if (url === "/billing/bank-payment-orders/") return Promise.resolve({ data: { items: [] } });
      if (url === "/students/12/commercial-context/") return Promise.reject({ response: { status: 403 } });
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderDetail();
    expect(await screen.findByText("Коммерческий контекст сейчас недоступен. Обновите карточку клиента.")).toBeInTheDocument();
  });

  it("opens an executable retry from a terminal SBP receipt for an active student", async () => {
    commercialAttempts = [retryableSbpReceipt()];

    renderDetail();

    fireEvent.click(await screen.findByRole("button", { name: "Повторить оплату СБП" }));
    expect(await screen.findByText(/Будет создана новая попытка СБП для этой же персоналки/)).toBeInTheDocument();
  });

  it.each(["failed", "cancelled"] as const)(
    "retries a terminal %s renewal from the receipt offer without loading subscriptions",
    async (status) => {
      commercialAttempts = [renewalReceipt(status)];
      post.mockResolvedValue({ data: { payment_id: 93 } });

      renderDetail();

      fireEvent.click(await screen.findByRole("button", { name: "Создать оплату снова" }));
      expect(await screen.findByText(/Base B.*6\s?500/)).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Зафиксировать оплату" }));

      await waitFor(() => {
        expect(post).toHaveBeenCalledWith(
          "/billing/payments/renewals/",
          expect.objectContaining({
            student_id: 12,
            renewed_from_subscription_id: 55,
            payment_method: "cash",
            expected_target_tariff_id: 9,
            expected_target_price: "6500.00",
          }),
        );
      });
      const payload = post.mock.calls[0]?.[1] as Record<string, unknown>;
      expect(payload).not.toHaveProperty("tariff_id");
      expect(payload).not.toHaveProperty("debt_ids");
      expect(payload).not.toHaveProperty("amount");
    },
  );

  it("refreshes a stale receipt offer before retrying with the new target", async () => {
    commercialAttempts = [renewalReceipt("failed")];
    post.mockImplementationOnce(() => {
      commercialAttempts = [
        renewalReceipt("failed", { id: 10, name: "Base C", price: "7000.00" }),
      ];
      return Promise.reject({
        response: { data: { code: "renewal_offer_stale", detail: "Offer changed" } },
      });
    });
    post.mockResolvedValueOnce({ data: { payment_id: 94 } });

    renderDetail();

    fireEvent.click(await screen.findByRole("button", { name: "Создать оплату снова" }));
    expect(await screen.findByText(/Base B.*6\s?500/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Зафиксировать оплату" }));

    await waitFor(() => {
      expect(get.mock.calls.filter(([url]) => url === "/students/12/commercial-context/")).toHaveLength(2);
    });
    fireEvent.click(screen.getByRole("button", { name: "Создать оплату снова" }));
    expect(await screen.findByText(/Base C.*7\s?000/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Зафиксировать оплату" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/billing/payments/renewals/",
        expect.objectContaining({
          student_id: 12,
          renewed_from_subscription_id: 55,
          expected_target_tariff_id: 10,
          expected_target_price: "7000.00",
        }),
      );
    });
  });

  it("does not use a cached subscription offer when the receipt marks renewal unavailable", async () => {
    commercialSubscriptions = [
      {
        id: 55,
        tariff_id: 4,
        tariff_name: "Base A",
        trainings_used: 1,
        trainings_total: 8,
        trainings_left: 7,
        expires_at: "2099-07-07T00:00:00Z",
        status: "active",
        renewal_target_tariff_id: 10,
        renewal_target_tariff_name: "Base C",
        renewal_target_price: "7000.00",
      },
    ];
    commercialAttempts = [renewalReceipt("cancelled", null)];

    renderDetail();

    fireEvent.click(await screen.findByRole("button", { name: "Создать оплату снова" }));
    const sheet = screen.getByRole("dialog");
    expect(
      within(sheet).getByText("Актуальная цена продления недоступна. Обновите карточку клиента."),
    ).toBeInTheDocument();
    expect(within(sheet).queryByText(/Base C/)).not.toBeInTheDocument();
    expect(within(sheet).getByRole("button", { name: "Зафиксировать оплату" })).toBeDisabled();
    expect(post).not.toHaveBeenCalled();
  });

  it("keeps an existing complete receipt readable after the availability flag is rolled back", async () => {
    personalAvailabilityEnabled = false;
    renderDetail();

    expect(await screen.findByText("Персональная запись и оплата")).toBeInTheDocument();
    expect(await screen.findByText("Оплата ожидает подтверждения владельцем")).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith("/students/12/commercial-context/");
  });

  it("consumes the one-time manual group-admission notice from the lead route", async () => {
    renderDetail({
      pathname: "/trainer/students/12",
      state: {
        groupSaleManualAdmission: {
          studentId: 12,
          paymentId: 91,
          financeState: "pending_manual",
        },
      },
    });

    expect(
      await screen.findByText("Mira Ivanova оформлен в группу. Оплата ожидает подтверждения."),
    ).toBeInTheDocument();
    expect(screen.getByTestId("student-detail-route-state")).toHaveTextContent("null");
  });
});
