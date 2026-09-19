"""Stable evidence contract for the manual operational-admission owner.

The owner lives in the lead-lifecycle service module so person mutation remains
there.  Callers pass this small immutable object instead of extending the
legacy student-id-only compatibility helper.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class ManualOperationalAdmissionEvidence:
    club_id: int
    student_id: int
    payment_id: int
    origin: Literal["group", "personal"]
    actor_user_id: int


class ManualOperationalAdmissionOwner(Protocol):
    def admit(self, *, evidence: ManualOperationalAdmissionEvidence) -> object:
        """Validate exact durable evidence before any person lifecycle mutation."""
