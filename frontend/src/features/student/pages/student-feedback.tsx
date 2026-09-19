import { useQuery } from "@tanstack/react-query";
import apiClient from "@/api/custom-fetch";
import { Skeleton } from "@/components/ui/skeleton";
import { FeedbackFormCard } from "@/features/feedback/components/feedback-form-card";
import type {
  FeedbackForm,
  FeedbackSubmitPayload,
  FeedbackSubmitResponse,
} from "@/features/feedback/types";
import { useAuthStore } from "@/features/auth/auth-store";
import { StudentPageIntro } from "../components/student-page-intro";
import { StudentSurfaceCard } from "../components/student-surface-card";

function FeedbackSkeleton() {
  return (
    <div className="space-y-4 px-5 pb-24 pt-4">
      <Skeleton className="h-[72px] rounded-xl" />
      <Skeleton className="h-[220px] rounded-xl" />
    </div>
  );
}

export default function StudentFeedback() {
  const studentId = useAuthStore((state) => state.studentId);
  const bootstrapStatus = useAuthStore((state) => state.studentBootstrapStatus);

  const {
    data: form,
    isLoading,
    isError,
    refetch,
  } = useQuery<FeedbackForm | null>({
    queryKey: ["student", "feedback", "form", studentId],
    queryFn: () =>
      apiClient
        .get<FeedbackForm | null>("/students/me/feedback/form/")
        .then((response) => response.data),
    enabled: bootstrapStatus === "resolved" && !!studentId,
    staleTime: 60_000,
  });

  if (bootstrapStatus === "idle" || bootstrapStatus === "loading" || !studentId) {
    return <FeedbackSkeleton />;
  }

  return (
    <div className="space-y-4 px-5 pb-24 pt-4">
      <StudentPageIntro
        eyebrow="Опрос"
        title="Обратная связь"
        description="Поделись впечатлением о тренировке, чтобы команда клуба видела твой ответ."
      />

      <FeedbackFormCard
        form={form}
        isLoading={isLoading}
        isError={isError}
        onRetry={() => void refetch()}
        surface={StudentSurfaceCard}
        emptyTitle="Активного опроса сейчас нет"
        emptyDescription="Когда клуб откроет форму обратной связи, она появится здесь."
        onSubmit={(payload: FeedbackSubmitPayload) =>
          apiClient
            .post<FeedbackSubmitResponse>("/students/me/feedback/submit/", payload)
            .then((response) => response.data)
        }
      />
    </div>
  );
}
