from __future__ import annotations

import json
import uuid
from datetime import time, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.test import override_settings
from django.utils import timezone

from apps.attendance.models import (
    Checkin,
    Schedule,
    ScheduleEnrollment,
    TrainingGroupMembership,
    TrainingGroupRolloutState,
)
from apps.billing.models import (
    BankPaymentOrder,
    Debt,
    PaymentRefundCase,
    Tariff,
    TrainingType,
)
from apps.billing.services import create_bank_payment_order, process_bank_payment_webhook
from apps.billing.tasks import create_sale_earning
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.clubs.timezones import club_localdate
from apps.common.management.commands.training_group_e2e import reconcile_fixture_group_to_active
from apps.students.models import Student
from apps.trainers.models import (
    Trainer,
    TrainerEarning,
    TrainerLocation,
    TrainerPayrollPeriodClose,
    TrainerRate,
)


class Command(BaseCommand):
    help = "Prepare an isolated provider refund resolution E2E fixture."

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
                "Prepared refund resolution E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        unique = uuid.uuid4().hex[:8]
        phone_seed = str(int(unique, 16) % 10_000_000).zfill(7)
        fixture_id = f"refund-resolution-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        owner_password = f"RefundResolution-{unique}-pass"

        club = Club.objects.create(
            name=f"Jaguar Refund Resolution E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        TrainingGroupRolloutState.objects.create(
            club=club,
            mode=TrainingGroupRolloutState.Mode.OFF,
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Refund Resolution E2E",
        )
        location = Location.objects.create(
            club=club,
            name=f"Refund Hall {fixture_id}",
            address="Refund resolution E2E fixture",
        )
        owner = self._create_owner(
            club=club,
            fixture_id=fixture_id,
            password=owner_password,
        )
        trainer = Trainer.objects.create(
            club=club,
            first_name="Refund",
            last_name="Trainer",
            phone=f"+155570{phone_seed}",
        )
        TrainerLocation.objects.create(club=club, trainer=trainer, location=location)
        training_type = TrainingType.objects.create(
            club=club,
            name=f"Refund Group {fixture_id}",
            slug=f"refund-group-{unique}",
            kind=TrainingType.Kind.GROUP,
            drop_in_price=Decimal("750.00"),
            trial_free=False,
        )
        TrainerRate.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            percent=Decimal("20.00"),
        )
        full_tariff = self._create_tariff(
            club=club,
            training_type=training_type,
            fixture_id=fixture_id,
            label="Full",
            payout_policy=Tariff.PayoutPolicy.NONE,
        )
        partial_tariff = self._create_tariff(
            club=club,
            training_type=training_type,
            fixture_id=fixture_id,
            label="Partial",
            payout_policy=Tariff.PayoutPolicy.ON_PAYMENT,
        )
        today = club_localdate(club, now)
        target_date = today + timedelta(days=2)
        target_schedule = Schedule.objects.create(
            club=club,
            day_of_week=target_date.weekday(),
            start_time=time(18, 0),
            end_time=time(19, 0),
            group_name=f"Refund Conversion Group {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=training_type,
            is_active=True,
        )
        second_target_date = target_date + timedelta(days=2)
        second_target_schedule = Schedule.objects.create(
            club=club,
            day_of_week=second_target_date.weekday(),
            start_time=time(18, 0),
            end_time=time(19, 0),
            group_name=target_schedule.group_name,
            trainer=trainer,
            location=location,
            training_type=training_type,
            is_active=True,
        )
        full_student = self._create_student(
            club=club,
            fixture_id=fixture_id,
            label="Full",
            phone=f"+155571{phone_seed}",
        )
        partial_student = self._create_student(
            club=club,
            fixture_id=fixture_id,
            label="Partial",
            phone=f"+155572{phone_seed}",
        )
        mixed_student = self._create_student(
            club=club,
            fixture_id=fixture_id,
            label="Mixed",
            phone=f"+155573{phone_seed}",
        )
        settled_debt = self._create_open_debt(
            club=club,
            student=partial_student,
            trainer=trainer,
            location=location,
            training_type=training_type,
            target_date=today,
            fixture_id=fixture_id,
        )

        with override_settings(
            PAYMENT_PROVIDER=BankPaymentOrder.Provider.MOCK,
            MOCK_PAYMENT_BASE_URL="https://pay.example.test",
        ):
            mixed_order = create_bank_payment_order(
                club_id=club.id,
                student_id=mixed_student.id,
                tariff_id=full_tariff.id,
                source=BankPaymentOrder.Source.OWNER,
                created_by_id=owner.id,
                target_schedule_id=target_schedule.id,
                target_start_date=target_date,
            )
            with (
                patch(
                    "apps.billing.service_modules.sale_earnings._enqueue_sale_earning_after_commit"
                ),
                patch("django_q.tasks.async_task"),
            ):
                self._send_provider_event(
                    order=mixed_order,
                    status="APPROVED",
                    event_id=f"{fixture_id}-mixed-approved",
                    operation_id=f"{fixture_id}-mixed-operation",
                    paid_at=now,
                )
        mixed_manual_enrollment = ScheduleEnrollment.objects.create(
            club=club,
            student=mixed_student,
            schedule=second_target_schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=target_date,
            created_from=ScheduleEnrollment.CreatedFrom.MANUAL,
        )
        canonical_group = reconcile_fixture_group_to_active(
            club=club,
            actor_user_id=owner.id,
            schedule_ids=[target_schedule.id, second_target_schedule.id],
            canonical_name=target_schedule.group_name,
            responsible_trainer_id=trainer.id,
            idempotency_prefix=f"{fixture_id}-canonical-group",
            require_manual_operational_admission=True,
        )
        mixed_order.refresh_from_db()
        mixed_membership = TrainingGroupMembership.objects.for_club(club).get(
            student=mixed_student,
            training_group_id=canonical_group["training_group_id"],
        )

        with override_settings(
            PAYMENT_PROVIDER=BankPaymentOrder.Provider.MOCK,
            MOCK_PAYMENT_BASE_URL="https://pay.example.test",
        ):
            full_order = create_bank_payment_order(
                club_id=club.id,
                student_id=full_student.id,
                tariff_id=full_tariff.id,
                source=BankPaymentOrder.Source.OWNER,
                created_by_id=owner.id,
                target_schedule_id=target_schedule.id,
                target_training_group_id=canonical_group["training_group_id"],
                target_start_date=target_date,
            )
            partial_order = create_bank_payment_order(
                club_id=club.id,
                student_id=partial_student.id,
                tariff_id=partial_tariff.id,
                source=BankPaymentOrder.Source.OWNER,
                created_by_id=owner.id,
                seller_trainer_id=trainer.id,
                debt_ids=[settled_debt.id],
                target_schedule_id=target_schedule.id,
                target_training_group_id=canonical_group["training_group_id"],
                target_start_date=target_date,
            )
            with (
                patch(
                    "apps.billing.service_modules.sale_earnings._enqueue_sale_earning_after_commit"
                ),
                patch("django_q.tasks.async_task"),
            ):
                self._send_provider_event(
                    order=full_order,
                    status="APPROVED",
                    event_id=f"{fixture_id}-full-approved",
                    operation_id=f"{fixture_id}-full-operation",
                    paid_at=now,
                )
                self._send_provider_event(
                    order=partial_order,
                    status="APPROVED",
                    event_id=f"{fixture_id}-partial-approved",
                    operation_id=f"{fixture_id}-partial-operation",
                    paid_at=now,
                )

            create_sale_earning(partial_order.payment_id, club.id)
            TrainerPayrollPeriodClose.objects.create(
                club=club,
                period_start=today,
                period_end=today,
                closed_by=owner,
                reason="E2E payroll paid before provider refund",
            )

            full_event = self._send_provider_event(
                order=full_order,
                status="REFUNDED",
                event_id=f"{fixture_id}-full-refunded",
                operation_id=f"{fixture_id}-full-operation",
            )
            partial_event = self._send_provider_event(
                order=partial_order,
                status="REFUNDED_PARTIALLY",
                event_id=f"{fixture_id}-partial-refunded",
                operation_id=f"{fixture_id}-partial-operation",
            )
            mixed_event = self._send_provider_event(
                order=mixed_order,
                status="REFUNDED",
                event_id=f"{fixture_id}-mixed-refunded",
                operation_id=f"{fixture_id}-mixed-operation",
            )

        full_order.refresh_from_db()
        full_order.payment.refresh_from_db()
        partial_order.refresh_from_db()
        partial_order.payment.refresh_from_db()
        mixed_order.refresh_from_db()
        mixed_order.payment.refresh_from_db()
        full_case = PaymentRefundCase.objects.for_club(club).get(provider_event=full_event)
        partial_case = PaymentRefundCase.objects.for_club(club).get(provider_event=partial_event)
        mixed_case = PaymentRefundCase.objects.for_club(club).get(provider_event=mixed_event)
        earning = TrainerEarning.objects.for_club(club).get(
            payment_id=partial_order.payment_id,
            earning_source=TrainerEarning.Source.SALE,
        )

        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "owner": {
                "user_id": owner.id,
                "email": owner.email,
                "password": owner_password,
            },
            "full": {
                "order_id": full_order.id,
                "payment_id": full_order.payment_id,
                "subscription_id": full_order.subscription_id,
                "student_id": full_student.id,
                "student_name": str(full_student),
                "amount": str(full_order.amount_snapshot),
                "refund_case_id": full_case.id,
                "provider_event_id": full_event.id,
                "conversion_enrollment_id": full_order.payment.conversion_enrollment_id,
                "conversion_group_membership_id": full_order.payment.conversion_group_membership_id,
            },
            "partial": {
                "order_id": partial_order.id,
                "payment_id": partial_order.payment_id,
                "subscription_id": partial_order.subscription_id,
                "student_id": partial_student.id,
                "student_name": str(partial_student),
                "amount": str(partial_order.amount_snapshot),
                "refund_amount": "1000.00",
                "refund_case_id": partial_case.id,
                "provider_event_id": partial_event.id,
                "settled_debt_id": settled_debt.id,
                "sale_earning_id": earning.id,
                "conversion_group_membership_id": partial_order.payment.conversion_group_membership_id,
            },
            "mixed": {
                "order_id": mixed_order.id,
                "payment_id": mixed_order.payment_id,
                "student_id": mixed_student.id,
                "student_name": str(mixed_student),
                "amount": str(mixed_order.amount_snapshot),
                "refund_case_id": mixed_case.id,
                "provider_event_id": mixed_event.id,
                "conversion_enrollment_id": mixed_order.payment.conversion_enrollment_id,
            },
            "target_group": {
                "id": canonical_group["training_group_id"],
                "schedule_ids": [target_schedule.id, second_target_schedule.id],
                "rollout_mode": canonical_group["mode"],
                "new_writes_enabled": canonical_group["new_writes_enabled"],
                "manual_operational_admission_enabled": canonical_group[
                    "manual_operational_admission_enabled"
                ],
            },
            "mixed_legacy": {
                "student_id": mixed_student.id,
                "membership_id": mixed_membership.id,
                "manual_enrollment_id": mixed_manual_enrollment.id,
                "manual_schedule_id": second_target_schedule.id,
            },
            "payroll": {
                "closed_date": today.isoformat(),
                "open_date": (today + timedelta(days=1)).isoformat(),
            },
            "expected": {
                "full_reason": "E2E provider full refund",
                "partial_reason": "E2E provider partial refund",
                "refund_adjustment": "-200.00",
            },
        }

    def _create_owner(self, *, club: Club, fixture_id: str, password: str):
        email = f"owner-{fixture_id}@refund-resolution-e2e.local"
        owner = get_user_model().objects.create_user(
            username=email,
            email=email,
            password=password,
        )
        ClubMembership.objects.create(
            user=owner,
            club=club,
            role=ClubMembership.Role.OWNER,
        )
        return owner

    def _create_tariff(
        self,
        *,
        club: Club,
        training_type: TrainingType,
        fixture_id: str,
        label: str,
        payout_policy: str,
    ) -> Tariff:
        return Tariff.objects.create(
            club=club,
            training_type=training_type,
            name=f"Refund {label} 8-Pack {fixture_id}",
            price=Decimal("5000.00"),
            trainings_limit=8,
            duration_days=30,
            scope=Tariff.Scope.CLUB,
            trainer_payout_policy=payout_policy,
            is_active=True,
        )

    def _create_student(
        self,
        *,
        club: Club,
        fixture_id: str,
        label: str,
        phone: str,
    ) -> Student:
        return Student.objects.create(
            club=club,
            first_name=f"Refund{label}",
            last_name=fixture_id[-8:],
            phone=phone,
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
        )

    def _create_open_debt(
        self,
        *,
        club: Club,
        student: Student,
        trainer: Trainer,
        location: Location,
        training_type: TrainingType,
        target_date,
        fixture_id: str,
    ) -> Debt:
        schedule = Schedule.objects.create(
            club=club,
            day_of_week=target_date.weekday(),
            start_time=time(10, 0),
            end_time=time(11, 0),
            group_name=f"Refund Debt Proof {fixture_id}",
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=target_date,
            is_active=True,
        )
        checkin = Checkin.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            trainer=trainer,
            location=location,
            date=target_date,
            source=Checkin.Source.MANUAL,
            is_debt=True,
        )
        return Debt.objects.create(
            club=club,
            student=student,
            checkin=checkin,
            tariff_price=training_type.drop_in_price,
            reason="no_subscription",
        )

    def _send_provider_event(
        self,
        *,
        order: BankPaymentOrder,
        status: str,
        event_id: str,
        operation_id: str,
        paid_at=None,
    ):
        payload = {
            "webhookType": "acquiringInternetPayment",
            "event_id": event_id,
            "status": status,
            "paymentLinkId": order.provider_payment_link_id,
            "operationId": operation_id,
            "amount": str(order.amount_snapshot),
        }
        if paid_at is not None:
            payload["paid_at"] = paid_at.isoformat()
        return process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=json.dumps(payload).encode(),
            headers={},
            request_id=f"e2e-{event_id}",
        )
