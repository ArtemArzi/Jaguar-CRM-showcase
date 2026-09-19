import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
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

function personalBooking(overrides: Record<string, unknown>) {
  return {
    schedule_id: 101,
    enrollment_id: 102,
    student_id: 12,
    trainer_id: 9,
    trainer_name: "Илья Тренер",
    location_id: 11,
    location_name: "Основной зал",
    training_type_id: 22,
    training_type_name: "Персональная тренировка",
    starts_at: "2099-07-07T10:00:00+05:00",
    ends_at: "2099-07-07T11:00:00+05:00",
    created_from: "personal_booking",
    status: "active",
    booking_kind: "entitlement",
    can_manage: true,
    can_reschedule: false,
    ...overrides,
  };
}

function renderStudentDetail() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={["/trainer/students/12"]}>
      <QueryClientProvider client={queryClient}>
        <Routes>
          <Route path="/trainer/students/:studentId" element={<StudentDetail />} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

describe("StudentDetail personal booking reschedule action", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAuthStore.setState({
      accessToken: null,
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
    get.mockImplementation((url: string) => {
      if (url === "/personal-availability/capability/") return Promise.resolve({ data: { enabled: false } });
      if (url === "/students/intakes/capability") return Promise.resolve({ data: { enabled: false } });
      if (url === "/students/12/") {
        return Promise.resolve({
          data: {
            id: 12,
            first_name: "Маша",
            last_name: "Иванова",
            phone: "",
            status: "active",
            contraindications: "",
            notes: [],
            account_access: null,
            can_manage_feedback: false,
          },
        });
      }
      if (url === "/billing/bank-payment-orders/") return Promise.resolve({ data: { items: [] } });
      if (url === "/billing/subscriptions/" || url === "/grades/students/12/progress/" || url === "/students/12/checkins/" || url === "/feedback/students/12/responses/") return Promise.resolve({ data: [] });
      if (url === "/students/12/personal-bookings/") {
        return Promise.resolve({
          data: [
            personalBooking({ enrollment_id: 102, can_reschedule: true }),
            personalBooking({ enrollment_id: 103, can_reschedule: false }),
          ],
        });
      }
      if (url === "/students/12/commercial-context/") return Promise.resolve({ data: { student_id: 12, attempts: [] } });
      return Promise.resolve({ data: [] });
    });
  });

  it("shows transfer only for a booking with the server-projected capability", async () => {
    renderStudentDetail();

    expect(await screen.findByText("Маша Иванова")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Перенести" })).toHaveLength(1);
  });
});
