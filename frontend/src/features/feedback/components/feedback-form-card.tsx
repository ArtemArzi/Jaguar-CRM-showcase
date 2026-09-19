import { useMemo, useState } from "react";
import type { ComponentType, FormEvent, ReactNode } from "react";
import { CheckCircle2, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import type {
  FeedbackAnswerPayload,
  FeedbackForm,
  FeedbackQuestion,
  FeedbackSubmitPayload,
  FeedbackSubmitResponse,
} from "../types";

type AnswerDraft = {
  rating_value: number | null;
  bool_value: boolean | null;
  text_value: string;
};

type SubmitState = "idle" | "success" | "duplicate";

interface FeedbackFormCardProps {
  form: FeedbackForm | null | undefined;
  isLoading: boolean;
  isError: boolean;
  onRetry: () => void;
  onSubmit: (payload: FeedbackSubmitPayload) => Promise<FeedbackSubmitResponse>;
  surface: ComponentType<{ children: ReactNode; className?: string }>;
  emptyTitle: string;
  emptyDescription: string;
  submitLabel?: string;
}

function getApiErrorCode(error: unknown): string | null {
  if (typeof error !== "object" || error === null || !("response" in error)) {
    return null;
  }

  const response = (error as { response?: { data?: { code?: unknown } } }).response;
  return typeof response?.data?.code === "string" ? response.data.code : null;
}

function isAnswered(question: FeedbackQuestion, draft: AnswerDraft | undefined) {
  if (!draft) return false;

  if (question.question_type === "rating") {
    return draft.rating_value !== null;
  }

  if (question.question_type === "yes_no") {
    return draft.bool_value !== null;
  }

  return draft.text_value.trim().length > 0;
}

function buildAnswers(
  questions: FeedbackQuestion[],
  drafts: Record<number, AnswerDraft>,
): FeedbackAnswerPayload[] {
  const answers: FeedbackAnswerPayload[] = [];

  questions.forEach((question) => {
    const draft = drafts[question.id];
    if (!isAnswered(question, draft)) return;

    if (question.question_type === "rating") {
      answers.push({ question_id: question.id, rating_value: draft.rating_value });
      return;
    }

    if (question.question_type === "yes_no") {
      answers.push({ question_id: question.id, bool_value: draft.bool_value });
      return;
    }

    answers.push({ question_id: question.id, text_value: draft.text_value.trim() });
  });

  return answers;
}

export function FeedbackFormCard({
  form,
  isLoading,
  isError,
  onRetry,
  onSubmit,
  surface: Surface,
  emptyTitle,
  emptyDescription,
  submitLabel = "Отправить",
}: FeedbackFormCardProps) {
  const [drafts, setDrafts] = useState<Record<number, AnswerDraft>>({});
  const [submitState, setSubmitState] = useState<SubmitState>("idle");
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  const requiredAnswered = useMemo(() => {
    if (!form) return false;
    return form.questions
      .filter((question) => question.is_required)
      .every((question) => isAnswered(question, drafts[question.id]));
  }, [drafts, form]);

  const canSubmit = Boolean(form) && requiredAnswered && !isSubmitting;

  const updateDraft = (questionId: number, patch: Partial<AnswerDraft>) => {
    setDrafts((current) => ({
      ...current,
      [questionId]: {
        rating_value: current[questionId]?.rating_value ?? null,
        bool_value: current[questionId]?.bool_value ?? null,
        text_value: current[questionId]?.text_value ?? "",
        ...patch,
      },
    }));
  };

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (!form || !canSubmit) return;

    setIsSubmitting(true);
    setSubmitError(null);
    try {
      const result = await onSubmit({
        form_id: form.id,
        answers: buildAnswers(form.questions, drafts),
      });
      setSubmitState(result.already_submitted ? "duplicate" : "success");
    } catch (error) {
      if (getApiErrorCode(error) === "already_submitted") {
        setSubmitState("duplicate");
      } else {
        setSubmitError("Не удалось отправить ответ. Попробуйте ещё раз.");
      }
    } finally {
      setIsSubmitting(false);
    }
  };

  if (isLoading) {
    return (
      <Surface className="p-5">
        <div className="space-y-3">
          <Skeleton className="h-5 w-40 rounded" />
          <Skeleton className="h-24 w-full rounded-xl" />
          <Skeleton className="h-12 w-full rounded-xl" />
        </div>
      </Surface>
    );
  }

  if (isError) {
    return (
      <Surface className="p-5">
        <div className="space-y-3">
          <p className="text-[17px] font-semibold">Опрос не загрузился</p>
          <p className="ui-body-muted">
            Проверьте подключение и попробуйте ещё раз.
          </p>
          <Button
            type="button"
            variant="outline"
            className="min-h-[44px] w-full"
            onClick={onRetry}
          >
            <RotateCcw />
            Повторить
          </Button>
        </div>
      </Surface>
    );
  }

  if (!form) {
    return (
      <Surface className="p-5">
        <div className="space-y-2">
          <p className="text-[17px] font-semibold">{emptyTitle}</p>
          <p className="ui-body-muted">
            {emptyDescription}
          </p>
        </div>
      </Surface>
    );
  }

  if (submitState !== "idle") {
    return (
      <Surface className="p-5">
        <div className="space-y-3">
          <span className="flex h-11 w-11 items-center justify-center rounded-2xl bg-emerald-50 text-emerald-600 ring-1 ring-emerald-100">
            <CheckCircle2 size={20} />
          </span>
          <div className="space-y-1">
            <p className="text-[18px] font-semibold">
              {submitState === "duplicate"
                ? "Ответ уже сохранён"
                : "Спасибо, ответ сохранён"}
            </p>
            <p className="ui-body-muted">
              {submitState === "duplicate"
                ? "Повторная отправка не создала новый ответ."
                : "Ваш ответ передан команде клуба."}
            </p>
          </div>
        </div>
      </Surface>
    );
  }

  return (
    <Surface>
      <CardContent className="space-y-4 py-5">
        <div className="space-y-1">
          <p className="ui-overline">
            Опрос
          </p>
          <h2 className="text-[20px] font-semibold leading-tight">{form.name}</h2>
        </div>

        <form className="space-y-4" onSubmit={handleSubmit}>
          {form.questions.map((question) => {
            const draft = drafts[question.id];
            return (
              <fieldset key={question.id} className="space-y-2.5">
                <legend className="text-[14px] font-medium leading-6 text-foreground">
                  {question.text}
                  {question.is_required ? (
                    <span className="ml-1 text-destructive">*</span>
                  ) : null}
                </legend>

                {question.question_type === "rating" ? (
                  <div className="grid grid-cols-5 gap-2">
                    {[1, 2, 3, 4, 5].map((rating) => (
                      <button
                        key={rating}
                        type="button"
                        className={cn(
                          "min-h-[44px] rounded-2xl border text-[15px] font-semibold transition",
                          draft?.rating_value === rating
                            ? "border-transparent bg-[var(--branding-accent)] text-foreground"
                            : "border-black/8 bg-white text-muted-foreground",
                        )}
                        aria-pressed={draft?.rating_value === rating}
                        onClick={() => updateDraft(question.id, { rating_value: rating })}
                      >
                        {rating}
                      </button>
                    ))}
                  </div>
                ) : null}

                {question.question_type === "yes_no" ? (
                  <div className="grid grid-cols-2 gap-2">
                    {[
                      { label: "Да", value: true },
                      { label: "Нет", value: false },
                    ].map((option) => (
                      <button
                        key={option.label}
                        type="button"
                        className={cn(
                          "min-h-[44px] rounded-2xl border px-3 text-[15px] font-semibold transition",
                          draft?.bool_value === option.value
                            ? "border-transparent bg-[var(--branding-accent)] text-foreground"
                            : "border-black/8 bg-white text-muted-foreground",
                        )}
                        aria-pressed={draft?.bool_value === option.value}
                        onClick={() => updateDraft(question.id, { bool_value: option.value })}
                      >
                        {option.label}
                      </button>
                    ))}
                  </div>
                ) : null}

                {question.question_type === "text" ? (
                  <textarea
                    className="min-h-[112px] w-full resize-none rounded-2xl border border-black/8 bg-white px-3 py-3 text-[15px] leading-6 outline-none transition focus:border-[var(--branding-accent)] focus:ring-2 focus:ring-[var(--branding-accent)]/20"
                    aria-label={question.text}
                    value={draft?.text_value ?? ""}
                    onChange={(event) =>
                      updateDraft(question.id, { text_value: event.target.value })
                    }
                  />
                ) : null}
              </fieldset>
            );
          })}

          {submitError ? (
            <p className="text-[14px] leading-6 text-destructive">{submitError}</p>
          ) : null}

          <Button type="submit" className="min-h-[48px] w-full" disabled={!canSubmit}>
            {isSubmitting ? "Отправляем..." : submitLabel}
          </Button>
        </form>
      </CardContent>
    </Surface>
  );
}
