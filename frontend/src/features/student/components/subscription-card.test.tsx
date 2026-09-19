import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import { SubscriptionCard } from "./subscription-card";

describe("SubscriptionCard", () => {
  it("shows a pending freeze request separately from active subscription status", () => {
    render(
      <MemoryRouter>
        <SubscriptionCard
          tariffName="Active package"
          trainingsUsed={3}
          trainingsTotal={8}
          trainingsLeft={5}
          expiresAt="2026-05-20"
          status="active"
          freezeStatus="pending"
        />
      </MemoryRouter>,
    );

    expect(screen.getByText("Ожидает")).toBeInTheDocument();
    expect(screen.getByText("Заморозка ожидает подтверждения")).toBeInTheDocument();
    expect(
      screen.getByText("Клуб проверяет заявку на заморозку абонемента."),
    ).toBeInTheDocument();
  });

  it("shows a cancelled refund state instead of presenting entitlement as available", () => {
    render(
      <MemoryRouter>
        <SubscriptionCard
          tariffName="Refunded package"
          trainingsUsed={3}
          trainingsTotal={8}
          trainingsLeft={5}
          expiresAt="2026-05-20"
          status="cancelled"
        />
      </MemoryRouter>,
    );

    expect(screen.getByText("Абонемент отменён")).toBeInTheDocument();
    expect(
      screen.getByText("Оплата возвращена, оставшиеся занятия недоступны."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/ещё доступны/)).not.toBeInTheDocument();
  });

  it.todo("renders progress bar with correct percentage");
  it.todo("shows warning when remaining <= 3");
  it.todo("shows expired badge when status is expired");
  it.todo("shows unlimited text when trainingsTotal is null");
});
