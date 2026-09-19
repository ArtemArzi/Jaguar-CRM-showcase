from __future__ import annotations

import json
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.billing.models import Payment, Subscription
from apps.billing.selectors import get_pending_subscription_freezes
from apps.clubs.models import Club, ClubMembership


class Command(BaseCommand):
    help = "Assert dashboard subscriptions/access-control E2E fixture state."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fixture",
            required=True,
            help="Path to fixture JSON from prepare_dashboard_access_subscriptions_e2e.",
        )
        parser.add_argument(
            "--timeout-seconds",
            type=float,
            default=30,
            help="Seconds to poll for dashboard subscriptions/access state before failing.",
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
                    raise CommandError(f"dashboard access subscriptions E2E assertion failed: {exc}") from exc
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
            "admin",
            "denied_users",
            "student",
            "sale_student",
            "tariff_id",
            "subscription_id",
            "pending_freeze_id",
            "expected",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_evidence(self, fixture: dict) -> dict:
        club = Club.objects.get(id=int(fixture["club_id"]))
        owner_user_id = int(fixture["owner"]["user_id"])
        owner_membership = ClubMembership.objects.filter(
            user_id=owner_user_id,
            club=club,
            is_active=True,
            role=ClubMembership.Role.OWNER,
        ).first()
        if owner_membership is None:
            raise CommandError("owner membership not found")
        admin_user_id = int(fixture["admin"]["user_id"])
        admin_membership = ClubMembership.objects.filter(
            user_id=admin_user_id,
            club=club,
            is_active=True,
            role=ClubMembership.Role.ADMIN,
        ).first()
        if admin_membership is None:
            raise CommandError("admin membership not found")

        denied_roles = self._collect_denied_roles(fixture=fixture, club=club)
        active_subscriptions = Subscription.objects.for_club(club).filter(
            status=Subscription.Status.ACTIVE,
            deleted_at__isnull=True,
        )
        subscription = active_subscriptions.get(id=int(fixture["subscription_id"]))
        direct_subscription = active_subscriptions.filter(
            student_id=int(fixture["sale_student"]["student_id"]),
            tariff_id=int(fixture["tariff_id"]),
        ).first()
        if direct_subscription is None:
            raise CommandError("transfer direct subscription not found")
        direct_payment = (
            Payment.objects.for_club(club)
            .filter(subscription=direct_subscription, deleted_at__isnull=True)
            .first()
        )
        if direct_payment is None:
            raise CommandError("transfer direct payment not found")
        pending_freezes = get_pending_subscription_freezes(club=club)
        pending_freeze = pending_freezes.get(id=int(fixture["pending_freeze_id"]))
        expected = fixture["expected"]

        if pending_freezes.count() != int(expected["pending_freeze_count"]):
            raise CommandError("pending freeze inbox count mismatch")
        if pending_freeze.subscription_id != subscription.id:
            raise CommandError("pending freeze subscription mismatch")
        if pending_freeze.days != int(expected["freeze_days"]):
            raise CommandError("pending freeze days mismatch")
        if pending_freeze.reason != expected["freeze_reason"]:
            raise CommandError("pending freeze reason mismatch")
        if pending_freeze.frozen_by_id != int(fixture["denied_users"]["trainer"]["user_id"]):
            raise CommandError("pending freeze requester mismatch")
        if subscription.trainings_left != int(expected["trainings_left"]):
            raise CommandError("subscription trainings_left mismatch")
        if direct_payment.status != Payment.Status.CONFIRMED:
            raise CommandError("transfer direct payment is not confirmed")
        if direct_payment.payment_method != expected["direct_payment_method"]:
            raise CommandError("transfer direct payment method mismatch")
        if direct_payment.recorded_by_id != owner_user_id:
            raise CommandError("transfer direct payment recorder mismatch")
        if direct_payment.verified_by_id != owner_user_id:
            raise CommandError("transfer direct payment verifier mismatch")

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "owner": {
                "user_id": owner_user_id,
                "membership_role": owner_membership.role,
            },
            "admin": {
                "user_id": admin_user_id,
                "membership_role": admin_membership.role,
            },
            "denied_roles": denied_roles,
            "subscriptions": {
                "active_count": active_subscriptions.count(),
                "subscription_id": subscription.id,
                "student_id": subscription.student_id,
                "status": subscription.status,
                "trainings_left": subscription.trainings_left,
                "trainings_used": subscription.trainings_used,
            },
            "direct_sale": {
                "subscription_id": direct_subscription.id,
                "payment_id": direct_payment.id,
                "payment_method": direct_payment.payment_method,
                "payment_status": direct_payment.status,
                "recorded_by_id": direct_payment.recorded_by_id,
                "verified_by_id": direct_payment.verified_by_id,
            },
            "freeze_inbox": {
                "pending_count": pending_freezes.count(),
                "pending_freeze_id": pending_freeze.id,
                "status": pending_freeze.status,
                "days": pending_freeze.days,
                "reason": pending_freeze.reason,
                "frozen_by_id": pending_freeze.frozen_by_id,
            },
        }

    def _collect_denied_roles(self, *, fixture: dict, club: Club) -> dict[str, str]:
        denied_roles = {}
        management_roles = {ClubMembership.Role.OWNER, ClubMembership.Role.ADMIN}
        for key in ("trainer", "student", "parent"):
            user_fixture = fixture["denied_users"].get(key)
            if not user_fixture:
                raise CommandError(f"missing denied user fixture: {key}")
            membership = ClubMembership.objects.filter(
                user_id=int(user_fixture["user_id"]),
                club=club,
                is_active=True,
            ).first()
            if membership is None:
                raise CommandError(f"denied user membership not found: {key}")
            if membership.role != user_fixture["role"]:
                raise CommandError(f"denied user role mismatch for {key}: got {membership.role}")
            if membership.role in management_roles:
                raise CommandError(f"denied user unexpectedly has management role: {key}")
            denied_roles[key] = membership.role
        return denied_roles
