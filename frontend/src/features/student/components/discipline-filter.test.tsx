import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DisciplineFilter } from "./discipline-filter";
import { useBrandingStore } from "@/features/branding/use-branding";

describe("DisciplineFilter", () => {
  beforeEach(() => {
    useBrandingStore.setState({
      primaryColor: "#111111",
      accentColor: "#FF6B00",
      clubName: "CRM Jaguar",
      logoUrl: "",
    });
  });

  it("opens mobile sheet and applies selected discipline", async () => {
    const onSelect = vi.fn();

    render(
      <DisciplineFilter
        disciplines={["Тайский бокс", "Персоналка"]}
        selected={null}
        onSelect={onSelect}
      />,
    );

    expect(screen.getByRole("button", { name: /выбрано/i })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /выбрано/i }));

    expect(screen.getByText("Выбор дисциплины")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /персоналка/i }));

    expect(onSelect).toHaveBeenCalledWith("Персоналка");
  });
});
