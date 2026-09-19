import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { SyncIndicator } from "./sync-indicator";

describe("SyncIndicator", () => {
  it("shows pending queue while online", () => {
    render(<SyncIndicator status="online" pendingCount={2} />);

    expect(screen.getByText("2 в очереди")).toBeInTheDocument();
  });

  it("shows a short sync warning next to the status", () => {
    render(
      <SyncIndicator
        status="online"
        pendingCount={0}
        syncError="Не синхронизировано 1 посещ.: запись недоступна"
      />,
    );

    expect(
      screen.getByText("Не синхронизировано 1 посещ.: запись недоступна"),
    ).toBeInTheDocument();
  });

  it("shows persistent safe rejected-work guidance until explicit acknowledgement", () => {
    const onAcknowledge = vi.fn();
    render(
      <SyncIndicator
        status="online"
        pendingCount={0}
        rejectedCheckins={[
          {
            stable_key: "terminal-1",
            student_id: 10,
            schedule_id: 42,
            training_type_id: 7,
            checkin_date: "2026-06-03",
            error_code: "subscription_component_credits_exhausted",
            queued_at: "2026-06-03T12:00:00.000Z",
            rejected_at: "2026-06-03T12:05:00.000Z",
          },
        ]}
        onAcknowledgeRejected={onAcknowledge}
      />,
    );

    expect(screen.getByText("1 посещение не записано")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Занятия этого типа закончились. Обратитесь к администратору",
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("subscription_component_credits_exhausted"),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText(
        "Попросите администратора исправить причину, затем подтвердите сообщение.",
      ),
    ).toBeInTheDocument();

    const button = screen.getByRole("button", { name: "Подтвердить" });
    expect(button).toHaveClass("min-h-16");
    fireEvent.click(button);
    expect(onAcknowledge).toHaveBeenCalledWith(["terminal-1"]);
  });
});
