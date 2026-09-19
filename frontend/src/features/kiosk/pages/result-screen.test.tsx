import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ResultScreen from "./result-screen";

const submitKioskCheckinWithOfflineQueue = vi.hoisted(() => vi.fn());

vi.mock("@/features/kiosk/lib/checkin-submit", () => ({
  submitKioskCheckinWithOfflineQueue,
}));

const baseSchedule = {
  schedule_id: 31,
  effective_date: "2026-06-04",
  start_time: "17:00:00",
  end_time: "18:00:00",
  group_name: "Kids Boxing",
  trainer_name: "Coach One",
  location_name: "Main Hall",
  training_type_id: 12,
  training_type_name: "Group",
};

const waitingKioskOption = {
  ...baseSchedule,
  group_name: "Персональная тренировка",
  training_type_name: "Персональная",
  self_checkin_status: "can_checkin" as const,
  reason_code: "",
  financial_status: "drop_in_debt" as const,
  subscription_id: null,
  drop_in_price: "2000.00",
  existing_checkin_id: null,
  checkin_window_status: "too_early" as const,
  checkin_opens_at: "2026-06-04T16:30:00+05:00",
  checkin_closes_at: "2026-06-04T18:00:00+05:00",
};

describe("ResultScreen", () => {
  beforeEach(() => {
    submitKioskCheckinWithOfflineQueue.mockReset();
  });

  it("shows kiosk-safe grade and subscription summary from the matched student", () => {
    render(
      <ResultScreen
        student={{
          id: 101,
          first_name: "Ivan",
          last_name: "Petrov",
          lookup_suffix: "4567",
          masked_phone: "+***4567",
          group_name: "Kids Boxing",
          grade_name: "Yellow belt",
          subscription_name: "Monthly",
          subscription_status: "active",
          trainings_left: 7,
        }}
        schedules={[baseSchedule]}
        goToFeedback={vi.fn()}
        goToNumpad={vi.fn()}
      />,
    );

    expect(screen.getByText("Yellow belt")).toBeInTheDocument();
    expect(screen.getByText("Monthly · 7 тр.")).toBeInTheDocument();
    expect(screen.queryByText("---")).not.toBeInTheDocument();
  });

  it("shows an unlimited subscription without remaining trainings", () => {
    render(
      <ResultScreen
        student={{
          id: 101,
          first_name: "Ivan",
          last_name: "Petrov",
          lookup_suffix: "4567",
          masked_phone: "+***4567",
          group_name: "",
          subscription_name: "Unlimited",
          subscription_status: "active",
          trainings_left: null,
        }}
        schedules={[baseSchedule]}
        goToFeedback={vi.fn()}
        goToNumpad={vi.fn()}
      />,
    );

    expect(screen.getByText("Без грейда")).toBeInTheDocument();
    expect(screen.getByText("Unlimited · Безлимит")).toBeInTheDocument();
  });

  it("shows frozen check-in rejection as a business error on the kiosk screen", async () => {
    const error = new Error("Request failed with status code 409") as Error & {
      response: {
        status: number;
        data: {
          checkin_blocked_reason: string;
          detail: string;
        };
      };
    };
    error.response = {
      status: 409,
      data: {
        checkin_blocked_reason: "enrollment_frozen",
        detail: "Абонемент заморожен",
      },
    };
    submitKioskCheckinWithOfflineQueue.mockRejectedValue(error);

    render(
      <ResultScreen
        student={{
          id: 101,
          first_name: "Ivan",
          last_name: "Petrov",
          lookup_suffix: "4567",
          masked_phone: "+***4567",
          group_name: "Kids Boxing",
          subscription_name: "Monthly",
          subscription_status: "frozen",
          trainings_left: 7,
        }}
        schedules={[baseSchedule]}
        goToFeedback={vi.fn()}
        goToNumpad={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /Kids Boxing/i }));

    expect(await screen.findByText("Абонемент заморожен")).toBeInTheDocument();
    expect(screen.queryByText(/сохран/i)).not.toBeInTheDocument();
  });

  it("shows when the personalized check-in opens without exposing payment state", () => {
    const goToNumpad = vi.fn();
    vi.useFakeTimers();
    render(
      <ResultScreen
        student={{
          id: 101,
          first_name: "Руслан",
          last_name: "Иванов",
          lookup_suffix: "4567",
          masked_phone: "+***4567",
          group_name: "",
          subscription_name: "",
          subscription_status: "",
          trainings_left: null,
        }}
        schedules={[baseSchedule]}
        kioskOptions={[waitingKioskOption]}
        goToFeedback={vi.fn()}
        goToNumpad={goToNumpad}
      />,
    );

    expect(screen.getByText("Персональная тренировка")).toBeInTheDocument();
    expect(screen.getByText("Чек-ин откроется в 16:30")).toBeInTheDocument();
    expect(
      screen.getByText("Введите номер снова после этого времени"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Персональная/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/оплат|подтвержден|долг/i)).not.toBeInTheDocument();

    act(() => vi.advanceTimersByTime(8_000));
    expect(goToNumpad).toHaveBeenCalledOnce();
    vi.useRealTimers();
  });
});
