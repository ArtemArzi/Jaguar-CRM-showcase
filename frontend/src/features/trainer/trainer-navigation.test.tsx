import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Navigate } from "react-router";
import { describe, expect, it, vi } from "vitest";
import { BottomNav } from "./components/bottom-nav";
import { trainerRoutes } from "./trainer-router";

vi.mock("@/api/custom-fetch", () => ({
  default: {
    get: vi.fn(() => Promise.resolve({ data: { items: [{ id: 1 }, { id: 2 }] } })),
  },
}));

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

function renderBottomNav(path = "/trainer") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <BottomNav />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("trainer navigation", () => {
  it("registers leads as a trainer child route", () => {
    const paths = trainerRoutes.map((route) => route.path);

    expect(paths).toContain("leads");
  });

  it("redirects the old direct student create route to requests", () => {
    const route = trainerRoutes.find((item) => item.path === "students/new");
    const element = route?.element as { type: unknown; props: { to: string; replace: boolean } };

    expect(element.type).toBe(Navigate);
    expect(element.props.to).toBe("/trainer/leads");
    expect(element.props.replace).toBe(true);
  });

  it("registers availability as a child route without adding a bottom tab", () => {
    const paths = trainerRoutes.map((route) => route.path);

    expect(paths).toContain("availability");

    renderBottomNav("/trainer/availability");

    expect(screen.getAllByRole("link")).toHaveLength(5);
    expect(screen.queryByRole("link", { name: /доступность/i })).not.toBeInTheDocument();
  });

  it("renders the leads tab without dropping the task badge", async () => {
    renderBottomNav("/trainer/leads");

    expect(screen.getByRole("link", { name: /заявки/i })).toHaveAttribute(
      "href",
      "/trainer/leads",
    );
    expect(screen.getByRole("link", { name: /задачи/i })).toBeInTheDocument();
    expect(await screen.findByText("2")).toBeInTheDocument();
  });
});
