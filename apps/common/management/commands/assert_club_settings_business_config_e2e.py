from __future__ import annotations

import json
import time
from datetime import time as time_of_day
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.attendance.models import Checkin, Schedule, ScheduleEnrollment
from apps.attendance.services import create_checkin
from apps.billing.models import Debt, Discount, Payment, Subscription, Tariff, TrainingType
from apps.billing.services import create_payment
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.documents.models import DocumentType
from apps.grades.models import Grade, GradeSystem
from apps.students.models import Student
from apps.trainers.models import Trainer


class Command(BaseCommand):
    help = "Assert club settings business config E2E side effects."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fixture",
            required=True,
            help="Path to fixture JSON from prepare_club_settings_business_config_e2e.",
        )
        parser.add_argument(
            "--timeout-seconds",
            type=float,
            default=30,
            help="Seconds to poll for settings state before failing.",
        )
        parser.add_argument(
            "--mode",
            choices=("final", "active-consumption"),
            default="final",
            help="Assertion mode: final persisted state or active business consumption before toggles.",
        )

    def handle(self, *args, **options):
        fixture = self._load_fixture(Path(options["fixture"]).expanduser())
        mode = options["mode"]
        timeout_seconds = max(float(options["timeout_seconds"]), 0)
        deadline = time.monotonic() + timeout_seconds

        while True:
            try:
                evidence = self._collect_evidence(fixture, mode=mode)
            except CommandError as exc:
                if time.monotonic() >= deadline:
                    raise CommandError(f"club settings business config E2E assertion failed: {exc}") from exc
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

        required = {"fixture_id", "club_id", "control_club_id", "owner", "expected", "consumption"}
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_evidence(self, fixture: dict, *, mode: str) -> dict:
        club = Club.objects.get(id=int(fixture["club_id"]))
        control_club = Club.objects.get(id=int(fixture["control_club_id"]))
        owner_user_id = int(fixture["owner"]["user_id"])
        expected = fixture["expected"]

        membership = ClubMembership.objects.filter(
            user_id=owner_user_id,
            club=club,
            is_active=True,
            role=ClubMembership.Role.OWNER,
        ).first()
        if membership is None:
            raise CommandError("owner membership not found")

        settings = ClubSettings.objects.get(club=club)
        self._assert_equal("club name display", settings.club_name_display, expected["club_name_display"])
        self._assert_hex_equal("primary color", settings.primary_color, expected["primary_color"])
        self._assert_hex_equal("accent color", settings.accent_color, expected["primary_color"])
        self._assert_equal("timezone", club.timezone, expected["timezone"])
        self._assert_equal("freeze enabled", settings.freeze_enabled, expected["freeze_enabled"])
        self._assert_equal("freeze max days", settings.freeze_max_days, expected["freeze_max_days"])
        self._assert_equal("freeze max count", settings.freeze_max_count, expected["freeze_max_count"])
        self._assert_equal(
            "min trainings to freeze",
            settings.min_trainings_to_freeze,
            expected["min_trainings_to_freeze"],
        )
        self._assert_equal("logo file removed", bool(settings.logo_file), False)
        self._assert_equal("logo url cleared", settings.logo_url, "")

        location = Location.objects.filter(club=club, name=expected["location_name"]).first()
        if location is None:
            raise CommandError("expected location was not created")
        self._assert_equal("location address", location.address, expected["location_address"])
        if Location.objects.filter(club=club, name=expected["location_initial_name"]).exists():
            raise CommandError("initial location name remained after edit")
        if Location.objects.filter(club=club, name=expected["location_delete_name"]).exists():
            raise CommandError("temporary location was not deleted")

        training_type = TrainingType.objects.for_club(club).filter(name=expected["training_type_name"]).first()
        if training_type is None:
            raise CommandError("expected training type was not created")
        self._assert_decimal("drop-in price", training_type.drop_in_price, expected["drop_in_price"])
        self._assert_equal("trial free", training_type.trial_free, expected["trial_free"])
        expected_training_type_active = (
            True if mode == "active-consumption" else expected["training_type_is_active"]
        )
        self._assert_equal("training type active", training_type.is_active, expected_training_type_active)
        if TrainingType.objects.for_club(club).filter(name=expected["training_type_initial_name"]).exists():
            raise CommandError("initial training type name remained after edit")

        tariff = (
            Tariff.objects.for_club(club)
            .select_related("training_type", "location")
            .filter(name=expected["tariff_name"])
            .first()
        )
        if tariff is None:
            raise CommandError("expected tariff was not created")
        self._assert_equal("tariff training type", tariff.training_type_id, training_type.id)
        self._assert_decimal("tariff price", tariff.price, expected["tariff_price"])
        self._assert_equal("tariff trainings limit", tariff.trainings_limit, expected["tariff_trainings_limit"])
        self._assert_equal("tariff duration days", tariff.duration_days, expected["tariff_duration_days"])
        self._assert_equal("tariff scope", tariff.scope, expected["tariff_scope"])
        self._assert_equal("tariff location", tariff.location_id, location.id)
        expected_tariff_active = True if mode == "active-consumption" else expected["tariff_is_active"]
        self._assert_equal("tariff active", tariff.is_active, expected_tariff_active)
        self._assert_equal("tariff description", tariff.description, expected["tariff_description"])
        if Tariff.objects.for_club(club).filter(name=expected["tariff_initial_name"]).exists():
            raise CommandError("initial tariff name remained after edit")

        discount = Discount.objects.for_club(club).filter(name=expected["discount_name"]).first()
        if discount is None:
            raise CommandError("expected discount was not created")
        self._assert_equal("discount type", discount.discount_type, expected["discount_type"])
        self._assert_decimal("discount value", discount.value, expected["discount_value"])
        expected_discount_active = True if mode == "active-consumption" else expected["discount_is_active"]
        self._assert_equal("discount active", discount.is_active, expected_discount_active)
        if Discount.objects.for_club(club).filter(name=expected["discount_initial_name"]).exists():
            raise CommandError("initial discount name remained after edit")

        control_settings = ClubSettings.objects.get(club=control_club)
        self._assert_equal("control primary color", control_settings.primary_color, "#222222")
        self._assert_equal("control timezone", control_club.timezone, "Europe/Moscow")
        self._assert_equal("control logo file", bool(control_settings.logo_file), False)
        self._assert_equal("control logo url", control_settings.logo_url, "")
        if Location.objects.filter(
            club=control_club,
            name__in=[
                expected["location_initial_name"],
                expected["location_name"],
                expected["location_delete_name"],
            ],
        ).exists():
            raise CommandError("location leaked into control club")

        grade_system = GradeSystem.objects.for_club(club).filter(discipline=expected["grade_system_name"]).first()
        if grade_system is None:
            raise CommandError("expected grade system was not created")
        grade = (
            Grade.objects.for_club(club)
            .filter(
                grade_system=grade_system,
                name=expected["grade_final_name"],
                order=expected["grade_final_order"],
                min_trainings=expected["grade_final_min_trainings"],
            )
            .first()
        )
        if grade is None:
            raise CommandError("expected edited grade was not created")
        if Grade.objects.for_club(club).filter(grade_system=grade_system, name=expected["grade_initial_name"]).exists():
            raise CommandError("initial grade name remained after edit")
        if Grade.objects.for_club(club).filter(grade_system=grade_system, name=expected["grade_delete_name"]).exists():
            raise CommandError("temporary grade was not deleted")
        if GradeSystem.objects.for_club(club).filter(discipline=expected["grade_system_delete_name"]).exists():
            raise CommandError("temporary grade system was not deleted")
        if GradeSystem.objects.for_club(control_club).filter(discipline=expected["grade_system_name"]).exists():
            raise CommandError("grade system leaked into control club")

        document_type = DocumentType.objects.for_club(club).filter(name=expected["document_type_name"]).first()
        if document_type is None:
            raise CommandError("expected document type was not created")
        self._assert_equal(
            "document type description",
            document_type.description,
            expected["document_type_description"],
        )
        self._assert_equal("document type scope", document_type.scope, expected["document_type_scope"])
        self._assert_equal("document type required", document_type.is_required, expected["document_type_is_required"])
        self._assert_equal("document type active", document_type.is_active, expected["document_type_is_active"])
        if DocumentType.objects.for_club(club).filter(name=expected["document_type_initial_name"]).exists():
            raise CommandError("initial document type name remained after edit")
        if DocumentType.objects.for_club(control_club).filter(
            name__in=[
                expected["document_type_initial_name"],
                expected["document_type_name"],
            ],
        ).exists():
            raise CommandError("document type leaked into control club")
        if TrainingType.objects.for_club(control_club).filter(name=expected["training_type_name"]).exists():
            raise CommandError("training type leaked into control club")
        if Tariff.objects.for_club(control_club).filter(
            name__in=[expected["tariff_initial_name"], expected["tariff_name"]],
        ).exists():
            raise CommandError("tariff leaked into control club")
        if Discount.objects.for_club(control_club).filter(
            name__in=[expected["discount_initial_name"], expected["discount_name"]],
        ).exists():
            raise CommandError("discount leaked into control club")

        active_consumption = None
        if mode == "active-consumption":
            active_consumption = self._assert_active_consumption(
                fixture=fixture,
                club=club,
                owner_user_id=owner_user_id,
                location=location,
                training_type=training_type,
                tariff=tariff,
                discount=discount,
            )

        return {
            "ok": True,
            "mode": mode,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "settings": {
                "club_name_display": settings.club_name_display,
                "primary_color": settings.primary_color,
                "timezone": club.timezone,
                "freeze_enabled": settings.freeze_enabled,
                "freeze_max_days": settings.freeze_max_days,
                "freeze_max_count": settings.freeze_max_count,
                "min_trainings_to_freeze": settings.min_trainings_to_freeze,
                "logo_file": bool(settings.logo_file),
                "logo_url": settings.logo_url,
            },
            "catalog": {
                "location_id": location.id,
                "location_name": location.name,
                "location_address": location.address,
                "temporary_location_deleted": True,
                "grade_system_id": grade_system.id,
                "grade_system_name": grade_system.discipline,
                "grade_id": grade.id,
                "grade_name": grade.name,
                "grade_min_trainings": grade.min_trainings,
                "temporary_grade_deleted": True,
                "temporary_grade_system_deleted": True,
            },
            "documents": {
                "document_type_id": document_type.id,
                "document_type_name": document_type.name,
                "document_type_scope": document_type.scope,
                "document_type_required": document_type.is_required,
                "document_type_active": document_type.is_active,
            },
            "billing": {
                "training_type_id": training_type.id,
                "training_type_active": training_type.is_active,
                "drop_in_price": self._money(training_type.drop_in_price),
                "trial_free": training_type.trial_free,
                "tariff_id": tariff.id,
                "tariff_active": tariff.is_active,
                "tariff_location_id": tariff.location_id,
                "discount_id": discount.id,
                "discount_active": discount.is_active,
            },
            "active_consumption": active_consumption,
            "tenant_control": {
                "control_club_id": control_club.id,
                "unchanged": True,
            },
        }

    def _assert_active_consumption(
        self,
        *,
        fixture: dict,
        club: Club,
        owner_user_id: int,
        location: Location,
        training_type: TrainingType,
        tariff: Tariff,
        discount: Discount,
    ) -> dict:
        expected = fixture["expected"]
        consumption = fixture["consumption"]
        sale_student = Student.objects.for_club(club).get(id=int(consumption["sale_student_id"]))
        drop_in_student = Student.objects.for_club(club).get(id=int(consumption["drop_in_student_id"]))
        trainer = Trainer.objects.for_club(club).get(id=int(consumption["trainer_id"]))

        payment = (
            Payment.objects.for_club(club)
            .select_related("subscription")
            .filter(student=sale_student, tariff=tariff)
            .order_by("id")
            .first()
        )
        if payment is None:
            with patch("django_q.tasks.async_task", return_value=None):
                payment = create_payment(
                    club_id=club.id,
                    student_id=sale_student.id,
                    tariff_id=tariff.id,
                    payment_method=Payment.Method.CASH,
                    discount_ids=[discount.id],
                    recorded_by_id=owner_user_id,
                )
        payment.refresh_from_db()
        subscription = payment.subscription
        if subscription is None:
            raise CommandError("active consumption payment did not create subscription")

        self._assert_decimal("payment original amount", payment.original_amount, expected["tariff_price"])
        self._assert_decimal(
            "discounted payment amount",
            payment.amount,
            expected["discounted_tariff_amount"],
        )
        if not payment.applied_discounts.filter(id=discount.id).exists():
            raise CommandError("active consumption payment did not apply expected discount")
        self._assert_equal("payment status", payment.status, Payment.Status.PENDING)
        self._assert_equal("subscription status", subscription.status, Subscription.Status.PENDING)
        self._assert_equal(
            "subscription trainings left",
            subscription.trainings_left,
            expected["tariff_trainings_limit"],
        )

        checkin_date = timezone.localdate()
        schedule = self._get_or_create_consumption_schedule(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            checkin_date=checkin_date,
            group_name=consumption["schedule_group_name"],
        )
        enrollment = ScheduleEnrollment.objects.for_club(club).filter(
            student=drop_in_student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            ends_on__isnull=True,
        ).first()
        if enrollment is None:
            enrollment = ScheduleEnrollment.objects.create(
                club=club,
                student=drop_in_student,
                schedule=schedule,
                status=ScheduleEnrollment.Status.ACTIVE,
                starts_on=checkin_date,
                created_from=ScheduleEnrollment.CreatedFrom.MANUAL,
            )

        with patch("apps.attendance.services.async_task", return_value=None):
            checkin_result = create_checkin(
                club_id=club.id,
                student_id=drop_in_student.id,
                schedule_id=schedule.id,
                training_type_id=training_type.id,
                source=Checkin.Source.BATCH,
                checkin_date=checkin_date,
            )
        checkin = Checkin.objects.for_club(club).get(id=checkin_result["checkin_id"])
        debt = Debt.objects.for_club(club).filter(
            student=drop_in_student,
            checkin=checkin,
            reason="no_subscription",
        ).first()
        if debt is None:
            raise CommandError("active consumption check-in did not create drop-in debt")
        self._assert_equal("drop-in check-in debt flag", checkin.is_debt, True)
        self._assert_equal("drop-in check-in subscription", checkin.subscription_id, None)
        self._assert_decimal("drop-in debt amount", debt.tariff_price, expected["drop_in_debt_amount"])

        return {
            "payment_id": payment.id,
            "payment_amount": self._money(payment.amount),
            "payment_original_amount": self._money(payment.original_amount),
            "payment_discount_ids": sorted(payment.applied_discounts.values_list("id", flat=True)),
            "subscription_id": subscription.id,
            "subscription_status": subscription.status,
            "subscription_trainings_left": subscription.trainings_left,
            "schedule_id": schedule.id,
            "enrollment_id": enrollment.id,
            "checkin_id": checkin.id,
            "checkin_is_debt": checkin.is_debt,
            "checkin_subscription_id": checkin.subscription_id,
            "debt_id": debt.id,
            "debt_amount": self._money(debt.tariff_price),
        }

    def _get_or_create_consumption_schedule(
        self,
        *,
        club: Club,
        trainer: Trainer,
        location: Location,
        training_type: TrainingType,
        checkin_date,
        group_name: str,
    ) -> Schedule:
        schedule = Schedule.objects.for_club(club).filter(
            group_name=group_name,
            one_time_date=checkin_date,
        ).first()
        if schedule is None:
            return Schedule.objects.create(
                club=club,
                trainer=trainer,
                location=location,
                training_type=training_type,
                group_name=group_name,
                day_of_week=checkin_date.weekday(),
                start_time=time_of_day(9, 0),
                end_time=time_of_day(10, 0),
                one_time_date=checkin_date,
            )
        if schedule.training_type_id != training_type.id:
            raise CommandError("active consumption schedule training type mismatch")
        if schedule.location_id != location.id:
            raise CommandError("active consumption schedule location mismatch")
        if schedule.trainer_id != trainer.id:
            raise CommandError("active consumption schedule trainer mismatch")
        return schedule

    def _assert_equal(self, label: str, actual, expected) -> None:
        if actual != expected:
            raise CommandError(f"{label} mismatch: expected {expected}, got {actual}")

    def _assert_hex_equal(self, label: str, actual: str, expected: str) -> None:
        if actual.lower() != expected.lower():
            raise CommandError(f"{label} mismatch: expected {expected}, got {actual}")

    def _assert_decimal(self, label: str, actual, expected: str) -> None:
        if actual is None:
            raise CommandError(f"{label} mismatch: expected {expected}, got null")
        if Decimal(str(actual)).quantize(Decimal("0.01")) != Decimal(expected).quantize(Decimal("0.01")):
            raise CommandError(f"{label} mismatch: expected {expected}, got {actual}")

    def _money(self, value) -> str:
        return str(Decimal(str(value)).quantize(Decimal("0.01")))
