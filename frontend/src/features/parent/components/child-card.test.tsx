import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import { ChildCard } from "./child-card";

describe("ChildCard redesign contract", () => {
  it("surfaces a compact attention summary for secondary children who need attention", () => {
    render(
      <MemoryRouter>
        <ChildCard
          id={2}
          firstName="Petr"
          lastName="Petrov"
          gradeName="Orange belt"
          subscriptionRemaining={1}
          subscriptionTotal={8}
          lastVisitDate="2026-03-20"
        />
      </MemoryRouter>,
    );

    expect(screen.getByText("Последняя")).toBeInTheDocument();
    expect(screen.getByText("Последняя тренировка")).toBeInTheDocument();
    expect(screen.getByText("Осталось 1 занятие")).toBeInTheDocument();
  });

  it("uses a stronger ended state when the subscription has no trainings left", () => {
    render(
      <MemoryRouter>
        <ChildCard
          id={2}
          firstName="Petr"
          lastName="Petrov"
          gradeName="Orange belt"
          subscriptionRemaining={0}
          subscriptionTotal={8}
          lastVisitDate="2026-03-20"
        />
      </MemoryRouter>,
    );

    expect(screen.getByText("Срочно")).toBeInTheDocument();
    expect(screen.getByText("Абонемент закончился")).toBeInTheDocument();
    expect(screen.getByText("Осталось 0 занятий")).toBeInTheDocument();
  });

  it("is a semantic link to the child profile", () => {
    render(
      <MemoryRouter>
        <ChildCard
          id={2}
          firstName="Petr"
          lastName="Petrov"
          gradeName="Orange belt"
          subscriptionRemaining={4}
          subscriptionTotal={8}
          lastVisitDate={null}
        />
      </MemoryRouter>,
    );

    expect(screen.getByRole("link", { name: /Petr Petrov/ })).toHaveAttribute(
      "href",
      "/parent/child/2",
    );
  });

  it("shows an active unlimited subscription instead of no-subscription danger", () => {
    render(
      <MemoryRouter>
        <ChildCard
          id={2}
          firstName="Petr"
          lastName="Petrov"
          gradeName="Orange belt"
          subscriptionRemaining={null}
          subscriptionTotal={null}
          subscriptionStatus="active"
          lastVisitDate={null}
        />
      </MemoryRouter>,
    );

    expect(screen.getByText("Безлимит")).toBeInTheDocument();
    expect(screen.queryByText("Нет активного абонемента")).not.toBeInTheDocument();
    expect(screen.queryByText("Срочно")).not.toBeInTheDocument();
  });

  it("shows pending freeze requests on active subscriptions", () => {
    render(
      <MemoryRouter>
        <ChildCard
          id={2}
          firstName="Petr"
          lastName="Petrov"
          gradeName="Orange belt"
          subscriptionRemaining={5}
          subscriptionTotal={8}
          subscriptionStatus="active"
          subscriptionFreezeStatus="pending"
          lastVisitDate={null}
        />
      </MemoryRouter>,
    );

    expect(screen.getByText("Ожидает")).toBeInTheDocument();
    expect(screen.getByText("Заморозка ожидает подтверждения")).toBeInTheDocument();
    expect(screen.getByText("Осталось 5 занятий")).toBeInTheDocument();
  });

  it("shows the next attended training context when schedule preview is available", () => {
    render(
      <MemoryRouter>
        <ChildCard
          id={2}
          firstName="Petr"
          lastName="Petrov"
          gradeName="Orange belt"
          subscriptionRemaining={4}
          subscriptionTotal={8}
          lastVisitDate={null}
          nextTrainingDayOfWeek={2}
          nextTrainingStartTime="17:30"
          nextTrainingGroupName="Kids Muay Thai"
          nextTrainingTrainerName="Ivan Petrov"
        />
      </MemoryRouter>,
    );

    expect(screen.getByText("Среда, 17:30")).toBeInTheDocument();
    expect(screen.getByText(/Kids Muay Thai/)).toBeInTheDocument();
    expect(screen.getByText(/Ivan Petrov/)).toBeInTheDocument();
  });
});
