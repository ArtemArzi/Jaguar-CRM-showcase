import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import StudentFeedback from "./student-feedback";
import { useAuthStore } from "@/features/auth/auth-store";

const { get, post } = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}));

vi.mock("@/api/custom-fetch", () => ({
  default: { get, post },
}));

function renderStudentFeedback() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  return render(
    <MemoryRouter initialEntries={["/student/feedback"]}>
      <QueryClientProvider client={queryClient}>
        <StudentFeedback />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

const feedbackForm = {
  id: 10,
  name: "Как прошла тренировка?",
  is_active: true,
  created_at: "2026-06-08T09:00:00Z",
  questions: [
    {
      id: 101,
      question_type: "rating",
      text: "Оцените тренировку",
      order: 1,
      is_required: true,
    },
    {
      id: 102,
      question_type: "text",
      text: "Комментарий",
      order: 2,
      is_required: false,
    },
  ],
};

describe("StudentFeedback", () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
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

  it("renders and submits the student feedback form without student_id", async () => {
    get.mockResolvedValue({ data: feedbackForm });
    post.mockResolvedValue({
      data: {
        id: 501,
        student_id: 7,
        form_id: 10,
        submitted_at: "2026-06-08T10:00:00Z",
        already_submitted: false,
      },
    });

    renderStudentFeedback();

    expect(await screen.findByText("Как прошла тренировка?")).toBeInTheDocument();
    const ratingButton = screen.getByRole("button", { name: "5" });
    fireEvent.click(ratingButton);
    expect(ratingButton).toHaveAttribute("aria-pressed", "true");
    fireEvent.change(screen.getByLabelText("Комментарий"), {
      target: { value: "Было полезно" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Отправить" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/students/me/feedback/submit/", {
        form_id: 10,
        answers: [
          { question_id: 101, rating_value: 5 },
          { question_id: 102, text_value: "Было полезно" },
        ],
      });
    });
    expect(await screen.findByText("Спасибо, ответ сохранён")).toBeInTheDocument();
  });

  it("shows duplicate state when the response already exists", async () => {
    get.mockResolvedValue({ data: feedbackForm });
    post.mockResolvedValue({
      data: {
        id: 501,
        student_id: 7,
        form_id: 10,
        submitted_at: "2026-06-08T10:00:00Z",
        already_submitted: true,
      },
    });

    renderStudentFeedback();

    fireEvent.click(await screen.findByRole("button", { name: "4" }));
    fireEvent.click(screen.getByRole("button", { name: "Отправить" }));

    expect(await screen.findByText("Ответ уже сохранён")).toBeInTheDocument();
  });

  it("shows no-active-form state", async () => {
    get.mockResolvedValue({ data: null });

    renderStudentFeedback();

    expect(await screen.findByText("Активного опроса сейчас нет")).toBeInTheDocument();
  });
});
