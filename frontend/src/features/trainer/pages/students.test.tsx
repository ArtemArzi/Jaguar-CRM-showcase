import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Students from "./students";
import type { StudentListItem } from "../types";

const { get, post } = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}));

vi.mock("@/api/custom-fetch", () => ({
  default: {
    get,
    post,
  },
}));

function renderStudents(
  students: StudentListItem[] = [],
  searchResults: Array<Record<string, unknown>> = [],
) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  get.mockImplementation((url: string, config?: { params?: Record<string, unknown> }) => {
    if (url === "/students/") {
      const offset = Number(config?.params?.offset ?? 0);
      const limit = Number(config?.params?.limit ?? students.length);
      return Promise.resolve({
        data: { items: students.slice(offset, offset + limit), count: students.length },
      });
    }
    if (url === "/students/intakes/capability") {
      return Promise.resolve({ data: { enabled: true } });
    }
    if (url === "/students/search/") {
      return Promise.resolve({ data: searchResults });
    }
    if (url === "/trainers/me/") {
      return Promise.resolve({ data: { id: 9 } });
    }
    return Promise.reject(new Error(`Unexpected GET ${url}`));
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/trainer/students"]}>
        <Students />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Trainer students page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    post.mockResolvedValue({ data: { id: 42 } });
  });

  it("uses the unified intake flow instead of direct student creation", async () => {
    renderStudents([]);

    expect(await screen.findByText("Пока нет учеников")).toBeInTheDocument();
    expect(screen.queryByText("Новый ученик")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Создать ученика" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Добавить ученика" }));

    expect(screen.getByRole("heading", { name: "Добавить ученика" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Имя клиента *"), {
      target: { value: "Мария" },
    });
    fireEvent.change(screen.getByLabelText("Телефон клиента *"), {
      target: { value: "+7 (900) 765-43-21" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Создать заявку" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/students/intakes/", {
        idempotency_key: expect.any(String),
        intake_kind: "new_contact",
        first_name: "Мария",
        last_name: "",
        phone: "+79007654321",
        guardian_phone: "",
        is_child: false,
        date_of_birth: null,
        source: "other",
        confirm_distinct_child: false,
      });
    });
    expect(post).not.toHaveBeenCalledWith("/students/", expect.anything());
  });

  it("loads the next student page instead of hiding records after the first page", async () => {
    const students = Array.from({ length: 51 }, (_, index) => ({
      id: index + 1,
      first_name: `Ученик${index + 1}`,
      last_name: "Тестовый",
      phone: `+700000000${String(index + 1).padStart(2, "0")}`,
      email: "",
      status: "active",
      is_child: false,
      date_of_birth: null,
      source: "manual",
    }));
    renderStudents(students);

    expect(await screen.findByText("Ученик1 Тестовый")).toBeInTheDocument();
    expect(screen.queryByText("Ученик51 Тестовый")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Загрузить ещё/ }));

    expect(await screen.findByText("Ученик51 Тестовый")).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith("/students/", {
      params: {
        limit: 50,
        offset: 50,
        workspace: "students",
        status: undefined,
        commercial_segment: undefined,
        q: undefined,
      },
    });
  });

  it("removes the Leads chip and routes unified search from server results", async () => {
    renderStudents([], [
      {
        id: 77,
        display_name: "Мария Лид",
        masked_phone: "+7900****77",
        target_workspace: "leads_active",
        route: "/trainer/leads?lead=77",
        identity_visibility: "full",
        allowed_action: "open",
      },
    ]);

    expect(
      await screen.findByRole("button", { name: /Без абонемента/ }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Заявки" })).not.toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText("Поиск по имени или телефону"), {
      target: { value: "Мария" },
    });

    expect(await screen.findByText("Мария Лид")).toBeInTheDocument();
    expect(screen.getByText(/Заявка/)).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith("/students/search/", {
      params: { q: "Мария", limit: 20 },
    });
  });

  it("maps every unified commercial chip to its exclusive commercial segment", async () => {
    renderStudents([]);

    await screen.findByRole("button", { name: /Без абонемента/ });
    expect(get).toHaveBeenCalledWith("/students/", {
      params: {
        limit: 50,
        offset: 0,
        workspace: "students",
        status: undefined,
        commercial_segment: undefined,
        q: undefined,
      },
    });
    get.mockClear();

    const segments = [
      ["Активные", "active_entitlement"],
      ["Без абонемента", "no_crm_entitlement"],
      ["В риске", "at_risk"],
      ["Ушли", "former"],
    ] as const;

    for (const [label, commercialSegment] of segments) {
      fireEvent.click(screen.getByRole("button", { name: new RegExp(`^${label}`) }));

      await waitFor(() => {
        expect(get).toHaveBeenCalledWith("/students/", {
          params: {
            limit: 50,
            offset: 0,
            workspace: "students",
            status: undefined,
            commercial_segment: commercialSegment,
            q: undefined,
          },
        });
      });
      get.mockClear();
    }
  });

  it("atomically reopens and claims an unassigned archived search result", async () => {
    post.mockResolvedValueOnce({ data: { id: 88 } });
    renderStudents([], [
      {
        id: 88,
        display_name: "Архивная заявка",
        masked_phone: "+7900****88",
        target_workspace: "leads_archived",
        route: "/trainer/leads?workspace=archived",
        identity_visibility: "masked",
        allowed_action: "can_reopen_and_claim",
      },
    ]);
    fireEvent.change(screen.getByPlaceholderText("Поиск по имени или телефону"), {
      target: { value: "Архивная" },
    });

    fireEvent.click(await screen.findByRole("button", { name: "Вернуть и забрать" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/leads/88/reopen-and-claim");
    });
  });
});
