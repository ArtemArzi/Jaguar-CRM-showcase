/* eslint-disable react-refresh/only-export-components -- route module exports route objects */
import { lazy } from "react";
import type { RouteObject } from "react-router";

const StudentHome = lazy(() => import("./pages/student-home"));
const StudentSchedule = lazy(() => import("./pages/student-schedule"));
const StudentAttendance = lazy(() => import("./pages/student-attendance"));
const StudentProfile = lazy(() => import("./pages/student-profile"));
const StudentFeedback = lazy(() => import("./pages/student-feedback"));

export const studentRoutes: RouteObject[] = [
  { index: true, element: <StudentHome /> },
  { path: "schedule", element: <StudentSchedule /> },
  { path: "attendance", element: <StudentAttendance /> },
  { path: "feedback", element: <StudentFeedback /> },
  { path: "profile", element: <StudentProfile /> },
];
