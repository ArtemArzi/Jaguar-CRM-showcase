from __future__ import annotations

import json
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.clubs.models import Club
from apps.common.phone import normalize_phone
from apps.leads.models import LeadIntakeEvent
from apps.leads.selectors import get_leads
from apps.students.models import Student


class Command(BaseCommand):
    help = "Assert public lead intake E2E side effects."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fixture",
            required=True,
            help="Path to fixture JSON from prepare_public_lead_intake_e2e.",
        )
        parser.add_argument(
            "--timeout-seconds",
            type=float,
            default=30,
            help="Seconds to poll before failing.",
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
                    raise CommandError(f"public lead intake E2E assertion failed: {exc}") from exc
                time.sleep(0.5)
                continue

            self.stdout.write(json.dumps(evidence, ensure_ascii=False, sort_keys=True, default=str))
            return

    def _load_fixture(self, path: Path) -> dict:
        if not path.exists():
            raise CommandError(f"fixture file not found: {path}")
        try:
            fixture = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandError(f"fixture file is not valid JSON: {path}") from exc

        required = {"fixture_id", "club_id", "payloads", "expected"}
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_evidence(self, fixture: dict) -> dict:
        club = Club.objects.get(id=int(fixture["club_id"]))
        payloads = fixture["payloads"]
        expected = fixture["expected"]

        first_event = self._event_for_payload(club=club, payload=payloads["first"], label="first")
        repeat_event = self._event_for_payload(
            club=club,
            payload=payloads["repeat_same_phone"],
            label="repeat same-phone",
        )
        existing_active_event = self._event_for_payload(
            club=club,
            payload=payloads["existing_active"],
            label="existing active repeat",
        )
        existing_lost_event = self._event_for_payload(
            club=club,
            payload=payloads["existing_lost"],
            label="existing lost repeat",
        )
        invalid_key = payloads["invalid_consent"]["idempotency_key"]
        invalid_phone = normalize_phone(payloads["invalid_consent"]["phone"])

        if first_event.student_id != repeat_event.student_id:
            raise CommandError("repeat lead intake did not reuse the existing student")
        if first_event.is_repeat_submission:
            raise CommandError("first lead intake was unexpectedly marked repeat")
        if not repeat_event.is_repeat_submission:
            raise CommandError("repeat same-phone lead intake was not marked repeat")

        student = first_event.student
        if student.club_id != club.id:
            raise CommandError("lead student was created in the wrong club")
        if student.status != Student.Status.LEAD:
            raise CommandError(f"lead student status mismatch: got {student.status}")
        if student.lead_status != Student.LeadStatus.NEW:
            raise CommandError(f"lead student lead_status mismatch: got {student.lead_status}")
        if student.source != Student.Source.WEBSITE:
            raise CommandError(f"lead student source mismatch: got {student.source}")

        first_key_count = LeadIntakeEvent.objects.for_club(club).filter(
            idempotency_key=payloads["first"]["idempotency_key"],
        ).count()
        if first_key_count != 1:
            raise CommandError(f"first idempotency key count mismatch: expected 1, got {first_key_count}")

        if LeadIntakeEvent.objects.for_club(club).filter(idempotency_key=invalid_key).exists():
            raise CommandError("invalid consent lead intake created an event")
        if Student.objects.for_club(club).filter(phone=invalid_phone).exists():
            raise CommandError("invalid consent lead intake created a student")

        self._assert_event(
            event=first_event,
            payload=payloads["first"],
            preferred_format=expected["first_preferred_format"],
        )
        self._assert_event(
            event=repeat_event,
            payload=payloads["repeat_same_phone"],
            preferred_format=expected["repeat_preferred_format"],
        )
        existing_re_submits = {
            "active": self._assert_existing_re_submit(
                club=club,
                fixture=fixture,
                event=existing_active_event,
                payload=payloads["existing_active"],
                expected_key="active",
                preferred_format=expected["existing_active_preferred_format"],
            ),
            "lost": self._assert_existing_re_submit(
                club=club,
                fixture=fixture,
                event=existing_lost_event,
                payload=payloads["existing_lost"],
                expected_key="lost",
                preferred_format=expected["existing_lost_preferred_format"],
            ),
        }

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "student": {
                "id": student.id,
                "status": student.status,
                "lead_status": student.lead_status,
                "source": student.source,
            },
            "events": {
                "first": self._event_evidence(first_event),
                "repeat_same_phone": self._event_evidence(repeat_event),
                "first_idempotency_key_count": first_key_count,
            },
            "existing_re_submits": existing_re_submits,
            "invalid_consent": {
                "event_created": False,
                "student_created": False,
            },
        }

    def _event_for_payload(self, *, club: Club, payload: dict, label: str) -> LeadIntakeEvent:
        event = (
            LeadIntakeEvent.objects.for_club(club)
            .select_related("student")
            .filter(idempotency_key=payload["idempotency_key"])
            .first()
        )
        if event is None:
            raise CommandError(f"{label} lead intake event not found")
        return event

    def _assert_event(self, *, event: LeadIntakeEvent, payload: dict, preferred_format: str) -> None:
        if event.preferred_format != preferred_format:
            raise CommandError(
                f"lead intake preferred_format mismatch: expected {preferred_format}, got {event.preferred_format}"
            )
        if event.goal != payload["goal"]:
            raise CommandError("lead intake goal mismatch")
        if event.source_page != payload["source"]["page"]:
            raise CommandError("lead intake source page mismatch")
        if event.utm_source != payload["source"]["utm_source"]:
            raise CommandError("lead intake UTM source mismatch")
        if event.privacy_policy_version != payload["consent"]["privacy_policy_version"]:
            raise CommandError("lead intake privacy policy version mismatch")
        if event.consent_text_hash != payload["consent"]["consent_text_hash"]:
            raise CommandError("lead intake consent hash mismatch")
        if not event.request_id:
            raise CommandError("lead intake request id missing")
        if not event.client_ip_hash:
            raise CommandError("lead intake client IP hash missing")
        if not event.user_agent_hash:
            raise CommandError("lead intake user agent hash missing")
        if event.telegram_status not in {
            LeadIntakeEvent.TelegramStatus.PENDING,
            LeadIntakeEvent.TelegramStatus.SKIPPED,
        }:
            raise CommandError(f"unexpected Telegram delivery status: {event.telegram_status}")

    def _assert_existing_re_submit(
        self,
        *,
        club: Club,
        fixture: dict,
        event: LeadIntakeEvent,
        payload: dict,
        expected_key: str,
        preferred_format: str,
    ) -> dict:
        expected = fixture["existing_students"][expected_key]
        if event.student_id != expected["student_id"]:
            raise CommandError(f"existing {expected_key} repeat did not reuse the expected student")
        if not event.is_repeat_submission:
            raise CommandError(f"existing {expected_key} repeat was not marked repeat")

        self._assert_event(event=event, payload=payload, preferred_format=preferred_format)

        student = event.student
        if student.status != expected["status"]:
            raise CommandError(
                f"existing {expected_key} status changed: expected {expected['status']}, got {student.status}"
            )
        if student.lead_status != expected["lead_status"]:
            raise CommandError(
                "existing "
                f"{expected_key} lead_status changed: expected {expected['lead_status']}, got {student.lead_status}"
            )
        if student.assigned_trainer_id != expected["assigned_trainer_id"]:
            raise CommandError(f"existing {expected_key} assigned trainer changed")
        if "loss_reason" in expected and student.loss_reason != expected["loss_reason"]:
            raise CommandError(f"existing {expected_key} loss reason changed")

        if Student.objects.for_club(club).filter(phone=student.phone, deleted_at__isnull=True).count() != 1:
            raise CommandError(f"existing {expected_key} repeat created a duplicate student")

        trainer_id = int(fixture["trainer"]["trainer_id"])
        pool_ids = set(get_leads(club=club, scope="pool").values_list("id", flat=True))
        mine_ids = set(get_leads(club=club, scope="mine", current_trainer_id=trainer_id).values_list("id", flat=True))
        expected_in_pool = bool(expected.get("in_pool", False))
        expected_in_mine = bool(expected.get("in_trainer_mine", False))
        actual_in_pool = student.id in pool_ids
        actual_in_mine = student.id in mine_ids
        if actual_in_pool != expected_in_pool:
            raise CommandError(
                f"existing {expected_key} pool visibility mismatch: "
                f"expected {expected_in_pool}, got {actual_in_pool}"
            )
        if actual_in_mine != expected_in_mine:
            raise CommandError(
                f"existing {expected_key} trainer mine visibility mismatch: "
                f"expected {expected_in_mine}, got {actual_in_mine}"
            )

        return {
            "event": self._event_evidence(event),
            "student": {
                "id": student.id,
                "status": student.status,
                "lead_status": student.lead_status,
                "assigned_trainer_id": student.assigned_trainer_id,
                "loss_reason": student.loss_reason,
            },
            "in_pool": actual_in_pool,
            "in_trainer_mine": actual_in_mine,
        }

    def _event_evidence(self, event: LeadIntakeEvent) -> dict:
        return {
            "id": event.id,
            "student_id": event.student_id,
            "is_repeat_submission": event.is_repeat_submission,
            "preferred_format": event.preferred_format,
            "has_request_id": bool(event.request_id),
            "has_client_ip_hash": bool(event.client_ip_hash),
            "has_user_agent_hash": bool(event.user_agent_hash),
            "telegram_status": event.telegram_status,
        }
