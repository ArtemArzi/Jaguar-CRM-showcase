import { Suspense } from "react";
import { Outlet } from "react-router";
import { BrandingProvider } from "@/features/branding/branding-provider";
import { ParentBottomNav } from "@/features/parent/components/parent-bottom-nav";
import { ParentBrandedHeader } from "@/features/parent/components/parent-branded-header";
import { ParentSurfaceCard } from "@/features/parent/components/parent-surface-card";

function ParentLoading() {
  return (
    <div className="flex min-h-[40vh] items-center justify-center px-4">
      <ParentSurfaceCard className="w-full max-w-md p-5 text-center text-[14px] text-muted-foreground">
        Loading...
      </ParentSurfaceCard>
    </div>
  );
}

export default function ParentShell() {
  return (
    <BrandingProvider>
      <div className="parent-shell-bg min-h-screen">
        <ParentBrandedHeader />
        <div className="pb-24">
          <main
            aria-label="Основной контент кабинета родителя"
            className="mx-auto flex min-h-[calc(100vh-4.5rem)] w-full max-w-screen-sm flex-col pb-10"
          >
            <Suspense fallback={<ParentLoading />}>
              <Outlet />
            </Suspense>
          </main>
        </div>
        <ParentBottomNav />
      </div>
    </BrandingProvider>
  );
}
