import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useBrandingStore } from "@/features/branding/use-branding";
import { LeadDetailSheet } from "./lead-detail-sheet";
import type { LeadData } from "./lead-card";

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

function makeLead(status: string): LeadData {
  return {
    id: 17,
    first_name: "Иван",
    last_name: "Петров",
    phone: "+79990000000",
    is_child: false,
    source: "instagram",
    assigned_trainer_id: 4,
    lead_status: status,
    status: "lead",
    loss_reason: null,
    trial_date: null,
    created_at: "2026-06-01T08:00:00Z",
  };
}

function renderSheet({
  lead = makeLead("new"),
  trainerId = 4,
  onOpenChange = vi.fn(),
  onEnrollInGroup,
}: {
  lead?: LeadData | null;
  trainerId?: number | null;
  onOpenChange?: (open: boolean) => void;
  onEnrollInGroup?: (lead: LeadData) => void;
} = {}) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  const view = render(
    <QueryClientProvider client={queryClient}>
      <LeadDetailSheet
        open
        onOpenChange={onOpenChange}
        lead={lead}
        trainerId={trainerId}
        onEnrollInGroup={onEnrollInGroup}
      />
    </QueryClientProvider>,
  );

  return { ...view, queryClient, onOpenChange };
}

describe("LeadDetailSheet", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useBrandingStore.setState({ timeZone: "Asia/Yekaterinburg" });
    post.mockResolvedValue({ data: makeLead("contacted") });
    get.mockResolvedValue({
      data: [
        {
          schedule_id: 33,
          group_name: "Утро",
          effective_date: "2026-06-10",
          effective_start_time: "10:00:00",
          effective_end_time: "11:00:00",
          trainer_id: 4,
          trainer_name: "Тренер",
          location_id: 5,
          location_name: "Зал",
          training_type_id: 2,
          training_type_name: "Муай-тай",
        },
      ],
    });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows lifecycle actions according to backend status rules", () => {
    const { rerender, queryClient } = renderSheet({ lead: makeLead("new") });

    expect(screen.getByRole("button", { name: "Связался" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Записать на пробную" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Пробная прошла" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Потерян" })).toBeInTheDocument();

    rerender(
      <QueryClientProvider client={queryClient}>
        <LeadDetailSheet
          open
          onOpenChange={vi.fn()}
          lead={makeLead("trial_booked")}
          trainerId={4}
        />
      </QueryClientProvider>,
    );

    expect(screen.queryByRole("button", { name: "Связался" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Записать на пробную" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Пробная прошла" })).toBeInTheDocument();

    rerender(
      <QueryClientProvider client={queryClient}>
        <LeadDetailSheet
          open
          onOpenChange={vi.fn()}
          lead={makeLead("thinking")}
          trainerId={4}
        />
      </QueryClientProvider>,
    );

    expect(screen.queryByRole("button", { name: "Связался" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Записать на пробную" })).toBeInTheDocument();
    expect(screen.queryByText(/передайте администратору/i)).not.toBeInTheDocument();
  });

  it("offers direct group enrollment before or after trial with status-specific copy", () => {
    const onEnrollInGroup = vi.fn();
    const onOpenChange = vi.fn();
    const { rerender, queryClient } = renderSheet({
      lead: makeLead("new"),
      onOpenChange,
      onEnrollInGroup,
    });

    fireEvent.click(screen.getByRole("button", { name: "Оформить сразу в группу" }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
    expect(onEnrollInGroup).toHaveBeenCalledWith(
      expect.objectContaining({ id: 17, lead_status: "new" }),
    );

    rerender(
      <QueryClientProvider client={queryClient}>
        <LeadDetailSheet
          open
          onOpenChange={vi.fn()}
          lead={makeLead("trial_booked")}
          trainerId={4}
          onEnrollInGroup={vi.fn()}
        />
      </QueryClientProvider>,
    );
    expect(
      screen.getByRole("button", { name: "Оформить без ожидания пробной" }),
    ).toBeInTheDocument();

    rerender(
      <QueryClientProvider client={queryClient}>
        <LeadDetailSheet
          open
          onOpenChange={vi.fn()}
          lead={makeLead("trial_done")}
          trainerId={4}
          onEnrollInGroup={vi.fn()}
        />
      </QueryClientProvider>,
    );
    expect(
      screen.getByRole("button", { name: "Оформить в группу" }),
    ).toBeInTheDocument();
  });

  it("requires a loss reason before calling the lose endpoint", async () => {
    renderSheet({ lead: makeLead("contacted") });

    fireEvent.click(screen.getByRole("button", { name: "Потерян" }));
    fireEvent.click(screen.getByRole("button", { name: "Сохранить потерю" }));

    expect(await screen.findByText("Выберите причину потери")).toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("Причина потери *"), {
      target: { value: "too_far" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить потерю" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/leads/17/lose", {
        loss_reason: "too_far",
      });
    });
  });

  it("requires a release reason before returning a lead to the pool", async () => {
    renderSheet({ lead: makeLead("contacted") });

    fireEvent.click(screen.getByRole("button", { name: "Передать администратору" }));
    fireEvent.click(screen.getByRole("button", { name: "Передать администратору" }));

    expect(await screen.findByText("Укажите причину передачи")).toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("Причина передачи *"), {
      target: { value: "Хочет к другому тренеру" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Передать администратору" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/leads/17/release", {
        reason: "Хочет к другому тренеру",
      });
    });
  });

  it("calls the status endpoints with allowed transition payloads", async () => {
    const { rerender, queryClient } = renderSheet({ lead: makeLead("new") });

    fireEvent.click(screen.getByRole("button", { name: "Связался" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/leads/17/status", {
        status: "contacted",
      });
    });

    post.mockClear();
    rerender(
      <QueryClientProvider client={queryClient}>
        <LeadDetailSheet
          open
          onOpenChange={vi.fn()}
          lead={makeLead("trial_booked")}
          trainerId={4}
        />
      </QueryClientProvider>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Пробная прошла" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/leads/17/status", {
        status: "trial_done",
      });
    });
  });

  it("books group trial from canonical occurrence without editable client time", async () => {
    renderSheet({ lead: makeLead("contacted") });

    fireEvent.click(screen.getByRole("button", { name: "Записать на пробную" }));
    fireEvent.change(screen.getByLabelText("Дата пробной *"), {
      target: { value: "2099-06-10" },
    });
    expect(screen.queryByLabelText("Время пробной *")).not.toBeInTheDocument();

    await screen.findByRole("option", { name: /Утро/ });
    fireEvent.change(screen.getByLabelText("Тренировка *"), {
      target: { value: "33" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить пробную" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/leads/17/book-trial", {
        mode: "group",
        schedule_id: 33,
        occurrence_date: "2099-06-10",
      });
    });
  });

  it("uses the branding club timezone for the earliest selectable trial date", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-15T20:30:00Z"));
    renderSheet({ lead: makeLead("contacted") });

    fireEvent.click(screen.getByRole("button", { name: "Записать на пробную" }));

    expect(screen.getByLabelText("Дата пробной *")).toHaveAttribute("min", "2026-07-16");
  });

  it("shows a stable next action when the selected trial has already started", async () => {
    post.mockRejectedValueOnce({
      response: { data: { code: "trial_start_not_future" } },
    });
    renderSheet({ lead: makeLead("contacted") });

    fireEvent.click(screen.getByRole("button", { name: "Записать на пробную" }));
    fireEvent.change(screen.getByLabelText("Дата пробной *"), {
      target: { value: "2099-06-10" },
    });
    await screen.findByRole("option", { name: /Утро/ });
    fireEvent.change(screen.getByLabelText("Тренировка *"), {
      target: { value: "33" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить пробную" }));

    expect(
      await screen.findByText("Эта тренировка уже началась. Выберите будущую дату и тренировку."),
    ).toBeInTheDocument();
  });
});
