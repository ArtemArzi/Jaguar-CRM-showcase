from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone
from ninja.testing import TestClient

from apps.attendance.models import PersonalAvailabilitySlot, Schedule, ScheduleEnrollment
from apps.attendance.personal_offers import personal_offer_payload
from apps.attendance.services.staff_intents import submit_staff_personal_intent
from apps.attendance.tests.factories import ScheduleFactory
from apps.billing.models import BankPaymentOrder, Subscription, Tariff, TariffComponent, TrainingType
from apps.billing.service_modules.catalog import update_tariff
from apps.billing.service_modules.personal_offers import resolve_personal_booking_offer
from apps.billing.tests.factories import SubscriptionFactory, TariffComponentFactory, TariffFactory, TrainingTypeFactory
from apps.clubs.models import ClubSettings
from apps.clubs.tests.factories import LocationFactory, UserFactory
from apps.clubs.timezones import club_localdate, club_zoneinfo
from apps.common.tests.helpers import make_auth_params as _auth_params
from apps.leads.models import LeadLifecycleEvent
from apps.leads.tests.factories import LeadFactory
from apps.students.models import Student
from apps.trainers.tests.factories import (
    TrainerFactory,
    TrainerLocationFactory,
)
from config.api import api

client = TestClient(api)


@pytest.fixture(autouse=True)
def _bypass_jwt_auth(bypass_jwt_auth):
    pass


def _complete_personal_offer(*, club, training_type, location):
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        location=location,
        scope=Tariff.Scope.LOCATION,
        price=Decimal("1250.00"),
        trainings_limit=1,
        duration_days=30,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
    )
    TariffComponentFactory(
        club=club,
        tariff=tariff,
        training_type=training_type,
        scope=Tariff.Scope.LOCATION,
        location=location,
        entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
        credits_total=1,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
        paid_amount_basis=tariff.price,
    )
    update_tariff(tariff_id=tariff.id, club_id=club.id, is_personal_booking_default=True)


@pytest.mark.django_db
def test_lead_commercial_context_is_staff_scoped_and_tenant_safe(
    settings,
    club,
    other_club,
    owner_user,
    trainer_user,
    student_user,
    parent_user,
):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    trainer = TrainerFactory(club=club, user=trainer_user, is_active=True)
    other_trainer = TrainerFactory(club=club, is_active=True)
    location = LocationFactory(club=club)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    TrainerLocationFactory(club=club, trainer=trainer, location=location, rate_personal=50)
    TrainerLocationFactory(club=club, trainer=other_trainer, location=location, rate_personal=50)
    _complete_personal_offer(club=club, training_type=training_type, location=location)
    lead = LeadFactory(club=club, assigned_trainer=trainer)
    slot_start = (timezone.now() + timedelta(days=7)).replace(hour=10, minute=0, second=0, microsecond=0)
    slot = PersonalAvailabilitySlot.objects.create(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
        starts_at=slot_start,
        ends_at=slot_start + timedelta(hours=1),
    )
    offer = resolve_personal_booking_offer(
        club_id=club.id,
        training_type_id=training_type.id,
        location_id=location.id,
    )
    digest = personal_offer_payload(slot=slot, offer=offer)["offer_digest"]
    submit_staff_personal_intent(
        club_id=club.id,
        slot_id=slot.id,
        student_id=lead.id,
        payment_method="pay_at_visit",
        subscription_id=None,
        offer_digest=digest,
        idempotency_key="lead-context-visit",
        actor_user_id=owner_user.id,
        bank_source=BankPaymentOrder.Source.OWNER,
    )

    owner_response = client.get(f"/leads/{lead.id}/commercial-context/", **_auth_params(owner_user, club))
    assert owner_response.status_code == 200
    assert owner_response.json()["student_id"] == lead.id
    assert owner_response.json()["attempts"][0]["status"] == "pay_at_visit"

    trainer_response = client.get(
        f"/leads/{lead.id}/commercial-context/",
        **_auth_params(trainer_user, club, role="trainer"),
    )
    assert trainer_response.status_code == 200

    for user, role in ((student_user, "student"), (parent_user, "parent")):
        response = client.get(f"/leads/{lead.id}/commercial-context/", **_auth_params(user, club, role=role))
        assert response.status_code == 403

    other_trainer_user = UserFactory()
    denied = client.get(
        f"/leads/{lead.id}/commercial-context/",
        **_auth_params(other_trainer_user, club, role="trainer"),
    )
    assert denied.status_code == 403

    foreign_lead = LeadFactory(club=other_club)
    foreign = client.get(
        f"/leads/{foreign_lead.id}/commercial-context/",
        **_auth_params(owner_user, club),
    )
    assert foreign.status_code == 404


