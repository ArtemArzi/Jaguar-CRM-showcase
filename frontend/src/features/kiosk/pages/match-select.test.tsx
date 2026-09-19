import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import MatchSelect from "./match-select";

const autoDetectTraining = vi.hoisted(() => vi.fn());
const fetchKioskOptions = vi.hoisted(() => vi.fn());
const submitKioskCheckinWithOfflineQueue = vi.hoisted(() => vi.fn());

vi.mock("@/features/kiosk/lib/kiosk-api", () => ({
  autoDetectTraining,
  fetchKioskOptions,
}));

vi.mock("@/features/kiosk/lib/checkin-submit", () => ({
  isNetworkCheckinFailure: () => false,
  submitKioskCheckinWithOfflineQueue,
}));

const student = {
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
};

const schedule = {
  schedule_id: 31,
  effective_date: "2026-06-08",
  start_time: "17:00:00",
  end_time: "18:00:00",
  group_name: "Kids Boxing",
  trainer_name: "Coach One",
  location_name: "Main Hall",
  training_type_id: 12,
  training_type_name: "Group",
};

const kioskOption = {
  ...schedule,
  self_checkin_status: "can_checkin",
  reason_code: "",
  financial_status: "subscription",
  subscription_id: 21,
  drop_in_price: null,
  existing_checkin_id: null,
  checkin_window_status: "open",
  checkin_opens_at: "2026-06-08T16:30:00+05:00",
  checkin_closes_at: "2026-06-08T18:00:00+05:00",
};

function renderMatchSelect() {
  const props = {
    matches: [student],
    phoneSuffix: "4567",
    schedules: [schedule],
    goToNumpad: vi.fn(),
    goToResult: vi.fn(),
    goToFeedback: vi.fn(),
    goToErrorFeedback: vi.fn(),
  };

  render(<MatchSelect {...props} />);
  return props;
}

describe("MatchSelect fast check-in errors", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    Object.defineProperty(window.navigator, "onLine", {
      configurable: true,
      value: true,
    });
    autoDetectTraining.mockReturnValue(schedule);
    fetchKioskOptions.mockResolvedValue({
      student_id: student.id,
      date: schedule.effective_date,
      options: [kioskOption],
    });
  });

  it("routes auto-checkin business failures to error feedback instead of manual result", async () => {
    const props = renderMatchSelect();
    const error = {
      response: {
        status: 409,
        data: {
          code: "training_type_mismatch",
          detail: "Private trainer note: wrong training type",
        },
      },
    };
    submitKioskCheckinWithOfflineQueue.mockRejectedValue(error);

    fireEvent.click(screen.getByRole("button", { name: /Ivan Petrov/i }));

    await waitFor(() => {
      expect(props.goToErrorFeedback).toHaveBeenCalledWith(error, student);
    });
    expect(props.goToResult).not.toHaveBeenCalled();
    expect(props.goToFeedback).not.toHaveBeenCalled();
  });

  it("routes a current duplicate returned by personalized options to feedback", async () => {
    const props = renderMatchSelect();
    fetchKioskOptions.mockResolvedValue({
      student_id: student.id,
      date: schedule.effective_date,
      options: [
        {
          ...kioskOption,
          self_checkin_status: "blocked",
          reason_code: "already_checked_in",
          existing_checkin_id: 99,
        },
      ],
    });

    fireEvent.click(screen.getByRole("button", { name: /Ivan Petrov/i }));

    await waitFor(() => {
      expect(props.goToErrorFeedback).toHaveBeenCalledWith(
        {
          data: {
            code: "already_checked_in",
            detail: "Подойдите к тренеру",
          },
        },
        student,
      );
    });
    expect(submitKioskCheckinWithOfflineQueue).not.toHaveBeenCalled();
    expect(props.goToResult).not.toHaveBeenCalled();
  });

  it("shows compact kiosk-safe context for offline matches", () => {
    autoDetectTraining.mockReturnValue(null);

    renderMatchSelect();

    expect(
      screen.getByText("Kids Boxing · Yellow belt · Monthly · 7 тр."),
    ).toBeInTheDocument();
    expect(screen.queryByText(student.masked_phone)).not.toBeInTheDocument();
  });
});
