import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ClientIntakeSheet } from "./client-intake-sheet";

const { post } = vi.hoisted(() => ({ post: vi.fn() }));

vi.mock("@/api/custom-fetch", () => ({ default: { post } }));

function renderSheet(onNavigate = vi.fn(), unifiedEnabled = true) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <ClientIntakeSheet
        open
        onOpenChange={vi.fn()}
        onNavigate={onNavigate}
        unifiedEnabled={unifiedEnabled}
      />
    </QueryClientProvider>,
  );
  return onNavigate;
}

function submitAdult(name = "Иван", phone = "+7 900 123-45-67") {
  fireEvent.change(screen.getByLabelText("Имя клиента *"), { target: { value: name } });
  fireEvent.change(screen.getByLabelText("Телефон клиента *"), { target: { value: phone } });
  fireEvent.click(screen.getByRole("button", { name: "Создать заявку" }));
}

describe("ClientIntakeSheet", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValue("11111111-1111-4111-8111-111111111111");
  });

  afterEach(() => vi.restoreAllMocks());

  it("opens without focusing an input", async () => {
    renderSheet();
    const dialog = screen.getByRole("dialog", { name: "Добавить ученика" });
    const nameInput = screen.getByLabelText("Имя клиента *");
    await waitFor(() => expect(dialog).toHaveFocus());
    expect(nameInput).not.toHaveFocus();
    act(() => nameInput.focus());
    expect(nameInput).toHaveFocus();
  });

  it("submits new contact through the unified intake without client trainer authority", async () => {
    post.mockResolvedValue({
      data: { result_kind: "created_new_contact", route: "/trainer/leads?lead=42" },
    });
    const navigate = renderSheet();

    fireEvent.change(screen.getByLabelText("Имя клиента *"), { target: { value: "Иван" } });
    fireEvent.change(screen.getByLabelText("Телефон клиента *"), { target: { value: "+7 900 123-45-67" } });
    fireEvent.click(screen.getByRole("button", { name: "Создать заявку" }));

    await waitFor(() => expect(post).toHaveBeenCalledWith("/students/intakes/", {
      idempotency_key: "11111111-1111-4111-8111-111111111111",
      intake_kind: "new_contact",
      first_name: "Иван",
      last_name: "",
      phone: "+79001234567",
      guardian_phone: "",
      is_child: false,
      date_of_birth: null,
      source: "other",
      confirm_distinct_child: false,
    }));
    expect(screen.getByText("Заявка создана")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Открыть заявку" }));
    expect(navigate).toHaveBeenCalledWith("/trainer/leads?lead=42");
  });

  it("creates an existing child student with a distinct mode receipt", async () => {
    post.mockResolvedValue({
      data: {
        result_kind: "created_existing_student",
        route: "/trainer/students/77",
        commercial_segment: "no_crm_entitlement",
      },
    });
    renderSheet();

    fireEvent.click(screen.getByRole("radio", { name: "Уже занимается" }));
    fireEvent.click(screen.getByRole("switch"));
    fireEvent.change(screen.getByLabelText("Имя ребёнка *"), { target: { value: "Маша" } });
    fireEvent.change(screen.getByLabelText("Фамилия"), { target: { value: "Петрова" } });
    fireEvent.change(screen.getByLabelText("Дата рождения"), { target: { value: "2018-04-05" } });
    fireEvent.change(screen.getByLabelText("Источник"), { target: { value: "recommendation" } });
    fireEvent.change(screen.getByLabelText("Телефон родителя *"), { target: { value: "+7 900 222-33-44" } });
    fireEvent.click(screen.getByRole("button", { name: "Добавить ученика" }));

    await waitFor(() => expect(post).toHaveBeenCalledWith("/students/intakes/", expect.objectContaining({
      intake_kind: "existing_student",
      first_name: "Маша",
      last_name: "Петрова",
      phone: "",
      guardian_phone: "+79002223344",
      is_child: true,
      date_of_birth: "2018-04-05",
      source: "recommendation",
    })));
    expect(screen.getByText("Ученик добавлен")).toBeInTheDocument();
    expect(screen.getByText("Без абонемента в CRM")).toBeInTheDocument();
  });

  it("keeps the legacy lead endpoint when the capability fails closed", async () => {
    post.mockResolvedValue({ data: { id: 42 } });
    renderSheet(vi.fn(), false);

    expect(screen.getByRole("heading", { name: "Новая заявка" })).toBeInTheDocument();
    expect(screen.queryByRole("radio", { name: "Уже занимается" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Дата рождения")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Источник")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Имя клиента *"), { target: { value: "Иван" } });
    fireEvent.change(screen.getByLabelText("Телефон клиента *"), { target: { value: "+7 900 123-45-67" } });
    fireEvent.click(screen.getByRole("button", { name: "Создать заявку" }));

    await waitFor(() => expect(post).toHaveBeenCalledWith("/leads/", {
      first_name: "Иван",
      last_name: "",
      phone: "+79001234567",
      guardian_phone: "",
      is_child: false,
    }));
  });

  it("preserves the legacy own-card action when the capability fails closed", async () => {
    post.mockRejectedValue({
      response: {
        data: {
          code: "duplicate_phone",
          duplicate_scope: "own",
          can_open_existing: true,
          existing_student: { id: 42, display_name: "Иван Иванов" },
        },
      },
    });
    const navigate = renderSheet(vi.fn(), false);

    submitAdult();
    fireEvent.click(await screen.findByRole("button", { name: "Открыть карточку" }));

    expect(navigate).toHaveBeenCalledWith("/trainer/students/42");
  });

  it("preserves the legacy pool duplicate action", async () => {
    post.mockRejectedValue({
      response: { data: { code: "duplicate_phone", duplicate_scope: "pool" } },
    });
    const navigate = renderSheet(vi.fn(), false);
    submitAdult();
    fireEvent.click(await screen.findByRole("button", { name: "Открыть свободные заявки" }));
    expect(navigate).toHaveBeenCalledWith("/trainer/leads");
  });

  it("preserves the legacy foreign-trainer dismissal", async () => {
    post.mockRejectedValue({
      response: { data: { code: "duplicate_phone", duplicate_scope: "other" } },
    });
    renderSheet(vi.fn(), false);
    submitAdult("Пётр", "+7 900 111-22-33");
    expect(await screen.findByText("Заявка уже в работе у другого тренера")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Понятно" }));
    expect(screen.queryByText("Заявка уже в работе у другого тренера")).not.toBeInTheDocument();
  });

  it("rotates the command key after editing a rejected persisted command", async () => {
    vi.mocked(globalThis.crypto.randomUUID)
      .mockReturnValueOnce("11111111-1111-4111-8111-111111111111")
      .mockReturnValueOnce("22222222-2222-4222-8222-222222222222");
    post
      .mockRejectedValueOnce({
        response: { data: { code: "duplicate", detail: "Такой контакт уже есть" } },
      })
      .mockResolvedValueOnce({
        data: { result_kind: "created_new_contact", route: "/trainer/leads?lead=43" },
      });
    renderSheet();

    submitAdult();
    expect(await screen.findByText("Такой контакт уже есть")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Телефон клиента *"), {
      target: { value: "+7 900 999-88-77" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Создать заявку" }));

    await waitFor(() => expect(post).toHaveBeenLastCalledWith(
      "/students/intakes/",
      expect.objectContaining({
        idempotency_key: "22222222-2222-4222-8222-222222222222",
        phone: "+79009998877",
      }),
    ));
    expect(screen.getByText("Заявка создана")).toBeInTheDocument();
  });

  it("confirms a legitimate sibling with a new idempotency key", async () => {
    vi.mocked(globalThis.crypto.randomUUID)
      .mockReturnValueOnce("11111111-1111-4111-8111-111111111111")
      .mockReturnValueOnce("22222222-2222-4222-8222-222222222222");
    post
      .mockRejectedValueOnce({
        response: {
          data: {
            detail: "Возможен дубль ребёнка",
            code: "possible_child_duplicate_confirmation_required",
            allowed_action: "confirm_distinct_child",
          },
        },
      })
      .mockResolvedValueOnce({
        data: { result_kind: "created_new_contact", route: "/trainer/leads?lead=88" },
      });
    renderSheet();
    fireEvent.click(screen.getByRole("switch"));
    fireEvent.change(screen.getByLabelText("Имя ребёнка *"), { target: { value: "Маша" } });
    fireEvent.change(screen.getByLabelText("Телефон родителя *"), { target: { value: "+7 900 222-33-44" } });
    fireEvent.click(screen.getByRole("button", { name: "Создать заявку" }));

    fireEvent.click(await screen.findByRole("button", { name: "Это другой ребёнок" }));

    await waitFor(() => expect(post).toHaveBeenLastCalledWith(
      "/students/intakes/",
      expect.objectContaining({
        idempotency_key: "22222222-2222-4222-8222-222222222222",
        confirm_distinct_child: true,
      }),
    ));
  });
});
