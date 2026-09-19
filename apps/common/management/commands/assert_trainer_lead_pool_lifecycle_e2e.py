from __future__ import annotations

import json
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.leads.models import LeadIntakeEvent, LeadLifecycleEvent
from apps.leads.selectors import get_leads
from apps.notifications.models import NotificationPreference
from apps.students.models import Student


class Command(BaseCommand):
    help = "Assert trainer lead pool lifecycle E2E side effects."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fixture",
            required=True,
            help="Path to fixture JSON from prepare_trainer_lead_pool_lifecycle_e2e.",
        )
        parser.add_argument(
            "--timeout-seconds",
            type=float,
            default=30,
            help="Seconds to poll for side effects before failing.",
        )

    def handle(self, *args, **options):
        fixture = self._load_fixture(Path(options["fixture"]).expanduser())
        timeout_seconds = max(float(options["timeout_seconds"]), 0)
        deadline = time.monotonic() + timeout_seconds

        while True:
            try:
                evidence = self._collect_evidence(fixture)
            except CommandError as exc:
                if time.monotonic() >= deadline:
                    raise CommandError(f"trainer lead pool lifecycle E2E assertion failed: {exc}") from exc
                time.sleep(0.5)
                continue

            self.stdout.write(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
            return

    def _load_fixture(self, path: Path) -> dict:
        if not path.exists():
            raise CommandError(f"fixture file not found: {path}")
        try:
            fixture = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandError(f"fixture file is not valid JSON: {path}") from exc

        required = {
            "fixture_id",
            "club_id",
            "foreign_club_id",
            "trainer",
            "other_trainer",
            "pool_lead",
            "conflict_lead",
            "loss_lead",
            "hidden_other_trainer_lead",
            "foreign_pool_lead",
            "expected",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_evidence(self, fixture: dict) -> dict:
        club_id = int(fixture["club_id"])
        foreign_club_id = int(fixture["foreign_club_id"])
        trainer_id = int(fixture["trainer"]["trainer_id"])
        trainer_user_id = int(fixture["trainer"]["user_id"])
        other_trainer_id = int(fixture["other_trainer"]["trainer_id"])
        other_trainer_user_id = int(fixture["other_trainer"]["user_id"])

        pool_lead = self._get_student(club_id=club_id, item=fixture["pool_lead"])
        conflict_lead = self._get_student(club_id=club_id, item=fixture["conflict_lead"])
        loss_lead = self._get_student(club_id=club_id, item=fixture["loss_lead"])
        hidden_other_trainer_lead = self._get_student(
            club_id=club_id,
            item=fixture["hidden_other_trainer_lead"],
        )
        foreign_pool_lead = self._get_student(
            club_id=foreign_club_id,
            item=fixture["foreign_pool_lead"],
        )

        self._assert_released_pool_lead(
            lead=pool_lead,
            fixture_item=fixture["pool_lead"],
            trainer_id=trainer_id,
            trainer_user_id=trainer_user_id,
            release_reason=fixture["expected"]["release_reason"],
        )
        self._assert_conflict_lead(
            lead=conflict_lead,
            fixture_item=fixture["conflict_lead"],
            other_trainer_id=other_trainer_id,
            other_trainer_user_id=other_trainer_user_id,
        )
        self._assert_lost_lead(
            lead=loss_lead,
            trainer_id=trainer_id,
            trainer_user_id=trainer_user_id,
            loss_reason=fixture["expected"]["loss_reason"],
        )

        if hidden_other_trainer_lead.assigned_trainer_id != other_trainer_id:
            raise CommandError("hidden other-trainer lead assignment changed")
        if foreign_pool_lead.club_id != foreign_club_id or foreign_pool_lead.assigned_trainer_id is not None:
            raise CommandError("foreign pool lead state changed unexpectedly")

        pool_ids = list(
            get_leads(club=pool_lead.club, scope="pool").values_list("id", flat=True)
        )
        mine_ids = list(
            get_leads(
                club=pool_lead.club,
                scope="mine",
                current_trainer_id=trainer_id,
            ).values_list("id", flat=True)
        )
        other_mine_ids = list(
            get_leads(
                club=pool_lead.club,
                scope="mine",
                current_trainer_id=other_trainer_id,
            ).values_list("id", flat=True)
        )
        all_ids = list(
            get_leads(club=pool_lead.club, scope="all").values_list("id", flat=True)
        )

        if pool_lead.id not in pool_ids:
            raise CommandError("released pool lead did not return to pool")
        if conflict_lead.id in pool_ids:
            raise CommandError("conflict lead leaked back into pool after other trainer claim")
        if loss_lead.id in mine_ids:
            raise CommandError("lost lead remains in trainer mine scope")
        if loss_lead.id in pool_ids:
            raise CommandError("lost lead remains in trainer pool scope")
        if loss_lead.id in all_ids:
            raise CommandError("lost lead remains in active lead all scope")
        if hidden_other_trainer_lead.id in mine_ids:
            raise CommandError("other trainer lead leaked into trainer mine scope")
        if foreign_pool_lead.id in pool_ids or foreign_pool_lead.id in mine_ids:
            raise CommandError("foreign club lead leaked into current club scope")
        if conflict_lead.id not in other_mine_ids:
            raise CommandError("other trainer cannot see claimed conflict lead")

        preference = NotificationPreference.objects.filter(user_id=trainer_user_id).first()
        if preference is None:
            raise CommandError("trainer notification preferences not found")
        expected_disabled_categories = fixture["expected"].get("profile_disabled_categories", [])
        if preference.disabled_categories != expected_disabled_categories:
            raise CommandError(
                "trainer notification disabled categories mismatch: "
                f"expected {expected_disabled_categories}, got {preference.disabled_categories}"
            )

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "pool": {
                "released_lead_id": pool_lead.id,
                "visible_pool_ids": pool_ids,
            },
            "mine": {
                "trainer_visible_ids": mine_ids,
                "other_trainer_visible_ids": other_mine_ids,
            },
            "conflict": {
                "lead_id": conflict_lead.id,
                "assigned_trainer_id": conflict_lead.assigned_trainer_id,
            },
            "lost": {
                "lead_id": loss_lead.id,
                "status": loss_lead.status,
                "lead_status": loss_lead.lead_status,
                "loss_reason": loss_lead.loss_reason,
                "visible_in_trainer_mine": loss_lead.id in mine_ids,
                "visible_in_pool": loss_lead.id in pool_ids,
                "visible_in_all": loss_lead.id in all_ids,
            },
            "profile": {
                "trainer_id": trainer_id,
                "disabled_categories": preference.disabled_categories,
            },
        }

    def _get_student(self, *, club_id: int, item: dict) -> Student:
        student = Student.objects.for_club(club_id).filter(
            id=int(item["id"]),
            deleted_at__isnull=True,
        ).first()
        if student is None:
            raise CommandError(f"student not found: {item['id']}")
        if student.first_name != item["first_name"]:
            raise CommandError(
                f"student first_name mismatch: expected {item['first_name']}, got {student.first_name}"
            )
        return student

    def _assert_released_pool_lead(
        self,
        *,
        lead: Student,
        fixture_item: dict,
        trainer_id: int,
        trainer_user_id: int,
        release_reason: str,
    ) -> None:
        self._assert_public_intake_event(lead=lead, fixture_item=fixture_item)
        if lead.status != Student.Status.LEAD:
            raise CommandError(f"released lead status mismatch: expected lead, got {lead.status}")
        if lead.lead_status != Student.LeadStatus.NEW:
            raise CommandError(
                f"released lead lead_status mismatch: expected new, got {lead.lead_status}"
            )
        if lead.assigned_trainer_id is not None:
            raise CommandError("released lead is still assigned")

        events = list(LeadLifecycleEvent.objects.for_club(lead.club_id).filter(student=lead))
        event_types = [event.event_type for event in events]
        expected_types = [
            LeadLifecycleEvent.EventType.LEAD_CLAIMED,
            LeadLifecycleEvent.EventType.LEAD_RELEASED,
        ]
        if event_types != expected_types:
            raise CommandError(f"released lead events mismatch: expected {expected_types}, got {event_types}")

        claimed, released = events
        if claimed.old_trainer_id is not None or claimed.new_trainer_id != trainer_id:
            raise CommandError("claim event trainer transition mismatch")
        if claimed.actor_id != trainer_user_id:
            raise CommandError("claim event actor mismatch")
        if released.old_trainer_id != trainer_id or released.new_trainer_id is not None:
            raise CommandError("release event trainer transition mismatch")
        if released.actor_id != trainer_user_id:
            raise CommandError("release event actor mismatch")
        if released.reason != release_reason:
            raise CommandError(f"release reason mismatch: expected {release_reason}, got {released.reason}")

    def _assert_conflict_lead(
        self,
        *,
        lead: Student,
        fixture_item: dict,
        other_trainer_id: int,
        other_trainer_user_id: int,
    ) -> None:
        self._assert_public_intake_event(lead=lead, fixture_item=fixture_item)
        if lead.assigned_trainer_id != other_trainer_id:
            raise CommandError("conflict lead was not claimed by other trainer")
        events = list(LeadLifecycleEvent.objects.for_club(lead.club_id).filter(student=lead))
        event_types = [event.event_type for event in events]
        if event_types != [LeadLifecycleEvent.EventType.LEAD_CLAIMED]:
            raise CommandError(f"conflict lead events mismatch: got {event_types}")
        event = events[0]
        if event.actor_id != other_trainer_user_id:
            raise CommandError("conflict claim actor mismatch")
        if event.old_trainer_id is not None or event.new_trainer_id != other_trainer_id:
            raise CommandError("conflict claim trainer transition mismatch")

    def _assert_lost_lead(
        self,
        *,
        lead: Student,
        trainer_id: int,
        trainer_user_id: int,
        loss_reason: str,
    ) -> None:
        if lead.status != Student.Status.LOST:
            raise CommandError(f"lost lead status mismatch: expected lost, got {lead.status}")
        if lead.lead_status is not None:
            raise CommandError(f"lost lead lead_status mismatch: expected null, got {lead.lead_status}")
        if lead.assigned_trainer_id != trainer_id:
            raise CommandError("lost lead trainer assignment changed")
        if lead.loss_reason != loss_reason:
            raise CommandError(f"lost lead reason mismatch: expected {loss_reason}, got {lead.loss_reason}")

        events = list(LeadLifecycleEvent.objects.for_club(lead.club_id).filter(student=lead))
        event_types = [event.event_type for event in events]
        if event_types != [LeadLifecycleEvent.EventType.LEAD_LOST]:
            raise CommandError(f"lost lead events mismatch: got {event_types}")
        event = events[0]
        if event.actor_id != trainer_user_id:
            raise CommandError("lost lead actor mismatch")
        if event.old_trainer_id != trainer_id or event.new_trainer_id != trainer_id:
            raise CommandError("lost lead trainer transition mismatch")
        if event.reason != loss_reason:
            raise CommandError(f"lost event reason mismatch: expected {loss_reason}, got {event.reason}")

    def _assert_public_intake_event(self, *, lead: Student, fixture_item: dict) -> None:
        event_id = fixture_item.get("intake_event_id")
        if not event_id:
            raise CommandError(f"fixture lead {fixture_item['id']} is missing intake_event_id")
        event = LeadIntakeEvent.objects.for_club(lead.club_id).filter(
            id=int(event_id),
            student=lead,
        ).first()
        if event is None:
            raise CommandError(f"lead intake event not found for lead {lead.id}")
        if lead.source != Student.Source.WEBSITE:
            raise CommandError(f"public intake lead source mismatch: expected website, got {lead.source}")
        if lead.status != Student.Status.LEAD:
            raise CommandError(f"public intake lead status mismatch: expected lead, got {lead.status}")
        if event.is_repeat_submission:
            raise CommandError("public intake event unexpectedly marked repeat submission")
        if not event.request_id or not event.client_ip_hash or not event.user_agent_hash:
            raise CommandError("public intake event missing safe diagnostic hashes")
