from __future__ import annotations

import json
import uuid
from datetime import time, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.attendance.models import Checkin, Schedule, ScheduleEnrollment
from apps.attendance.services import create_checkin
from apps.attendance.services.training_group_memberships import create_training_group_membership
from apps.billing.models import Subscription, Tariff, TrainingType
from apps.clubs.models import Club, ClubMembership, ClubSettings, Location
from apps.clubs.timezones import club_localdate
from apps.common.management.commands.training_group_e2e import reconcile_fixture_group_to_active
from apps.retention.models import RetentionTask
from apps.students.models import Student
from apps.trainers.models import Trainer, TrainerLocation, TrainerRate


class Command(BaseCommand):
    help = "Prepare an isolated fixture for the unified commercial journey E2E."
    FIXTURE_PHONE_INDICES = range(1, 11)
    MAX_FIXTURE_PHONE_BLOCK_ATTEMPTS = 20

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
                "Prepared unified commercial journey E2E fixture "
                f"{fixture['fixture_id']} at {output_path} "
                f"(club_id={fixture['club_id']})"
            )
        )

    @transaction.atomic
    def _create_fixture(self) -> dict:
        now = timezone.now()
        unique, fixture_phones = self._reserve_fixture_phone_block()

        def fixture_phone(index: int) -> str:
            return fixture_phones[index]

        fixture_id = f"unified-commercial-e2e-{now:%Y%m%d%H%M%S}-{unique}"
        password = f"UnifiedCommercial-{unique}-pass"
        club = Club.objects.create(
            name=f"Jaguar Unified Commercial E2E {fixture_id}",
            city="E2E",
            disciplines=["muay_thai"],
            timezone="Europe/Moscow",
        )
        ClubSettings.objects.create(
            club=club,
            primary_color="#111111",
            accent_color="#FF6B00",
            club_name_display="Jaguar Unified Commercial E2E",
            unified_client_journey_enabled=True,
            commercial_journey_protocol_version=ClubSettings.CommercialJourneyProtocol.V2,
        )
        user_model = get_user_model()
        email = f"trainer-{fixture_id}@unified-commercial-e2e.local"
        user = user_model.objects.create_user(username=email, email=email, password=password)
        ClubMembership.objects.create(user=user, club=club, role=ClubMembership.Role.TRAINER)
        owner_user = user_model.objects.create_user(
            username=f"owner-{fixture_id}@unified-commercial-e2e.local",
            email=f"owner-{fixture_id}@unified-commercial-e2e.local",
        )
        ClubMembership.objects.create(user=owner_user, club=club, role=ClubMembership.Role.OWNER)
        trainer = Trainer.objects.create(
            club=club,
            first_name="Unified",
            last_name="Trainer",
            phone=fixture_phone(1),
            user=user,
        )
        location = Location.objects.create(club=club, name="Unified E2E Hall")
        training_type = TrainingType.objects.create(
            club=club,
            name="Unified E2E Group",
            slug=f"unified-e2e-{unique}",
            kind=TrainingType.Kind.GROUP,
            trial_free=True,
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
            name=f"Unified commercial 8-pack {fixture_id}",
            price=Decimal("5000.00"),
            trainings_limit=8,
            duration_days=30,
            scope=Tariff.Scope.CLUB,
            is_active=True,
        )
        trial_at = now + timedelta(days=2)
        local_trial = trial_at.astimezone(ZoneInfo(club.timezone))
        schedule = Schedule.objects.create(
            club=club,
            day_of_week=local_trial.weekday(),
            start_time=local_trial.time().replace(tzinfo=None, second=0, microsecond=0),
            end_time=(local_trial + timedelta(hours=1)).time().replace(
                tzinfo=None,
                second=0,
                microsecond=0,
            ),
            group_name="Unified Exact Trial",
            trainer=trainer,
            location=location,
            training_type=training_type,
        )
        trial_lead = self._lead(
            club=club,
            trainer=trainer,
            first_name=f"ExactTrial{unique}",
            phone=fixture_phone(2),
            lead_status=Student.LeadStatus.TRIAL_BOOKED,
        )
        ScheduleEnrollment.objects.create(
            club=club,
            student=trial_lead,
            schedule=schedule,
            status=ScheduleEnrollment.Status.TRIAL,
            trial_at=trial_at,
            created_from=ScheduleEnrollment.CreatedFrom.LEAD_BOOKING,
        )
        contact_lead = self._lead(
            club=club,
            trainer=trainer,
            first_name=f"ContactLead{unique}",
            phone=fixture_phone(3),
            lead_status=Student.LeadStatus.NEW,
        )
        archived_lead = Student.objects.create(
            club=club,
            first_name=f"ArchivedLead{unique}",
            phone=fixture_phone(4),
            status=Student.Status.LOST,
            lead_status=None,
            became_student_at=None,
            source="other",
            assigned_trainer=None,
            loss_reason=Student.LossReason.CHANGED_MIND,
        )
        student = Student.objects.create(
            club=club,
            first_name=f"NoEntitlement{unique}",
            phone=fixture_phone(5),
            status=Student.Status.ACTIVE,
            lead_status=None,
            became_student_at=now,
            source="other",
            assigned_trainer=trainer,
        )
        group_start_date = club_localdate(club) + timedelta(days=1)
        group_schedule = Schedule.objects.create(
            club=club,
            day_of_week=group_start_date.weekday(),
            start_time=time(18, 0),
            end_time=time(19, 0),
            group_name=f"Unified exact commercial group {unique}",
            trainer=trainer,
            location=location,
            training_type=training_type,
            is_active=True,
        )
        second_group_date = group_start_date + timedelta(days=2)
        second_group_schedule = Schedule.objects.create(
            club=club,
            day_of_week=second_group_date.weekday(),
            start_time=time(18, 0),
            end_time=time(19, 0),
            group_name=group_schedule.group_name,
            trainer=trainer,
            location=location,
            training_type=training_type,
            is_active=True,
        )
        canonical_group = reconcile_fixture_group_to_active(
            club=club,
            actor_user_id=user.id,
            schedule_ids=[group_schedule.id, second_group_schedule.id],
            canonical_name=group_schedule.group_name,
            responsible_trainer_id=trainer.id,
            idempotency_prefix=f"{fixture_id}-canonical-group",
            require_manual_operational_admission=True,
        )
        cash_trial_date = club_localdate(club)
        cash_trial_schedule = Schedule.objects.create(
            club=club,
            day_of_week=cash_trial_date.weekday(),
            start_time=time(17, 0),
            end_time=time(18, 0),
            group_name=f"Unified completed trial {unique}",
            trainer=trainer,
            location=location,
            training_type=training_type,
            is_active=True,
        )
        cash_group_lead = self._lead(
            club=club,
            trainer=trainer,
            first_name=f"CashGroupLead{unique}",
            phone=fixture_phone(6),
            lead_status=Student.LeadStatus.TRIAL_BOOKED,
        )
        cash_group_lead.status = Student.Status.TRIAL
        cash_group_lead.save(update_fields=["status", "updated_at"])
        ScheduleEnrollment.objects.create(
            club=club,
            student=cash_group_lead,
            schedule=cash_trial_schedule,
            status=ScheduleEnrollment.Status.TRIAL,
            starts_on=cash_trial_date,
            ends_on=cash_trial_date,
            created_from=ScheduleEnrollment.CreatedFrom.LEAD_BOOKING,
        )
        with patch("apps.attendance.services.async_task"):
            create_checkin(
                club_id=club.id,
                student_id=cash_group_lead.id,
                schedule_id=cash_trial_schedule.id,
                training_type_id=training_type.id,
                source=Checkin.Source.MANUAL,
                checkin_date=cash_trial_date,
            )
        cash_group_lead.refresh_from_db()
        if cash_group_lead.lead_status != Student.LeadStatus.TRIAL_DONE:
            raise CommandError("The cash-admission fixture must have an exact completed trial check-in.")
        RetentionTask.objects.create(
            club=club,
            student=cash_group_lead,
            trainer=trainer,
            task_type=RetentionTask.TaskType.POST_TRIAL,
            due_date=group_start_date,
        )
        sbp_group_lead = self._lead(
            club=club,
            trainer=trainer,
            first_name=f"SbpGroupLead{unique}",
            phone=fixture_phone(10),
            lead_status=Student.LeadStatus.NEW,
        )
        checkin_group_lead = self._lead(
            club=club,
            trainer=trainer,
            first_name=f"CheckinGroupLead{unique}",
            phone=fixture_phone(7),
            lead_status=Student.LeadStatus.NEW,
        )
        RetentionTask.objects.create(
            club=club,
            student=checkin_group_lead,
            trainer=trainer,
            task_type=RetentionTask.TaskType.NEW_LEAD,
            due_date=group_start_date,
        )
        reject_group_lead = self._lead(
            club=club,
            trainer=trainer,
            first_name=f"RejectGroupLead{unique}",
            phone=fixture_phone(8),
            lead_status=Student.LeadStatus.THINKING,
        )
        RetentionTask.objects.create(
            club=club,
            student=reject_group_lead,
            trainer=trainer,
            task_type=RetentionTask.TaskType.NEW_LEAD,
            due_date=group_start_date,
        )
        renewal_student = Student.objects.create(
            club=club,
            first_name=f"RenewalGroup{unique}",
            phone=fixture_phone(9),
            status=Student.Status.ACTIVE,
            lead_status=None,
            became_student_at=now,
            source="other",
            assigned_trainer=trainer,
        )
        renewal_source = Subscription.objects.create(
            club=club,
            student=renewal_student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            paid_amount=tariff.price,
            trainings_left=0,
            trainings_used=tariff.trainings_limit,
            expires_at=now + timedelta(days=5),
            scope=tariff.scope,
        )
        renewal_membership = create_training_group_membership(
            club_id=club.id,
            student_id=renewal_student.id,
            training_group_id=canonical_group["training_group_id"],
            starts_on=group_start_date,
            source="manual",
            actor_user_id=user.id,
            rationale="Isolated Slice 6 renewal source fixture.",
            idempotency_key=f"{fixture_id}-renewal-membership",
        )
        return {
            "fixture_id": fixture_id,
            "club_id": club.id,
            "trainer": {
                "user_id": user.id,
                "trainer_id": trainer.id,
                "email": email,
                "password": password,
            },
            "owner": {"user_id": owner_user.id},
            "trial_lead": self._person(trial_lead),
            "contact_lead": self._person(contact_lead),
            "archived_lead": self._person(archived_lead),
            "student": self._person(student),
            "cash_group_lead": self._person(cash_group_lead),
            "sbp_group_lead": self._person(sbp_group_lead),
            "checkin_group_lead": self._person(checkin_group_lead),
            "reject_group_lead": self._person(reject_group_lead),
            "commercial": {
                "protocol_version": "v2",
                "tariff_id": tariff.id,
                "tariff_name": tariff.name,
                "tariff_price": f"{tariff.price:.2f}",
                "tariff_trainings_limit": tariff.trainings_limit,
                "tariff_duration_days": tariff.duration_days,
                "training_type_id": training_type.id,
                "training_group_id": canonical_group["training_group_id"],
                "schedule_id": group_schedule.id,
                "group_name": group_schedule.group_name,
                "start_date": group_start_date.isoformat(),
                "second_schedule_id": second_group_schedule.id,
                "renewal_student_id": renewal_student.id,
                "renewal_source_subscription_id": renewal_source.id,
                "renewal_membership_id": renewal_membership.id,
            },
            "expected": {
                "trial_display": local_trial.strftime("%d.%m.%Y %H:%M"),
                "manual_receipt_status": "Оплата ожидает подтверждения владельцем",
                "sbp_receipt_status": "Ожидает оплаты через СБП",
            },
        }

    def _reserve_fixture_phone_block(self) -> tuple[str, dict[int, str]]:
        user_model = get_user_model()
        for _ in range(self.MAX_FIXTURE_PHONE_BLOCK_ATTEMPTS):
            unique = uuid.uuid4().hex[:8]
            phone_stem = f"{int(unique, 16) % 10_000_000:07d}"
            fixture_phones = {
                index: f"+79{phone_stem}{index:02d}" for index in self.FIXTURE_PHONE_INDICES
            }
            candidate_phones = tuple(fixture_phones.values())
            student_phone_conflict = Student.objects.unscoped().filter(phone__in=candidate_phones).exists()
            username_conflict = user_model.objects.filter(username__in=candidate_phones).exists()
            if not student_phone_conflict and not username_conflict:
                return unique, fixture_phones

        raise CommandError(
            "Unable to reserve an isolated fixture phone block after "
            f"{self.MAX_FIXTURE_PHONE_BLOCK_ATTEMPTS} attempts."
        )

    def _lead(
        self,
        *,
        club: Club,
        trainer: Trainer,
        first_name: str,
        phone: str,
        lead_status: str,
    ) -> Student:
        return Student.objects.create(
            club=club,
            first_name=first_name,
            phone=phone,
            status=Student.Status.LEAD,
            lead_status=lead_status,
            source="other",
            assigned_trainer=trainer,
        )

    def _person(self, student: Student) -> dict:
        return {"id": student.id, "first_name": student.first_name}
