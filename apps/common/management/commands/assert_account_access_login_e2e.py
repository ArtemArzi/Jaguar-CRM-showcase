from __future__ import annotations

import json
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.attendance.models import ScheduleEnrollment
from apps.billing.models import Payment, TrainingType
from apps.clubs.models import ClubMembership
from apps.students.models import AccountAccess, Student


class Command(BaseCommand):
    help = "Assert account access login real-stack E2E side effects from a fixture JSON."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fixture",
            required=True,
            help="Path to fixture JSON from prepare_account_access_login_e2e.",
        )
        parser.add_argument(
            "--timeout-seconds",
            type=float,
            default=10,
            help="Seconds to poll for account access side effects before failing.",
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
                    raise CommandError(f"account access login E2E assertion failed: {exc}") from exc
                time.sleep(0.25)
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
            "trainer_id",
            "student_id",
            "child_student_id",
            "subscription_id",
            "child_subscription_id",
            "payment_id",
            "child_payment_id",
            "conversion_enrollment_id",
            "child_conversion_enrollment_id",
            "expected",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_evidence(self, fixture: dict) -> dict:
        club_id = int(fixture["club_id"])
        trainer_id = int(fixture["trainer_id"])
        student_id = int(fixture["student_id"])
        child_student_id = int(fixture["child_student_id"])
        subscription_id = int(fixture["subscription_id"])
        child_subscription_id = int(fixture["child_subscription_id"])
        payment_id = int(fixture["payment_id"])
        child_payment_id = int(fixture["child_payment_id"])
        conversion_enrollment_id = int(fixture["conversion_enrollment_id"])
        child_conversion_enrollment_id = int(fixture["child_conversion_enrollment_id"])
        expected = fixture["expected"]

        student = (
            Student.objects.for_club(club_id)
            .select_related("user", "assigned_trainer")
            .get(id=student_id, deleted_at__isnull=True)
        )
        if student.status != Student.Status.ACTIVE:
            raise CommandError(f"student status mismatch: expected active, got {student.status}")
        if student.assigned_trainer_id != trainer_id:
            raise CommandError(
                f"student assigned trainer mismatch: expected {trainer_id}, got {student.assigned_trainer_id}"
            )
        if student.user_id is None:
            raise CommandError("student is not linked to a user")

        access = (
            AccountAccess.objects.for_club(club_id)
            .select_related("user", "student")
            .get(student_id=student_id, role=AccountAccess.Role.STUDENT)
        )
        if access.status not in {AccountAccess.Status.OPEN, AccountAccess.Status.RESET}:
            raise CommandError(f"account access status mismatch: expected open or reset, got {access.status}")
        if access.status == AccountAccess.Status.RESET and (
            access.reset_at is None or access.reset_by_id is None
        ):
            raise CommandError("reset account access is missing reset audit fields")
        if access.user_id != student.user_id:
            raise CommandError(
                f"account access user mismatch: expected {student.user_id}, got {access.user_id}"
            )
        if access.user.username != student.phone:
            raise CommandError("account access username does not match the student phone login")
        if access.must_change_password is not True:
            raise CommandError("account access must_change_password is not true")
        if access.temporary_credential_revealed_at is None:
            raise CommandError("account access temporary_credential_revealed_at is not set")
        if not access.user.has_usable_password():
            raise CommandError("account access user has no usable password")

        memberships = list(ClubMembership.objects.filter(user=access.user, is_active=True).order_by("id"))
        if len(memberships) != 1:
            raise CommandError(f"issued user active membership count mismatch: expected 1, got {len(memberships)}")
        membership = memberships[0]
        if membership.club_id != club_id:
            raise CommandError(
                f"issued user membership club mismatch: expected {club_id}, got {membership.club_id}"
            )
        if membership.role != ClubMembership.Role.STUDENT:
            raise CommandError(f"issued user membership role mismatch: expected student, got {membership.role}")

        other_access_exists = AccountAccess.objects.exclude(club_id=club_id).filter(user=access.user).exists()
        if other_access_exists:
            raise CommandError("issued user has account access outside the fixture club")

        child = (
            Student.objects.for_club(club_id)
            .select_related("parent_user", "assigned_trainer")
            .get(id=child_student_id, deleted_at__isnull=True)
        )
        if child.status != Student.Status.ACTIVE:
            raise CommandError(f"child status mismatch: expected active, got {child.status}")
        if not child.is_child:
            raise CommandError("child fixture student is not marked as child")
        if child.assigned_trainer_id != trainer_id:
            raise CommandError(
                f"child assigned trainer mismatch: expected {trainer_id}, got {child.assigned_trainer_id}"
            )
        if child.user_id is not None:
            raise CommandError("child student was linked to a student user instead of parent user")
        if child.parent_user_id is None:
            raise CommandError("child is not linked to a parent user")

        parent_access = (
            AccountAccess.objects.for_club(club_id)
            .select_related("user", "student")
            .get(student_id=child_student_id, role=AccountAccess.Role.PARENT)
        )
        if parent_access.status not in {AccountAccess.Status.OPEN, AccountAccess.Status.RESET}:
            raise CommandError(
                f"parent account access status mismatch: expected open or reset, got {parent_access.status}"
            )
        if parent_access.status == AccountAccess.Status.RESET and (
            parent_access.reset_at is None or parent_access.reset_by_id is None
        ):
            raise CommandError("reset parent account access is missing reset audit fields")
        if parent_access.user_id != child.parent_user_id:
            raise CommandError(
                f"parent access user mismatch: expected {child.parent_user_id}, got {parent_access.user_id}"
            )
        expected_parent_username = fixture["child"]["parent_username"]
        if parent_access.user.username != expected_parent_username:
            raise CommandError("parent account access username does not match the issued parent phone login")
        if parent_access.must_change_password is not True:
            raise CommandError("parent account access must_change_password is not true")
        if parent_access.temporary_credential_revealed_at is None:
            raise CommandError("parent account access temporary_credential_revealed_at is not set")
        if not parent_access.user.has_usable_password():
            raise CommandError("parent account access user has no usable password")

        parent_memberships = list(
            ClubMembership.objects.filter(user=parent_access.user, is_active=True).order_by("id")
        )
        if len(parent_memberships) != 1:
            raise CommandError(
                f"issued parent active membership count mismatch: expected 1, got {len(parent_memberships)}"
            )
        parent_membership = parent_memberships[0]
        if parent_membership.club_id != club_id:
            raise CommandError(
                f"issued parent membership club mismatch: expected {club_id}, got {parent_membership.club_id}"
            )
        if parent_membership.role != ClubMembership.Role.PARENT:
            raise CommandError(
                f"issued parent membership role mismatch: expected parent, got {parent_membership.role}"
            )
        parent_other_access_exists = AccountAccess.objects.exclude(club_id=club_id).filter(
            user=parent_access.user
        ).exists()
        if parent_other_access_exists:
            raise CommandError("issued parent user has account access outside the fixture club")

        payment = self._assert_pending_manual_admission(
            club_id=club_id,
            student=student,
            payment_id=payment_id,
            subscription_id=subscription_id,
            enrollment_id=conversion_enrollment_id,
            expected=expected,
        )
        child_payment = self._assert_pending_manual_admission(
            club_id=club_id,
            student=child,
            payment_id=child_payment_id,
            subscription_id=child_subscription_id,
            enrollment_id=child_conversion_enrollment_id,
            expected=expected,
        )

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "club_id": club_id,
            "student": {
                "id": student.id,
                "status": student.status,
                "user_id": student.user_id,
                "assigned_trainer_id": student.assigned_trainer_id,
            },
            "child": {
                "id": child.id,
                "status": child.status,
                "is_child": child.is_child,
                "user_id": child.user_id,
                "parent_user_id": child.parent_user_id,
                "assigned_trainer_id": child.assigned_trainer_id,
            },
            "account_access": {
                "id": access.id,
                "role": access.role,
                "status": access.status,
                "user_id": access.user_id,
                "must_change_password": access.must_change_password,
                "temporary_credential_revealed": access.temporary_credential_revealed_at is not None,
            },
            "membership": {
                "id": membership.id,
                "club_id": membership.club_id,
                "role": membership.role,
            },
            "parent_account_access": {
                "id": parent_access.id,
                "role": parent_access.role,
                "status": parent_access.status,
                "user_id": parent_access.user_id,
                "must_change_password": parent_access.must_change_password,
                "temporary_credential_revealed": parent_access.temporary_credential_revealed_at is not None,
            },
            "parent_membership": {
                "id": parent_membership.id,
                "club_id": parent_membership.club_id,
                "role": parent_membership.role,
            },
            "subscription": {
                "id": payment.subscription_id,
                "status": payment.subscription.status,
                "paid": payment.subscription.paid_amount is not None and payment.subscription.paid_amount > 0,
                "trainings_left": payment.subscription.trainings_left,
            },
            "child_subscription": {
                "id": child_payment.subscription_id,
                "status": child_payment.subscription.status,
                "paid": (
                    child_payment.subscription.paid_amount is not None
                    and child_payment.subscription.paid_amount > 0
                ),
                "trainings_left": child_payment.subscription.trainings_left,
            },
        }

    def _assert_pending_manual_admission(
        self,
        *,
        club_id: int,
        student: Student,
        payment_id: int,
        subscription_id: int,
        enrollment_id: int,
        expected: dict,
    ) -> Payment:
        payment = (
            Payment.objects.for_club(club_id)
            .select_related(
                "subscription",
                "tariff__training_type",
                "target_schedule",
                "conversion_enrollment",
            )
            .get(id=payment_id)
        )
        if payment.student_id != student.id:
            raise CommandError("manual admission payment belongs to another student")
        if payment.status != expected["payment_status"]:
            raise CommandError(f"manual admission payment status mismatch: got {payment.status}")
        if payment.payment_method not in {Payment.Method.CASH, Payment.Method.TRANSFER}:
            raise CommandError("manual admission payment method is not manual")
        if payment.subscription_id != subscription_id or payment.conversion_enrollment_id != enrollment_id:
            raise CommandError("manual admission provenance links do not match fixture")
        if (
            payment.target_schedule_id is None
            or payment.target_start_date is None
            or payment.target_start_date.isoformat() != expected["target_start_date"]
            or payment.target_training_type_kind_snapshot != TrainingType.Kind.GROUP
            or payment.tariff.training_type.kind != TrainingType.Kind.GROUP
        ):
            raise CommandError("manual admission target provenance is invalid")
        subscription = payment.subscription
        enrollment = payment.conversion_enrollment
        if subscription is None or enrollment is None:
            raise CommandError("manual admission is missing owned subscription or enrollment")
        if (
            subscription.id != subscription_id
            or subscription.student_id != student.id
            or subscription.status != expected["subscription_status"]
            or subscription.deleted_at is not None
            or subscription.paid_amount is not None
        ):
            raise CommandError("manual admission subscription is not pending/unpaid")
        if (
            enrollment.club_id != club_id
            or enrollment.student_id != student.id
            or enrollment.schedule_id != payment.target_schedule_id
            or enrollment.starts_on != payment.target_start_date
            or enrollment.status != ScheduleEnrollment.Status.ACTIVE
            or enrollment.created_from != ScheduleEnrollment.CreatedFrom.PAID_CONVERSION
        ):
            raise CommandError("manual admission enrollment provenance is invalid")
        return payment
