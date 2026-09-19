import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "@/features/auth/auth-store";
import ParentFeedback from "./parent-feedback";

const { get, post } = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}));

vi.mock("@/api/custom-fetch", () => ({
  default: { get, post },
}));

function renderParentFeedback(initialEntry = "/parent/child/3/feedback") {
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
          <Route path="/parent/child/:childId/feedback" element={<ParentFeedback />} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

const feedbackForm = {
  id: 20,
  name: "Опрос для родителя",
  is_active: true,
  created_at: "2026-06-08T09:00:00Z",
  questions: [
    {
      id: 201,
      question_type: "yes_no",
      text: "Ребёнку понравилась тренировка?",
      order: 1,
      is_required: true,
    },
    {
      id: 202,
      question_type: "text",
      text: "Комментарий",
      order: 2,
      is_required: false,
    },
  ],
};

describe("ParentFeedback", () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
    useAuthStore.setState({
      accessToken: "header.eyJzdWIiOiJwYXJlbnQtMSJ9.signature",
      role: "parent",
      clubId: 1,
      isAuthenticated: true,
    });
  });

  it("renders and submits a parent child-scoped feedback form", async () => {
    get.mockResolvedValue({ data: feedbackForm });
    post.mockResolvedValue({
      data: {
        id: 601,
        student_id: 3,
        form_id: 20,
        submitted_at: "2026-06-08T10:00:00Z",
        already_submitted: false,
      },
    });

    renderParentFeedback();

    expect(await screen.findByText("Опрос для родителя")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Да" }));
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "Всё хорошо" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Отправить" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/parents/children/3/feedback/submit/", {
        form_id: 20,
        answers: [
          { question_id: 201, bool_value: true },
          { question_id: 202, text_value: "Всё хорошо" },
        ],
      });
    });
    expect(await screen.findByText("Спасибо, ответ сохранён")).toBeInTheDocument();
  });

  it("shows duplicate state for an existing child response", async () => {
    get.mockResolvedValue({ data: feedbackForm });
    post.mockResolvedValue({
      data: {
        id: 601,
        student_id: 3,
        form_id: 20,
        submitted_at: "2026-06-08T10:00:00Z",
        already_submitted: true,
      },
    });

    renderParentFeedback();

    fireEvent.click(await screen.findByRole("button", { name: "Нет" }));
    fireEvent.click(screen.getByRole("button", { name: "Отправить" }));

    expect(await screen.findByText("Ответ уже сохранён")).toBeInTheDocument();
  });

  it("shows no-active-form state", async () => {
    get.mockResolvedValue({ data: null });

    renderParentFeedback();

    expect(await screen.findByText("Активного опроса сейчас нет")).toBeInTheDocument();
  });

  it("shows a retryable error state when child-scoped form access fails", async () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    get.mockRejectedValue(new Error("not found"));

    renderParentFeedback();

    expect(await screen.findByText("Опрос не загрузился")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Повторить" })).toBeInTheDocument();

    consoleError.mockRestore();
  });
});
