import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ScheduleFormSheet } from "./schedule-form-sheet";
import type { ScheduleOut } from "../types";

const { get, post, put } = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
}));

vi.mock("@/api/custom-fetch", () => ({
  default: {
    get,
    post,
    put,
  },
}));

function renderSheet({
  editSchedule = null,
  onOpenChange = vi.fn(),
}: {
  editSchedule?: ScheduleOut | null;
  onOpenChange?: (open: boolean) => void;
} = {}) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <ScheduleFormSheet
        open
        onOpenChange={onOpenChange}
        trainerId={9}
        editSchedule={editSchedule}
        defaultDate="2026-06-04"
      />
    </QueryClientProvider>,
  );
}

function fillCreateForm() {
  fireEvent.change(screen.getByLabelText("Дата *"), {
    target: { value: "2026-06-04" },
  });
  fireEvent.change(screen.getByLabelText("Начало *"), {
    target: { value: "10:00" },
  });
  fireEvent.change(screen.getByLabelText("Конец *"), {
    target: { value: "11:00" },
  });
  fireEvent.change(screen.getByLabelText("Группа *"), {
    target: { value: "Kids" },
  });
  fireEvent.change(screen.getByLabelText("Локация *"), {
    target: { value: "2" },
  });
}

describe("ScheduleFormSheet", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    post.mockResolvedValue({ data: {} });
    put.mockResolvedValue({ data: {} });
    get.mockImplementation((url: string) => {
      if (url === "/clubs/locations/") {
        return Promise.resolve({
          data: [{ id: 2, name: "Main Hall" }],
        });
      }
      if (url === "/billing/training-types/") {
        return Promise.resolve({
          data: [
            {
              id: 7,
              name: "Muay Thai",
              slug: "muay-thai",
              is_active: true,
              grade_system_id: null,
            },
            {
              id: 8,
              name: "Boxing",
              slug: "boxing",
              is_active: true,
              grade_system_id: null,
            },
          ],
        });
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });
  });

  it("requires a training type and sends training_type_id when creating a session", async () => {
    renderSheet();

    await waitFor(() => {
      expect(get).toHaveBeenCalledWith("/billing/training-types/");
    });

    fillCreateForm();

    expect(screen.getByRole("button", { name: "Создать тренировку" })).toBeDisabled();

    fireEvent.change(screen.getByLabelText("Тип тренировки *"), {
      target: { value: "7" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Создать тренировку" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        "/schedules/",
        expect.objectContaining({
          day_of_week: 0,
          one_time_date: "2026-06-04",
          start_time: "10:00",
          end_time: "11:00",
          group_name: "Kids",
          trainer_id: 9,
          location_id: 2,
          training_type_id: 7,
        }),
      );
    });
  });

  it("uses the edited schedule training type as the initial update value", async () => {
    const editSchedule: ScheduleOut = {
      id: 33,
      day_of_week: 3,
      start_time: "12:00:00",
      end_time: "13:00:00",
      group_name: "Boxing Pro",
      trainer_id: 9,
      trainer_name: "Coach",
      location_id: 2,
      location_name: "Main Hall",
      training_type_id: 8,
      is_active: true,
      one_time_date: "2026-06-05",
    };

    renderSheet({ editSchedule });

    const trainingTypeSelect = await screen.findByLabelText("Тип тренировки *");
    await waitFor(() => {
      expect(trainingTypeSelect).toHaveValue("8");
    });

    fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));

    await waitFor(() => {
      expect(put).toHaveBeenCalledWith(
        "/schedules/33/",
        expect.objectContaining({
          one_time_date: "2026-06-05",
          start_time: "12:00",
          end_time: "13:00",
          group_name: "Boxing Pro",
          location_id: 2,
          training_type_id: 8,
        }),
      );
    });
  });
});
