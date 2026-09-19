import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import StudentProfile from "./student-profile";
import { useAuthStore } from "@/features/auth/auth-store";

const { get } = vi.hoisted(() => ({
  get: vi.fn(),
}));

vi.mock("@/api/custom-fetch", () => ({
  default: { get },
}));

vi.mock("@/features/notifications/components/notification-preferences", () => ({
  default: () => <div>notifications</div>,
}));

function renderProfilePage() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  return render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <StudentProfile />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

describe("StudentProfile", () => {
  beforeEach(() => {
    localStorage.clear();
    get.mockReset();
    useAuthStore.setState({
      accessToken: null,
      refreshToken: null,
      role: "student",
      clubId: 1,
      trainerId: null,
      studentId: 7,
      isAuthenticated: true,
      studentBootstrapStatus: "resolved",
      studentBootstrapError: null,
    });
  });

  it("shows a profile-level error when /students/me fails", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/students/me/") {
        return Promise.reject(new Error("boom"));
      }

      if (url === "/documents/students/7/checklist/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderProfilePage();

    expect(
      await screen.findByText(
        "Не удалось загрузить профиль. Попробуйте обновить страницу.",
      ),
    ).toBeInTheDocument();
  });

  it("shows a document-level error without hiding loaded profile data", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/students/me/") {
        return Promise.resolve({
          data: {
            id: 7,
            first_name: "Masha",
            last_name: "Ivanova",
            phone: "",
            email: "",
            status: "active",
            is_child: false,
            date_of_birth: null,
          },
        });
      }

      if (url === "/documents/students/7/checklist/") {
        return Promise.reject(new Error("docs failed"));
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderProfilePage();

    expect(await screen.findByText("Masha Ivanova")).toBeInTheDocument();
    expect(screen.getAllByText("Не указан")).toHaveLength(4);
    expect(
      screen.getByText(
        "Не удалось загрузить документы. Попробуйте обновить страницу.",
      ),
    ).toBeInTheDocument();
  });

  it("shows historical inactive checklist items without an upload CTA", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/students/me/") {
        return Promise.resolve({
          data: {
            id: 7,
            first_name: "Masha",
            last_name: "Ivanova",
            phone: "",
            email: "",
            status: "active",
            is_child: false,
            date_of_birth: null,
          },
        });
      }

      if (url === "/documents/students/7/checklist/") {
        return Promise.resolve({
          data: [
            {
              document_type: {
                id: 9,
                name: "Архивная справка",
                description: "Тип больше не используется",
                is_required: false,
                is_active: false,
              },
              is_provided: false,
              has_file: false,
            },
          ],
        });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderProfilePage();

    expect(await screen.findByText("Архивная справка")).toBeInTheDocument();
    expect(screen.getByText("Архивный тип")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /загрузить/i }),
    ).not.toBeInTheDocument();
  });

  it("shows churned status as not attending instead of only left", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/students/me/") {
        return Promise.resolve({
          data: {
            id: 7,
            first_name: "Petr",
            last_name: "Petrov",
            phone: "",
            email: "",
            status: "churned",
            is_child: false,
            date_of_birth: null,
          },
        });
      }

      if (url === "/documents/students/7/checklist/") {
        return Promise.resolve({ data: [] });
      }

      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    renderProfilePage();

    expect(await screen.findAllByText("Не посещает 30+ дней")).toHaveLength(2);
    expect(screen.queryByText("Ушёл")).not.toBeInTheDocument();
  });
});
