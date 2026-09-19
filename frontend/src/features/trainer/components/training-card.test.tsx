import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useBrandingStore } from "@/features/branding/use-branding";
import type { ScheduleOccurrenceOut } from "../types";
import { TrainingCard } from "./training-card";

const navigate = vi.fn();

vi.mock("react-router", async () => {
  const actual = await vi.importActual<typeof import("react-router")>(
    "react-router",
  );

  return {
    ...actual,
    useNavigate: () => navigate,
  };
});

const baseOccurrence: ScheduleOccurrenceOut = {
  schedule_id: 42,
  group_name: "Персональная тренировка",
  effective_date: "2026-04-15",
  effective_start_time: "18:00:00",
  effective_end_time: "19:00:00",
  trainer_id: 7,
  trainer_name: "Анна Иванова",
  location_id: 3,
  location_name: "Зал на Вайнера",
  one_time_date: "2026-04-15",
  is_rescheduled: false,
  is_substitute: false,
  training_type_kind: "personal",
};

const groupOccurrence: ScheduleOccurrenceOut = {
  ...baseOccurrence,
  group_name: "Группа муай-тай",
  one_time_date: null,
  training_type_kind: "group",
};

describe("TrainingCard", () => {
  beforeEach(() => {
    navigate.mockReset();
    vi.useFakeTimers();
    useBrandingStore.setState({ timeZone: "UTC" });
    vi.setSystemTime(new Date("2026-04-15T18:30:00Z"));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("renders effective trainer, location and occurrence flags", () => {
    render(
      <TrainingCard
        schedule={{
          ...baseOccurrence,
          is_rescheduled: true,
          is_substitute: true,
        }}
        currentTrainerId={7}
      />,
    );

    expect(screen.getByText("Зал на Вайнера")).toBeInTheDocument();
    expect(screen.getByText("Анна Иванова")).toBeInTheDocument();
    expect(screen.getByText("перенос")).toBeInTheDocument();
    expect(screen.getByText("замена")).toBeInTheDocument();
  });

  it("hides actions for foreign schedule occurrence", () => {
    render(
      <TrainingCard
        schedule={{
          ...baseOccurrence,
          trainer_id: 99,
          trainer_name: "Другой тренер",
        }}
        currentTrainerId={7}
      />,
    );

    expect(screen.queryByText("Статус")).not.toBeInTheDocument();
    expect(screen.queryByText("Открыть")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Действия")).not.toBeInTheDocument();
  });

  it("opens status for substitute occurrence but hides management menu", () => {
    render(
      <TrainingCard
        schedule={{
          ...baseOccurrence,
          is_substitute: true,
        }}
        currentTrainerId={7}
      />,
    );

    expect(screen.getByText("Статус")).toBeInTheDocument();
    expect(screen.queryByLabelText("Действия")).not.toBeInTheDocument();
  });

  it("navigates to session status with the effective occurrence date", () => {
    render(<TrainingCard schedule={baseOccurrence} currentTrainerId={7} />);

    fireEvent.click(screen.getByRole("button", { name: "Статус" }));

    expect(navigate).toHaveBeenCalledWith(
      "/trainer/schedule/42/checkin?date=2026-04-15",
    );
  });

  it("hides the management menu after the occurrence has started", () => {
    render(<TrainingCard schedule={baseOccurrence} currentTrainerId={7} />);

    expect(screen.getByRole("button", { name: "Статус" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Действия")).not.toBeInTheDocument();
  });

  it("opens future own session detail before check-in start", () => {
    vi.setSystemTime(new Date("2026-04-15T17:30:00Z"));

    render(<TrainingCard schedule={baseOccurrence} currentTrainerId={7} />);

    fireEvent.click(screen.getByRole("button", { name: "Открыть" }));

    expect(navigate).toHaveBeenCalledWith(
      "/trainer/schedule/42/checkin?date=2026-04-15",
    );
  });

  it("allows opening the guest flow before check-in start", () => {
    const onAddGuest = vi.fn();
    vi.setSystemTime(new Date("2026-04-15T17:30:00Z"));

    render(
      <TrainingCard
        schedule={groupOccurrence}
        currentTrainerId={7}
        onAddGuest={onAddGuest}
      />,
    );

    expect(screen.getByRole("button", { name: "Открыть" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Добавить гостя" }));

    expect(onAddGuest).toHaveBeenCalledWith(groupOccurrence);
    expect(navigate).not.toHaveBeenCalled();
  });

  it("uses the club time zone when deciding whether the occurrence started", () => {
    useBrandingStore.setState({ timeZone: "Europe/Moscow" });
    vi.setSystemTime(new Date("2026-04-15T15:30:00Z"));

    render(<TrainingCard schedule={baseOccurrence} currentTrainerId={7} />);

    expect(screen.getByRole("button", { name: "Статус" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Открыть" })).not.toBeInTheDocument();
  });

  it("does not expose the whole card as a nested button", () => {
    render(<TrainingCard schedule={groupOccurrence} currentTrainerId={7} />);

    expect(screen.queryByRole("button", { name: /Группа муай-тай/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Статус" })).toBeInTheDocument();
  });

  it("hides the guest flow for personal one-time sessions", () => {
    render(
      <TrainingCard
        schedule={baseOccurrence}
        currentTrainerId={7}
        onAddGuest={vi.fn()}
      />,
    );

    expect(screen.queryByRole("button", { name: "Добавить гостя" })).not.toBeInTheDocument();
  });
});
