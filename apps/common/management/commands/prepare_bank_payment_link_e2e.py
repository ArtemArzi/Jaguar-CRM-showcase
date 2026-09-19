from __future__ import annotations

import json
import uuid
from datetime import time, timedelta
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.attendance.models import Checkin, Schedule
from apps.billing.models import BankPaymentOrder, Debt, Payment, Subscription, Tariff, TrainingType
from apps.billing.services import create_bank_payment_order, process_bank_payment_webhook
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.clubs.timezones import club_localdate
from apps.common.management.commands.training_group_e2e import reconcile_fixture_group_to_active
from apps.students.models import Student
from apps.trainers.models import Trainer, TrainerLocation, TrainerRate


class Command(BaseCommand):
    help = "Prepare an isolated fixture for bank payment link real-stack E2E."

    def add_arguments(self, parser):
        parser.add_argument("--output", required=True, help="Path to write fixture JSON.")

    def handle(self, *args, **options):
        output_path = Path(options["output"]).expanduser()
        if output_path.exists() and output_path.is_dir():
            raise CommandError("--output must point to a JSON file, not a directory")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        fixture = self._create_fixture()
        output_path.write_text(
            json.dumps(fixture, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.stdout.write(
            self.style.SUCCESS(
                "Prepared bank payment link E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        today = timezone.localdate()
        unique = uuid.uuid4().hex[:8]
        phone_seed = str(int(unique, 16) % 10_000_000).zfill(7)
        fixture_id = f"bank-payment-link-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        trainer_password = f"BankPayTrainer-{unique}-pass"
        student_password = f"BankPayStudent-{unique}-pass"
        parent_password = f"BankPayParent-{unique}-pass"
        owner_password = f"BankPayOwner-{unique}-pass"

        club = Club.objects.create(
            name=f"Jaguar Bank Payment E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Bank Payment E2E",
        )
        location = Location.objects.create(
            club=club,
            name=f"Bank Payment Hall {fixture_id}",
            address="Bank payment link E2E fixture",
        )

        trainer_user = self._create_user(
            fixture_id=fixture_id,
            role="trainer",
            password=trainer_password,
        )
        student_user = self._create_user(
            fixture_id=fixture_id,
            role="student",
            password=student_password,
        )
        parent_user = self._create_user(
            fixture_id=fixture_id,
            role="parent",
            password=parent_password,
        )
        owner_user = self._create_user(
            fixture_id=fixture_id,
            role="owner",
            password=owner_password,
        )
        ClubMembership.objects.create(user=trainer_user, club=club, role=ClubMembership.Role.TRAINER)
        ClubMembership.objects.create(user=student_user, club=club, role=ClubMembership.Role.STUDENT)
        ClubMembership.objects.create(user=parent_user, club=club, role=ClubMembership.Role.PARENT)
        ClubMembership.objects.create(user=owner_user, club=club, role=ClubMembership.Role.OWNER)

        trainer = Trainer.objects.create(
            club=club,
            first_name="Bank",
            last_name="Trainer",
            phone=f"+15558{phone_seed}0",
            user=trainer_user,
        )
        training_type = TrainingType.objects.create(
            club=club,
            name=f"Bank Payment Group {fixture_id}",
            slug=f"bank-payment-group-{unique}",
            kind=TrainingType.Kind.GROUP,
            drop_in_price=Decimal("1100.00"),
            trial_free=False,
        )
        TrainerLocation.objects.create(club=club, trainer=trainer, location=location)
        TrainerRate.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            percent=Decimal("25.00"),
        )
        tariff = Tariff.objects.create(
            club=club,
            training_type=training_type,
            name=f"Bank Link 8-Pack {fixture_id}",
            price=Decimal("5000.00"),
            trainings_limit=8,
            duration_days=30,
            scope=Tariff.Scope.CLUB,
            is_active=True,
        )
        target_start_date = club_localdate(club) + timedelta(days=1)
        target_schedule = Schedule.objects.create(
            club=club,
            day_of_week=target_start_date.weekday(),
            start_time=time(18, 0),
            end_time=time(19, 0),
            group_name=f"Bank Payment Permanent Group {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=None,
            is_active=True,
        )
        second_target_start_date = target_start_date + timedelta(days=2)
        second_target_schedule = Schedule.objects.create(
            club=club,
            day_of_week=second_target_start_date.weekday(),
            start_time=time(18, 0),
            end_time=time(19, 0),
            group_name=target_schedule.group_name,
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=None,
            is_active=True,
        )
        canonical_group = reconcile_fixture_group_to_active(
            club=club,
            actor_user_id=trainer_user.id,
            schedule_ids=[target_schedule.id, second_target_schedule.id],
            canonical_name=target_schedule.group_name,
            responsible_trainer_id=trainer.id,
            idempotency_prefix=f"{fixture_id}-canonical-group",
            require_manual_operational_admission=True,
        )

        trainer_student = Student.objects.create(
            club=club,
            first_name="Trainer",
            last_name="BankLinkStudent",
            phone=f"+15558{phone_seed}1",
            email="",
            is_child=False,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            assigned_trainer=trainer,
        )
        student = Student.objects.create(
            club=club,
            first_name="Student",
            last_name="BankLinkSelf",
            phone=f"+15558{phone_seed}2",
            email="",
            is_child=False,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            user=student_user,
        )
        child = Student.objects.create(
            club=club,
            first_name="Parent",
            last_name="BankLinkChild",
            phone=f"+15558{phone_seed}3",
            email="",
            is_child=True,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            parent_user=parent_user,
        )
        owner_create_student = Student.objects.create(
            club=club,
            first_name="Owner",
            last_name="BankLinkCreate",
            phone=f"+15558{phone_seed}7",
            email="",
            is_child=False,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
        )
        owner_manual_review_student = Student.objects.create(
            club=club,
            first_name="Owner",
            last_name="BankLinkReview",
            phone=f"+15558{phone_seed}8",
            email="",
            is_child=False,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
        )
        finance_manual_student = self._create_group_lifecycle_student(
            club=club,
            trainer=trainer,
            phone=f"+15558{phone_seed}9",
            last_name="FinanceManual",
        )
        finance_history_student = self._create_group_lifecycle_student(
            club=club,
            trainer=trainer,
            phone=f"+15557{phone_seed}9",
            last_name="FinanceHistory",
        )
        finance_manual_payment = Payment.objects.create(
            club=club,
            student=finance_manual_student,
            tariff=tariff,
            amount=tariff.price,
            original_amount=tariff.price,
            payment_method=Payment.Method.CASH,
            status=Payment.Status.PENDING,
            recorded_by=owner_user,
            seller_trainer=trainer,
        )
        finance_confirmed_online_order = self._create_group_lifecycle_order(
            club=club,
            student=finance_history_student,
            tariff=tariff,
            trainer=trainer,
            trainer_user_id=owner_user.id,
            source=BankPaymentOrder.Source.OWNER,
            target_schedule=target_schedule,
            training_group_id=canonical_group["training_group_id"],
            target_start_date=target_start_date,
        )
        self._process_mock_webhook(
            order=finance_confirmed_online_order,
            fixture_id=fixture_id,
            suffix="finance-history-approved",
            amount=finance_confirmed_online_order.amount_snapshot,
        )
        finance_confirmed_online_order.refresh_from_db()
        finance_confirmed_online_payment = finance_confirmed_online_order.payment
        self._create_subscription(
            club=club,
            student=student,
            tariff=tariff,
            now=now,
            status=Subscription.Status.EXPIRED,
            expires_at=now - timedelta(days=1),
        )
        self._create_subscription(club=club, student=child, tariff=tariff, now=now)
        trainer_debt = self._create_open_debt(
            club=club,
            student=trainer_student,
            trainer=trainer,
            location=location,
            training_type=training_type,
            today=today,
            fixture_id=fixture_id,
        )
        trainer_order = create_bank_payment_order(
            club_id=club.id,
            student_id=trainer_student.id,
            tariff_id=tariff.id,
            source="trainer",
            created_by_id=trainer_user.id,
            seller_trainer_id=trainer.id,
            debt_ids=[trainer_debt.id],
            target_schedule_id=target_schedule.id,
            target_training_group_id=canonical_group["training_group_id"],
            target_start_date=target_start_date,
            enforce_trainer_group_contract=True,
        )
        approval_student = self._create_group_lifecycle_student(
            club=club,
            trainer=trainer,
            phone=f"+15558{phone_seed}4",
            last_name="BankLinkApproval",
        )
        failure_student = self._create_group_lifecycle_student(
            club=club,
            trainer=trainer,
            phone=f"+15558{phone_seed}5",
            last_name="BankLinkFailure",
        )
        expiry_student = self._create_group_lifecycle_student(
            club=club,
            trainer=trainer,
            phone=f"+15558{phone_seed}6",
            last_name="BankLinkExpiry",
        )
        approval_order = self._create_group_lifecycle_order(
            club=club,
            student=approval_student,
            tariff=tariff,
            trainer=trainer,
            trainer_user_id=trainer_user.id,
            target_schedule=target_schedule,
            training_group_id=canonical_group["training_group_id"],
            target_start_date=target_start_date,
        )
        failure_order = self._create_group_lifecycle_order(
            club=club,
            student=failure_student,
            tariff=tariff,
            trainer=trainer,
            trainer_user_id=trainer_user.id,
            target_schedule=target_schedule,
            training_group_id=canonical_group["training_group_id"],
            target_start_date=target_start_date,
        )
        expiry_order = self._create_group_lifecycle_order(
            club=club,
            student=expiry_student,
            tariff=tariff,
            trainer=trainer,
            trainer_user_id=trainer_user.id,
            target_schedule=target_schedule,
            training_group_id=canonical_group["training_group_id"],
            target_start_date=target_start_date,
        )
        student_order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source="student",
            created_by_id=student_user.id,
        )
        parent_order = create_bank_payment_order(
            club_id=club.id,
            student_id=child.id,
            tariff_id=tariff.id,
            source="parent",
            created_by_id=parent_user.id,
        )
        owner_manual_review_order = self._create_group_lifecycle_order(
            club=club,
            student=owner_manual_review_student,
            tariff=tariff,
            trainer=trainer,
            trainer_user_id=owner_user.id,
            source=BankPaymentOrder.Source.OWNER,
            target_schedule=target_schedule,
            training_group_id=canonical_group["training_group_id"],
            target_start_date=target_start_date,
        )
        manual_review_operation_id = f"{fixture_id}-manual-review-operation"
        self._process_mock_webhook(
            order=owner_manual_review_order,
            fixture_id=fixture_id,
            suffix="manual-review-mismatch",
            amount=owner_manual_review_order.amount_snapshot + Decimal("1.00"),
            operation_id=manual_review_operation_id,
        )
        owner_manual_review_order.refresh_from_db()

        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "owner": {
                "email": owner_user.email,
                "password": owner_password,
                "user_id": owner_user.id,
            },
            "owner_create_student": {
                "id": owner_create_student.id,
                "name": str(owner_create_student),
            },
            "owner_manual_review": {
                "student_id": owner_manual_review_student.id,
                "student_name": str(owner_manual_review_student),
                "order_id": owner_manual_review_order.id,
                "private_error_code": owner_manual_review_order.last_error_code,
                "private_error_message": owner_manual_review_order.last_error_message,
                "provider_name": "Тестовый провайдер",
                "provider_status_label": "Оплата подтверждена",
                "provider_reference_suffix": manual_review_operation_id[-12:],
            },
            "finance_workspace": {
                "browser_confirms_manual_payment": True,
                "manual_payment_id": finance_manual_payment.id,
                "manual_student_name": str(finance_manual_student),
                "confirmed_online_payment_id": finance_confirmed_online_payment.id,
                "confirmed_online_student_name": str(finance_history_student),
            },
            "trainer": {
                "email": trainer_user.email,
                "password": trainer_password,
                "user_id": trainer_user.id,
                "trainer_id": trainer.id,
            },
            "trainer_student": {
                "id": trainer_student.id,
                "name": str(trainer_student),
                "debt_id": trainer_debt.id,
                "debt_checkin_id": trainer_debt.checkin_id,
                "order_id": trainer_order.id,
            },
            "approval": {"student_id": approval_student.id, "order_id": approval_order.id},
            "provider_failure": {"student_id": failure_student.id, "order_id": failure_order.id},
            "expiry": {"student_id": expiry_student.id, "order_id": expiry_order.id},
            "target_group": {
                "schedule_id": target_schedule.id,
                "name": target_schedule.group_name,
                "start_date": target_start_date.isoformat(),
                "training_group_id": canonical_group["training_group_id"],
                "rollout_mode": canonical_group["mode"],
                "second_schedule_id": second_target_schedule.id,
                "second_start_date": second_target_start_date.isoformat(),
                "new_writes_enabled": canonical_group["new_writes_enabled"],
                "manual_operational_admission_enabled": canonical_group[
                    "manual_operational_admission_enabled"
                ],
            },
            "student": {
                "email": student_user.email,
                "password": student_password,
                "user_id": student_user.id,
                "student_id": student.id,
                "name": str(student),
                "order_id": student_order.id,
            },
            "parent": {
                "email": parent_user.email,
                "password": parent_password,
                "user_id": parent_user.id,
                "child_id": child.id,
                "child_name": str(child),
                "order_id": parent_order.id,
            },
            "tariff": {
                "id": tariff.id,
                "name": tariff.name,
                "price": "5000.00",
            },
            "expected": {
                "provider": "mock",
                "status": "pending",
                "receipt_mode": "none",
                "staff_ttl_minutes": 10080,
                "self_service_ttl_minutes": 4320,
                "browser_statuses": {
                    "trainer": BankPaymentOrder.Status.CANCELLED,
                    "student": BankPaymentOrder.Status.APPROVED,
                    "parent": BankPaymentOrder.Status.CANCELLED,
                    "owner": BankPaymentOrder.Status.PENDING,
                },
            },
            "created_at": now.isoformat(),
        }

    def _create_subscription(
        self,
        *,
        club: Club,
        student: Student,
        tariff: Tariff,
        now,
        status: str = Subscription.Status.ACTIVE,
        expires_at=None,
    ) -> None:
        Subscription.objects.create(
            club=club,
            student=student,
            tariff=tariff,
            status=status,
            paid_amount=tariff.price,
            trainings_left=tariff.trainings_limit,
            trainings_used=0,
            expires_at=expires_at or now + timedelta(days=30),
            scope=tariff.scope,
            location=None,
        )

    def _create_group_lifecycle_student(
        self,
        *,
        club: Club,
        trainer: Trainer,
        phone: str,
        last_name: str,
    ) -> Student:
        return Student.objects.create(
            club=club,
            first_name="Bank",
            last_name=last_name,
            phone=phone,
            email="",
            is_child=False,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            assigned_trainer=trainer,
        )

    def _create_group_lifecycle_order(
        self,
        *,
        club: Club,
        student: Student,
        tariff: Tariff,
        trainer: Trainer,
        trainer_user_id: int,
        target_schedule: Schedule,
        training_group_id: int,
        target_start_date,
        source: str = BankPaymentOrder.Source.TRAINER,
    ):
        return create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=source,
            created_by_id=trainer_user_id,
            seller_trainer_id=trainer.id,
            target_schedule_id=target_schedule.id,
            target_training_group_id=training_group_id,
            target_start_date=target_start_date,
            enforce_trainer_group_contract=True,
        )

    def _process_mock_webhook(
        self,
        *,
        order: BankPaymentOrder,
        fixture_id: str,
        suffix: str,
        amount: Decimal,
        operation_id: str | None = None,
    ) -> None:
        payload = {
            "webhookType": "acquiringInternetPayment",
            "event_id": f"{fixture_id}-{suffix}",
            "status": "APPROVED",
            "paymentLinkId": order.provider_payment_link_id,
            "operationId": operation_id or f"{fixture_id}-{suffix}-operation",
            "amount": str(amount),
            "paid_at": timezone.now().isoformat(),
        }
        process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=json.dumps(payload).encode(),
            headers={},
            request_id=f"e2e-{fixture_id}-{suffix}",
        )

    def _create_open_debt(
        self,
        *,
        club: Club,
        student: Student,
        trainer: Trainer,
        location: Location,
        training_type: TrainingType,
        today,
        fixture_id: str,
    ) -> Debt:
        schedule = Schedule.objects.create(
            club=club,
            day_of_week=today.weekday(),
            start_time=time(0, 0),
            end_time=time(23, 59),
            group_name=f"Bank Link Debt Proof {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=today,
            is_active=True,
        )
        checkin = Checkin.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            trainer=trainer,
            location=location,
            date=today,
            source=Checkin.Source.MANUAL,
            subscription=None,
            is_debt=True,
        )
        return Debt.objects.create(
            club=club,
            student=student,
            checkin=checkin,
            tariff_price=training_type.drop_in_price,
            reason="no_subscription",
        )

    def _create_user(self, *, fixture_id: str, role: str, password: str):
        user_model = get_user_model()
        username = f"{fixture_id}-{role}"
        email = f"{role}-{fixture_id}@bank-payment-link-e2e.local"
        return user_model.objects.create_user(username=username, email=email, password=password)
