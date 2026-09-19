import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DaySelector } from "./day-selector";

describe("DaySelector", () => {
  it("marks the provided club-local today instead of browser-local today", () => {
    render(
      <DaySelector
        selectedDate={new Date(2026, 5, 30)}
        today={new Date(2026, 5, 29)}
        onDateChange={vi.fn()}
      />,
    );

    expect(screen.getByRole("button", { name: "Пн 29, сегодня" })).toHaveAttribute("aria-current", "date");
    expect(screen.getByRole("button", { name: "Вт 30" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Вт 30" })).not.toHaveAttribute("aria-current");
  });
});
