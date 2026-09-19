import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import NumpadHome from "./numpad-home";

const lookupStudents = vi.hoisted(() => vi.fn());
const fetchTodaySchedules = vi.hoisted(() => vi.fn());
const fetchKioskOptions = vi.hoisted(() => vi.fn());
const autoDetectTraining = vi.hoisted(() => vi.fn());
const lookupStudentsOffline = vi.hoisted(() => vi.fn());
const getTodaySchedulesOffline = vi.hoisted(() => vi.fn());
const submitKioskCheckinWithOfflineQueue = vi.hoisted(() => vi.fn());

vi.mock("@/features/kiosk/lib/kiosk-api", () => ({
  lookupStudents,
  fetchTodaySchedules,
  fetchKioskOptions,
  autoDetectTraining,
}));

vi.mock("@/features/kiosk/lib/kiosk-db", () => ({
  lookupStudentsOffline,
  getTodaySchedulesOffline,
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

function businessError(code: string, detail: string) {
  return {
    response: {
      status: 409,
      data: { code, detail },
    },
  };
}

function renderNumpadHome() {
  const props = {
    logoUrl: "",
    clubName: "Jaguar",
    goToMatches: vi.fn(),
    goToResult: vi.fn(),
    goToFeedback: vi.fn(),
    goToErrorFeedback: vi.fn(),
    schedules: [schedule],
    setSchedules: vi.fn(),
  };

  render(<NumpadHome {...props} />);
  return props;
}

async function enterFastLookupDigits() {
  for (const digit of ["1", "2", "3", "4"]) {
    fireEvent.click(screen.getByRole("button", { name: `Цифра ${digit}` }));
  }

  await waitFor(() => {
    expect(submitKioskCheckinWithOfflineQueue).toHaveBeenCalled();
  });
}

function enterLookupDigits() {
  for (const digit of ["1", "2", "3", "4"]) {
    fireEvent.click(screen.getByRole("button", { name: `Цифра ${digit}` }));
  }
}

describe("NumpadHome fast check-in errors", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    Object.defineProperty(window.navigator, "onLine", {
      configurable: true,
      value: true,
    });
    lookupStudents.mockResolvedValue([student]);
    lookupStudentsOffline.mockResolvedValue([]);
    fetchTodaySchedules.mockResolvedValue([schedule]);
    fetchKioskOptions.mockResolvedValue({
      student_id: student.id,
      date: schedule.effective_date,
      options: [kioskOption],
    });
    getTodaySchedulesOffline.mockResolvedValue([]);
    autoDetectTraining.mockReturnValue(schedule);
  });

  it.each([
    [
      "enrollment_frozen",
      "Private trainer note: freeze reason must not leak",
    ],
    ["training_type_mismatch", "Private trainer note: wrong type details"],
    ["drop_in_price_required", "Private trainer note: missing price setup"],
  ])(
    "routes %s business failures to error feedback instead of manual result",
    async (code, detail) => {
      const props = renderNumpadHome();
      const error = businessError(code, detail);
      submitKioskCheckinWithOfflineQueue.mockRejectedValue(error);

      await enterFastLookupDigits();

      await waitFor(() => {
        expect(props.goToErrorFeedback).toHaveBeenCalledWith(error, student);
      });
      expect(props.goToResult).not.toHaveBeenCalled();
      expect(props.goToFeedback).not.toHaveBeenCalled();
      expect(props.goToMatches).not.toHaveBeenCalled();
    },
  );

  it("routes a current frozen booking from personalized options to feedback", async () => {
    fetchKioskOptions.mockResolvedValue({
      student_id: student.id,
      date: schedule.effective_date,
      options: [
        {
          ...kioskOption,
          self_checkin_status: "blocked",
          reason_code: "enrollment_frozen",
        },
      ],
    });
    const props = renderNumpadHome();

    enterLookupDigits();

    await waitFor(() => {
      expect(props.goToErrorFeedback).toHaveBeenCalledWith(
        {
          data: {
            code: "enrollment_frozen",
            detail: "Подойдите к тренеру",
          },
        },
        student,
      );
    });
    expect(submitKioskCheckinWithOfflineQueue).not.toHaveBeenCalled();
    expect(props.goToResult).not.toHaveBeenCalled();
  });

  it("shows the upcoming personalized booking instead of using the blocked current group", async () => {
    const blockedCurrentGroup = {
      ...kioskOption,
      schedule_id: 5,
      group_name: "Текущая группа",
      self_checkin_status: "blocked",
      reason_code: "drop_in_price_required",
      financial_status: "blocked",
      subscription_id: null,
      checkin_window_status: "open",
      checkin_opens_at: "2026-06-08T16:30:00+05:00",
      checkin_closes_at: "2026-06-08T18:00:00+05:00",
    };
    const upcomingPersonal = {
      ...kioskOption,
      schedule_id: 17,
      group_name: "Персональная тренировка",
      financial_status: "drop_in_debt",
      subscription_id: null,
      checkin_window_status: "too_early",
      checkin_opens_at: "2026-06-08T20:30:00+05:00",
      checkin_closes_at: "2026-06-08T22:00:00+05:00",
    };
    fetchKioskOptions.mockResolvedValue({
      student_id: student.id,
      date: schedule.effective_date,
      options: [blockedCurrentGroup, upcomingPersonal],
    });
    const props = renderNumpadHome();

    enterLookupDigits();

    await waitFor(() => {
      expect(props.goToResult).toHaveBeenCalledWith(student, [
        blockedCurrentGroup,
        upcomingPersonal,
      ]);
    });
    expect(submitKioskCheckinWithOfflineQueue).not.toHaveBeenCalled();
    expect(autoDetectTraining).not.toHaveBeenCalled();
  });

  it("requires explicit child selection when siblings share one guardian phone", async () => {
    const sibling = {
      ...student,
      id: 102,
      first_name: "Petr",
      group_name: "Older kids",
    };
    lookupStudents.mockResolvedValue([student, sibling]);
    const props = renderNumpadHome();

    enterLookupDigits();

    await waitFor(() => {
      expect(props.goToMatches).toHaveBeenCalledWith([student, sibling], "1234");
    });
    expect(fetchKioskOptions).not.toHaveBeenCalled();
    expect(submitKioskCheckinWithOfflineQueue).not.toHaveBeenCalled();
    expect(props.goToResult).not.toHaveBeenCalled();
  });
});
