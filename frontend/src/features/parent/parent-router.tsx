/* eslint-disable react-refresh/only-export-components -- route modules export route objects */
import { lazy } from "react";
import type { RouteObject } from "react-router";

const ParentHome = lazy(() => import("./pages/parent-home"));
const ParentChild = lazy(() => import("./pages/parent-child"));
const ParentFeedback = lazy(() => import("./pages/parent-feedback"));

export const parentRoutes: RouteObject[] = [
  { index: true, element: <ParentHome /> },
  { path: "child/:childId/feedback", element: <ParentFeedback /> },
  { path: "child/:childId", element: <ParentChild /> },
];
