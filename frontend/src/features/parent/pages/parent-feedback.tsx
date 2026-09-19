import { useNavigate, useParams } from "react-router";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import apiClient from "@/api/custom-fetch";
import { Skeleton } from "@/components/ui/skeleton";
import { FeedbackFormCard } from "@/features/feedback/components/feedback-form-card";
import type {
  FeedbackForm,
  FeedbackSubmitPayload,
  FeedbackSubmitResponse,
} from "@/features/feedback/types";
import { ParentPageIntro } from "../components/parent-page-intro";
import { ParentSurfaceCard } from "../components/parent-surface-card";
import { usePrivateQueryScope } from "@/features/auth/private-query-scope";

function ParentFeedbackSkeleton() {
  return (
    <div className="space-y-4 px-5 pb-24 pt-4">
      <Skeleton className="h-[44px] w-32 rounded-xl" />
      <Skeleton className="h-[72px] rounded-xl" />
      <Skeleton className="h-[220px] rounded-xl" />
    </div>
  );
}

export default function ParentFeedback() {
  const { scope: parentPrivateScope, isReady: parentPrivateReady } = usePrivateQueryScope("parent");
  const { childId } = useParams<{ childId: string }>();
  const navigate = useNavigate();

  const {
    data: form,
    isLoading,
    isError,
    refetch,
  } = useQuery<FeedbackForm | null>({
    queryKey: ["parent", "child", childId, "feedback", "form", ...parentPrivateScope],
    queryFn: () =>
      apiClient
        .get<FeedbackForm | null>(`/parents/children/${childId}/feedback/form/`)
        .then((response) => response.data),
    enabled: parentPrivateReady && !!childId,
    staleTime: 60_000,
  });

  if (!childId) {
    return <ParentFeedbackSkeleton />;
  }

  return (
    <div className="space-y-4 px-5 pb-24 pt-4">
      <button
        type="button"
        onClick={() => navigate(`/parent/child/${childId}`)}
        className="-ml-2 flex min-h-[44px] items-center gap-2 px-2 text-[14px] text-muted-foreground"
      >
        <ArrowLeft className="h-5 w-5" />
        Назад
      </button>

      <ParentPageIntro
        eyebrow="Опрос"
        title="Обратная связь"
        description="Ответ будет сохранён в карточке ребёнка и виден команде клуба."
      />

      <FeedbackFormCard
        form={form}
        isLoading={isLoading}
        isError={isError}
        onRetry={() => void refetch()}
        surface={ParentSurfaceCard}
        emptyTitle="Активного опроса сейчас нет"
        emptyDescription="Когда клуб откроет форму обратной связи, она появится здесь."
        onSubmit={(payload: FeedbackSubmitPayload) =>
          apiClient
            .post<FeedbackSubmitResponse>(
              `/parents/children/${childId}/feedback/submit/`,
              payload,
            )
            .then((response) => response.data)
        }
      />
    </div>
  );
}
