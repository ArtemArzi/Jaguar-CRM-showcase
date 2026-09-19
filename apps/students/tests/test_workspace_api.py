from decimal import Decimal

import pytest
from django.utils import timezone
from ninja.testing import TestClient

from apps.attendance.models import ScheduleEnrollment
from apps.attendance.tests.factories import ScheduleFactory
from apps.billing.models import Payment, Subscription, TrainingType
from apps.billing.tests.factories import (
    PaymentFactory,
    SubscriptionComponentFactory,
    SubscriptionFactory,
    TariffFactory,
)
from apps.clubs.models import ClubSettings
from apps.common.tests.helpers import make_auth_params as _auth_params
from apps.leads.models import LeadLifecycleEvent
from apps.leads.tests.factories import LeadFactory
from apps.students.models import Student
from apps.students.tests.factories import StudentFactory
from apps.trainers.tests.factories import TrainerFactory
from config.api import api

client = TestClient(api)


@pytest.fixture(autouse=True)
def _bypass_jwt_auth(bypass_jwt_auth):
    pass


def _enable_unified(settings, club):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(
        club=club,
        defaults={"unified_client_journey_enabled": True},
    )


@pytest.mark.django_db
def test_flag_on_students_workspace_excludes_leads_and_archived_prospects(
    settings,
    club,
    owner_user,
):
    _enable_unified(settings, club)
    student = StudentFactory(
        club=club,
        status=Student.Status.ACTIVE,
        lead_status=None,
        became_student_at=timezone.now(),
    )
    former = StudentFactory(
        club=club,
        status=Student.Status.LOST,
        lead_status=None,
        became_student_at=timezone.now(),
    )
    LeadFactory(club=club)
    StudentFactory(
        club=club,
        status=Student.Status.LOST,
        lead_status=None,
        became_student_at=None,
    )

    response = client.get(
        "/students/?workspace=students",
        **_auth_params(owner_user, club),
    )

    assert response.status_code == 200
    items = {item["id"]: item for item in response.json()["items"]}
    assert set(items) == {student.id, former.id}
    assert items[student.id]["commercial_segment"] == "no_crm_entitlement"
    assert items[former.id]["commercial_segment"] == "former"


@pytest.mark.django_db
def test_flag_off_students_list_preserves_legacy_lead_visibility(settings, club, owner_user):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = False
    lead = LeadFactory(club=club)

    response = client.get("/students/", **_auth_params(owner_user, club))

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [lead.id]
    assert response.json()["items"][0]["commercial_segment"] is None


@pytest.mark.django_db
def test_commercial_segment_priority_and_filter(settings, club, owner_user):
    _enable_unified(settings, club)
    at_risk = StudentFactory(
        club=club,
        status=Student.Status.AT_RISK,
        lead_status=None,
        became_student_at=timezone.now(),
    )
    entitlement = StudentFactory(
        club=club,
        status=Student.Status.ACTIVE,
        lead_status=None,
        became_student_at=timezone.now(),
    )
    active_subscription = SubscriptionFactory(
        club=club,
        student=entitlement,
        status=Subscription.Status.ACTIVE,
        paid_amount=Decimal("5000"),
    )
    SubscriptionComponentFactory(
        club=club,
        subscription=active_subscription,
    )
    pending = StudentFactory(
        club=club,
        status=Student.Status.ACTIVE,
        lead_status=None,
        became_student_at=timezone.now(),
    )
    subscription_without_component = StudentFactory(
        club=club,
        status=Student.Status.ACTIVE,
        lead_status=None,
        became_student_at=timezone.now(),
    )
    SubscriptionFactory(
        club=club,
        student=subscription_without_component,
        status=Subscription.Status.ACTIVE,
        paid_amount=Decimal("5000"),
    )
    schedule = ScheduleFactory(
        club=club,
        training_type__kind=TrainingType.Kind.GROUP,
    )
    enrollment = ScheduleEnrollment.objects.create(
        club=club,
        student=pending,
        schedule=schedule,
        status=ScheduleEnrollment.Status.ACTIVE,
        starts_on=timezone.localdate(),
        created_from=ScheduleEnrollment.CreatedFrom.PAID_CONVERSION,
    )
    pending_tariff = TariffFactory(
        club=club,
        training_type=schedule.training_type,
    )
    pending_subscription = SubscriptionFactory(
        club=club,
        student=pending,
        tariff=pending_tariff,
        status=Subscription.Status.PENDING,
    )
    PaymentFactory(
        club=club,
        student=pending,
        tariff=pending_tariff,
        subscription=pending_subscription,
        payment_method=Payment.Method.CASH,
        status=Payment.Status.PENDING,
        target_schedule=schedule,
        target_start_date=timezone.localdate(),
        target_training_type_id_snapshot=schedule.training_type_id,
        target_training_type_kind_snapshot=TrainingType.Kind.GROUP,
        target_location_id_snapshot=schedule.location_id,
        conversion_enrollment=enrollment,
    )

    response = client.get(
        "/students/?workspace=students",
        **_auth_params(owner_user, club),
    )
    segments = {item["id"]: item["commercial_segment"] for item in response.json()["items"]}
    assert segments[at_risk.id] == "at_risk"
    assert segments[entitlement.id] == "active_entitlement"
    assert segments[pending.id] == "pending_admission"
    assert segments[subscription_without_component.id] == "no_crm_entitlement"

    filtered = client.get(
        "/students/?workspace=students&commercial_segment=no_crm_entitlement",
        **_auth_params(owner_user, club),
    )
    assert filtered.status_code == 200
    assert [item["id"] for item in filtered.json()["items"]] == [
        subscription_without_component.id
    ]


