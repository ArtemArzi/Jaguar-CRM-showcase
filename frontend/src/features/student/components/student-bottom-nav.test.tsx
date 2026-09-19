import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";
import { StudentBottomNav } from "./student-bottom-nav";

vi.mock("@/features/branding/use-branding", () => ({
  useBrandingStore: (selector: (state: { primaryColor: string; accentColor: string }) => string) =>
    selector({
      primaryColor: "#111111",
      accentColor: "#f5c542",
    }),
}));

vi.mock("@/features/branding/use-contrast-color", () => ({
  isDarkBackground: () => true,
  getContrastingAccent: () => "#f5c542",
}));

describe("StudentBottomNav", () => {
  it("renders 4 navigation tabs", () => {
    render(
      <MemoryRouter initialEntries={["/student"]}>
        <StudentBottomNav />
      </MemoryRouter>,
    );

    expect(screen.getByRole("link", { name: /главная/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /расписание/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /посещения/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /профиль/i })).toBeInTheDocument();
  });

  it("highlights the active tab with branding styles", () => {
    render(
      <MemoryRouter initialEntries={["/student/profile"]}>
        <StudentBottomNav />
      </MemoryRouter>,
    );

    const profileTab = screen.getByRole("link", { name: /профиль/i });
    expect(profileTab).toHaveStyle({ color: "rgb(245, 197, 66)" });
    expect(profileTab).toHaveStyle({
      backgroundColor: "rgba(255,255,255,0.12)",
    });
  });
});
