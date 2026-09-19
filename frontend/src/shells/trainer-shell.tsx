import { Suspense, useEffect } from "react";
import { Outlet } from "react-router";
import { BrandingProvider } from "@/features/branding/branding-provider";
import { BrandedHeader } from "@/features/trainer/components/branded-header";
import { BottomNav } from "@/features/trainer/components/bottom-nav";
import { QueryStateNotice } from "@/features/trainer/components/query-state-notice";
import {
  trainerIdentityIsMissing,
  useTrainerIdentity,
} from "@/features/trainer/hooks/use-trainer-identity";
import { useAuthStore } from "@/features/auth/auth-store";

function TrainerLoading() {
  return (
    <div className="flex items-center justify-center h-screen bg-[#F5F2ED]">
      Загружаем профиль тренера...
    </div>
  );
}

function TrainerIdentityGate() {
  const trainerId = useAuthStore((s) => s.trainerId);
  const setTrainerId = useAuthStore((s) => s.setTrainerId);
  const identity = useTrainerIdentity();
  const identityFailed = identity.isError || identity.isRefetchError;

  useEffect(() => {
    if (identity.data?.id !== undefined && trainerId !== identity.data.id) {
      setTrainerId(identity.data.id);
    }
  }, [identity.data?.id, setTrainerId, trainerId]);

  if (identity.isPending || (identity.data && trainerId !== identity.data.id)) {
    return <TrainerLoading />;
  }

  if (identityFailed || !identity.data) {
    const missingProfile = trainerIdentityIsMissing(identity.error);
    return (
      <div className="mx-auto flex min-h-[70vh] max-w-md items-center px-4 py-8">
        <QueryStateNotice
          title={
            missingProfile
              ? "Профиль тренера не привязан"
              : "Не удалось загрузить профиль тренера"
          }
          message={
            missingProfile
              ? "Обратитесь к владельцу или администратору клуба, чтобы они привязали профиль к вашему аккаунту."
              : "Разделы тренера временно недоступны. Проверьте соединение и повторите попытку."
          }
          retrying={identity.isFetching}
          onRetry={() => void identity.refetch()}
        />
      </div>
    );
  }

  return (
    <>
      <div className="pb-20">
        <Suspense fallback={<TrainerLoading />}>
          <Outlet />
        </Suspense>
      </div>
      <BottomNav />
    </>
  );
}

export default function TrainerShell() {
  return (
    <BrandingProvider>
      <div className="min-h-screen bg-[#F5F2ED]">
        <BrandedHeader />
        <TrainerIdentityGate />
      </div>
    </BrandingProvider>
  );
}
