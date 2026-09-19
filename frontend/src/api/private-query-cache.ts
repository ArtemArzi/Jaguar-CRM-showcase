import type { QueryClient } from "@tanstack/react-query";

/** Marker lets auth transitions remove only data that must never cross an actor boundary. */
export const PRIVATE_QUERY_SCOPE_MARKER = "private-actor-scope";

export type PrivateQueryAudience = "parent" | "student" | "staff";

export type PrivateQueryScope = readonly [
  typeof PRIVATE_QUERY_SCOPE_MARKER,
  number | "no-club",
  string | "anonymous",
  PrivateQueryAudience,
  string | "no-role",
];

const registeredQueryClientRefs = new Map<QueryClient, number>();

export function privateQueryScope({
  clubId,
  actorSubject,
  audience,
  role,
}: {
  readonly clubId: number | null;
  readonly actorSubject: string | null;
  readonly audience: PrivateQueryAudience;
  readonly role: string | null;
}): PrivateQueryScope {
  return [
    PRIVATE_QUERY_SCOPE_MARKER,
    typeof clubId === "number" && Number.isSafeInteger(clubId) && clubId > 0
      ? clubId
      : "no-club",
    actorSubject ?? "anonymous",
    audience,
    role ?? "no-role",
  ];
}

export function isPrivateQueryKey(queryKey: readonly unknown[]) {
  return queryKey.includes(PRIVATE_QUERY_SCOPE_MARKER);
}

export function registerPrivateQueryClient(queryClient: QueryClient) {
  registeredQueryClientRefs.set(
    queryClient,
    (registeredQueryClientRefs.get(queryClient) ?? 0) + 1,
  );
  let released = false;
  return () => {
    if (released) return;
    released = true;
    const remaining = (registeredQueryClientRefs.get(queryClient) ?? 1) - 1;
    if (remaining > 0) {
      registeredQueryClientRefs.set(queryClient, remaining);
      return;
    }
    registeredQueryClientRefs.delete(queryClient);
  };
}

export function removePrivateQueries(queryClient: QueryClient) {
  queryClient.removeQueries({
    predicate: (query) => isPrivateQueryKey(query.queryKey),
  });
}

/** Called directly by auth transitions, before the next actor can render cached private data. */
export function purgePrivateQueryCaches() {
  registeredQueryClientRefs.forEach((_, queryClient) => removePrivateQueries(queryClient));
}
