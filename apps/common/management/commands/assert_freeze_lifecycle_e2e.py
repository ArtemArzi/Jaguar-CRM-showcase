from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.test import Client
from django.utils import timezone

from apps.billing.models import Subscription, SubscriptionFreeze


class Command(BaseCommand):
    help = "Assert owner/admin freeze lifecycle E2E side effects from a prepared fixture JSON."

    def add_arguments(self, parser):
        parser.add_argument("--fixture", required=True, help="Path to fixture JSON from prepare_freeze_lifecycle_e2e.")
        parser.add_argument(
            "--timeout-seconds",
            type=float,
            default=30,
            help="Seconds to poll for freeze lifecycle side effects before failing.",
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
                    raise CommandError(f"freeze lifecycle E2E assertion failed: {exc}") from exc
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
            "owner",
            "approve_subscription_id",
            "approve_freeze_id",
            "reject_subscription_id",
            "reject_freeze_id",
            "unfreeze_subscription_id",
            "unfreeze_freeze_id",
            "trainer_user_id",
            "student_user_id",
            "parent_user_id",
            "denial_subscription_id",
            "denial_freeze_id",
            "denial_unfreeze_subscription_id",
            "denial_unfreeze_freeze_id",
            "expected",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_evidence(self, fixture: dict) -> dict:
        club_id = int(fixture["club_id"])
        owner_user_id = int(fixture["owner"]["user_id"])
        expected = fixture["expected"]

        approve_subscription = Subscription.objects.for_club(club_id).get(id=fixture["approve_subscription_id"])
        approve_freeze = SubscriptionFreeze.objects.for_club(club_id).get(id=fixture["approve_freeze_id"])
        reject_subscription = Subscription.objects.for_club(club_id).get(id=fixture["reject_subscription_id"])
        reject_freeze = SubscriptionFreeze.objects.for_club(club_id).get(id=fixture["reject_freeze_id"])
        unfreeze_subscription = Subscription.objects.for_club(club_id).get(id=fixture["unfreeze_subscription_id"])
        unfreeze_freeze = SubscriptionFreeze.objects.for_club(club_id).get(id=fixture["unfreeze_freeze_id"])
        denial_subscription = Subscription.objects.for_club(club_id).get(id=fixture["denial_subscription_id"])
        denial_freeze = SubscriptionFreeze.objects.for_club(club_id).get(id=fixture["denial_freeze_id"])
        denial_unfreeze_subscription = Subscription.objects.for_club(club_id).get(
            id=fixture["denial_unfreeze_subscription_id"]
        )
        denial_unfreeze_freeze = SubscriptionFreeze.objects.for_club(club_id).get(
            id=fixture["denial_unfreeze_freeze_id"]
        )

        self._assert_approved(
            subscription=approve_subscription,
            freeze=approve_freeze,
            expected=expected,
            owner_user_id=owner_user_id,
        )
        self._assert_rejected(
            subscription=reject_subscription,
            freeze=reject_freeze,
            expected=expected,
            owner_user_id=owner_user_id,
        )
        self._assert_unfrozen(
            subscription=unfreeze_subscription,
            freeze=unfreeze_freeze,
            expected=expected,
        )
        non_management_denials = self._assert_non_management_denied(
            fixture=fixture,
            subscription=denial_subscription,
            freeze=denial_freeze,
            unfreeze_subscription=denial_unfreeze_subscription,
            unfreeze_freeze=denial_unfreeze_freeze,
            expected=expected,
        )

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "approve_freeze": self._freeze_evidence(approve_freeze),
            "approve_subscription": self._subscription_evidence(approve_subscription),
            "reject_freeze": self._freeze_evidence(reject_freeze),
            "reject_subscription": self._subscription_evidence(reject_subscription),
            "unfreeze_freeze": self._freeze_evidence(unfreeze_freeze),
            "unfreeze_subscription": self._subscription_evidence(unfreeze_subscription),
            "non_management_denials": non_management_denials,
        }

    def _assert_approved(
        self,
        *,
        subscription: Subscription,
        freeze: SubscriptionFreeze,
        expected: dict,
        owner_user_id: int,
    ) -> None:
        if freeze.status != SubscriptionFreeze.FreezeStatus.APPROVED:
            raise CommandError(f"approve freeze not approved: got {freeze.status}")
        if freeze.approved_by_id != owner_user_id:
            raise CommandError(f"approve freeze actor mismatch: expected {owner_user_id}, got {freeze.approved_by_id}")
        if freeze.rejected_by_id is not None:
            raise CommandError(f"approve freeze unexpectedly has rejected_by={freeze.rejected_by_id}")
        if freeze.decision_at is None:
            raise CommandError("approve freeze decision_at is not set")
        if freeze.decision_reason:
            raise CommandError(f"approve freeze decision_reason should be blank, got {freeze.decision_reason!r}")
        if freeze.days != int(expected["approve_days"]):
            raise CommandError(f"approve freeze days mismatch: expected {expected['approve_days']}, got {freeze.days}")
        if subscription.status != Subscription.Status.FROZEN:
            raise CommandError(f"approve subscription not frozen: got {subscription.status}")
        expected_expires = self._parse_dt(expected["approve_expires_at_before"]) + timezone.timedelta(
            days=int(expected["approve_days"])
        )
        if subscription.expires_at != expected_expires:
            raise CommandError(
                f"approve subscription expires_at mismatch: expected {expected_expires}, got {subscription.expires_at}"
            )

    def _assert_rejected(
        self,
        *,
        subscription: Subscription,
        freeze: SubscriptionFreeze,
        expected: dict,
        owner_user_id: int,
    ) -> None:
        if freeze.status != SubscriptionFreeze.FreezeStatus.REJECTED:
            raise CommandError(f"reject freeze not rejected: got {freeze.status}")
        if freeze.rejected_by_id != owner_user_id:
            raise CommandError(f"reject freeze actor mismatch: expected {owner_user_id}, got {freeze.rejected_by_id}")
        if freeze.approved_by_id is not None:
            raise CommandError(f"reject freeze unexpectedly has approved_by={freeze.approved_by_id}")
        if freeze.decision_at is None:
            raise CommandError("reject freeze decision_at is not set")
        if freeze.decision_reason != expected["reject_decision_reason"]:
            raise CommandError(
                f"reject freeze decision_reason mismatch: expected {expected['reject_decision_reason']!r}, "
                f"got {freeze.decision_reason!r}"
            )
        if subscription.status != Subscription.Status.ACTIVE:
            raise CommandError(f"reject subscription status mismatch: expected active, got {subscription.status}")
        expected_expires = self._parse_dt(expected["reject_expires_at_before"])
        if subscription.expires_at != expected_expires:
            raise CommandError(
                f"reject subscription expires_at changed: expected {expected_expires}, got {subscription.expires_at}"
            )

    def _assert_unfrozen(
        self,
        *,
        subscription: Subscription,
        freeze: SubscriptionFreeze,
        expected: dict,
    ) -> None:
        if freeze.status != SubscriptionFreeze.FreezeStatus.APPROVED:
            raise CommandError(f"unfreeze freeze status mismatch: got {freeze.status}")
        if freeze.ends_at is None:
            raise CommandError("unfreeze freeze ends_at is not set")
        if freeze.days != int(expected["unfreeze_actual_days_after"]):
            raise CommandError(
                f"unfreeze actual days mismatch: expected {expected['unfreeze_actual_days_after']}, got {freeze.days}"
            )
        if subscription.status != Subscription.Status.ACTIVE:
            raise CommandError(f"unfreeze subscription not active: got {subscription.status}")
        expected_expires = self._parse_dt(expected["unfreeze_expires_at_after"])
        if subscription.expires_at != expected_expires:
            raise CommandError(
                f"unfreeze subscription expires_at mismatch: expected {expected_expires}, got {subscription.expires_at}"
            )

    def _assert_non_management_denied(
        self,
        *,
        fixture: dict,
        subscription: Subscription,
        freeze: SubscriptionFreeze,
        unfreeze_subscription: Subscription,
        unfreeze_freeze: SubscriptionFreeze,
        expected: dict,
    ) -> dict:
        role_user_ids = {
            "trainer": int(fixture["trainer_user_id"]),
            "student": int(fixture["student_user_id"]),
            "parent": int(fixture["parent_user_id"]),
        }
        user_model = get_user_model()
        role_statuses: dict[str, dict[str, int]] = {}

        for role, user_id in role_user_ids.items():
            client = Client()
            client.force_login(user_model.objects.get(id=user_id))
            approve_response = client.post(
                f"/dashboard/billing/freezes/{freeze.id}/approve/",
                HTTP_HX_REQUEST="true",
            )
            reject_response = client.post(
                f"/dashboard/billing/freezes/{freeze.id}/reject/",
                {"decision_reason": f"{role} denial probe"},
                HTTP_HX_REQUEST="true",
            )
            unfreeze_response = client.post(
                f"/dashboard/billing/subscriptions/{unfreeze_subscription.id}/unfreeze/",
                HTTP_HX_REQUEST="true",
            )

            role_statuses[role] = {
                "approve_status": approve_response.status_code,
                "reject_status": reject_response.status_code,
                "unfreeze_status": unfreeze_response.status_code,
            }
            for action, response in (
                ("approve", approve_response),
                ("reject", reject_response),
                ("unfreeze", unfreeze_response),
            ):
                if response.status_code != 403:
                    raise CommandError(
                        f"{role} non-management {action} expected 403, got {response.status_code}"
                    )

        subscription.refresh_from_db()
        freeze.refresh_from_db()
        unfreeze_subscription.refresh_from_db()
        unfreeze_freeze.refresh_from_db()

        if freeze.status != SubscriptionFreeze.FreezeStatus.PENDING:
            raise CommandError(f"denial freeze status changed: got {freeze.status}")
        if freeze.approved_by_id is not None:
            raise CommandError(f"denial freeze unexpectedly has approved_by={freeze.approved_by_id}")
        if freeze.rejected_by_id is not None:
            raise CommandError(f"denial freeze unexpectedly has rejected_by={freeze.rejected_by_id}")
        if freeze.decision_at is not None:
            raise CommandError("denial freeze unexpectedly has decision_at")
        if freeze.decision_reason:
            raise CommandError(f"denial freeze decision_reason should be blank, got {freeze.decision_reason!r}")
        if freeze.days != int(expected["denial_days"]):
            raise CommandError(f"denial freeze days mismatch: expected {expected['denial_days']}, got {freeze.days}")
        if subscription.status != Subscription.Status.ACTIVE:
            raise CommandError(f"denial subscription status changed: expected active, got {subscription.status}")
        expected_denial_expires = self._parse_dt(expected["denial_expires_at_before"])
        if subscription.expires_at != expected_denial_expires:
            raise CommandError(
                "denial subscription expires_at changed: "
                f"expected {expected_denial_expires}, got {subscription.expires_at}"
            )

        if unfreeze_freeze.status != SubscriptionFreeze.FreezeStatus.APPROVED:
            raise CommandError(f"denial unfreeze freeze status changed: got {unfreeze_freeze.status}")
        if unfreeze_freeze.ends_at is not None:
            raise CommandError("denial unfreeze freeze unexpectedly has ends_at")
        if unfreeze_freeze.days != int(expected["denial_unfreeze_days"]):
            raise CommandError(
                "denial unfreeze days changed: "
                f"expected {expected['denial_unfreeze_days']}, got {unfreeze_freeze.days}"
            )
        if unfreeze_subscription.status != Subscription.Status.FROZEN:
            raise CommandError(
                f"denial unfreeze subscription status changed: expected frozen, got {unfreeze_subscription.status}"
            )
        expected_unfreeze_expires = self._parse_dt(expected["denial_unfreeze_expires_at_before"])
        if unfreeze_subscription.expires_at != expected_unfreeze_expires:
            raise CommandError(
                "denial unfreeze subscription expires_at changed: "
                f"expected {expected_unfreeze_expires}, got {unfreeze_subscription.expires_at}"
            )

        return {
            "roles": role_statuses,
            "denial_freeze": self._freeze_evidence(freeze),
            "denial_subscription": self._subscription_evidence(subscription),
            "denial_unfreeze_freeze": self._freeze_evidence(unfreeze_freeze),
            "denial_unfreeze_subscription": self._subscription_evidence(unfreeze_subscription),
        }

    def _freeze_evidence(self, freeze: SubscriptionFreeze) -> dict:
        return {
            "id": freeze.id,
            "status": freeze.status,
            "days": freeze.days,
            "reason": freeze.reason,
            "approved_by_id": freeze.approved_by_id,
            "rejected_by_id": freeze.rejected_by_id,
            "decision_at": freeze.decision_at.isoformat() if freeze.decision_at else None,
            "decision_reason": freeze.decision_reason,
            "starts_at": freeze.starts_at.isoformat(),
            "ends_at": freeze.ends_at.isoformat() if freeze.ends_at else None,
            "ended": freeze.ends_at is not None,
        }

    def _subscription_evidence(self, subscription: Subscription) -> dict:
        return {
            "id": subscription.id,
            "status": subscription.status,
            "expires_at": subscription.expires_at.isoformat() if subscription.expires_at else None,
            "trainings_left": subscription.trainings_left,
            "trainings_used": subscription.trainings_used,
        }

    def _parse_dt(self, value: str):
        return datetime.fromisoformat(value)
