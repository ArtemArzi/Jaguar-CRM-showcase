from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.attendance.models import Checkin, CheckinCascadeEvent, GroupSession
from apps.billing.models import Debt
from apps.clubs.models import ClubMembership


class Command(BaseCommand):
    help = "Assert owner/admin batch check-in guard, retry, and provenance E2E state."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fixture",
            required=True,
            help="Path to fixture JSON from prepare_owner_batch_checkin_e2e.",
        )

    def handle(self, *args, **options):
        fixture = self._load_fixture(Path(options["fixture"]).expanduser())
        evidence = self._build_evidence(fixture)
        self.stdout.write(json.dumps(evidence, ensure_ascii=False, sort_keys=True))

    def _load_fixture(self, path: Path) -> dict:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CommandError(f"Cannot read owner batch check-in fixture: {exc}") from exc

    def _build_evidence(self, fixture: dict) -> dict:
        club_id = int(fixture["club_id"])
        self._assert_roles(club_id=club_id, fixture=fixture)
        early = self._early_evidence(club_id=club_id, fixture=fixture)
        finished = self._finished_evidence(club_id=club_id, fixture=fixture)
        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "early": early,
            "finished": finished,
        }

    def _assert_roles(self, *, club_id: int, fixture: dict) -> None:
        for key, expected_role in (
            ("owner", ClubMembership.Role.OWNER),
            ("admin", ClubMembership.Role.ADMIN),
            ("trainer", ClubMembership.Role.TRAINER),
        ):
            role = ClubMembership.objects.get(
                club_id=club_id,
                user_id=int(fixture[key]["user_id"]),
            ).role
            if role != expected_role:
                raise CommandError(
                    f"{key} membership role mismatch: expected {expected_role}, got {role}"
                )

    def _early_evidence(self, *, club_id: int, fixture: dict) -> dict:
        expected = fixture["early"]
        target_date = date.fromisoformat(expected["date"])
        schedule_id = int(expected["schedule_id"])
        student_id = int(expected["student_id"])
        checkins = Checkin.objects.for_club(club_id).filter(
            schedule_id=schedule_id,
            date=target_date,
        )
        checkin_ids = list(checkins.values_list("id", flat=True))
        evidence = {
            "checkin_count": len(checkin_ids),
            "group_session_count": GroupSession.objects.for_club(club_id).filter(
                schedule_id=schedule_id,
                date=target_date,
            ).count(),
            "debt_count": Debt.objects.for_club(club_id).filter(student_id=student_id).count(),
            "cascade_event_count": CheckinCascadeEvent.objects.for_club(club_id).filter(
                checkin_id__in=checkin_ids,
            ).count(),
        }
        if any(evidence.values()):
            raise CommandError(f"early batch attempt left side effects: {evidence}")
        return evidence

    def _finished_evidence(self, *, club_id: int, fixture: dict) -> dict:
        expected = fixture["finished"]
        target_date = date.fromisoformat(expected["date"])
        schedule_id = int(expected["schedule_id"])
        student_id = int(expected["student_id"])
        session = GroupSession.objects.for_club(club_id).filter(
            schedule_id=schedule_id,
            date=target_date,
        ).first()
        if session is None:
            raise CommandError("finished batch session not found")

        checkins = Checkin.objects.for_club(club_id).filter(
            schedule_id=schedule_id,
            student_id=student_id,
            date=target_date,
            deleted_at__isnull=True,
            cancelled_at__isnull=True,
        )
        checkin_count = checkins.count()
        if checkin_count != 1:
            raise CommandError(
                f"finished batch check-in count mismatch: expected 1, got {checkin_count}"
            )
        checkin = checkins.get()
        expected_owner_id = int(fixture["owner"]["user_id"])
        expected_notes = fixture["expected"]["admin_notes"]
        expected_tags = fixture["expected"]["admin_topic_tags"]
        if session.closed_at is None:
            raise CommandError("finished batch session has no closed_at")
        if session.closed_by_id != expected_owner_id:
            raise CommandError(
                "finished batch closed_by mismatch: "
                f"expected {expected_owner_id}, got {session.closed_by_id}"
            )
        if session.close_source != GroupSession.CloseSource.BATCH:
            raise CommandError(
                "finished batch close_source mismatch: "
                f"expected {GroupSession.CloseSource.BATCH}, got {session.close_source}"
            )
        if session.notes != expected_notes or session.topic_tags != expected_tags:
            raise CommandError("finished batch editable correction fields mismatch")
        if session.attendee_count != 1:
            raise CommandError(
                f"finished batch attendee_count mismatch: expected 1, got {session.attendee_count}"
            )
        if checkin.source != Checkin.Source.BATCH:
            raise CommandError(
                f"finished check-in source mismatch: expected {Checkin.Source.BATCH}, got {checkin.source}"
            )

        debt_count = Debt.objects.for_club(club_id).filter(
            student_id=student_id,
            checkin=checkin,
        ).count()
        cascade_event_count = CheckinCascadeEvent.objects.for_club(club_id).filter(
            checkin=checkin,
        ).count()
        if debt_count != 1:
            raise CommandError(
                f"finished batch debt count mismatch: expected 1, got {debt_count}"
            )
        if cascade_event_count != 6:
            raise CommandError(
                "finished batch cascade event count mismatch: "
                f"expected 6, got {cascade_event_count}"
            )

        return {
            "group_session_id": session.id,
            "closed_at": session.closed_at.isoformat(),
            "closed_by_id": session.closed_by_id,
            "close_source": session.close_source,
            "checkin_count": checkin_count,
            "checkin_id": checkin.id,
            "debt_count": debt_count,
            "cascade_event_count": cascade_event_count,
            "notes": session.notes,
            "topic_tags": session.topic_tags,
        }
