/** Role-to-route mapping for post-login redirect and auth guard */
export const ROLE_ROUTES: Record<string, string> = {
  trainer: "/trainer/",
  student: "/student/",
  parent: "/parent/",
  owner: "/dashboard/login/",
  admin: "/dashboard/login/",
};
