import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router";
import {
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";
import AvailabilityCalendar from "./availability-calendar";
import { useAuthStore } from "@/features/auth/auth-store";
import type { TrainerPersonalAvailabilitySlot } from "../types";

const {
  apiGet,
  apiPost,
  listTrainerAvailability,
  generateTrainerAvailability,
  blockTrainerAvailabilitySlot,
  unblockTrainerAvailabilitySlot,
  cancelTrainerAvailabilitySlot,
} = vi.hoisted(() => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  listTrainerAvailability: vi.fn(),
  generateTrainerAvailability: vi.fn(),
  blockTrainerAvailabilitySlot: vi.fn(),
  unblockTrainerAvailabilitySlot: vi.fn(),
  cancelTrainerAvailabilitySlot: vi.fn(),
}));

vi.mock("@/api/custom-fetch", () => ({
  default: {
    get: apiGet,
    post: apiPost,
  },
}));

vi.mock("../lib/personal-availability", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/personal-availability")>()),
  listTrainerAvailability,
  generateTrainerAvailability,
  blockTrainerAvailabilitySlot,
  unblockTrainerAvailabilitySlot,
  cancelTrainerAvailabilitySlot,
}));

function toISODateLocal(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function getWeekStart(date: Date): Date {
  const start = new Date(date);
  const day = start.getDay();
  const diff = day === 0 ? -6 : 1 - day;
  start.setDate(start.getDate() + diff);
  start.setHours(0, 0, 0, 0);
  return start;
}

function addDays(date: Date, days: number): Date {
  const next = new Date(date);
  next.setDate(next.getDate() + days);
  return next;
}

function toBackendWeekday(date: Date): number {
  const day = date.getDay();
  return day === 0 ? 6 : day - 1;
}

function slot(
  overrides: Partial<TrainerPersonalAvailabilitySlot>,
): TrainerPersonalAvailabilitySlot {
  const today = toISODateLocal(new Date());
  return {
    id: 1,
    date: today,
    starts_at: `${today}T10:00:00`,
    ends_at: `${today}T11:00:00`,
    trainer_id: 9,
    trainer_name: "Артем",
    location_id: 2,
    location_name: "Основной зал",
    training_type_id: 7,
    training_type_name: "Персональная",
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

function LocationProbe() {
  return <output data-testid="route-path">{useLocation().pathname}</output>;
}

function renderPage(initialEntry = "/trainer/availability") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <AvailabilityCalendar />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("AvailabilityCalendar", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAuthStore.setState({ clubId: 1 });
    const today = toISODateLocal(new Date());
    apiGet.mockImplementation((url: string) => {
      if (url === "/clubs/locations/") {
        return Promise.resolve({ data: [{ id: 2, name: "Основной зал" }] });
      }
      if (url === "/billing/training-types/") {
        return Promise.resolve({
          data: [
            { id: 7, name: "Персональная", kind: "personal", is_active: true },
            { id: 8, name: "Группа", kind: "group", is_active: true },
          ],
        });
      }
      if (url === "/billing/tariffs/") {
        return Promise.resolve({
          data: {
            items: [
              {
                id: 33,
                name: "Разовая персоналка",
                price: 2000,
                training_type: {
                  id: 7,
                  name: "Персональная",
                  slug: "personal",
                  kind: "personal",
                  is_active: true,
                },
                trainings_limit: 1,
                duration_days: 1,
                is_active: true,
              },
            ],
          },
        });
      }
      if (url === "/billing/subscriptions/") {
        return Promise.resolve({ data: [] });
      }
      if (url === "/personal-availability/capability/") {
        return Promise.resolve({ data: { enabled: false } });
      }
      if (url === "/students/") {
        return Promise.resolve({
          data: [
            {
              id: 44,
              first_name: "Иван",
              last_name: "Клиент",
              phone: "+79990000000",
              email: "",
              status: "lead",
              is_child: false,
              date_of_birth: null,
              source: "website",
            },
          ],
        });
      }
      if (url === "/students/44/") {
        return Promise.resolve({
          data: {
            id: 44,
            first_name: "Иван",
            last_name: "Клиент",
            status: "lead",
          },
        });
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });
    listTrainerAvailability.mockResolvedValue([
      slot({ id: 1, status: "published" }),
      slot({
        id: 2,
        starts_at: `${today}T11:00:00`,
        ends_at: `${today}T12:00:00`,
        status: "held",
        can_block: false,
        can_cancel: false,
      }),
      slot({
        id: 3,
        starts_at: `${today}T12:00:00`,
        ends_at: `${today}T13:00:00`,
        status: "booked",
        booked_enrollment_id: 77,
        can_block: false,
        can_cancel: false,
      }),
      slot({
        id: 4,
        starts_at: `${today}T13:00:00`,
        ends_at: `${today}T14:00:00`,
        status: "blocked",
        block_reason: "занят",
        can_block: false,
        can_unblock: true,
        can_cancel: true,
      }),
    ]);
    generateTrainerAvailability.mockResolvedValue({ created: [], skipped: [] });
    blockTrainerAvailabilitySlot.mockResolvedValue(slot({ id: 1, status: "blocked" }));
    unblockTrainerAvailabilitySlot.mockResolvedValue(slot({ id: 4, status: "published" }));
    cancelTrainerAvailabilitySlot.mockResolvedValue(slot({ id: 1, status: "cancelled" }));
    apiPost.mockResolvedValue({ data: { id: 91, price_snapshot: "2000.00" } });
  });

  it("loads the selected week and renders slot statuses", async () => {
    const weekStart = getWeekStart(new Date());
    const weekEnd = addDays(weekStart, 6);
    renderPage();

    await screen.findAllByText("Персональная");

    expect(listTrainerAvailability).toHaveBeenCalledWith({
      dateFrom: toISODateLocal(weekStart),
      dateTo: toISODateLocal(weekEnd),
    });
    expect(screen.getAllByText("Свободно").length).toBeGreaterThan(0);
    expect(screen.getByText("Ожидает оплаты")).toBeInTheDocument();
    expect(screen.getByText("Записан ученик")).toBeInTheDocument();
    expect(screen.getByText("Заблокировано")).toBeInTheDocument();
  });

  it("generates one-day slots without sending a trainer id", async () => {
    const selectedDay = toISODateLocal(new Date());
    renderPage();

    fireEvent.click(screen.getByRole("button", { name: "Опубликовать слоты" }));

    await screen.findByLabelText("Тип тренировки");
    await screen.findByRole("option", { name: "Персональная" });
    await screen.findByRole("option", { name: "Основной зал" });
    fireEvent.change(screen.getByLabelText("Тип тренировки"), {
      target: { value: "7" },
    });
    fireEvent.change(screen.getByLabelText("Зал"), {
      target: { value: "2" },
    });
    const submitButton = screen.getByRole("button", { name: "Опубликовать" });
    await waitFor(() => {
      expect(submitButton).toBeEnabled();
    });
    fireEvent.click(submitButton);

    await waitFor(() => {
      expect(generateTrainerAvailability).toHaveBeenCalledWith(
        expect.objectContaining({
          date_from: selectedDay,
          date_to: selectedDay,
          weekdays: [toBackendWeekday(new Date())],
          start_time: "10:00",
          end_time: "14:00",
          slot_duration_minutes: 60,
          buffer_minutes: 0,
          location_id: 2,
          training_type_id: 7,
        }),
      );
    });
    expect(generateTrainerAvailability.mock.calls[0][0]).not.toHaveProperty(
      "trainer_id",
    );
  });

  it("shows a fixed server price but never renders an empty legacy offer as zero", async () => {
    listTrainerAvailability.mockResolvedValueOnce([
      slot({ id: 1, offer_price: "2500.00", offer_tariff_name: "Персоналка" }),
      slot({ id: 2, offer_price: "" }),
    ]);
    renderPage();

    expect(await screen.findByText(/2\s*500\s*₽/)).toBeInTheDocument();
    expect(screen.queryByText("0 ₽")).not.toBeInTheDocument();
  });

  it("explains that an owner must configure pricing when publication fails closed", async () => {
    generateTrainerAvailability.mockRejectedValueOnce({
      response: { data: { code: "personal_booking_tariff_not_configured" } },
    });
    renderPage();

    fireEvent.click(screen.getByRole("button", { name: "Опубликовать слоты" }));
    await screen.findByRole("option", { name: "Персональная" });
    await screen.findByRole("option", { name: "Основной зал" });
    fireEvent.change(screen.getByLabelText("Тип тренировки"), {
      target: { value: "7" },
    });
    fireEvent.change(screen.getByLabelText("Зал"), { target: { value: "2" } });
    const submitButton = screen.getByRole("button", { name: "Опубликовать" });
    await waitFor(() => expect(submitButton).toBeEnabled());
    fireEvent.click(submitButton);

    await waitFor(() => expect(generateTrainerAvailability).toHaveBeenCalledOnce());
    expect(await screen.findByText(/Нельзя опубликовать персональные слоты/)).toBeInTheDocument();
  });

  it("generates repeated weekly slots when repeat mode is selected", async () => {
    const weekStart = getWeekStart(new Date());
    const weekEnd = addDays(weekStart, 6);
    renderPage();

    fireEvent.click(screen.getByRole("button", { name: "Опубликовать слоты" }));
    fireEvent.click(await screen.findByRole("button", { name: "Повторять" }));

    await screen.findByLabelText("Тип тренировки");
    await screen.findByRole("option", { name: "Персональная" });
    await screen.findByRole("option", { name: "Основной зал" });
    fireEvent.change(screen.getByLabelText("Тип тренировки"), {
      target: { value: "7" },
    });
    fireEvent.change(screen.getByLabelText("Зал"), {
      target: { value: "2" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Опубликовать" }));

    await waitFor(() => {
      expect(generateTrainerAvailability).toHaveBeenCalledWith(
        expect.objectContaining({
          date_from: toISODateLocal(weekStart),
          date_to: toISODateLocal(weekEnd),
          weekdays: [toBackendWeekday(new Date())],
        }),
      );
    });
  });

  it("navigates between weeks and reloads the calendar window", async () => {
    const weekStart = getWeekStart(new Date());
    const nextWeekStart = addDays(weekStart, 7);
    const nextWeekEnd = addDays(nextWeekStart, 6);
    renderPage();

    await screen.findAllByText("Персональная");
    fireEvent.click(screen.getByRole("button", { name: "Следующая неделя" }));

    await waitFor(() => {
      expect(listTrainerAvailability).toHaveBeenLastCalledWith({
        dateFrom: toISODateLocal(nextWeekStart),
        dateTo: toISODateLocal(nextWeekEnd),
      });
    });
  });

  it("blocks a published slot from the action sheet", async () => {
    renderPage();

    const slotTitles = await screen.findAllByText("Персональная");
    fireEvent.click(slotTitles[0]);
    fireEvent.change(await screen.findByLabelText("Причина"), {
      target: { value: "занят" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Закрыть слот" }));

    await waitFor(() => {
      expect(blockTrainerAvailabilitySlot).toHaveBeenCalledWith({
        slotId: 1,
        reason: "занят",
      });
    });
  });

  it("cancels a blocked slot from the action sheet", async () => {
    renderPage();

    fireEvent.click(await screen.findByText("занят"));
    fireEvent.click(screen.getByRole("button", { name: "Отменить публикацию" }));

    await waitFor(() => {
      expect(cancelTrainerAvailabilitySlot).toHaveBeenCalledWith(4);
    });
  });

  it("keeps one sheet while a published slot moves through client search and a fixed booking form", async () => {
    renderPage();

    fireEvent.click((await screen.findAllByText("Персональная"))[0]);
    fireEvent.click(screen.getByRole("button", { name: "Записать клиента" }));
    expect(screen.getAllByRole("dialog")).toHaveLength(1);

    fireEvent.change(screen.getByLabelText("Поиск клиента"), {
      target: { value: "999 000" },
    });
    await waitFor(() => {
      expect(apiGet).toHaveBeenCalledWith("/students/", { params: { q: "999 000" } });
    });
    const clientButton = await screen.findByRole("button", { name: /Иван Клиент/ });
    fireEvent.click(clientButton);

    await screen.findByText("Слот зафиксирован");
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    expect(screen.queryByLabelText("Дата *")).not.toBeInTheDocument();
    expect(screen.getAllByText(/2099|20\d\d/).length).toBeGreaterThan(0);
    await screen.findByRole("option", { name: /Разовая персоналка/ });
    fireEvent.change(screen.getByLabelText("Разовая персоналка *"), {
      target: { value: "33" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Записать с оплатой в клубе" }));

    await waitFor(() => {
      expect(apiPost).toHaveBeenCalledWith(
        "/personal-availability/slots/1/drop-in-bookings/",
        expect.objectContaining({ student_id: 44, tariff_id: 33 }),
      );
    });
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    expect(await screen.findByText(/Клиент записан/)).toBeInTheDocument();
  });

  it("opens the same fixed-slot sheet for a safely preselected lead from the route", async () => {
    renderPage("/trainer/availability?student_id=44");

    expect(await screen.findByText(/Выбран клиент: Иван Клиент/)).toBeInTheDocument();
    fireEvent.click((await screen.findAllByText("Персональная"))[0]);

    expect(await screen.findByText("Слот зафиксирован")).toBeInTheDocument();
    expect(screen.queryByLabelText("Поиск клиента")).not.toBeInTheDocument();
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
  });

  it("routes a v2 manual admission from the lead flow to the student detail", async () => {
    const defaultGet = apiGet.getMockImplementation();
    apiGet.mockImplementation((url: string, config?: unknown) => {
      if (url === "/personal-availability/capability/") {
        return Promise.resolve({
          data: { enabled: true, staff_command_protocol_version: "v2" },
        });
      }
      if (url === "/personal-availability/offers/") {
        return Promise.resolve({
          data: {
            offer_tariff_id: 33,
            offer_tariff_name: "Разовая персоналка",
            offer_price: "2000.00",
            offer_digest: "calendar-v2-offer",
          },
        });
      }
      if (url === "/billing/discounts/") return Promise.resolve({ data: [] });
      if (url === "/students/44/personal-booking-payment-reservations/") {
        return Promise.resolve({ data: [] });
      }
      return defaultGet?.(url, config);
    });
    apiPost.mockResolvedValueOnce({
      data: {
        kind: "personal_staff_intent",
        booking_id: 91,
        training_type_id: 7,
        training_type_name: "Персональная",
        trainer_id: 9,
        trainer_name: "Артем",
        location_id: 2,
        location_name: "Основной зал",
        amount: "2000.00",
        payment_method: "cash",
        status: "pending",
        allowed_actions: [],
        resource_route: "/api/personal-drop-in-bookings/91/",
        workspace_state: "student",
        finance_state: "pending_manual",
        command_replayed: false,
      },
    });

    renderPage("/trainer/availability?student_id=44");
    expect(await screen.findByText(/Выбран клиент: Иван Клиент/)).toBeInTheDocument();
    fireEvent.click((await screen.findAllByText("Персональная"))[0]);
    const submit = await screen.findByRole("button", {
      name: /Записать Иван Клиент, наличные,/,
    });
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);

    await waitFor(() => {
      expect(apiPost).toHaveBeenCalledWith(
        "/personal-availability/v2/slots/1/staff-intents/",
        expect.objectContaining({ protocol_version: "v2", payment_method: "cash" }),
      );
    });
    await waitFor(() => {
      expect(screen.getByTestId("route-path")).toHaveTextContent("/trainer/students/44");
    });
  });

  it("keeps direct-time personal booking reachable for the safely preselected client", async () => {
    renderPage("/trainer/availability?student_id=44");

    expect(await screen.findByText(/Выбран клиент: Иван Клиент/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Указать время" }));

    expect(await screen.findByText("Записать персоналку")).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toHaveTextContent("Иван Клиент");
  });

  it("keeps an unavailable preselected person out of the fixed-slot form", async () => {
    const defaultGet = apiGet.getMockImplementation();
    apiGet.mockImplementation((url: string, config?: unknown) => {
      if (url === "/students/44/") {
        return Promise.reject({ response: { status: 403 } });
      }
      return defaultGet?.(url, config);
    });

    renderPage("/trainer/availability?student_id=44");

    expect(
      await screen.findByText(
        "Выбранный клиент сейчас недоступен. Найдите клиента в карточке опубликованного слота.",
      ),
    ).toBeInTheDocument();
    fireEvent.click((await screen.findAllByText("Персональная"))[0]);
    expect(screen.queryByText("Слот зафиксирован")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Записать клиента" })).toBeInTheDocument();
  });
});