@pytest.mark.django_db
def test_person_search_shapes_trainer_privacy_and_routes(settings, club, trainer_user):
    _enable_unified(settings, club)
    trainer = TrainerFactory(club=club, user=trainer_user)
    other_trainer = TrainerFactory(club=club)
    own_lead = LeadFactory(
        club=club,
        assigned_trainer=trainer,
        first_name="Searchable",
        phone="+79001110001",
    )
    pool = LeadFactory(
        club=club,
        assigned_trainer=None,
        first_name="Searchable",
        phone="+79001110002",
    )
    LeadFactory(
        club=club,
        assigned_trainer=other_trainer,
        first_name="Searchable",
        phone="+79001110003",
    )
    own_student = StudentFactory(
        club=club,
        assigned_trainer=trainer,
        first_name="Searchable",
        status=Student.Status.ACTIVE,
        lead_status=None,
        became_student_at=timezone.now(),
        phone="+79001110004",
    )
    archived_pool = StudentFactory(
        club=club,
        assigned_trainer=None,
        first_name="Searchable",
        status=Student.Status.LOST,
        lead_status=None,
        became_student_at=None,
        phone="+79001110005",
    )

    response = client.get(
        "/students/search/?q=Searchable",
        **_auth_params(trainer_user, club, role="trainer"),
    )

    assert response.status_code == 200
    results = response.json()
    by_id = {item["id"]: item for item in results if item["id"] is not None}
    assert by_id[own_lead.id]["route"] == f"/trainer/leads?lead={own_lead.id}"
    assert by_id[own_student.id]["route"] == f"/trainer/students/{own_student.id}"
    assert by_id[pool.id]["allowed_action"] == "can_claim"
    assert by_id[pool.id]["identity_visibility"] == "masked"
    assert by_id[archived_pool.id]["allowed_action"] == "can_reopen_and_claim"
    assert by_id[archived_pool.id]["identity_visibility"] == "masked"
    assert any(item["identity_visibility"] == "none" for item in results)
    serialized = response.content.decode()
    assert "+79001110001" not in serialized
    assert "+79001110002" not in serialized
    assert "+79001110003" not in serialized


@pytest.mark.django_db
def test_person_search_applies_limit_after_trainer_privacy_scope(
    settings,
    club,
    trainer_user,
):
    _enable_unified(settings, club)
    trainer = TrainerFactory(club=club, user=trainer_user)
    other_trainer = TrainerFactory(club=club)
    own_lead = LeadFactory(
        club=club,
        assigned_trainer=trainer,
        first_name="Crowdout",
    )
    for index in range(5):
        LeadFactory(
            club=club,
            assigned_trainer=other_trainer,
            first_name="Crowdout",
            phone=f"+7900222000{index}",
        )

    response = client.get(
        "/students/search/?q=Crowdout&limit=2",
        **_auth_params(trainer_user, club, role="trainer"),
    )

    assert response.status_code == 200
    assert len(response.json()) == 2
    assert any(item["id"] == own_lead.id for item in response.json())
    assert any(item["identity_visibility"] == "none" for item in response.json())


@pytest.mark.django_db
def test_owner_can_restore_soft_deleted_identity_with_audit(settings, club, owner_user):
    _enable_unified(settings, club)
    deleted = LeadFactory(club=club, assigned_trainer=None)
    deleted.soft_delete()

    response = client.post(
        f"/students/intakes/{deleted.id}/restore",
        **_auth_params(owner_user, club),
    )

    assert response.status_code == 200
    assert response.json()["result_kind"] == "restored"
    deleted.refresh_from_db()
    assert deleted.deleted_at is None
    assert LeadLifecycleEvent.objects.for_club(club).filter(
        student=deleted,
        event_type=LeadLifecycleEvent.EventType.LEAD_RESTORED,
    ).count() == 1


@pytest.mark.django_db
def test_owner_restore_refuses_a_live_identity_collision(settings, club, owner_user):
    _enable_unified(settings, club)
    deleted = LeadFactory(club=club, phone="+79005550123")
    deleted.soft_delete()
    LeadFactory(club=club, phone=deleted.phone)

    response = client.post(
        f"/students/intakes/{deleted.id}/restore",
        **_auth_params(owner_user, club),
    )

    assert response.status_code == 400
    assert response.json()["code"] == "restore_identity_conflict"
    deleted.refresh_from_db()
    assert deleted.deleted_at is not None
