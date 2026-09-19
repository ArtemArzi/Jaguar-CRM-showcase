import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { AlertOverlay } from "./alert-overlay";

describe("AlertOverlay", () => {
  it("shows compact cascade badges even when a submitted student has no alerts", () => {
    const onDismiss = vi.fn();

    render(
      <AlertOverlay
        students={[
          {
            id: 1,
            first_name: "Masha",
            last_name: "Ivanova",
            alerts: [],
            checkin: {
              checkin_id: 10,
              student_id: 1,
              is_debt: true,
              subscription_id: 20,
              alerts: [],
              created: false,
              duplicate: true,
              subscription_effect: "deducted",
              debt_effect: "created",
              salary_queued: true,
              parent_notification_queued: true,
              grade_progress_queued: true,
              group_analytics_queued: true,
              retention_auto_close_queued: true,
              post_trial_task_queued: true,
              trainings_left_push_queued: true,
            },
          },
          {
            id: 2,
            first_name: "Petr",
            last_name: "Petrov",
            alerts: [{ type: "newcomer", icon: "user-plus", message: "Новый" }],
            checkin: {
              checkin_id: 11,
              student_id: 2,
              is_debt: false,
              subscription_id: null,
              alerts: [{ type: "newcomer", icon: "user-plus", message: "Новый" }],
              created: true,
              duplicate: false,
              subscription_effect: "none",
              debt_effect: "none",
              salary_queued: false,
              parent_notification_queued: false,
              grade_progress_queued: false,
              group_analytics_queued: false,
              retention_auto_close_queued: false,
              post_trial_task_queued: false,
              trainings_left_push_queued: false,
            },
          },
        ]}
        onDismiss={onDismiss}
      />,
    );

    expect(screen.getByText("Сводка по группе")).toBeInTheDocument();
    expect(screen.getByText("Ivanova Masha")).toBeInTheDocument();
    expect(screen.getByText("Petrov Petr")).toBeInTheDocument();
    expect(screen.getByText("Новичок")).toBeInTheDocument();
    expect(screen.getByText("Уже отмечен")).toBeInTheDocument();
    expect(screen.getByText("Абонемент")).toBeInTheDocument();
    expect(screen.getByText("Долг")).toBeInTheDocument();
    expect(screen.getByText("ЗП")).toBeInTheDocument();
    expect(screen.getByText("Родитель")).toBeInTheDocument();
    expect(screen.getByText("Прогресс")).toBeInTheDocument();
    expect(screen.getByText("Аналитика")).toBeInTheDocument();
    expect(screen.getByText("Задачи")).toBeInTheDocument();
    expect(screen.queryByText("Push")).not.toBeInTheDocument();
    expect(onDismiss).not.toHaveBeenCalled();
  });
});
