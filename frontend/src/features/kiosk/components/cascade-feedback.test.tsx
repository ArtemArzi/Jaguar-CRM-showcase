import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import CascadeFeedback from "./cascade-feedback";

const student = {
  id: 10,
  first_name: "Ivan",
  last_name: "Petrov",
  lookup_suffix: "1234",
  masked_phone: "+***1234",
  group_name: "Kids",
};

describe("CascadeFeedback", () => {
  it("shows guest booking check-in as a saved visit", () => {
    render(
      <CascadeFeedback
        student={student}
        result={{
          checkin_id: 101,
          is_debt: false,
          subscription_id: 12,
          created: true,
          duplicate: false,
          subscription_effect: "deducted",
          debt_effect: "none",
          salary_queued: true,
          parent_notification_queued: true,
          grade_progress_queued: true,
          alerts: [
            {
              type: "info",
              icon: "check",
              message: "Гость отмечен",
            },
          ],
        }}
        goToNumpad={vi.fn()}
      />,
    );

    expect(screen.getByText("Посещение сохранено")).toBeInTheDocument();
    expect(screen.getByText("Абонемент списан")).toBeInTheDocument();
    expect(screen.queryByText("Запись создана")).not.toBeInTheDocument();
    expect(screen.queryByText("Подойдите к тренеру для отметки")).not.toBeInTheDocument();
    expect(screen.queryByText("Зарплата тренеру в очереди")).not.toBeInTheDocument();
    expect(screen.queryByText("Уведомление родителю в очереди")).not.toBeInTheDocument();
    expect(screen.queryByText("Прогресс в очереди")).not.toBeInTheDocument();
  });
});
