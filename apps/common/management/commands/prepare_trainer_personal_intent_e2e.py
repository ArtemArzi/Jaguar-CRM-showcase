from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.attendance.models import PersonalAvailabilitySlot, TrainingGroupRolloutState
from apps.attendance.services import generate_kiosk_pin
from apps.billing.models import Discount, Subscription, SubscriptionComponent, Tariff, TariffComponent, TrainingType
from apps.billing.services import create_discount, create_tariff
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.clubs.timezones import club_localdate, club_zoneinfo
from apps.students.models import Student
from apps.trainers.models import Trainer, TrainerLocation, TrainerRate


class Command(BaseCommand):
    help = "Prepare an isolated flag-on staff personal intent E2E fixture."

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
                "Prepared trainer personal intent E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']}, trainer_id={fixture['trainer']['trainer_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        unique = uuid.uuid4().hex[:8]
        phone_seed = str(int(unique, 16) % 10_000_000).zfill(7)
        fixture_id = f"trainer-personal-intent-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        trainer_password = f"TrainerPersonalIntent-{unique}-pass"

        club = Club.objects.create(
            name=f"Jaguar Trainer Personal Intent E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            # Use a non-default club zone so the direct-payment retry proves
            # that UTC receipt instants are restored as club wall time.
            timezone="Asia/Yekaterinburg",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Personal Intent E2E",
            unified_client_journey_enabled=True,
        )
        TrainingGroupRolloutState.objects.create(club=club, mode=TrainingGroupRolloutState.Mode.OFF)
        location = Location.objects.create(
            club=club,
            name=f"Personal Intent Hall {fixture_id}",
            address="Trainer personal intent E2E fixture",
        )

        trainer_user = self._create_user(
            fixture_id=fixture_id,
            role="trainer",
            password=trainer_password,
        )
        ClubMembership.objects.create(user=trainer_user, club=club, role=ClubMembership.Role.TRAINER)
        trainer = Trainer.objects.create(
            club=club,
            first_name="Intent",
            last_name="Trainer",
            phone=f"+155581{phone_seed}",
            user=trainer_user,
        )
        TrainerLocation.objects.create(club=club, trainer=trainer, location=location)

        training_type = TrainingType.objects.create(
            club=club,
            name=f"Personal Intent {fixture_id}",
            slug=f"personal-intent-{unique}",
            kind=TrainingType.Kind.PERSONAL,
            drop_in_price=Decimal("9999.00"),
            trial_free=False,
        )
        tariff = create_tariff(
            club_id=club.id,
            name=f"Персоналка по серверным условиям {fixture_id}",
            training_type_id=training_type.id,
            price=Decimal("2700.00"),
            trainings_limit=1,
            duration_days=30,
            scope=Tariff.Scope.LOCATION,
            location_id=location.id,
            trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
            components=[
                {
                    "name": f"Персональная тренировка {fixture_id}",
                    "training_type_id": training_type.id,
                    "entitlement_kind": TariffComponent.EntitlementKind.FINITE_CREDITS,
                    "credits_total": 1,
                    "scope": Tariff.Scope.LOCATION,
                    "location_id": location.id,
                    "trainer_payout_policy": Tariff.PayoutPolicy.ON_CHECKIN,
                    "paid_amount_basis": Decimal("2700.00"),
                }
            ],
            is_personal_booking_default=True,
        )
        tariff_component = TariffComponent.objects.for_club(club).get(tariff=tariff)
        personal_discount = create_discount(
            club_id=club.id,
            name=f"Разовая скидка 500 руб {fixture_id}",
            discount_type=Discount.Type.FIXED,
            value=Decimal("500.00"),
        )
        TrainerRate.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            percent=Decimal("50.00"),
        )

        cash_lead = Student.objects.create(
            club=club,
            first_name="Cash",
            last_name="IntentLead",
            phone=f"+155582{phone_seed}",
            status=Student.Status.LEAD,
            source=Student.Source.OTHER,
            lead_status=Student.LeadStatus.CONTACTED,
            assigned_trainer=trainer,
        )
        sbp_student = Student.objects.create(
            club=club,
            first_name="Sbp",
            last_name="IntentStudent",
            phone=f"+155583{phone_seed}",
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            assigned_trainer=trainer,
        )
        entitlement_student = Student.objects.create(
            club=club,
            first_name="Entitlement",
            last_name="IntentStudent",
            phone=f"+155584{phone_seed}",
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            assigned_trainer=trainer,
        )
        pay_at_visit_student = Student.objects.create(
            club=club,
            first_name="PayAtVisit",
            last_name="IntentStudent",
            phone=f"+155585{phone_seed}",
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            assigned_trainer=trainer,
        )
        pay_at_visit_sbp_student = Student.objects.create(
            club=club,
            first_name="PayAtVisitSbp",
            last_name="IntentStudent",
            phone=f"+155588{phone_seed}",
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            assigned_trainer=trainer,
        )
        terminal_sbp_student = Student.objects.create(
            club=club,
            first_name="TerminalSbp",
            last_name="IntentStudent",
            phone=f"+155586{phone_seed}",
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            assigned_trainer=trainer,
        )
        direct_student = Student.objects.create(
            club=club,
            first_name="Direct",
            last_name="IntentStudent",
            phone=f"+155587{phone_seed}",
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            assigned_trainer=trainer,
        )
        direct_terminal_sbp_student = Student.objects.create(
            club=club,
            first_name="DirectTerminalSbp",
            last_name="IntentStudent",
            phone=f"+155589{phone_seed}",
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            assigned_trainer=trainer,
        )
        correction_student = Student.objects.create(
            club=club,
            first_name="Correction",
            last_name="IntentStudent",
            phone=f"+155590{phone_seed}",
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            assigned_trainer=trainer,
        )
        direct_correction_student = Student.objects.create(
            club=club,
            first_name="DirectCorrection",
            last_name="IntentStudent",
            phone=f"+155591{phone_seed}",
            status=Student.Status.ACTIVE,
            source=Student.Source.OTHER,
            assigned_trainer=trainer,
        )
        entitlement_subscription = Subscription.objects.create(
            club=club,
            student=entitlement_student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            paid_amount=tariff.price,
            trainings_left=1,
            trainings_used=0,
            expires_at=now + timedelta(days=30),
            scope=tariff.scope,
            location=location,
            trainer_payout_policy_snapshot=Tariff.PayoutPolicy.ON_CHECKIN,
        )
        SubscriptionComponent.objects.create(
            club=club,
            subscription=entitlement_subscription,
            tariff_component=tariff_component,
            name_snapshot=tariff_component.name,
            training_type=training_type,
            entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
            credits_total=1,
            credits_left=1,
            credits_used=0,
            scope=Tariff.Scope.LOCATION,
            location=location,
            trainer_payout_policy_snapshot=Tariff.PayoutPolicy.ON_CHECKIN,
            paid_amount_basis_snapshot=tariff.price,
            unit_amount_basis_snapshot=tariff.price,
            is_active=True,
        )

        today = club_localdate(club)
        cash_slot = self._slot(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            target_day=today + timedelta(days=7),
            starts_at=time(10, 0),
            ends_at=time(11, 0),
        )
        sbp_slot = self._slot(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            target_day=today + timedelta(days=8),
            starts_at=time(14, 0),
            ends_at=time(15, 0),
        )
        entitlement_slot = self._slot(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            target_day=today + timedelta(days=9),
            starts_at=time(11, 0),
            ends_at=time(12, 0),
        )
        pay_at_visit_slot = self._slot(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            target_day=today + timedelta(days=10),
            starts_at=time(12, 0),
            ends_at=time(13, 0),
        )
        pay_at_visit_sbp_slot = self._slot(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            target_day=today + timedelta(days=11),
            starts_at=time(13, 0),
            ends_at=time(14, 0),
        )
        terminal_sbp_slot = self._slot(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            target_day=today + timedelta(days=12),
            starts_at=time(14, 0),
            ends_at=time(15, 0),
        )
        correction_slot = self._slot(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            target_day=today + timedelta(days=13),
            starts_at=time(10, 0),
            ends_at=time(11, 0),
        )
        correction_destination_slot = self._slot(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            target_day=today + timedelta(days=16),
            starts_at=time(10, 0),
            ends_at=time(11, 0),
        )
        direct_day = today + timedelta(days=14)
        direct_terminal_day = today + timedelta(days=15)
        direct_correction_day = today + timedelta(days=17)
        direct_terminal_starts_at = timezone.make_aware(
            datetime.combine(direct_terminal_day, time(15, 0)),
            club_zoneinfo(club),
        )
        kiosk_pin = generate_kiosk_pin(club_id=club.id)

        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "club_timezone": club.timezone,
            "trainer": {
                "user_id": trainer_user.id,
                "trainer_id": trainer.id,
                "email": trainer_user.email,
                "password": trainer_password,
                "name": f"{trainer.first_name} {trainer.last_name}",
            },
            "cash_lead": {"student_id": cash_lead.id, "name": str(cash_lead)},
            "sbp_student": {"student_id": sbp_student.id, "name": str(sbp_student)},
            "entitlement_student": {
                "student_id": entitlement_student.id,
                "name": str(entitlement_student),
                "subscription_id": entitlement_subscription.id,
            },
            "pay_at_visit_student": {
                "student_id": pay_at_visit_student.id,
                "name": str(pay_at_visit_student),
            },
            "pay_at_visit_sbp_student": {
                "student_id": pay_at_visit_sbp_student.id,
                "name": str(pay_at_visit_sbp_student),
            },
            "terminal_sbp_student": {
                "student_id": terminal_sbp_student.id,
                "name": str(terminal_sbp_student),
            },
            "direct_student": {"student_id": direct_student.id, "name": str(direct_student)},
            "direct_terminal_sbp_student": {
                "student_id": direct_terminal_sbp_student.id,
                "name": str(direct_terminal_sbp_student),
            },
            "correction_student": {
                "student_id": correction_student.id,
                "name": str(correction_student),
            },
            "direct_correction_student": {
                "student_id": direct_correction_student.id,
                "name": str(direct_correction_student),
            },
            "kiosk": {"activation_pin": kiosk_pin},
            "location": {"id": location.id, "name": location.name},
            "personal_training_type": {"id": training_type.id, "name": training_type.name},
            "personal_tariff": {"id": tariff.id, "name": tariff.name, "price": str(tariff.price)},
            "personal_discount": {
                "id": personal_discount.id,
                "name": personal_discount.name,
                "value": str(personal_discount.value),
            },
            "cash_slot": self._slot_fixture(cash_slot),
            "sbp_slot": self._slot_fixture(sbp_slot),
            "entitlement_slot": self._slot_fixture(entitlement_slot),
            "pay_at_visit_slot": self._slot_fixture(pay_at_visit_slot),
            "pay_at_visit_sbp_slot": self._slot_fixture(pay_at_visit_sbp_slot),
            "terminal_sbp_slot": self._slot_fixture(terminal_sbp_slot),
            "correction_slot": self._slot_fixture(correction_slot),
            "correction_destination_slot": self._slot_fixture(correction_destination_slot),
            "direct_booking": {
                "date": direct_day.isoformat(),
                "start_time": "15:00",
                "end_time": "16:00",
            },
            "direct_terminal_booking": {
                "date": direct_terminal_day.isoformat(),
                "start_time": "15:00",
                "end_time": "16:00",
                "starts_at_utc": direct_terminal_starts_at.astimezone(UTC).isoformat(),
            },
            "direct_correction_booking": {
                "date": direct_correction_day.isoformat(),
                "start_time": "10:00",
                "end_time": "11:00",
            },
            "expected": {
                "amount": str(tariff.price),
                "amount_display": "2\u00a0700\u00a0₽",
                "discount_amount": "500.00",
                "discount_amount_display": "500\u00a0₽",
                "discounted_amount": "2200.00",
                "discounted_amount_display": "2\u00a0200\u00a0₽",
                "cash_status": "Оплата ожидает подтверждения владельцем",
                "sbp_status": "Ожидает оплаты через СБП",
                "entitlement_status": "Записан",
                "pay_at_visit_status": "К оплате при посещении",
                "debt_open_status": "Долг ожидает оплаты",
            },
            "created_at": now.isoformat(),
        }

    def _slot(self, *, club, trainer, location, training_type, target_day, starts_at, ends_at):
        zoneinfo = club_zoneinfo(club)
        return PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=timezone.make_aware(datetime.combine(target_day, starts_at), zoneinfo),
            ends_at=timezone.make_aware(datetime.combine(target_day, ends_at), zoneinfo),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )

    @staticmethod
    def _slot_fixture(slot: PersonalAvailabilitySlot) -> dict:
        return {
            "id": slot.id,
            "date": slot.starts_at.date().isoformat(),
            "start_time": slot.starts_at.strftime("%H:%M"),
            "end_time": slot.ends_at.strftime("%H:%M"),
        }

    @staticmethod
    def _create_user(*, fixture_id: str, role: str, password: str):
        user_model = get_user_model()
        email = f"{role}-{fixture_id}@trainer-personal-intent-e2e.local"
        return user_model.objects.create_user(username=email, email=email, password=password)
