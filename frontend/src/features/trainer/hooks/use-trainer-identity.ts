import { useQuery } from "@tanstack/react-query";
import apiClient from "@/api/custom-fetch";
import { useAuthStore } from "@/features/auth/auth-store";
import { decodeJwtPayload } from "@/lib/jwt";

export interface TrainerIdentity {
  readonly id: number;
  readonly first_name?: string;
  readonly last_name?: string;
  readonly student_count?: number;
}

function identitySubject(accessToken: string | null): string {
  const subject = accessToken ? decodeJwtPayload(accessToken)?.sub : null;
  return typeof subject === "string" || typeof subject === "number"
    ? String(subject)
    : "unknown";
}

export function useTrainerIdentity() {
  const accessToken = useAuthStore((state) => state.accessToken);
  const role = useAuthStore((state) => state.role);
  const clubId = useAuthStore((state) => state.clubId);
  const subject = identitySubject(accessToken);

  return useQuery<TrainerIdentity>({
    queryKey: ["trainer", "me", "auth-context", role, clubId, subject],
    queryFn: ({ signal }) =>
      apiClient
        .get<TrainerIdentity>("/trainers/me/", { signal })
        .then((response) => response.data),
    enabled: role === "trainer" && clubId !== null,
    staleTime: 5 * 60_000,
    refetchOnMount: "always",
  });
}

export function trainerIdentityIsMissing(error: unknown): boolean {
  if (!error || typeof error !== "object") return false;
  const response = "response" in error ? error.response : undefined;
  if (!response || typeof response !== "object") return false;
  return "status" in response && response.status === 404;
}
