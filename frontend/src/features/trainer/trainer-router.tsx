/* eslint-disable react-refresh/only-export-components -- route module exports route objects */
import { lazy } from "react";
import { Navigate, type RouteObject } from "react-router";
import { RouteErrorBoundary } from "@/components/error-boundary";

const ScheduleHome = lazy(() => import("./pages/schedule-home"));
const AvailabilityCalendar = lazy(() => import("./pages/availability-calendar"));
const BatchCheckin = lazy(() => import("./pages/batch-checkin"));
const Students = lazy(() => import("./pages/students"));
const StudentDetail = lazy(() => import("./pages/student-detail"));
const StudentForm = lazy(() => import("./pages/student-form"));
const Leads = lazy(() => import("./pages/leads"));
const Tasks = lazy(() => import("./pages/tasks"));
const TaskDetail = lazy(() => import("./pages/task-detail"));
const Profile = lazy(() => import("./pages/profile"));
const Salary = lazy(() => import("./pages/salary"));
const SalaryDetail = lazy(() => import("./pages/salary-detail"));

export const trainerRoutes: RouteObject[] = [
  {
    index: true,
    element: <ScheduleHome />,
    errorElement: <RouteErrorBoundary />,
  },
  {
    path: "schedule/:scheduleId/checkin",
    element: <BatchCheckin />,
    errorElement: <RouteErrorBoundary />,
  },
  {
    path: "availability",
    element: <AvailabilityCalendar />,
    errorElement: <RouteErrorBoundary />,
  },
  {
    path: "students",
    element: <Students />,
    errorElement: <RouteErrorBoundary />,
  },
  {
    path: "students/new",
    element: <Navigate to="/trainer/leads" replace />,
    errorElement: <RouteErrorBoundary />,
  },
  {
    path: "students/:studentId",
    element: <StudentDetail />,
    errorElement: <RouteErrorBoundary />,
  },
  {
    path: "students/:studentId/edit",
    element: <StudentForm />,
    errorElement: <RouteErrorBoundary />,
  },
  { path: "leads", element: <Leads />, errorElement: <RouteErrorBoundary /> },
  { path: "tasks", element: <Tasks />, errorElement: <RouteErrorBoundary /> },
  {
    path: "tasks/:taskId",
    element: <TaskDetail />,
    errorElement: <RouteErrorBoundary />,
  },
  {
    path: "profile",
    element: <Profile />,
    errorElement: <RouteErrorBoundary />,
  },
  {
    path: "profile/salary",
    element: <Salary />,
    errorElement: <RouteErrorBoundary />,
  },
  {
    path: "salary",
    element: <SalaryDetail />,
    errorElement: <RouteErrorBoundary />,
  },
];
