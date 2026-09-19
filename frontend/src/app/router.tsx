/* eslint-disable react-refresh/only-export-components -- route module exports the app router */
import { createBrowserRouter, Navigate } from "react-router";
import { lazy, Suspense } from "react";
import { AuthGuard } from "@/features/auth/auth-guard";
import { RouteErrorBoundary } from "@/components/error-boundary";
import { trainerRoutes } from "@/features/trainer/trainer-router";
import { studentRoutes } from "@/features/student/student-router";
import { parentRoutes } from "@/features/parent/parent-router";

const LoginPage = lazy(() => import("@/features/auth/login-page"));
const AppEntryPage = lazy(() => import("@/features/auth/app-entry-page"));
const ParentInvitePage = lazy(() => import("@/features/parent/pages/parent-invite"));
const PaymentReturnPage = lazy(() => import("@/features/payments/payment-return-page"));
const KioskShell = lazy(() => import("@/shells/kiosk-shell"));
const TrainerShell = lazy(() => import("@/shells/trainer-shell"));
const StudentShell = lazy(() => import("@/shells/student-shell"));
const ParentShell = lazy(() => import("@/shells/parent-shell"));

const Loading = () => (
  <div className="flex flex-col items-center justify-center h-screen gap-4">
    <div className="h-8 w-48 animate-pulse rounded-lg bg-neutral-200" />
    <div className="h-4 w-32 animate-pulse rounded bg-neutral-200" />
  </div>
);

export const router = createBrowserRouter([
  {
    path: "/app",
    element: (
      <Suspense fallback={<Loading />}>
        <AppEntryPage />
      </Suspense>
    ),
    errorElement: <RouteErrorBoundary />,
  },
  {
    path: "/login",
    element: (
      <Suspense fallback={<Loading />}>
        <LoginPage />
      </Suspense>
    ),
    errorElement: <RouteErrorBoundary />,
  },
  {
    path: "/parent-invite/:token",
    element: (
      <Suspense fallback={<Loading />}>
        <ParentInvitePage />
      </Suspense>
    ),
    errorElement: <RouteErrorBoundary />,
  },
  {
    path: "/payments/return",
    element: (
      <Suspense fallback={<Loading />}>
        <PaymentReturnPage />
      </Suspense>
    ),
    errorElement: <RouteErrorBoundary />,
  },
  {
    path: "/kiosk/*",
    element: (
      <Suspense fallback={<Loading />}>
        <KioskShell />
      </Suspense>
    ),
    errorElement: <RouteErrorBoundary />,
  },
  {
    path: "/trainer",
    element: (
      <Suspense fallback={<Loading />}>
        <AuthGuard role="trainer">
          <TrainerShell />
        </AuthGuard>
      </Suspense>
    ),
    children: trainerRoutes,
    errorElement: <RouteErrorBoundary />,
  },
  {
    path: "/student",
    element: (
      <Suspense fallback={<Loading />}>
        <AuthGuard role="student">
          <StudentShell />
        </AuthGuard>
      </Suspense>
    ),
    children: studentRoutes,
    errorElement: <RouteErrorBoundary />,
  },
  {
    path: "/parent",
    element: (
      <Suspense fallback={<Loading />}>
        <AuthGuard role="parent">
          <ParentShell />
        </AuthGuard>
      </Suspense>
    ),
    children: parentRoutes,
    errorElement: <RouteErrorBoundary />,
  },
  { path: "*", element: <Navigate to="/app" replace /> },
]);
