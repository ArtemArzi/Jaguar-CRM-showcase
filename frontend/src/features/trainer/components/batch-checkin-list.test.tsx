import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { BatchCheckinList } from "./batch-checkin-list";

describe("BatchCheckinList", () => {
  it("marks frozen students as unavailable without a toggle button", () => {
    render(
      <BatchCheckinList
        students={[
          {
            id: 1,
            first_name: "Masha",
            last_name: "Ivanova",
            alerts: [],
            checkin_status: "waiting",
          },
          {
            id: 2,
            first_name: "Petr",
            last_name: "Petrov",
            alerts: [],
            enrollment_status: "frozen",
            checkin_blocked_reason: "enrollment_frozen",
            checkin_status: "blocked",
          },
        ]}
      />,
    );

    expect(screen.getByText("Заморожен")).toBeInTheDocument();
    expect(screen.getByText("Недоступен: заморожен")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Petrov Petr/ })).not.toBeInTheDocument();
    expect(screen.getByText("Ждет отметки")).toBeInTheDocument();
  });

  it("shows guest and self-booking roster badges", () => {
    render(
      <BatchCheckinList
        students={[
          {
            id: 1,
            first_name: "Nina",
            last_name: "Ivanova",
            alerts: [],
            created_from: "student_self_booking",
            is_guest_visit: true,
            checkin_status: "checked_in",
          },
        ]}
      />,
    );

    expect(screen.getByText("Гость")).toBeInTheDocument();
    expect(screen.getByText("Сам записался")).toBeInTheDocument();
    expect(screen.getByText("Отмечен")).toBeInTheDocument();
  });

  it("turns roster rows into accessible buttons when a detail handler is provided", () => {
    const onStudentSelect = vi.fn();
    const student = {
      id: 1,
      first_name: "Nina",
      last_name: "Ivanova",
      alerts: [],
      checkin_status: "waiting" as const,
    };

    render(
      <BatchCheckinList
        students={[student]}
        onStudentSelect={onStudentSelect}
      />,
    );

    fireEvent.click(
      screen.getByRole("button", {
        name: /Открыть контекст ученика Ivanova Nina: Ждет отметки/,
      }),
    );

    expect(onStudentSelect).toHaveBeenCalledWith(student);
  });

  it("shows an explicit empty roster state", () => {
    render(<BatchCheckinList students={[]} />);

    expect(screen.getByText("В списке занятия пока нет учеников")).toBeInTheDocument();
    expect(
      screen.getByText("Добавленные гости и записанные ученики появятся здесь."),
    ).toBeInTheDocument();
  });
});
