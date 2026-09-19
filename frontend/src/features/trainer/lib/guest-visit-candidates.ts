import type { GuestVisitCandidate, StudentWithAlerts } from "../types";

const GUEST_VISIT_STATUSES = new Set(["active", "lead", "trial", "at_risk", "churned"]);

export function filterGuestVisitCandidates({
  candidates,
  rosterStudents,
}: {
  candidates: readonly GuestVisitCandidate[];
  rosterStudents: readonly StudentWithAlerts[];
}): GuestVisitCandidate[] {
  const rosterIds = new Set(rosterStudents.map((student) => student.id));

  return candidates
    .filter((student) => GUEST_VISIT_STATUSES.has(student.status))
    .filter((student) => !rosterIds.has(student.id))
    .slice(0, 20);
}