@pytest.fixture
def trainer_with_user(club, trainer_user):
    return TrainerFactory(club=club, user=trainer_user)


@pytest.fixture
def other_trainer(club):
    return TrainerFactory(club=club)


@pytest.mark.django_db
class TestCreateLeadAPI:
    def test_owner_create_rejects_assigned_trainer_wrong_club(self, club, other_club, owner_user):
        other_trainer = TrainerFactory(club=other_club)

        response = client.post(
            "/leads/",
            json={
                "first_name": "Cross Tenant",
                "phone": "+79001234567",
                "assigned_trainer_id": other_trainer.id,
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "trainer_club_mismatch"

    def test_trainer_create_forces_self_assignment(
        self, club, trainer_user, trainer_with_user, other_trainer
    ):
        response = client.post(
            "/leads/",
            json={
                "first_name": "Trainer Lead",
                "phone": "+79001234568",
                "assigned_trainer_id": other_trainer.id,
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 201
        assert response.json()["assigned_trainer_id"] == trainer_with_user.id

    def test_trainer_create_duplicate_own_lead_returns_open_existing_action(
        self, club, trainer_user, trainer_with_user
    ):
        existing = LeadFactory(
            club=club,
            assigned_trainer=trainer_with_user,
            first_name="Existing",
            phone="+79001234570",
        )

        response = client.post(
            "/leads/",
            json={"first_name": "Duplicate", "phone": "+79001234570"},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 409
        data = response.json()
        assert data["code"] == "duplicate_phone"
        assert data["duplicate_scope"] == "own"
        assert data["can_open_existing"] is True
        assert data["existing_student"]["id"] == existing.id

    def test_trainer_create_duplicate_pool_lead_returns_pool_action(
        self, club, trainer_user, trainer_with_user
    ):
        LeadFactory(
            club=club,
            assigned_trainer=None,
            first_name="Pool",
            phone="+79001234571",
        )

        response = client.post(
            "/leads/",
            json={"first_name": "Duplicate", "phone": "+79001234571"},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 409
        data = response.json()
        assert data["code"] == "duplicate_phone"
        assert data["duplicate_scope"] == "pool"
        assert data["can_open_existing"] is False
        assert "existing_student" not in data

    def test_owner_create_child_lead_keeps_guardian_phone(self, club, owner_user):
        response = client.post(
            "/leads/",
            json={
                "first_name": "Child",
                "phone": "+79001234572",
                "is_child": True,
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 201
        data = response.json()
        assert data["phone"] == ""
        assert data["guardian_phone"] == "+79001234572"

    def test_owner_create_same_child_without_birth_date_keeps_legacy_duplicate_response(
        self,
        club,
        owner_user,
    ):
        payload = {
            "first_name": "Masha",
            "last_name": "Petrova",
            "phone": "",
            "guardian_phone": "+79001234573",
            "is_child": True,
        }
        first = client.post("/leads/", json=payload, **_auth_params(owner_user, club))
        duplicate = client.post("/leads/", json=payload, **_auth_params(owner_user, club))

        assert first.status_code == 201
        assert duplicate.status_code == 409
        assert duplicate.json()["code"] == "duplicate_phone"

    def test_owner_create_same_named_child_siblings_with_distinct_birth_dates(self, club, owner_user):
        shared_identity = {
            "first_name": "Masha",
            "last_name": "Petrova",
            "phone": "",
            "guardian_phone": "+79001234574",
            "is_child": True,
        }
        first = client.post(
            "/leads/",
            json={**shared_identity, "date_of_birth": "2017-05-06"},
            **_auth_params(owner_user, club),
        )
        sibling = client.post(
            "/leads/",
            json={**shared_identity, "date_of_birth": "2018-05-06"},
            **_auth_params(owner_user, club),
        )

        assert first.status_code == sibling.status_code == 201
        assert Student.objects.for_club(club).filter(guardian_phone="+79001234574").count() == 2


@pytest.mark.django_db
class TestUpdateLeadStatusAPI:
    def test_status_endpoint_rejects_trial_booking_bypass(self, club, owner_user):
        lead = LeadFactory(club=club, lead_status=Student.LeadStatus.NEW)

        response = client.post(
            f"/leads/{lead.id}/status",
            json={"status": Student.LeadStatus.TRIAL_BOOKED},
            **_auth_params(owner_user, club),
        )

        lead.refresh_from_db()
        assert response.status_code == 400
        assert response.json()["code"] == "trial_booking_requires_book_trial"
        assert lead.status == Student.Status.LEAD
        assert lead.lead_status == Student.LeadStatus.NEW
        assert lead.trial_date is None


@pytest.mark.django_db
class TestConvertLeadAPI:
    def test_owner_convert_unpaid_lead_returns_domain_error(self, club, owner_user):
        lead = LeadFactory(club=club, lead_status=Student.LeadStatus.THINKING)

        response = client.post(
            f"/leads/{lead.id}/convert",
            **_auth_params(owner_user, club),
        )

        lead.refresh_from_db()
        assert response.status_code == 400
        assert response.json()["code"] == "lead_conversion_requires_paid_subscription"
        assert lead.status == Student.Status.LEAD
        assert lead.lead_status == Student.LeadStatus.THINKING
        assert not LeadLifecycleEvent.objects.for_club(club).filter(student=lead).exists()

    def test_owner_convert_paid_active_lead(self, club, owner_user):
        lead = LeadFactory(club=club, lead_status=Student.LeadStatus.THINKING)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=training_type, price=Decimal("5000"))
        SubscriptionFactory(
            tariff=tariff,
            student=lead,
            status=Subscription.Status.ACTIVE,
            paid_amount=Decimal("5000"),
        )

        response = client.post(
            f"/leads/{lead.id}/convert",
            **_auth_params(owner_user, club),
        )

        lead.refresh_from_db()
        assert response.status_code == 200
        assert response.json()["status"] == Student.Status.ACTIVE
        assert response.json()["lead_status"] is None
        assert lead.status == Student.Status.ACTIVE
        assert lead.lead_status is None


@pytest.mark.django_db
class TestTrainerLeadScopeAPI:
    def test_trainer_list_only_returns_own_assigned_leads(
        self, club, trainer_user, trainer_with_user, other_trainer
    ):
        own_lead = LeadFactory(club=club, assigned_trainer=trainer_with_user, first_name="Own")
        LeadFactory(club=club, assigned_trainer=other_trainer, first_name="Other")
        LeadFactory(club=club, assigned_trainer=None, first_name="Unassigned")

        response = client.get(
            f"/leads/?assigned_trainer_id={other_trainer.id}",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["items"][0]["id"] == own_lead.id

    def test_trainer_scope_mine_returns_only_own_assigned_leads(
        self, club, trainer_user, trainer_with_user, other_trainer
    ):
        own_lead = LeadFactory(club=club, assigned_trainer=trainer_with_user)
        LeadFactory(club=club, assigned_trainer=other_trainer)
        LeadFactory(club=club, assigned_trainer=None)

        response = client.get(
            "/leads/?scope=mine",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["items"][0]["id"] == own_lead.id
        assert data["items"][0]["phone"] == own_lead.phone

    def test_trainer_scope_pool_returns_unassigned_without_raw_phone(
        self, club, trainer_user, trainer_with_user, other_trainer
    ):
        pool_lead = LeadFactory(
            club=club,
            assigned_trainer=None,
            first_name="Pool",
        )
        LeadFactory(club=club, assigned_trainer=trainer_with_user)
        LeadFactory(club=club, assigned_trainer=other_trainer)

        response = client.get(
            "/leads/?scope=pool",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        item = data["items"][0]
        assert item["id"] == pool_lead.id
        assert "phone" not in item
        assert "guardian_phone" not in item
        assert item["masked_phone"] != pool_lead.phone

    def test_trainer_scope_pool_masks_child_guardian_phone(
        self, club, trainer_user, trainer_with_user
    ):
        pool_lead = LeadFactory(
            club=club,
            assigned_trainer=None,
            is_child=True,
            phone="",
            guardian_phone="+79001234573",
        )

        response = client.get(
            "/leads/?scope=pool",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        item = response.json()["items"][0]
        assert item["id"] == pool_lead.id
        assert "phone" not in item
        assert "guardian_phone" not in item
        assert item["masked_phone"].endswith("73")

    def test_trainer_scope_all_still_hides_other_trainer_assigned_leads(
        self, club, trainer_user, trainer_with_user, other_trainer
    ):
        own_lead = LeadFactory(club=club, assigned_trainer=trainer_with_user)
        LeadFactory(club=club, assigned_trainer=other_trainer)
        LeadFactory(club=club, assigned_trainer=None)

        response = client.get(
            "/leads/?scope=all",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["items"][0]["id"] == own_lead.id

    def test_admin_scope_all_returns_assigned_and_pool_same_club(
        self, club, other_club, admin_user, trainer_with_user, other_trainer
    ):
        assigned = LeadFactory(club=club, assigned_trainer=trainer_with_user)
        pool = LeadFactory(club=club, assigned_trainer=None)
        LeadFactory(club=other_club, assigned_trainer=None)

        response = client.get(
            "/leads/?scope=all",
            **_auth_params(admin_user, club, role="admin"),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2
        assert {item["id"] for item in data["items"]} == {assigned.id, pool.id}

    def test_trainer_cannot_get_other_trainer_lead(
        self, club, trainer_user, trainer_with_user, other_trainer
    ):
        lead = LeadFactory(club=club, assigned_trainer=other_trainer)

        response = client.get(
            f"/leads/{lead.id}",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403

    def test_trainer_cannot_update_other_trainer_lead_status(
        self, club, trainer_user, trainer_with_user, other_trainer
    ):
        lead = LeadFactory(club=club, assigned_trainer=other_trainer, lead_status="new")

        response = client.post(
            f"/leads/{lead.id}/status",
            json={"status": "contacted"},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403

    def test_trainer_cannot_book_other_trainer_lead_trial(
        self, club, trainer_user, trainer_with_user, other_trainer
    ):
        lead = LeadFactory(club=club, assigned_trainer=other_trainer, lead_status="new")

        response = client.post(
            f"/leads/{lead.id}/book-trial",
            json={"trial_date": timezone.now().isoformat()},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403

    def test_trainer_can_book_own_thinking_lead_trial_with_schedule(
        self, club, trainer_user, trainer_with_user
    ):
        trial_day = club_localdate(club) + timedelta(days=7)
        trial_dt = timezone.make_aware(
            datetime.combine(trial_day, time(18, 0)),
            club_zoneinfo(club),
        )
        schedule = ScheduleFactory(
            club=club,
            trainer=trainer_with_user,
            day_of_week=trial_dt.date().weekday(),
            start_time=time(18, 0),
            end_time=time(19, 0),
        )
        lead = LeadFactory(
            club=club,
            assigned_trainer=trainer_with_user,
            lead_status=Student.LeadStatus.THINKING,
        )

        response = client.post(
            f"/leads/{lead.id}/book-trial",
            json={
                "trial_date": trial_dt.isoformat(),
                "schedule_id": schedule.id,
                "occurrence_date": trial_day.isoformat(),
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )

        lead.refresh_from_db()
        enrollment = ScheduleEnrollment.objects.get(club=club, student=lead, schedule=schedule)
        assert response.status_code == 200
        assert response.json()["lead_status"] == Student.LeadStatus.TRIAL_BOOKED
        assert response.json()["trial_date"] is not None
        assert lead.status == Student.Status.TRIAL
        assert enrollment.status == ScheduleEnrollment.Status.TRIAL
        assert enrollment.starts_on == trial_dt.date()
        assert enrollment.ends_on == trial_dt.date()
        assert enrollment.created_from == ScheduleEnrollment.CreatedFrom.LEAD_BOOKING

    def test_group_trial_api_accepts_canonical_occurrence_without_client_time(
        self,
        club,
        trainer_user,
        trainer_with_user,
    ):
        club.timezone = "Asia/Yekaterinburg"
        club.save(update_fields=["timezone"])
        occurrence_date = date(2026, 7, 16)
        schedule = ScheduleFactory(
            club=club,
            trainer=trainer_with_user,
            day_of_week=occurrence_date.weekday(),
            start_time=time(0, 5),
            end_time=time(1, 5),
        )
        lead = LeadFactory(
            club=club,
            assigned_trainer=trainer_with_user,
            lead_status=Student.LeadStatus.THINKING,
        )
        now = timezone.make_aware(datetime(2026, 7, 15, 23, 55), club_zoneinfo(club))
        canonical_start = timezone.make_aware(
            datetime.combine(occurrence_date, time(0, 5)),
            club_zoneinfo(club),
        )

        with patch("apps.leads.services.timezone.now", return_value=now):
            response = client.post(
                f"/leads/{lead.id}/book-trial",
                json={
                    "mode": "group",
                    "schedule_id": schedule.id,
                    "occurrence_date": occurrence_date.isoformat(),
                },
                **_auth_params(trainer_user, club, role="trainer"),
            )

        assert response.status_code == 200
        lead.refresh_from_db()
        enrollment = ScheduleEnrollment.objects.for_club(club).get(student=lead, schedule=schedule)
        assert lead.trial_date == canonical_start
        assert enrollment.trial_at == canonical_start

    def test_group_trial_api_rejects_legacy_unscheduled_payload_without_mutation(
        self,
        club,
        trainer_user,
        trainer_with_user,
    ):
        lead = LeadFactory(
            club=club,
            assigned_trainer=trainer_with_user,
            lead_status=Student.LeadStatus.THINKING,
        )
        future_time = timezone.now() + timedelta(days=1)

        response = client.post(
            f"/leads/{lead.id}/book-trial",
            json={"mode": "group", "trial_date": future_time.isoformat()},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "trial_schedule_required"
        lead.refresh_from_db()
        assert lead.status == Student.Status.LEAD
        assert lead.lead_status == Student.LeadStatus.THINKING
        assert lead.trial_date is None
        assert not ScheduleEnrollment.objects.for_club(club).filter(student=lead).exists()
        assert not LeadLifecycleEvent.objects.for_club(club).filter(student=lead).exists()

    def test_personal_trial_api_is_rejected_before_mutation(
        self,
        club,
        trainer_user,
        trainer_with_user,
    ):
        trial_day = club_localdate(club) + timedelta(days=7)
        starts_at = timezone.make_aware(
            datetime.combine(trial_day, time(16, 0)),
            club_zoneinfo(club),
        )
        ends_at = timezone.make_aware(
            datetime.combine(trial_day, time(17, 0)),
            club_zoneinfo(club),
        )
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(
            club=club,
            kind=TrainingType.Kind.PERSONAL,
            trial_free=True,
        )
        TrainerLocationFactory(club=club, trainer=trainer_with_user, location=location)
        lead = LeadFactory(
            club=club,
            assigned_trainer=trainer_with_user,
            lead_status=Student.LeadStatus.CONTACTED,
        )

        response = client.post(
            f"/leads/{lead.id}/book-trial",
            json={
                "mode": "personal",
                "trial_date": starts_at.isoformat(),
                "starts_at": starts_at.isoformat(),
                "ends_at": ends_at.isoformat(),
                "location_id": location.id,
                "training_type_id": training_type.id,
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "personal_trial_not_supported"
        lead.refresh_from_db()
        assert lead.status == Student.Status.LEAD
        assert lead.lead_status == Student.LeadStatus.CONTACTED
        assert lead.trial_date is None
        assert not Schedule.objects.for_club(club).filter(one_time_date=starts_at.date()).exists()
        assert not ScheduleEnrollment.objects.for_club(club).filter(student=lead).exists()
        assert not LeadLifecycleEvent.objects.for_club(club).filter(student=lead).exists()

    def test_trainer_cannot_book_own_lead_trial_on_other_trainer_schedule(
        self, club, trainer_user, trainer_with_user, other_trainer
    ):
        trial_day = club_localdate(club) + timedelta(days=7)
        trial_dt = timezone.make_aware(
            datetime.combine(trial_day, time(18, 0)),
            club_zoneinfo(club),
        )
        schedule = ScheduleFactory(
            club=club,
            trainer=other_trainer,
            day_of_week=trial_dt.date().weekday(),
            start_time=time(18, 0),
            end_time=time(19, 0),
        )
        lead = LeadFactory(
            club=club,
            assigned_trainer=trainer_with_user,
            lead_status=Student.LeadStatus.THINKING,
        )

        response = client.post(
            f"/leads/{lead.id}/book-trial",
            json={
                "trial_date": trial_dt.isoformat(),
                "schedule_id": schedule.id,
                "occurrence_date": trial_day.isoformat(),
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )

        lead.refresh_from_db()
        assert response.status_code == 400
        assert response.json()["code"] == "schedule_trainer_mismatch"
        assert lead.status == Student.Status.LEAD
        assert lead.lead_status == Student.LeadStatus.THINKING
        assert lead.trial_date is None
        assert not ScheduleEnrollment.objects.filter(club=club, student=lead).exists()
        assert not LeadLifecycleEvent.objects.filter(student=lead).exists()

    def test_trainer_cannot_lose_other_trainer_lead(
        self, club, trainer_user, trainer_with_user, other_trainer
    ):
        lead = LeadFactory(club=club, assigned_trainer=other_trainer, lead_status="thinking")

        response = client.post(
            f"/leads/{lead.id}/lose",
            json={"loss_reason": "expensive"},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403


@pytest.mark.django_db
class TestLeadAssignmentAPI:
    def test_trainer_can_claim_unassigned_active_lead(self, club, trainer_user, trainer_with_user):
        lead = LeadFactory(club=club, assigned_trainer=None, lead_status=Student.LeadStatus.NEW)

        response = client.post(
            f"/leads/{lead.id}/claim",
            json={},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        lead.refresh_from_db()
        assert response.status_code == 200
        assert response.json()["assigned_trainer_id"] == trainer_with_user.id
        assert lead.assigned_trainer_id == trainer_with_user.id
        event = LeadLifecycleEvent.objects.get(student=lead)
        assert event.event_type == LeadLifecycleEvent.EventType.LEAD_CLAIMED
        assert event.new_trainer_id == trainer_with_user.id
        assert event.actor_id == trainer_user.id

    def test_claim_assigned_lead_returns_conflict(
        self, club, trainer_user, trainer_with_user, other_trainer
    ):
        lead = LeadFactory(club=club, assigned_trainer=other_trainer)

        response = client.post(
            f"/leads/{lead.id}/claim",
            json={},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        lead.refresh_from_db()
        assert response.status_code == 409
        assert lead.assigned_trainer_id == other_trainer.id

    def test_trainer_release_own_lead_requires_reason(
        self, club, trainer_user, trainer_with_user
    ):
        lead = LeadFactory(club=club, assigned_trainer=trainer_with_user)

        response = client.post(
            f"/leads/{lead.id}/release",
            json={"reason": ""},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "reason_required"

    def test_trainer_can_release_own_lead_with_reason(
        self, club, trainer_user, trainer_with_user
    ):
        lead = LeadFactory(club=club, assigned_trainer=trainer_with_user)

        response = client.post(
            f"/leads/{lead.id}/release",
            json={"reason": "wants another location"},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        lead.refresh_from_db()
        assert response.status_code == 200
        assert response.json()["assigned_trainer_id"] is None
        assert lead.assigned_trainer_id is None
        event = LeadLifecycleEvent.objects.get(student=lead)
        assert event.event_type == LeadLifecycleEvent.EventType.LEAD_RELEASED
        assert event.old_trainer_id == trainer_with_user.id
        assert event.new_trainer_id is None
        assert event.reason == "wants another location"

    def test_admin_can_assign_reassign_and_unassign_same_club_trainer(
        self, club, admin_user, trainer_with_user, other_trainer
    ):
        lead = LeadFactory(club=club, assigned_trainer=None)

        assign_response = client.post(
            f"/leads/{lead.id}/assign",
            json={"trainer_id": trainer_with_user.id, "reason": "owner decision"},
            **_auth_params(admin_user, club, role="admin"),
        )
        reassign_response = client.post(
            f"/leads/{lead.id}/assign",
            json={"trainer_id": other_trainer.id, "reason": "schedule fit"},
            **_auth_params(admin_user, club, role="admin"),
        )
        unassign_response = client.post(
            f"/leads/{lead.id}/assign",
            json={"trainer_id": None, "reason": "back to pool"},
            **_auth_params(admin_user, club, role="admin"),
        )

        lead.refresh_from_db()
        assert assign_response.status_code == 200
        assert reassign_response.status_code == 200
        assert unassign_response.status_code == 200
        assert lead.assigned_trainer_id is None
        events = list(LeadLifecycleEvent.objects.filter(student=lead).order_by("created_at"))
        assert [event.event_type for event in events] == [
            LeadLifecycleEvent.EventType.LEAD_ASSIGNED,
            LeadLifecycleEvent.EventType.LEAD_REASSIGNED,
            LeadLifecycleEvent.EventType.LEAD_RELEASED,
        ]
        assert events[0].new_trainer_id == trainer_with_user.id
        assert events[1].old_trainer_id == trainer_with_user.id
        assert events[1].new_trainer_id == other_trainer.id
        assert events[2].old_trainer_id == other_trainer.id
        assert events[2].new_trainer_id is None

    def test_admin_assign_rejects_cross_club_trainer(self, club, other_club, admin_user):
        lead = LeadFactory(club=club, assigned_trainer=None)
        other_club_trainer = TrainerFactory(club=other_club)

        response = client.post(
            f"/leads/{lead.id}/assign",
            json={"trainer_id": other_club_trainer.id, "reason": "bad tenant"},
            **_auth_params(admin_user, club, role="admin"),
        )

        lead.refresh_from_db()
        assert response.status_code == 400
        assert response.json()["code"] == "trainer_club_mismatch"
        assert lead.assigned_trainer_id is None

    def test_trainer_cannot_assign_lead(self, club, trainer_user, trainer_with_user, other_trainer):
        lead = LeadFactory(club=club, assigned_trainer=trainer_with_user)

        response = client.post(
            f"/leads/{lead.id}/assign",
            json={"trainer_id": other_trainer.id, "reason": "not allowed"},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403
