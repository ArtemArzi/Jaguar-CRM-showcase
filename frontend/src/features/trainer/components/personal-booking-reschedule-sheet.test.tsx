import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "@/features/auth/auth-store";
import { useBrandingStore } from "@/features/branding/use-branding";
import type { PersonalBooking, TrainerPersonalAvailabilitySlot } from "../types";
import { personalRescheduleCandidates } from "../lib/personal-booking-reschedule";
import { PersonalBookingRescheduleSheet } from "./personal-booking-reschedule-sheet";

const { get, post, listTrainerAvailability } = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  listTrainerAvailability: vi.fn(),
}));

vi.mock("@/api/custom-fetch", () => ({ default: { get, post } }));
vi.mock("../lib/personal-availability", () => ({ listTrainerAvailability }));

const booking: PersonalBooking = {
  schedule_id: 101,
  enrollment_id: 102,
  student_id: 7,
  trainer_id: 9,
  trainer_name: "Илья Тренер",
  location_id: 11,
  location_name: "Основной зал",
  training_type_id: 22,
  training_type_name: "Персональная тренировка",
  starts_at: "2099-07-07T10:00:00",
  ends_at: "2099-07-07T11:00:00",
  created_from: "personal_booking",
  status: "active",
  booking_kind: "entitlement",
  can_manage: true,
  can_reschedule: true,
};

function slot(
  overrides: Partial<TrainerPersonalAvailabilitySlot> = {},
): TrainerPersonalAvailabilitySlot {
  return {
    id: 81,
    date: "2099-07-08",
    starts_at: "2099-07-08T12:00:00+05:00",
    ends_at: "2099-07-08T13:00:00+05:00",
    trainer_id: 9,
    trainer_name: "Илья Тренер",
    location_id: 11,
    location_name: "Основной зал",
    training_type_id: 22,
    training_type_name: "Персональная тренировка",
    training_type_kind: "personal",
    status: "published",
    block_reason: "",
    booked_enrollment_id: null,
    can_block: true,
    can_unblock: false,
    can_cancel: true,
    ...overrides,
  };
}

function renderSheet(overrides: Partial<PersonalBooking> = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const onOpenChange = vi.fn();
  const view = render(
    <QueryClientProvider client={queryClient}>
      <PersonalBookingRescheduleSheet
        open
        onOpenChange={onOpenChange}
        booking={{ ...booking, ...overrides }}
        studentId={7}
        studentName="Маша Иванова"
      />
    </QueryClientProvider>,
  );
  return { queryClient, onOpenChange, ...view };
}

