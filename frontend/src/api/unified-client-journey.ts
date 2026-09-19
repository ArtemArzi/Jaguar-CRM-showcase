import { useQuery } from "@tanstack/react-query";
import apiClient from "@/api/custom-fetch";
import { privateQueryScope } from "@/api/private-query-cache";
import { getAuthTokenSubject, useAuthStore } from "@/features/auth/auth-store";

interface UnifiedClientJourneyCapability {
  enabled: boolean;
  group_sale_command_protocol_version?: "v1" | "v2" | "invalid";
}

interface PersonalAvailabilityCapability extends UnifiedClientJourneyCapability {
  /** Server-owned selector for the frozen staff mutation routes. */
  staff_command_protocol_version: "v1" | "v2" | "invalid";
}

export const unifiedClientJourneyCapabilityQueryKey = [
  "students",
  "intakes",
  "capability",
] as const;

export const personalAvailabilityCapabilityQueryKey = [
  "personal-availability",
  "capability",
] as const;

function isPositiveSafeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}

export type UnifiedClientJourneyCapabilityMode = "unified" | "legacy" | "unavailable";

export function getUnifiedClientJourneyCapabilityQueryKey(clubId: number | null) {
  return [
    ...unifiedClientJourneyCapabilityQueryKey,
    isPositiveSafeInteger(clubId) ? clubId : "no-club",
  ] as const;
}

export function getUnifiedClientJourneyCapabilityMode(query: {
  data?: unknown;
  isSuccess: boolean;
  isRefetchError?: boolean;
}): UnifiedClientJourneyCapabilityMode {
  if (!query.isSuccess || query.isRefetchError) return "unavailable";
  const enabled = (query.data as Partial<UnifiedClientJourneyCapability> | undefined)?.enabled;
  if (enabled === true) return "unified";
  if (enabled === false) return "legacy";
  return "unavailable";
}

/** A missing or stale group protocol must fail closed instead of choosing v1. */
export function getGroupSaleCommandProtocol(query: {
  data?: unknown;
  isSuccess: boolean;
  isRefetchError?: boolean;
}): "v1" | "v2" | null {
  if (!query.isSuccess || query.isRefetchError) return null;
  const protocol = (query.data as Partial<UnifiedClientJourneyCapability> | undefined)
    ?.group_sale_command_protocol_version;
  return protocol === "v1" || protocol === "v2" ? protocol : null;
}

export function getPersonalAvailabilityCapabilityQueryKey(
  clubId: number | null,
  accessToken: string | null = null,
  role: string | null = null,
) {
  return [
    ...personalAvailabilityCapabilityQueryKey,
    privateQueryScope({
      clubId,
      actorSubject: getAuthTokenSubject(accessToken),
      audience: role === "parent" ? "parent" : role === "student" ? "student" : "staff",
      role,
    }),
  ] as const;
}

export type PersonalAvailabilityCapabilityMode = "unified" | "legacy" | "unavailable";

export function getPersonalAvailabilityCapabilityMode(query: {
  data?: unknown;
  isSuccess: boolean;
  isRefetchError?: boolean;
}): PersonalAvailabilityCapabilityMode {
  if (!query.isSuccess || query.isRefetchError) return "unavailable";
  const enabled = (query.data as Partial<UnifiedClientJourneyCapability> | undefined)?.enabled;
  if (enabled === true) return "unified";
  if (enabled === false) return "legacy";
  return "unavailable";
}

/** A missing or stale route selector must never make a new staff write choose v1. */
export function getPersonalStaffCommandProtocol(query: {
  data?: unknown;
  isSuccess: boolean;
  isRefetchError?: boolean;
}): "v1" | "v2" | null {
  if (!query.isSuccess || query.isRefetchError) return null;
  const protocol = (query.data as Partial<PersonalAvailabilityCapability> | undefined)
    ?.staff_command_protocol_version;
  return protocol === "v1" || protocol === "v2" ? protocol : null;
}

export function useUnifiedClientJourneyCapabilityQuery() {
  const clubId = useAuthStore((state) => state.clubId);
  return useQuery({
    queryKey: getUnifiedClientJourneyCapabilityQueryKey(clubId),
    queryFn: async () =>
      (
        await apiClient.get<UnifiedClientJourneyCapability>(
          "/students/intakes/capability",
        )
      ).data,
    staleTime: 60_000,
    retry: false,
  });
}

export function useUnifiedClientJourneyCapability(): boolean {
  return getUnifiedClientJourneyCapabilityMode(useUnifiedClientJourneyCapabilityQuery()) === "unified";
}

export function usePersonalAvailabilityCapabilityQuery() {
  const clubId = useAuthStore((state) => state.clubId);
  const accessToken = useAuthStore((state) => state.accessToken);
  const role = useAuthStore((state) => state.role);
  return useQuery({
    queryKey: getPersonalAvailabilityCapabilityQueryKey(clubId, accessToken, role),
    queryFn: async () =>
      (
        await apiClient.get<PersonalAvailabilityCapability>(
          "/personal-availability/capability/",
        )
      ).data,
    staleTime: 60_000,
    retry: false,
    enabled: isPositiveSafeInteger(clubId),
  });
}

export function usePersonalAvailabilityCapability(): boolean {
  const query = usePersonalAvailabilityCapabilityQuery();
  return getPersonalAvailabilityCapabilityMode(query) === "unified";
}
