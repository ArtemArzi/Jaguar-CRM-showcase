import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ParentShell from "./parent-shell";
import { useAuthStore } from "@/features/auth/auth-store";

const { get } = vi.hoisted(() => ({
  get: vi.fn(),
}));

vi.mock("@/api/custom-fetch", () => ({
  default: { get },
}));

vi.mock("@/features/branding/branding-provider", () => ({
  BrandingProvider: ({ children }: { children: ReactNode }) => (
    <div data-testid="branding-provider">{children}</div>
  ),
}));

function renderParentShell(initialEntry = "/parent") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <QueryClientProvider client={queryClient}>
        <Routes>
          <Route path="/parent" element={<ParentShell />}>
            <Route index element={<div>parent-content</div>} />
            <Route path="child/:childId" element={<div>child-content</div>} />
          </Route>
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

describe("ParentShell redesign contract", () => {
  beforeEach(() => {
    get.mockReset();
    get.mockResolvedValue({
      data: [{ id: 7 }],
    });
    useAuthStore.setState({
      accessToken: "header.eyJzdWIiOiJwYXJlbnQtNyJ9.signature",
      clubId: 1,
      role: "parent",
      isAuthenticated: true,
    });
  });

  it("renders a branded header and a stable page frame around parent routes", async () => {
    renderParentShell();

    expect(
      screen.getByRole("banner", { name: "Кабинет родителя" }),
    ).toBeInTheDocument();

    const pageFrame = screen.getByRole("main", {
      name: "Основной контент кабинета родителя",
    });

    expect(within(pageFrame).getByText("parent-content")).toBeInTheDocument();
  });

  it("renders parent-specific bottom navigation with child-scoped links", async () => {
    renderParentShell();

    const nav = screen.getByRole("navigation", {
      name: "Нижняя навигация родителя",
    });

    expect(within(nav).getByRole("link", { name: /Дети/ })).toHaveAttribute(
      "href",
      "/parent",
    );
    await waitFor(() => {
      expect(within(nav).getByRole("link", { name: /Занятия/ })).toHaveAttribute(
        "href",
        "/parent/child/7#groups",
      );
      expect(within(nav).getByRole("link", { name: /Оплата/ })).toHaveAttribute(
        "href",
        "/parent/child/7#subscription",
      );
    });
    expect(within(nav).getByRole("link", { name: /Настройки/ })).toHaveAttribute(
      "href",
      "/parent#settings",
    );
  });

  it("uses the current child route before falling back to the first child", () => {
    renderParentShell("/parent/child/11");

    const nav = screen.getByRole("navigation", {
      name: "Нижняя навигация родителя",
    });

    expect(within(nav).getByRole("link", { name: /Занятия/ })).toHaveAttribute(
      "href",
      "/parent/child/11#groups",
    );
    expect(within(nav).getByRole("link", { name: /Оплата/ })).toHaveAttribute(
      "href",
      "/parent/child/11#subscription",
    );
  });
});