describe("PersonalBookingRescheduleSheet", () => {
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
    listTrainerAvailability.mockResolvedValue([slot()]);
    post.mockResolvedValue({ data: {} });
  });

  it("only presents future compatible published slots and excludes the current exact time", () => {
    const candidates = personalRescheduleCandidates({
      booking,
      timeZone: "Asia/Yekaterinburg",
      now: new Date("2099-07-01T00:00:00+05:00"),
      slots: [
        slot(),
        slot({ id: 82, date: "2099-07-07", starts_at: booking.starts_at, ends_at: booking.ends_at }),
        slot({
          id: 88,
          date: "2099-07-07",
          starts_at: "2099-07-07T05:00:00Z",
          ends_at: "2099-07-07T06:00:00Z",
        }),
        slot({ id: 83, trainer_id: 10 }),
        slot({ id: 84, location_id: 12 }),
        slot({ id: 85, training_type_id: 23 }),
        slot({ id: 86, status: "booked" }),
        slot({ id: 87, starts_at: "2099-06-30T12:00:00+05:00" }),
      ],
    });

    expect(candidates.map((candidate) => candidate.id)).toEqual([81]);
  });

  it("renders UTC availability timestamps in the authoritative club timezone", async () => {
    listTrainerAvailability.mockResolvedValue([
      slot({
        starts_at: "2099-07-08T07:00:00Z",
        ends_at: "2099-07-08T08:00:00Z",
      }),
    ]);
    renderSheet();

    expect(await screen.findByRole("button", { name: /08\.07\.2099, 12:00–13:00/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /07:00–08:00/ })).not.toBeInTheDocument();
    expect(screen.getByText("07.07.2099, 10:00–11:00")).toBeInTheDocument();
    expect(screen.queryByText(/15:00–16:00/)).not.toBeInTheDocument();
  });

  it("waits for an authoritative club timezone before loading or allowing reschedule", async () => {
    useBrandingStore.setState({
      timeZoneStatus: "pending",
      timeZoneClubId: 1,
      isTimeZoneAuthoritative: false,
    });
    renderSheet();

    expect(await screen.findByRole("status")).toHaveTextContent(
      "Проверяем часовой пояс клуба перед переносом...",
    );
    expect(listTrainerAvailability).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Подтвердить перенос" })).not.toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });

  it("fails closed when the timezone authority belongs to another club", async () => {
    useBrandingStore.setState({
      timeZoneStatus: "ready",
      timeZoneClubId: 2,
      isTimeZoneAuthoritative: true,
    });
    renderSheet();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Не удалось подтвердить часовой пояс клуба. Обновите страницу перед переносом.",
    );
    expect(listTrainerAvailability).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Подтвердить перенос" })).not.toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });

  it("sends only the exact endpoint and payload, then invalidates scoped queries", async () => {
    const { queryClient, onOpenChange } = renderSheet();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    expect(await screen.findByText("Маша Иванова")).toBeInTheDocument();
    expect(screen.getByText("Илья Тренер · Основной зал")).toBeInTheDocument();
    expect(screen.getByText("Персональная тренировка")).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: /12:00/ }));
    fireEvent.click(screen.getByRole("button", { name: "Подтвердить перенос" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/personal-bookings/102/reschedule/",
        expect.objectContaining({
          destination_slot_id: 81,
          reason: "Перенос по согласованию с клиентом",
          idempotency_key: expect.any(String),
        }),
      );
    });
    const payload = post.mock.calls[0]?.[1] as Record<string, unknown>;
    expect(Object.keys(payload).sort()).toEqual([
      "destination_slot_id",
      "idempotency_key",
      "reason",
    ]);
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: ["student", "7", "personal-bookings"],
    });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["trainer", "availability"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["schedules"] });
  });

  it("uses the drop-in endpoint when the immutable booking authority is a drop-in", async () => {
    const { onOpenChange } = renderSheet({ booking_kind: "drop_in", booking_id: 71 });

    fireEvent.click(await screen.findByRole("button", { name: /12:00/ }));
    fireEvent.click(screen.getByRole("button", { name: "Подтвердить перенос" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/personal-drop-in-bookings/71/reschedule/",
        expect.objectContaining({ destination_slot_id: 81 }),
      );
    });
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("keeps an idempotency key on retry and gives a stale-slot refresh path", async () => {
    post.mockRejectedValueOnce({ response: { data: { detail: "Слот уже занят" } } });
    const { onOpenChange } = renderSheet();

    fireEvent.click(await screen.findByRole("button", { name: /12:00/ }));
    fireEvent.click(screen.getByRole("button", { name: "Подтвердить перенос" }));

    expect(await screen.findByText(/Время могло уже стать недоступно/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Обновить варианты" }));
    await waitFor(() => expect(listTrainerAvailability).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole("button", { name: "Повторить перенос" }));

    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(post.mock.calls[1]?.[1]).toEqual(post.mock.calls[0]?.[1]);
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
  });

  it("rotates the key only when the trainer changes the exact command", async () => {
    listTrainerAvailability.mockResolvedValue([slot(), slot({ id: 82, starts_at: "2099-07-09T12:00:00+05:00", ends_at: "2099-07-09T13:00:00+05:00", date: "2099-07-09" })]);
    post.mockRejectedValueOnce(new Error("network unavailable"));
    renderSheet();

    fireEvent.click(await screen.findByRole("button", { name: /08.07.2099, 12:00/ }));
    fireEvent.click(screen.getByRole("button", { name: "Подтвердить перенос" }));
    expect(await screen.findByText(/Время могло уже стать недоступно/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /09.07.2099, 12:00/ }));
    fireEvent.click(screen.getByRole("button", { name: "Подтвердить перенос" }));

    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    const firstPayload = post.mock.calls[0]?.[1] as Record<string, unknown>;
    const secondPayload = post.mock.calls[1]?.[1] as Record<string, unknown>;
    expect(secondPayload.destination_slot_id).toBe(82);
    expect(secondPayload.idempotency_key).not.toBe(firstPayload.idempotency_key);
  });
});
