import { useEffect, useMemo } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  privateQueryScope,
  registerPrivateQueryClient,
  type PrivateQueryAudience,
} from "@/api/private-query-cache";
import { getAuthTokenSubject, useAuthStore } from "./auth-store";

export function usePrivateQueryScope(audience: PrivateQueryAudience) {
  const queryClient = useQueryClient();
  const accessToken = useAuthStore((state) => state.accessToken);
  const clubId = useAuthStore((state) => state.clubId);
  const isAuthenticated = useAuthStore((state) => state.isAuthenticated);
  const role = useAuthStore((state) => state.role);
  const actorSubject = getAuthTokenSubject(accessToken);

  useEffect(() => registerPrivateQueryClient(queryClient), [queryClient]);

  const scope = useMemo(
    () => privateQueryScope({ clubId, actorSubject, audience, role }),
    [actorSubject, audience, clubId, role],
  );
  const isReady =
    isAuthenticated &&
    actorSubject !== null &&
    typeof clubId === "number" &&
    Number.isSafeInteger(clubId) &&
    clubId > 0 &&
    (audience === "staff" ? role !== null && role !== "student" && role !== "parent" : role === audience);

  return { scope, isReady };
}
