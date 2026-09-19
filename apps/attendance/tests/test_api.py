from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone
from ninja.testing import TestClient

from apps.attendance.models import (
    Checkin,
    CheckinCascadeEvent,
    GroupSession,
    PersonalAvailabilitySlot,
    PersonalBookingPaymentReservation,
    Schedule,
    ScheduleBookingEvent,
    ScheduleEnrollment,
    ScheduleException,
    TrainingGroup,
    TrainingGroupMappingEvent,
    TrainingGroupMembership,
    TrainingGroupRolloutState,
)
from apps.attendance.services import activate_kiosk, generate_kiosk_pin
from apps.attendance.tests.factories import (
    CheckinFactory,
    GroupSessionFactory,
    ScheduleExceptionFactory,
    ScheduleFactory,
)
from apps.attendance.tests.rollout import update_training_group_rollout_state_for_test
from apps.billing.models import BankPaymentOrder, BankPaymentProviderEvent, Debt, Payment, Subscription, TrainingType
from apps.billing.tests.factories import SubscriptionFactory, TariffFactory, TrainingTypeFactory
from apps.clubs.models import ClubSettings
from apps.clubs.tests.factories import LocationFactory, UserFactory
from apps.common.exceptions import BusinessLogicError
from apps.common.tests.helpers import make_auth_params as _auth_params
from apps.leads.models import LeadLifecycleEvent
from apps.students.models import Student
from apps.students.tests.factories import StudentFactory
from apps.trainers.tests.factories import TrainerFactory, TrainerLocationFactory
from config.api import api

client = TestClient(api)


def _future_date(days: int) -> str:
    return _future_date_obj(days).isoformat()


def _future_date_obj(days: int) -> date:
    return timezone.localdate() + timedelta(days=days)


def _future_datetime(days: int, *, hour: int, minute: int = 0) -> str:
    return datetime.combine(_future_date_obj(days), time(hour, minute)).isoformat()


@pytest.fixture(autouse=True)
def _bypass_jwt_auth(bypass_jwt_auth):
    pass


def _kiosk_auth_params(club):
    pin = generate_kiosk_pin(club_id=club.id)
    token = activate_kiosk(pin=pin)["token"]
    return {"headers": {"X-Kiosk-Token": token}}


def _aware_datetime(target_date: date, slot_time: time):
    return timezone.make_aware(datetime.combine(target_date, slot_time))


@pytest.mark.django_db
def test_personal_availability_capability_is_tenant_scoped_and_available_to_all_portal_roles(
    settings,
    club,
    owner_user,
    student_user,
    parent_user,
):
    from apps.clubs.models import ClubSettings

    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(
        club=club,
        defaults={"unified_client_journey_enabled": True},
    )
    for user, role in (
        (owner_user, "owner"),
        (student_user, "student"),
        (parent_user, "parent"),
    ):
        response = client.get(
            "/personal-availability/capability/",
            **_auth_params(user, club, role=role),
        )
        assert response.status_code == 200
        assert response.json() == {
            "enabled": True,
            "staff_command_protocol_version": "v1",
        }

    ClubSettings.objects.filter(club=club).update(unified_client_journey_enabled=False)
    response = client.get(
        "/personal-availability/capability/",
        **_auth_params(owner_user, club),
    )
    assert response.status_code == 200
    assert response.json() == {
        "enabled": False,
        "staff_command_protocol_version": "v1",
    }


@pytest.mark.django_db
def test_personal_availability_capability_projects_safe_staff_protocol_without_readiness_details(
    settings,
    club,
    owner_user,
):
    """Trainer clients need a server-owned route selector, not owner readiness."""

    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = True
    ClubSettings.objects.update_or_create(
        club=club,
        defaults={
            "unified_client_journey_enabled": True,
            "commercial_journey_protocol_version": ClubSettings.CommercialJourneyProtocol.V2,
        },
    )

    response = client.get(
        "/personal-availability/capability/",
        **_auth_params(owner_user, club, role="trainer"),
    )

    assert response.status_code == 200
    assert response.json() == {
        "enabled": True,
        "staff_command_protocol_version": "v2",
    }


@pytest.mark.django_db
def test_unified_reservation_apis_require_unified_self_service_and_stable_staff_key(
    settings,
    club,
    owner_user,
    student_user,
):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.update_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    trainer = TrainerFactory(club=club, is_active=True)
    location = LocationFactory(club=club)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    TrainerLocationFactory(club=club, trainer=trainer, location=location)
    student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
    starts_at = _aware_datetime(_future_date_obj(8), time(10, 0))
    slot = PersonalAvailabilitySlot.objects.create(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
    )
    payload = {"tariff_id": None, "offer_digest": None}
    responses = [
        client.post(
            f"/personal-availability/{slot.id}/payment-reservations/",
            json=payload,
            **_auth_params(student_user, club, role="student"),
        ),
        client.post(
            f"/personal-availability/slots/{slot.id}/staff-payment-reservations/",
            json={**payload, "student_id": student.id},
            **_auth_params(owner_user, club),
        ),
        client.post(
            f"/students/{student.id}/personal-booking-payment-reservations/",
            json={
                **payload,
                "trainer_id": trainer.id,
                "starts_at": starts_at.isoformat(),
                "ends_at": (starts_at + timedelta(hours=1)).isoformat(),
                "location_id": location.id,
                "training_type_id": training_type.id,
            },
            **_auth_params(owner_user, club),
        ),
    ]
    assert [response.status_code for response in responses] == [400, 400, 400]
    assert [response.json()["code"] for response in responses] == [
        "unified_personal_command_required",
        "idempotency_key_required",
        "idempotency_key_required",
    ]


@pytest.mark.django_db
class TestListSchedules:
    def test_list_schedules(self, club, owner_user):
        ScheduleFactory(club=club)
        response = client.get("/schedules/", **_auth_params(owner_user, club))
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1

    def test_list_schedules_tenant_isolation(self, club, other_club, owner_user):
        ScheduleFactory(club=club)
        ScheduleFactory(club=other_club)
        response = client.get("/schedules/", **_auth_params(owner_user, club))
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1


@pytest.mark.django_db
class TestTrainingGroupOperationalApi:
    def test_list_is_owner_scoped_and_rejects_trainer_role(
        self, club, other_club, owner_user, trainer_user
    ):
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        location = LocationFactory(club=club)
        trainer = TrainerFactory(club=club)
        group = TrainingGroup.objects.create(
            club=club,
            name="Owner group",
            training_type=training_type,
            location=location,
            responsible_trainer=trainer,
            status=TrainingGroup.Status.ACTIVE,
        )
        foreign_type = TrainingTypeFactory(club=other_club, kind=TrainingType.Kind.GROUP)
        foreign_location = LocationFactory(club=other_club)
        foreign_trainer = TrainerFactory(club=other_club)
        TrainingGroup.objects.create(
            club=other_club,
            name="Foreign group",
            training_type=foreign_type,
            location=foreign_location,
            responsible_trainer=foreign_trainer,
            status=TrainingGroup.Status.ACTIVE,
        )

        owner_response = client.get(
            "/schedules/training-groups/",
            **_auth_params(owner_user, club),
        )
        trainer_response = client.get(
            "/schedules/training-groups/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert owner_response.status_code == 200
        assert [item["id"] for item in owner_response.json()["items"]] == [group.id]
        assert trainer_response.status_code == 403

    def test_create_accepts_only_shadow_or_active_rollout(self, settings, club, owner_user):
        settings.TRAINING_GROUP_NEW_WRITES_ENABLED = True
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        location = LocationFactory(club=club)
        trainer = TrainerFactory(club=club)
        rollout_state = TrainingGroupRolloutState.objects.for_club(club).get()
        update_training_group_rollout_state_for_test(
            TrainingGroupRolloutState.objects.filter(id=rollout_state.id),
            mode=TrainingGroupRolloutState.Mode.SHADOW
        )

        response = client.post(
            "/schedules/training-groups/",
            json={
                "name": "New canonical group",
                "training_type_id": training_type.id,
                "location_id": location.id,
                "responsible_trainer_id": trainer.id,
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 201
        assert response.json()["name"] == "New canonical group"

    def test_new_group_and_membership_api_writes_fail_closed_without_switch(
        self, club, owner_user, settings
    ):
        settings.TRAINING_GROUP_NEW_WRITES_ENABLED = False
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        location = LocationFactory(club=club)
        trainer = TrainerFactory(club=club)
        rollout_state = TrainingGroupRolloutState.objects.for_club(club).get()
        update_training_group_rollout_state_for_test(
            TrainingGroupRolloutState.objects.filter(id=rollout_state.id),
            mode=TrainingGroupRolloutState.Mode.SHADOW
        )

        group_response = client.post(
            "/schedules/training-groups/",
            json={
                "name": "Blocked canonical group",
                "training_type_id": training_type.id,
                "location_id": location.id,
                "responsible_trainer_id": trainer.id,
            },
            **_auth_params(owner_user, club),
        )

        assert group_response.status_code == 400
        assert group_response.json()["code"] == "training_group_writes_disabled"
        assert not TrainingGroup.objects.for_club(club).exists()

        group = TrainingGroup.objects.create(
            club=club,
            name="Existing canonical group",
            training_type=training_type,
            location=location,
            responsible_trainer=trainer,
            status=TrainingGroup.Status.ACTIVE,
        )
        student = StudentFactory(club=club)
        membership_response = client.post(
            "/schedules/training-groups/memberships/",
            json={
                "student_id": student.id,
                "training_group_id": group.id,
                "starts_on": "2030-01-07",
                "source": TrainingGroupMembership.Source.MANUAL,
                "rationale": "Blocked new canonical membership.",
                "idempotency_key": "api-blocked-canonical-membership",
            },
            **_auth_params(owner_user, club),
        )

        assert membership_response.status_code == 400
        assert membership_response.json()["code"] == "training_group_writes_disabled"
        assert not TrainingGroupMembership.objects.for_club(club).filter(student=student).exists()

    def test_archive_requires_owner_audit_inputs_and_retries_exactly_once(
        self, club, owner_user, trainer_user
    ):
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        location = LocationFactory(club=club)
        trainer = TrainerFactory(club=club)
        group = TrainingGroup.objects.create(
            club=club,
            name="Closed archive group",
            training_type=training_type,
            location=location,
            responsible_trainer=trainer,
            status=TrainingGroup.Status.ACTIVE,
        )
        payload = {
            "rationale": "Owner confirmed the group has no open lifecycle state.",
            "idempotency_key": "api-archive-closed-group",
        }

        forbidden_response = client.post(
            f"/schedules/training-groups/{group.id}/archive/",
            json=payload,
            **_auth_params(trainer_user, club, role="trainer"),
        )
        response = client.post(
            f"/schedules/training-groups/{group.id}/archive/",
            json=payload,
            **_auth_params(owner_user, club),
        )
        retry_response = client.post(
            f"/schedules/training-groups/{group.id}/archive/",
            json=payload,
            **_auth_params(owner_user, club),
        )

        assert forbidden_response.status_code == 403
        assert response.status_code == 200
        assert retry_response.status_code == 200
        assert response.json()["status"] == TrainingGroup.Status.ARCHIVED
        assert retry_response.json() == response.json()
        archive_event = TrainingGroupMappingEvent.objects.for_club(club).get(
            training_group=group,
            action="archived",
        )
        assert archive_event.actor_id == owner_user.id
        assert archive_event.idempotency_key == payload["idempotency_key"]


@pytest.mark.django_db
class TestTrainingGroupReconciliationApi:
    def test_apply_is_owner_scoped_digest_bound_and_redacted(
        self, settings, club, owner_user, trainer_user
    ):
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        location = LocationFactory(club=club)
        trainer = TrainerFactory(club=club)
        first_schedule = ScheduleFactory(
            club=club,
            training_type=training_type,
            location=location,
            trainer=trainer,
        )
        second_schedule = ScheduleFactory(
            club=club,
            training_type=training_type,
            location=location,
            trainer=trainer,
            day_of_week=2,
        )
        student = StudentFactory(club=club)
        ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=first_schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=date(2026, 7, 1),
            created_from=ScheduleEnrollment.CreatedFrom.MANUAL,
        )
        enter_payload = {
            "target_mode": TrainingGroupRolloutState.Mode.RECONCILING,
            "rationale": "Open a real owner reconciliation epoch for API proof.",
            "idempotency_key": "api-rollout-enter-reconciling",
        }
        forbidden_enter = client.post(
            "/schedules/training-group-rollout/transition/",
            json=enter_payload,
            **_auth_params(trainer_user, club, role="trainer"),
        )
        enter_response = client.post(
            "/schedules/training-group-rollout/transition/",
            json=enter_payload,
            **_auth_params(owner_user, club),
        )
        assert forbidden_enter.status_code == 403
        assert enter_response.status_code == 200
        assert enter_response.json()["mode"] == TrainingGroupRolloutState.Mode.RECONCILING
        preview_payload = {
            "schedule_ids": [first_schedule.id, second_schedule.id],
            "canonical_name": "API reconciliation group",
        }
        preview_response = client.post(
            "/schedules/training-group-reconciliation/preview/",
            json=preview_payload,
            **_auth_params(owner_user, club),
        )
        assert preview_response.status_code == 200

        apply_payload = {
            **preview_payload,
            "preview_digest": preview_response.json()["digest"],
            "rationale": "Apply the owner-reviewed reconciliation preview.",
            "idempotency_key": "api-reconciliation-apply",
        }
        forbidden_response = client.post(
            "/schedules/training-group-reconciliation/apply/",
            json=apply_payload,
            **_auth_params(trainer_user, club, role="trainer"),
        )
        blocked_response = client.post(
            "/schedules/training-group-reconciliation/apply/",
            json=apply_payload,
            **_auth_params(owner_user, club),
        )
        assert blocked_response.status_code == 400
        assert blocked_response.json()["code"] == "training_group_writes_disabled"
        assert not TrainingGroup.objects.for_club(club).exists()

        settings.TRAINING_GROUP_NEW_WRITES_ENABLED = True
        response = client.post(
            "/schedules/training-group-reconciliation/apply/",
            json=apply_payload,
            **_auth_params(owner_user, club),
        )
        retry_response = client.post(
            "/schedules/training-group-reconciliation/apply/",
            json=apply_payload,
            **_auth_params(owner_user, club),
        )

        assert forbidden_response.status_code == 403
        assert response.status_code == 200
        assert retry_response.status_code == 200
        assert retry_response.json() == response.json()
        assert set(response.json()) == {
            "training_group_id",
            "status",
            "preview_digest",
            "selected_schedule_ids",
            "membership_count",
            "projection_count",
            "linked_payment_count",
            "roster_delta_digest",
            "rollout_gate_digest",
            "batch_id",
        }
        transition_payload = {
            "target_mode": TrainingGroupRolloutState.Mode.SHADOW,
            "rationale": "Move only after the owner approves the returned epoch digest.",
            "idempotency_key": "api-rollout-shadow",
        }
        missing_digest = client.post(
            "/schedules/training-group-rollout/transition/",
            json=transition_payload,
            **_auth_params(owner_user, club),
        )
        wrong_digest = client.post(
            "/schedules/training-group-rollout/transition/",
            json={**transition_payload, "rollout_gate_digest": "0" * 64},
            **_auth_params(owner_user, club),
        )
        assert missing_digest.status_code == 400
        assert wrong_digest.status_code == 400
        assert TrainingGroupRolloutState.objects.for_club(club).get().mode == TrainingGroupRolloutState.Mode.RECONCILING

        exact_payload = {
            **transition_payload,
            "rollout_gate_digest": response.json()["rollout_gate_digest"],
        }
        shadow_response = client.post(
            "/schedules/training-group-rollout/transition/",
            json=exact_payload,
            **_auth_params(owner_user, club),
        )
        shadow_retry = client.post(
            "/schedules/training-group-rollout/transition/",
            json=exact_payload,
            **_auth_params(owner_user, club),
        )
        assert shadow_response.status_code == 200
        assert shadow_retry.status_code == 200
        assert shadow_retry.json() == shadow_response.json()
        assert shadow_response.json()["mode"] == TrainingGroupRolloutState.Mode.SHADOW
        assert response.json()["selected_schedule_ids"] == [first_schedule.id, second_schedule.id]
        assert "source_enrollment" not in response.content.decode()

    def test_inventory_and_preview_are_owner_scoped_and_mutation_free(self, club, other_club, owner_user):
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        location = LocationFactory(club=club)
        trainer = TrainerFactory(club=club)
        first_schedule = ScheduleFactory(
            club=club,
            training_type=training_type,
            location=location,
            trainer=trainer,
        )
        second_schedule = ScheduleFactory(
            club=club,
            training_type=training_type,
            location=location,
            trainer=trainer,
            day_of_week=2,
        )
        foreign_schedule = ScheduleFactory(club=other_club)
        student = StudentFactory(club=club)
        ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=first_schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=date(2026, 7, 1),
            created_from=ScheduleEnrollment.CreatedFrom.MANUAL,
        )
        before_counts = (
            TrainingGroup.objects.for_club(club).count(),
            TrainingGroupMembership.objects.for_club(club).count(),
            TrainingGroupMappingEvent.objects.for_club(club).count(),
            TrainingGroupRolloutState.objects.for_club(club).count(),
        )

        inventory_response = client.get(
            "/schedules/training-group-reconciliation/",
            **_auth_params(owner_user, club),
        )
        preview_response = client.post(
            "/schedules/training-group-reconciliation/preview/",
            json={
                "schedule_ids": [first_schedule.id, second_schedule.id],
                "canonical_name": "Preview only",
            },
            **_auth_params(owner_user, club),
        )
        foreign_response = client.post(
            "/schedules/training-group-reconciliation/preview/",
            json={
                "schedule_ids": [first_schedule.id, foreign_schedule.id],
                "canonical_name": "Preview only",
            },
            **_auth_params(owner_user, club),
        )

        assert inventory_response.status_code == 200
        assert {item["schedule_id"] for item in inventory_response.json()["schedules"]} == {
            first_schedule.id,
            second_schedule.id,
        }
        assert preview_response.status_code == 200
        assert preview_response.json()["selected_schedule_ids"] == [first_schedule.id, second_schedule.id]
        assert preview_response.json()["proposed_roster_deltas"] == [
            {
                "student_id": student.id,
                "schedule_id": first_schedule.id,
                "effective_starts_on": "2026-07-01",
                "earliest_legacy_starts_on": "2026-07-01",
                "action": "link_existing",
                "requires_explicit_starts_on": False,
            },
            {
                "student_id": student.id,
                "schedule_id": second_schedule.id,
                "effective_starts_on": "2026-07-01",
                "earliest_legacy_starts_on": "2026-07-01",
                "action": "add_compatibility_projection",
                "requires_explicit_starts_on": False,
            },
        ]
        assert foreign_response.status_code == 400
        assert foreign_response.json()["detail"].startswith("unknown_schedule_ids:")
        assert (
            TrainingGroup.objects.for_club(club).count(),
            TrainingGroupMembership.objects.for_club(club).count(),
            TrainingGroupMappingEvent.objects.for_club(club).count(),
            TrainingGroupRolloutState.objects.for_club(club).count(),
        ) == before_counts

    def test_owner_api_allows_only_deferred_provider_queue_to_shadow_and_keeps_active_strict(
        self,
        club,
        owner_user,
    ):
        BankPaymentProviderEvent.objects.create(
            club=club,
            provider=BankPaymentOrder.Provider.MOCK,
            event_type="owner-api-deferred-provider-event",
            received_at=timezone.now(),
            processing_status=BankPaymentProviderEvent.ProcessingStatus.DEFERRED,
        )
        enter_response = client.post(
            "/schedules/training-group-rollout/transition/",
            json={
                "target_mode": TrainingGroupRolloutState.Mode.RECONCILING,
                "rationale": "Open owner reconciliation with only the provider replay queue pending.",
                "idempotency_key": "owner-api-deferred-only-enter",
            },
            **_auth_params(owner_user, club),
        )
        shadow_response = client.post(
            "/schedules/training-group-rollout/transition/",
            json={
                "target_mode": TrainingGroupRolloutState.Mode.SHADOW,
                "rationale": "Queue replay can leave reconciliation for shadow.",
                "idempotency_key": "owner-api-deferred-only-shadow",
                "rollout_gate_digest": "",
            },
            **_auth_params(owner_user, club),
        )
        active_response = client.post(
            "/schedules/training-group-rollout/transition/",
            json={
                "target_mode": TrainingGroupRolloutState.Mode.ACTIVE,
                "rationale": "Active remains blocked until replay drains the queue.",
                "idempotency_key": "owner-api-deferred-only-active",
                "rollout_gate_digest": "",
            },
            **_auth_params(owner_user, club),
        )

        assert enter_response.status_code == 200
        assert shadow_response.status_code == 200
        assert shadow_response.json()["mode"] == TrainingGroupRolloutState.Mode.SHADOW
        assert active_response.status_code == 400
        assert TrainingGroupRolloutState.objects.for_club(club).get().mode == TrainingGroupRolloutState.Mode.SHADOW


@pytest.mark.django_db
class TestCreateSchedule:
    def test_create_schedule_endpoint(self, club, owner_user):
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        response = client.post(
            "/schedules/",
            json={
                "day_of_week": 0,
                "start_time": "10:00:00",
                "end_time": "11:00:00",
                "group_name": "Adults Boxing",
                "trainer_id": trainer.id,
                "location_id": location.id,
                "training_type_id": training_type.id,
            },
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 201
        data = response.json()
        assert data["group_name"] == "Adults Boxing"
        assert data["trainer_id"] == trainer.id
        assert data["training_type_id"] == training_type.id

    def test_create_schedule_rejects_training_type_wrong_club(self, club, other_club, owner_user):
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=other_club)

        response = client.post(
            "/schedules/",
            json={
                "day_of_week": 0,
                "start_time": "10:00:00",
                "end_time": "11:00:00",
                "group_name": "Cross Tenant Type",
                "trainer_id": trainer.id,
                "location_id": location.id,
                "training_type_id": training_type.id,
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "training_type_club_mismatch"

    def test_create_one_time_schedule_rejects_duplicate_trainer_slot(self, club, owner_user):
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        ScheduleFactory(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=_future_date_obj(15),
            start_time=time(10, 0),
            end_time=time(11, 0),
        )

        response = client.post(
            "/schedules/",
            json={
                "day_of_week": 0,
                "start_time": "10:00:00",
                "end_time": "11:00:00",
                "group_name": "Duplicate Slot",
                "trainer_id": trainer.id,
                "location_id": location.id,
                "training_type_id": training_type.id,
                "one_time_date": _future_date(15),
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "schedule_slot_conflict"

    @patch("apps.attendance.services.async_task")
    def test_schedule_created_through_api_appears_in_kiosk_and_accepts_checkin(self, mock_async, club, owner_user):
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        student = StudentFactory(club=club, status="active")
        today = date.today()

        response = client.post(
            "/schedules/",
            json={
                "day_of_week": today.weekday(),
                "start_time": "10:00:00",
                "end_time": "11:00:00",
                "group_name": "Kiosk Proof",
                "trainer_id": trainer.id,
                "location_id": location.id,
                "training_type_id": training_type.id,
            },
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 201, response.json()
        schedule_id = response.json()["id"]
        ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule_id=schedule_id,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=today,
        )

        kiosk_auth = _kiosk_auth_params(club)
        today_response = client.get(
            f"/checkins/kiosk/schedules/today/?date={today.isoformat()}",
            **kiosk_auth,
        )
        assert today_response.status_code == 200
        assert any(item["schedule_id"] == schedule_id for item in today_response.json())

        checkin_response = client.post(
            "/checkins/kiosk/",
            json={
                "student_id": student.id,
                "schedule_id": schedule_id,
                "training_type_id": training_type.id,
                "checkin_date": today.isoformat(),
            },
            **kiosk_auth,
        )
        assert checkin_response.status_code == 200
        assert checkin_response.json()["created"] is True

    def test_create_schedule_permission(self, club, trainer_user):
        trainer = TrainerFactory(club=club, user=trainer_user)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        response = client.post(
            "/schedules/",
            json={
                "day_of_week": 0,
                "start_time": "10:00:00",
                "end_time": "11:00:00",
                "group_name": "Test",
                "trainer_id": trainer.id,
                "location_id": location.id,
                "training_type_id": training_type.id,
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert response.status_code == 403


@pytest.mark.django_db
class TestUpdateSchedule:
    def test_update_schedule_persists_same_club_training_type(self, club, owner_user):
        old_type = TrainingTypeFactory(club=club)
        new_type = TrainingTypeFactory(club=club)
        schedule = ScheduleFactory(club=club, training_type=old_type)

        response = client.put(
            f"/schedules/{schedule.id}/",
            json={"training_type_id": new_type.id},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        assert response.json()["training_type_id"] == new_type.id

    def test_update_schedule_rejects_trainer_wrong_club(self, club, other_club, owner_user):
        schedule = ScheduleFactory(club=club)
        other_trainer = TrainerFactory(club=other_club)

        response = client.put(
            f"/schedules/{schedule.id}/",
            json={"trainer_id": other_trainer.id},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "trainer_club_mismatch"

    def test_update_schedule_rejects_location_wrong_club(self, club, other_club, owner_user):
        schedule = ScheduleFactory(club=club)
        other_location = LocationFactory(club=other_club)

        response = client.put(
            f"/schedules/{schedule.id}/",
            json={"location_id": other_location.id},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "location_club_mismatch"

    def test_update_schedule_rejects_training_type_wrong_club(self, club, other_club, owner_user):
        schedule = ScheduleFactory(club=club)
        other_type = TrainingTypeFactory(club=other_club)

        response = client.put(
            f"/schedules/{schedule.id}/",
            json={"training_type_id": other_type.id},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "training_type_club_mismatch"


@pytest.mark.django_db
class TestCancelSession:
    def test_cancel_session_endpoint(self, club, owner_user):
        session_date = _future_date_obj(7)
        schedule = ScheduleFactory(club=club, day_of_week=session_date.weekday())
        response = client.post(
            f"/schedules/{schedule.id}/cancel/",
            json={"date": session_date.isoformat(), "reason": "Holiday"},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 201, response.json()
        data = response.json()
        assert data["exception_type"] == "cancelled"
        assert data["reason"] == "Holiday"

    def test_cancel_session_endpoint_rejects_live_checkins(self, club, owner_user):
        session_date = _future_date_obj(7)
        schedule = ScheduleFactory(club=club, day_of_week=session_date.weekday())
        CheckinFactory(
            club=club,
            schedule=schedule,
            training_type=schedule.training_type,
            date=session_date,
        )

        response = client.post(
            f"/schedules/{schedule.id}/cancel/",
            json={"date": session_date.isoformat(), "reason": "Holiday"},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "schedule_session_has_checkins"
        assert not ScheduleException.objects.for_club(club).filter(schedule=schedule, date=session_date).exists()


@pytest.mark.django_db
class TestRescheduleSession:
    def test_reschedule_session_endpoint(self, club, owner_user):
        session_date = _future_date_obj(7)
        new_date = _future_date_obj(8)
        schedule = ScheduleFactory(club=club, day_of_week=session_date.weekday())
        response = client.post(
            f"/schedules/{schedule.id}/reschedule/",
            json={
                "date": session_date.isoformat(),
                "new_date": new_date.isoformat(),
                "new_start_time": "14:00:00",
                "new_end_time": "15:00:00",
                "reason": "Venue change",
            },
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 201
        data = response.json()
        assert data["exception_type"] == "rescheduled"
        assert data["new_date"] == new_date.isoformat()

    def test_reschedule_session_endpoint_rejects_live_checkins(self, club, owner_user):
        session_date = _future_date_obj(7)
        schedule = ScheduleFactory(club=club, day_of_week=session_date.weekday())
        new_date = _future_date_obj(8)
        CheckinFactory(
            club=club,
            schedule=schedule,
            training_type=schedule.training_type,
            date=session_date,
        )

        response = client.post(
            f"/schedules/{schedule.id}/reschedule/",
            json={
                "date": session_date.isoformat(),
                "new_date": new_date.isoformat(),
                "new_start_time": "14:00:00",
                "new_end_time": "15:00:00",
                "reason": "Venue change",
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "schedule_session_has_checkins"
        assert not ScheduleException.objects.for_club(club).filter(schedule=schedule, date=session_date).exists()


@pytest.mark.django_db
class TestSubstituteTrainer:
    def test_substitute_trainer_endpoint(self, club, owner_user):
        session_date = _future_date_obj(7)
        schedule = ScheduleFactory(club=club, day_of_week=session_date.weekday())
        sub_trainer = TrainerFactory(club=club)
        response = client.post(
            f"/schedules/{schedule.id}/substitute/",
            json={
                "date": session_date.isoformat(),
                "substitute_trainer_id": sub_trainer.id,
                "reason": "Sick leave",
            },
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 201
        data = response.json()
        assert data["exception_type"] == "substitute"
        assert data["substitute_trainer_id"] == sub_trainer.id

    def test_substitute_trainer_endpoint_allows_rescheduled_session_new_date(self, club, owner_user):
        session_date = _future_date_obj(7)
        new_date = _future_date_obj(8)
        schedule = ScheduleFactory(club=club, day_of_week=session_date.weekday())
        sub_trainer = TrainerFactory(club=club)
        ScheduleException.objects.create(
            club=club,
            schedule=schedule,
            date=session_date,
            exception_type=ScheduleException.ExceptionType.RESCHEDULED,
            new_date=new_date,
            new_start_time=time(14, 0),
            new_end_time=time(15, 0),
        )

        response = client.post(
            f"/schedules/{schedule.id}/substitute/",
            json={
                "date": new_date.isoformat(),
                "substitute_trainer_id": sub_trainer.id,
                "reason": "Coach swap after venue change",
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 201
        data = response.json()
        assert data["exception_type"] == "substitute"
        assert data["date"] == new_date.isoformat()
        assert data["substitute_trainer_id"] == sub_trainer.id


@pytest.mark.django_db
class TestTodaySessions:
    def test_get_today_sessions_endpoint(self, club, owner_user):
        today = _future_date_obj(7)
        ScheduleFactory(club=club, day_of_week=0)
        ScheduleFactory(club=club, day_of_week=2)  # Wednesday

        with patch("apps.attendance.selectors.date_type") as mock_date:
            mock_date.today.return_value = today
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            response = client.get("/schedules/today/", **_auth_params(owner_user, club))

        assert response.status_code == 200
        # We can't fully control today in endpoint without param, but the endpoint works
        # The response will be based on actual today -- just check it returns 200

    def test_unclosed_sessions_endpoint_returns_bounded_past_range(self, club, owner_user):
        older_date = timezone.localdate() - timedelta(days=3)
        yesterday = timezone.localdate() - timedelta(days=1)
        older_schedule = ScheduleFactory(
            club=club,
            day_of_week=older_date.weekday(),
            group_name="Older open",
            start_time=time(10, 0),
            end_time=time(11, 0),
        )
        yesterday_schedule = ScheduleFactory(
            club=club,
            day_of_week=yesterday.weekday(),
            group_name="Yesterday open",
            start_time=time(12, 0),
            end_time=time(13, 0),
        )
        closed_schedule = ScheduleFactory(
            club=club,
            day_of_week=older_date.weekday(),
            group_name="Older closed",
            start_time=time(14, 0),
            end_time=time(15, 0),
        )
        GroupSessionFactory(
            club=club,
            schedule=closed_schedule,
            date=older_date,
            closed_at=timezone.now(),
        )

        response = client.get(
            (
                "/schedules/unclosed/"
                f"?date_from={older_date.isoformat()}&date_to={yesterday.isoformat()}"
            ),
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        rows = response.json()
        assert [(item["schedule_id"], item["effective_date"]) for item in rows] == [
            (older_schedule.id, older_date.isoformat()),
            (yesterday_schedule.id, yesterday.isoformat()),
        ]

    def test_unclosed_sessions_range_requires_both_bounds(self, club, owner_user):
        response = client.get(
            f"/schedules/unclosed/?date_from={(timezone.localdate() - timedelta(days=1)).isoformat()}",
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["detail"] == "date_from and date_to are required together"

    def test_unclosed_sessions_range_is_limited_to_fourteen_days(self, club, owner_user):
        date_from = timezone.localdate() - timedelta(days=20)
        date_to = timezone.localdate() - timedelta(days=1)

        response = client.get(
            f"/schedules/unclosed/?date_from={date_from.isoformat()}&date_to={date_to.isoformat()}",
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["detail"] == "Unclosed sessions range is limited to 14 days"

    def test_unclosed_sessions_range_excludes_today_and_future(self, club, owner_user):
        club.timezone = "Europe/Moscow"
        club.save(update_fields=["timezone"])
        today = date(2099, 1, 6)
        yesterday = today - timedelta(days=1)
        tomorrow = today + timedelta(days=1)
        yesterday_schedule = ScheduleFactory(
            club=club,
            day_of_week=yesterday.weekday(),
            group_name="Past open",
        )
        ScheduleFactory(club=club, day_of_week=today.weekday(), group_name="Today open")
        ScheduleFactory(club=club, day_of_week=tomorrow.weekday(), group_name="Future open")
        now = datetime(2099, 1, 6, 12, 0, tzinfo=ZoneInfo("Europe/Moscow"))

        with patch("apps.attendance.api.timezone.now", return_value=now):
            response = client.get(
                f"/schedules/unclosed/?date_from={yesterday.isoformat()}&date_to={tomorrow.isoformat()}",
                **_auth_params(owner_user, club),
            )

        assert response.status_code == 200
        assert [item["schedule_id"] for item in response.json()] == [yesterday_schedule.id]

    def test_unclosed_sessions_range_is_trainer_scoped(self, club, trainer_user):
        trainer = TrainerFactory(club=club, user=trainer_user)
        other_trainer = TrainerFactory(club=club)
        target_date = timezone.localdate() - timedelta(days=1)
        owned = ScheduleFactory(
            club=club,
            trainer=trainer,
            day_of_week=target_date.weekday(),
            group_name="Owned open",
        )
        ScheduleFactory(
            club=club,
            trainer=other_trainer,
            day_of_week=target_date.weekday(),
            group_name="Foreign open",
        )

        response = client.get(
            f"/schedules/unclosed/?date_from={target_date.isoformat()}&date_to={target_date.isoformat()}",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        assert [item["schedule_id"] for item in response.json()] == [owned.id]


@pytest.mark.django_db
class TestScheduleExceptions:
    def test_get_schedule_exceptions(self, club, owner_user):
        schedule = ScheduleFactory(club=club)
        ScheduleExceptionFactory(schedule=schedule, date=_future_date_obj(7), exception_type="cancelled")
        ScheduleExceptionFactory(schedule=schedule, date=_future_date_obj(8), exception_type="rescheduled")

        response = client.get(
            f"/schedules/{schedule.id}/exceptions/",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2


@pytest.mark.django_db
class TestScheduleEnrollmentLifecycleAPI:
    def test_create_list_and_roster_for_assigned_student_without_checkins(self, club, owner_user):
        target_date = date.today()
        student = StudentFactory(club=club, status="active")
        schedule = ScheduleFactory(club=club, day_of_week=target_date.weekday())

        response = client.post(
            "/schedules/enrollments/",
            json={
                "student_id": student.id,
                "schedule_id": schedule.id,
                "status": "active",
                "starts_on": target_date.isoformat(),
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 201
        created = response.json()
        assert created["student_id"] == student.id
        assert created["schedule_id"] == schedule.id
        assert created["status"] == ScheduleEnrollment.Status.ACTIVE

        list_response = client.get(
            f"/schedules/enrollments/?student_id={student.id}&schedule_id={schedule.id}&status=active",
            **_auth_params(owner_user, club),
        )
        roster_response = client.get(
            f"/schedules/{schedule.id}/students/?date={target_date.isoformat()}",
            **_auth_params(owner_user, club),
        )

        assert list_response.status_code == 200
        assert list_response.json()["count"] == 1
        assert roster_response.status_code == 200
        assert {row["id"] for row in roster_response.json()} == {student.id}

    def test_cancel_enrollment_removes_student_after_boundary(self, club, owner_user):
        reference_date = date.today()
        boundary = reference_date - date.resolution
        student = StudentFactory(club=club, status="active")
        schedule = ScheduleFactory(club=club, day_of_week=reference_date.weekday())
        enrollment = ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=reference_date - date.resolution,
        )
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            trainer=schedule.trainer,
            location=schedule.location,
            date=reference_date - date.resolution,
        )

        response = client.post(
            f"/schedules/enrollments/{enrollment.id}/cancel/",
            json={"ends_on": boundary.isoformat()},
            **_auth_params(owner_user, club),
        )
        roster_response = client.get(
            f"/schedules/{schedule.id}/students/?date={reference_date.isoformat()}",
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        assert response.json()["status"] == ScheduleEnrollment.Status.CANCELLED
        assert response.json()["ends_on"] == boundary.isoformat()
        assert roster_response.status_code == 200
        assert roster_response.json() == []

    def test_transfer_enrollment_moves_student_to_target_schedule(self, club, owner_user):
        reference_date = date.today()
        boundary = reference_date - date.resolution
        student = StudentFactory(club=club, status="active")
        old_schedule = ScheduleFactory(club=club, day_of_week=reference_date.weekday())
        new_schedule = ScheduleFactory(
            club=club,
            day_of_week=reference_date.weekday(),
            group_name="New Group",
        )
        enrollment = ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=old_schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=reference_date - date.resolution,
        )

        response = client.post(
            f"/schedules/enrollments/{enrollment.id}/transfer/",
            json={
                "target_schedule_id": new_schedule.id,
                "ends_on": boundary.isoformat(),
            },
            **_auth_params(owner_user, club),
        )
        old_roster_response = client.get(
            f"/schedules/{old_schedule.id}/students/?date={reference_date.isoformat()}",
            **_auth_params(owner_user, club),
        )
        new_roster_response = client.get(
            f"/schedules/{new_schedule.id}/students/?date={reference_date.isoformat()}",
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["closed_enrollment"]["status"] == ScheduleEnrollment.Status.TRANSFERRED
        assert data["new_enrollment"]["schedule_id"] == new_schedule.id
        assert old_roster_response.status_code == 200
        assert old_roster_response.json() == []
        assert new_roster_response.status_code == 200
        assert {row["id"] for row in new_roster_response.json()} == {student.id}

    def test_freeze_and_unfreeze_enrollment(self, club, owner_user):
        student = StudentFactory(club=club, status="active")
        schedule = ScheduleFactory(club=club)
        enrollment = ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=date.today(),
        )

        freeze_response = client.post(
            f"/schedules/enrollments/{enrollment.id}/freeze/",
            **_auth_params(owner_user, club),
        )
        unfreeze_response = client.post(
            f"/schedules/enrollments/{enrollment.id}/unfreeze/",
            **_auth_params(owner_user, club),
        )

        assert freeze_response.status_code == 200
        assert freeze_response.json()["status"] == ScheduleEnrollment.Status.FROZEN
        assert unfreeze_response.status_code == 200
        assert unfreeze_response.json()["status"] == ScheduleEnrollment.Status.ACTIVE

    def test_schedule_students_marks_frozen_enrollment(self, club, owner_user):
        target_date = date.today()
        student = StudentFactory(club=club, status="active")
        schedule = ScheduleFactory(club=club, day_of_week=target_date.weekday())
        ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.FROZEN,
            starts_on=target_date,
        )

        response = client.get(
            f"/schedules/{schedule.id}/students/?date={target_date.isoformat()}",
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == student.id
        assert data[0]["enrollment_status"] == ScheduleEnrollment.Status.FROZEN
        assert data[0]["checkin_blocked_reason"] == "enrollment_frozen"

    def test_create_enrollment_rejects_wrong_club_student_or_schedule(
        self,
        club,
        other_club,
        owner_user,
    ):
        student = StudentFactory(club=club, status="active")
        other_student = StudentFactory(club=other_club, status="active")
        schedule = ScheduleFactory(club=club)
        other_schedule = ScheduleFactory(club=other_club)

        wrong_student_response = client.post(
            "/schedules/enrollments/",
            json={
                "student_id": other_student.id,
                "schedule_id": schedule.id,
                "starts_on": date.today().isoformat(),
            },
            **_auth_params(owner_user, club),
        )
        wrong_schedule_response = client.post(
            "/schedules/enrollments/",
            json={
                "student_id": student.id,
                "schedule_id": other_schedule.id,
                "starts_on": date.today().isoformat(),
            },
            **_auth_params(owner_user, club),
        )

        assert wrong_student_response.status_code == 400
        assert wrong_student_response.json()["code"] == "student_club_mismatch"
        assert wrong_schedule_response.status_code == 400
        assert wrong_schedule_response.json()["code"] == "schedule_club_mismatch"

    def test_wrong_club_enrollment_action_rejected(self, club, other_club, owner_user):
        other_student = StudentFactory(club=other_club, status="active")
        other_schedule = ScheduleFactory(club=other_club)
        other_enrollment = ScheduleEnrollment.objects.create(
            club=other_club,
            student=other_student,
            schedule=other_schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=date.today(),
        )

        response = client.post(
            f"/schedules/enrollments/{other_enrollment.id}/cancel/",
            json={"ends_on": date.today().isoformat()},
            **_auth_params(owner_user, club),
        )

        assert response.status_code in {400, 404}

    def test_trainer_cannot_manage_enrollment_lifecycle(self, club, trainer_user):
        student = StudentFactory(club=club, status="active")
        schedule = ScheduleFactory(club=club)

        create_response = client.post(
            "/schedules/enrollments/",
            json={
                "student_id": student.id,
                "schedule_id": schedule.id,
                "starts_on": date.today().isoformat(),
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )
        list_response = client.get(
            "/schedules/enrollments/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert create_response.status_code == 403
        assert list_response.status_code == 403

    def test_guest_visit_endpoint_creates_one_day_enrollment_and_roster_metadata(self, club, owner_user):
        target_date = _future_date_obj(7)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        student = StudentFactory(club=club, status="active")

        response = client.post(
            f"/schedules/{schedule.id}/guest-visits/",
            json={
                "date": target_date.isoformat(),
                "student_id": student.id,
                "origin": "walk_in_checkin",
                "idempotency_key": "api-guest-visit-1",
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 201
        data = response.json()
        assert data["student_id"] == student.id
        assert data["schedule_id"] == schedule.id
        assert data["created"] is True
        assert data["already_member"] is False
        assert data["is_guest_visit"] is True
        assert data["created_from"] == ScheduleEnrollment.CreatedFrom.GUEST_VISIT
        assert data["starts_on"] == target_date.isoformat()
        assert data["ends_on"] == target_date.isoformat()
        assert data["financial_preview"]["code"] == "resolved_at_checkin"
        assert Checkin.objects.for_club(club).count() == 0
        assert Debt.objects.for_club(club).count() == 0

        enrollment = ScheduleEnrollment.objects.for_club(club).get(id=data["enrollment_id"])
        event = ScheduleBookingEvent.objects.for_club(club).get(enrollment=enrollment)
        assert event.origin == ScheduleBookingEvent.Origin.WALK_IN_CHECKIN
        assert event.actor_id == owner_user.id
        assert event.metadata == {"idempotency_key": "api-guest-visit-1"}

        roster_response = client.get(
            f"/schedules/{schedule.id}/students/?date={target_date.isoformat()}",
            **_auth_params(owner_user, club),
        )
        assert roster_response.status_code == 200
        roster = roster_response.json()
        assert len(roster) == 1
        assert roster[0]["id"] == student.id
        assert roster[0]["enrollment_id"] == enrollment.id
        assert roster[0]["created_from"] == ScheduleEnrollment.CreatedFrom.GUEST_VISIT
        assert roster[0]["starts_on"] == target_date.isoformat()
        assert roster[0]["ends_on"] == target_date.isoformat()
        assert roster[0]["is_guest_visit"] is True

    def test_guest_visit_endpoint_is_idempotent_for_same_student_schedule_date(self, club, owner_user):
        target_date = _future_date_obj(7)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        student = StudentFactory(club=club, status="active")
        payload = {
            "date": target_date.isoformat(),
            "student_id": student.id,
            "origin": "planned_session_action",
        }

        first_response = client.post(
            f"/schedules/{schedule.id}/guest-visits/",
            json=payload,
            **_auth_params(owner_user, club),
        )
        second_response = client.post(
            f"/schedules/{schedule.id}/guest-visits/",
            json=payload,
            **_auth_params(owner_user, club),
        )

        assert first_response.status_code == 201
        assert second_response.status_code == 200
        assert second_response.json()["created"] is False
        assert second_response.json()["enrollment_id"] == first_response.json()["enrollment_id"]
        assert ScheduleEnrollment.objects.for_club(club).filter(
            student=student,
            schedule=schedule,
            starts_on=target_date,
            ends_on=target_date,
        ).count() == 1

    @patch("apps.attendance.services.checkin.async_task")
    def test_guest_visit_endpoint_accepts_lead_identity_and_keeps_checkin_eligible(
        self,
        mock_async_task,
        club,
        owner_user,
    ):
        target_date = _future_date_obj(7)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        lead = StudentFactory(
            club=club,
            status=Student.Status.LEAD,
            lead_status=Student.LeadStatus.NEW,
        )

        response = client.post(
            f"/schedules/{schedule.id}/guest-visits/",
            json={
                "date": target_date.isoformat(),
                "lead_id": lead.id,
                "origin": "planned_session_action",
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 201
        assert response.json()["student_id"] == lead.id
        lead.refresh_from_db()
        assert lead.status == Student.Status.TRIAL
        assert lead.lead_status == Student.LeadStatus.TRIAL_BOOKED
        assert lead.trial_date is not None
        enrollment = ScheduleEnrollment.objects.for_club(club).get(id=response.json()["enrollment_id"])
        assert enrollment.student_id == lead.id
        assert enrollment.status == ScheduleEnrollment.Status.TRIAL
        assert enrollment.created_from == ScheduleEnrollment.CreatedFrom.GUEST_VISIT
        assert LeadLifecycleEvent.objects.for_club(club).filter(
            student=lead,
            event_type=LeadLifecycleEvent.EventType.TRIAL_BOOKED,
            old_lead_status=Student.LeadStatus.NEW,
            new_lead_status=Student.LeadStatus.TRIAL_BOOKED,
        ).exists()

        after_session_end = datetime.combine(
            target_date,
            time(12, 0),
            tzinfo=ZoneInfo("Europe/Moscow"),
        )
        with patch("apps.attendance.selectors.timezone.now", return_value=after_session_end):
            checkin_response = client.post(
                "/checkins/batch/",
                json={
                    "schedule_id": schedule.id,
                    "date": target_date.isoformat(),
                    "present_student_ids": [lead.id],
                    "training_type_id": training_type.id,
                    "topic_tags": [],
                    "notes": "",
                },
                **_auth_params(owner_user, club),
            )

        assert checkin_response.status_code == 200
        lead.refresh_from_db()
        assert lead.lead_status == Student.LeadStatus.TRIAL_DONE
        assert Checkin.objects.for_club(club).filter(student=lead, schedule=schedule, date=target_date).exists()

    def test_guest_visit_endpoint_rejects_one_time_group_schedule(self, club, owner_user):
        target_date = _future_date_obj(7)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            one_time_date=target_date,
            training_type=training_type,
        )
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)

        response = client.post(
            f"/schedules/{schedule.id}/guest-visits/",
            json={
                "date": target_date.isoformat(),
                "student_id": student.id,
                "origin": "planned_session_action",
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "guest_visit_requires_recurring_schedule"

    def test_guest_visit_endpoint_requires_exactly_one_identity(self, club, owner_user):
        target_date = _future_date_obj(7)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )

        response = client.post(
            f"/schedules/{schedule.id}/guest-visits/",
            json={"date": target_date.isoformat()},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "exactly_one_guest_identity_required"

    def test_guest_visit_candidates_are_masked_and_roster_scoped(self, club, owner_user):
        target_date = _future_date_obj(7)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        candidate = StudentFactory(
            club=club,
            first_name="Nina",
            last_name="Ivanova",
            phone="+79001112233",
            status="active",
        )
        roster_student = StudentFactory(
            club=club,
            first_name="Nina",
            last_name="Roster",
            status="active",
        )
        lead = StudentFactory(
            club=club,
            first_name="Nina",
            last_name="Lead",
            status="lead",
        )
        ScheduleEnrollment.objects.create(
            club=club,
            student=roster_student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=target_date,
        )

        response = client.get(
            (
                f"/schedules/{schedule.id}/guest-visit-candidates/"
                f"?date={target_date.isoformat()}&q=Nina"
            ),
            **_auth_params(owner_user, club),
        )
        short_response = client.get(
            (
                f"/schedules/{schedule.id}/guest-visit-candidates/"
                f"?date={target_date.isoformat()}&q=N"
            ),
            **_auth_params(owner_user, club),
        )
        short_phone_response = client.get(
            (
                f"/schedules/{schedule.id}/guest-visit-candidates/"
                f"?date={target_date.isoformat()}&q=22"
            ),
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        data = response.json()
        assert [item["id"] for item in data] == [candidate.id, lead.id]
        assert data[0]["first_name"] == "Nina"
        assert data[0]["last_name"] == "Ivanova"
        assert data[0]["kind"] == "student"
        assert data[0]["masked_phone"].endswith("2233")
        assert data[0]["masked_phone"] != candidate.phone
        assert "phone" not in data[0]
        assert "email" not in data[0]
        assert data[1]["kind"] == "lead"
        assert short_response.status_code == 200
        assert short_response.json() == []
        assert short_phone_response.status_code == 200
        assert short_phone_response.json() == []

    def test_guest_visit_candidates_require_schedule_occurrence(self, club, owner_user):
        target_date = _future_date_obj(7)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        StudentFactory(club=club, first_name="Nina", status="active")

        response = client.get(
            (
                f"/schedules/{schedule.id}/guest-visit-candidates/"
                f"?date={(target_date + timedelta(days=1)).isoformat()}&q=Nina"
            ),
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 404

    def test_student_guest_booking_derives_student_and_records_self_booking(
        self,
        club,
        owner_user,
        student_user,
    ):
        target_date = _future_date_obj(7)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)

        response = client.post(
            f"/schedules/{schedule.id}/guest-bookings/",
            json={
                "date": target_date.isoformat(),
                "idempotency_key": "student-self-booking-1",
            },
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 201
        data = response.json()
        assert data["student_id"] == student.id
        assert data["schedule_id"] == schedule.id
        assert data["created"] is True
        assert data["created_from"] == ScheduleEnrollment.CreatedFrom.STUDENT_SELF_BOOKING
        assert data["is_guest_visit"] is True
        assert Checkin.objects.for_club(club).count() == 0
        assert Debt.objects.for_club(club).count() == 0

        enrollment = ScheduleEnrollment.objects.for_club(club).get(id=data["enrollment_id"])
        assert enrollment.student_id == student.id
        assert enrollment.starts_on == target_date
        assert enrollment.ends_on == target_date
        event = ScheduleBookingEvent.objects.for_club(club).get(enrollment=enrollment)
        assert event.origin == ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING
        assert event.actor_id == student_user.id
        assert event.metadata == {"idempotency_key": "student-self-booking-1"}

        roster_response = client.get(
            f"/schedules/{schedule.id}/students/?date={target_date.isoformat()}",
            **_auth_params(student_user, club, role="trainer"),
        )
        assert roster_response.status_code == 403

        staff_roster_response = client.get(
            f"/schedules/{schedule.id}/students/?date={target_date.isoformat()}",
            **_auth_params(owner_user, club),
        )
        assert staff_roster_response.status_code == 200
        roster = staff_roster_response.json()
        assert roster[0]["id"] == student.id
        assert roster[0]["created_from"] == ScheduleEnrollment.CreatedFrom.STUDENT_SELF_BOOKING
        assert roster[0]["is_guest_visit"] is True

    def test_student_guest_booking_rejects_child_student_id(self, club, student_user):
        target_date = _future_date_obj(7)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)

        response = client.post(
            f"/schedules/{schedule.id}/guest-bookings/",
            json={
                "date": target_date.isoformat(),
                "child_student_id": 12345,
            },
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "child_student_id_not_allowed"

    def test_student_guest_booking_requires_subscription_or_dropin_policy(self, club, student_user):
        target_date = _future_date_obj(7)
        training_type = TrainingTypeFactory(
            club=club,
            kind=TrainingType.Kind.GROUP,
            drop_in_price=None,
            trial_free=False,
        )
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)

        response = client.post(
            f"/schedules/{schedule.id}/guest-bookings/",
            json={"date": target_date.isoformat()},
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "drop_in_price_required"
        assert ScheduleEnrollment.objects.for_club(club).count() == 0

    def test_parent_guest_booking_uses_child_scope_and_parent_origin(self, club, parent_user):
        target_date = _future_date_obj(7)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        child = StudentFactory(
            club=club,
            is_child=True,
            parent_user=parent_user,
            status=Student.Status.ACTIVE,
        )

        response = client.post(
            f"/schedules/{schedule.id}/guest-bookings/",
            json={
                "date": target_date.isoformat(),
                "child_student_id": child.id,
            },
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 201
        data = response.json()
        assert data["student_id"] == child.id
        assert data["created_from"] == ScheduleEnrollment.CreatedFrom.STUDENT_SELF_BOOKING
        event = ScheduleBookingEvent.objects.for_club(club).get(enrollment_id=data["enrollment_id"])
        assert event.origin == ScheduleBookingEvent.Origin.PARENT_SELF_BOOKING
        assert event.actor_id == parent_user.id

    def test_parent_guest_booking_requires_own_child(self, club, parent_user):
        target_date = _future_date_obj(7)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        other_child = StudentFactory(club=club, is_child=True, status=Student.Status.ACTIVE)

        missing_child_response = client.post(
            f"/schedules/{schedule.id}/guest-bookings/",
            json={"date": target_date.isoformat()},
            **_auth_params(parent_user, club, role="parent"),
        )
        other_child_response = client.post(
            f"/schedules/{schedule.id}/guest-bookings/",
            json={
                "date": target_date.isoformat(),
                "child_student_id": other_child.id,
            },
            **_auth_params(parent_user, club, role="parent"),
        )

        assert missing_child_response.status_code == 400
        assert missing_child_response.json()["code"] == "child_student_id_required"
        assert other_child_response.status_code == 404

    def test_owner_cannot_use_self_service_guest_booking_endpoint(self, club, owner_user):
        target_date = _future_date_obj(7)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )

        response = client.post(
            f"/schedules/{schedule.id}/guest-bookings/",
            json={"date": target_date.isoformat()},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 403

    def test_student_books_published_personal_availability_slot(self, club, student_user):
        target_date = _future_date_obj(7)
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
        tariff = TariffFactory(training_type=training_type)
        subscription = SubscriptionFactory(club=club, student=student, tariff=tariff)
        slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=_aware_datetime(target_date, time(10, 0)),
            ends_at=_aware_datetime(target_date, time(11, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )

        response = client.post(
            f"/personal-availability/{slot.id}/book/",
            json={"idempotency_key": "student-personal-slot-1"},
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 201
        data = response.json()
        assert data["availability_slot_id"] == slot.id
        assert data["student_id"] == student.id
        assert data["trainer_id"] == trainer.id
        assert data["location_id"] == location.id
        assert data["training_type_id"] == training_type.id
        assert data["created"] is True
        assert data["created_from"] == ScheduleEnrollment.CreatedFrom.PERSONAL_BOOKING
        slot.refresh_from_db()
        assert slot.status == PersonalAvailabilitySlot.Status.BOOKED
        assert slot.booked_enrollment_id == data["enrollment_id"]
        event = ScheduleBookingEvent.objects.for_club(club).get(enrollment_id=data["enrollment_id"])
        assert event.origin == ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING
        assert event.metadata == {
            "availability_slot_id": slot.id,
            "idempotency_key": "student-personal-slot-1",
            "subscription_id": subscription.id,
        }

    def test_parent_books_published_personal_availability_for_own_child(self, club, parent_user):
        target_date = _future_date_obj(7)
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        child = StudentFactory(
            club=club,
            parent_user=parent_user,
            is_child=True,
            status=Student.Status.ACTIVE,
        )
        tariff = TariffFactory(training_type=training_type)
        SubscriptionFactory(club=club, student=child, tariff=tariff)
        slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=_aware_datetime(target_date, time(10, 0)),
            ends_at=_aware_datetime(target_date, time(11, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )

        response = client.post(
            f"/personal-availability/{slot.id}/book/",
            json={"child_student_id": child.id},
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 201
        data = response.json()
        assert data["student_id"] == child.id
        event = ScheduleBookingEvent.objects.for_club(club).get(enrollment_id=data["enrollment_id"])
        assert event.origin == ScheduleBookingEvent.Origin.PARENT_SELF_BOOKING
        assert event.actor_id == parent_user.id

    def test_parent_cannot_book_personal_availability_for_non_child(self, club, parent_user):
        target_date = _future_date_obj(7)
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        other_child = StudentFactory(club=club, is_child=True, status=Student.Status.ACTIVE)
        tariff = TariffFactory(training_type=training_type)
        SubscriptionFactory(club=club, student=other_child, tariff=tariff)
        slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=_aware_datetime(target_date, time(10, 0)),
            ends_at=_aware_datetime(target_date, time(11, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )

        response = client.post(
            f"/personal-availability/{slot.id}/book/",
            json={"child_student_id": other_child.id},
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 404
        assert Schedule.objects.for_club(club).filter(one_time_date=target_date).count() == 0
        slot.refresh_from_db()
        assert slot.status == PersonalAvailabilitySlot.Status.PUBLISHED

    def test_student_without_personal_subscription_sees_payment_required_slot(self, club, student_user):
        target_date = _future_date_obj(7)
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
        tariff = TariffFactory(
            club=club,
            training_type=training_type,
            name="Разовая персоналка",
            price=Decimal("2000.00"),
            trainings_limit=1,
        )
        slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=_aware_datetime(target_date, time(10, 0)),
            ends_at=_aware_datetime(target_date, time(11, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )

        response = client.get(
            f"/personal-availability/options/?date={target_date.isoformat()}",
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["slot_id"] == slot.id
        assert data[0]["booking_status"] == "can_pay"
        assert data[0]["reason_code"] == "payment_required"
        assert data[0]["subscription_id"] is None
        assert data[0]["payment_tariff_id"] == tariff.id
        assert data[0]["payment_tariff_name"] == "Разовая персоналка"
        assert data[0]["payment_amount"] == "2000.00"

    def test_student_creates_personal_availability_payment_reservation(
        self,
        settings,
        club,
        student_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        target_date = _future_date_obj(7)
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
        tariff = TariffFactory(
            club=club,
            training_type=training_type,
            name="Разовая персоналка",
            price=Decimal("2000.00"),
            trainings_limit=1,
        )
        slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=_aware_datetime(target_date, time(10, 0)),
            ends_at=_aware_datetime(target_date, time(11, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )

        response = client.post(
            f"/personal-availability/{slot.id}/payment-reservations/",
            json={
                "tariff_id": tariff.id,
                "idempotency_key": "student-personal-payment-hold-1",
            },
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 201
        data = response.json()
        assert data["student_id"] == student.id
        assert data["availability_slot_id"] == slot.id
        assert data["status"] == PersonalBookingPaymentReservation.Status.PENDING_PAYMENT
        assert data["bank_payment_order_id"] is not None
        assert data["provider_payment_url"]
        assert data["can_cancel"] is True
        slot.refresh_from_db()
        assert slot.status == PersonalAvailabilitySlot.Status.HELD
        order = BankPaymentOrder.objects.for_club(club).get(id=data["bank_payment_order_id"])
        assert order.source == BankPaymentOrder.Source.STUDENT
        assert order.subscription.status == Subscription.Status.PENDING
        assert order.payment.status == Payment.Status.PENDING

    def test_exact_personal_payment_reservation_is_actor_scoped_and_retains_status(
        self,
        settings,
        club,
        student_user,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        target_date = _future_date_obj(7)
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
        other_user = UserFactory()
        StudentFactory(club=club, user=other_user, status=Student.Status.ACTIVE)
        tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=1)
        first_slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=_aware_datetime(target_date, time(10, 0)),
            ends_at=_aware_datetime(target_date, time(11, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )
        second_slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=_aware_datetime(target_date, time(12, 0)),
            ends_at=_aware_datetime(target_date, time(13, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )
        own = client.post(
            f"/personal-availability/{first_slot.id}/payment-reservations/",
            json={"tariff_id": tariff.id},
            **_auth_params(student_user, club, role="student"),
        )
        other = client.post(
            f"/personal-availability/{second_slot.id}/payment-reservations/",
            json={"tariff_id": tariff.id},
            **_auth_params(other_user, club, role="student"),
        )
        own_exact = client.get(
            f"/personal-availability/payment-reservations/{own.json()['id']}/",
            **_auth_params(student_user, club, role="student"),
        )
        hidden_other = client.get(
            f"/personal-availability/payment-reservations/{other.json()['id']}/",
            **_auth_params(student_user, club, role="student"),
        )
        hidden_role = client.get(
            f"/personal-availability/payment-reservations/{own.json()['id']}/",
            **_auth_params(owner_user, club, role="owner"),
        )

        assert own.status_code == other.status_code == 201
        assert own_exact.status_code == 200
        assert own_exact.json()["id"] == own.json()["id"]
        assert own_exact.json()["status"] == PersonalBookingPaymentReservation.Status.PENDING_PAYMENT
        assert hidden_other.status_code == 404
        assert hidden_other.json()["detail"] == "Not found"
        assert hidden_role.status_code == 404
        assert hidden_role.json()["detail"] == "Not found"

    def test_student_personal_payment_reservation_list_hides_cancel_after_expiry(
        self,
        settings,
        club,
        student_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        target_date = _future_date_obj(7)
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
        tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=1)
        slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=_aware_datetime(target_date, time(10, 0)),
            ends_at=_aware_datetime(target_date, time(11, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )
        created = client.post(
            f"/personal-availability/{slot.id}/payment-reservations/",
            json={"tariff_id": tariff.id},
            **_auth_params(student_user, club, role="student"),
        )
        assert created.status_code == 201, created.json()
        reservation = PersonalBookingPaymentReservation.objects.for_club(club).get(
            id=created.json()["id"]
        )
        reservation.expires_at = timezone.now() - timedelta(minutes=1)
        reservation.save(update_fields=["expires_at", "updated_at"])

        response = client.get(
            "/personal-availability/payment-reservations/?status=pending_payment",
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == reservation.id
        assert data[0]["can_cancel"] is False

    def test_student_cannot_create_second_pending_personal_payment_reservation_same_tariff(
        self,
        settings,
        club,
        student_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        target_date = _future_date_obj(7)
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
        tariff = TariffFactory(
            club=club,
            training_type=training_type,
            name="Разовая персоналка",
            price=Decimal("2000.00"),
            trainings_limit=1,
        )
        first_slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=_aware_datetime(target_date, time(10, 0)),
            ends_at=_aware_datetime(target_date, time(11, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )
        second_slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=_aware_datetime(target_date, time(12, 0)),
            ends_at=_aware_datetime(target_date, time(13, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )

        created = client.post(
            f"/personal-availability/{first_slot.id}/payment-reservations/",
            json={"tariff_id": tariff.id},
            **_auth_params(student_user, club, role="student"),
        )
        response = client.post(
            f"/personal-availability/{second_slot.id}/payment-reservations/",
            json={"tariff_id": tariff.id},
            **_auth_params(student_user, club, role="student"),
        )

        assert created.status_code == 201, created.json()
        assert response.status_code == 400
        assert response.json()["code"] == "personal_payment_reservation_pending_exists"
        second_slot.refresh_from_db()
        assert second_slot.status == PersonalAvailabilitySlot.Status.PUBLISHED
        assert PersonalBookingPaymentReservation.objects.for_club(club).count() == 1

    def test_parent_creates_child_personal_availability_payment_reservation(
        self,
        settings,
        club,
        parent_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        target_date = _future_date_obj(7)
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        child = StudentFactory(
            club=club,
            parent_user=parent_user,
            is_child=True,
            status=Student.Status.ACTIVE,
        )
        tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=1)
        slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=_aware_datetime(target_date, time(11, 0)),
            ends_at=_aware_datetime(target_date, time(12, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )

        response = client.post(
            f"/personal-availability/{slot.id}/payment-reservations/",
            json={"child_student_id": child.id, "tariff_id": tariff.id},
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 201, response.json()
        data = response.json()
        assert data["student_id"] == child.id
        order = BankPaymentOrder.objects.for_club(club).get(id=data["bank_payment_order_id"])
        assert order.source == BankPaymentOrder.Source.PARENT

    def test_parent_payment_reservation_uses_club_local_time_after_existing_personal_booking(
        self,
        settings,
        club,
        student_user,
        parent_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        target_date = _future_date_obj(8)
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(
            club=club,
            trainer=trainer,
            location=location,
            rate_personal=50,
        )
        package_student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
        child = StudentFactory(
            club=club,
            parent_user=parent_user,
            is_child=True,
            status=Student.Status.ACTIVE,
        )
        tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=1)
        SubscriptionFactory(club=club, student=package_student, tariff=tariff)
        package_slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=_aware_datetime(target_date, time(15, 30)),
            ends_at=_aware_datetime(target_date, time(16, 30)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )
        payment_slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=_aware_datetime(target_date, time(18, 0)),
            ends_at=_aware_datetime(target_date, time(19, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )
        booked = client.post(
            f"/personal-availability/{package_slot.id}/book/",
            json={"idempotency_key": "package-personal-local-time"},
            **_auth_params(student_user, club, role="student"),
        )
        assert booked.status_code == 201, booked.json()
        booked_schedule = Schedule.objects.for_club(club).get(id=booked.json()["schedule_id"])
        assert booked_schedule.start_time == time(15, 30)
        assert booked_schedule.end_time == time(16, 30)

        response = client.post(
            f"/personal-availability/{payment_slot.id}/payment-reservations/",
            json={
                "child_student_id": child.id,
                "tariff_id": tariff.id,
                "idempotency_key": "parent-personal-local-time",
            },
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 201, response.json()
        data = response.json()
        assert data["student_id"] == child.id
        assert data["availability_slot_id"] == payment_slot.id

    def test_parent_personal_payment_reservation_idempotency_key_is_child_scoped(
        self,
        settings,
        club,
        parent_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        target_date = _future_date_obj(7)
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        first_child = StudentFactory(
            club=club,
            parent_user=parent_user,
            is_child=True,
            status=Student.Status.ACTIVE,
        )
        second_child = StudentFactory(
            club=club,
            parent_user=parent_user,
            is_child=True,
            status=Student.Status.ACTIVE,
        )
        tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=1)
        first_slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=_aware_datetime(target_date, time(9, 0)),
            ends_at=_aware_datetime(target_date, time(10, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )
        second_slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=_aware_datetime(target_date, time(15, 0)),
            ends_at=_aware_datetime(target_date, time(16, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )

        created = client.post(
            f"/personal-availability/{first_slot.id}/payment-reservations/",
            json={
                "child_student_id": first_child.id,
                "tariff_id": tariff.id,
                "idempotency_key": "shared-personal-hold-key",
            },
            **_auth_params(parent_user, club, role="parent"),
        )
        response = client.post(
            f"/personal-availability/{second_slot.id}/payment-reservations/",
            json={
                "child_student_id": second_child.id,
                "tariff_id": tariff.id,
                "idempotency_key": "shared-personal-hold-key",
            },
            **_auth_params(parent_user, club, role="parent"),
        )

        assert created.status_code == 201, created.json()
        assert response.status_code == 400
        assert response.json()["code"] == "personal_payment_reservation_idempotency_conflict"
        assert "id" not in response.json()
        second_slot.refresh_from_db()
        assert second_slot.status == PersonalAvailabilitySlot.Status.PUBLISHED
        assert PersonalBookingPaymentReservation.objects.for_club(club).count() == 1

    def test_student_personal_payment_reservation_list_hides_other_source(
        self,
        settings,
        club,
        student_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        target_date = _future_date_obj(7)
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
        tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=1)
        slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=_aware_datetime(target_date, time(16, 0)),
            ends_at=_aware_datetime(target_date, time(17, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )
        created = client.post(
            f"/personal-availability/{slot.id}/payment-reservations/",
            json={"tariff_id": tariff.id},
            **_auth_params(student_user, club, role="student"),
        )
        order = BankPaymentOrder.objects.for_club(club).get(id=created.json()["bank_payment_order_id"])
        order.source = BankPaymentOrder.Source.TRAINER
        order.save(update_fields=["source", "updated_at"])

        response = client.get(
            "/personal-availability/payment-reservations/?status=pending_payment",
            **_auth_params(student_user, club, role="student"),
        )

        assert created.status_code == 201, created.json()
        assert response.status_code == 200
        assert response.json() == []

    def test_student_cancels_personal_payment_reservation_and_releases_slot(
        self,
        settings,
        club,
        student_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        target_date = _future_date_obj(7)
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
        tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=1)
        slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=_aware_datetime(target_date, time(12, 0)),
            ends_at=_aware_datetime(target_date, time(13, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )
        created = client.post(
            f"/personal-availability/{slot.id}/payment-reservations/",
            json={"tariff_id": tariff.id},
            **_auth_params(student_user, club, role="student"),
        )

        response = client.post(
            f"/personal-availability/payment-reservations/{created.json()['id']}/cancel/",
            json={},
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 200
        assert response.json()["status"] == PersonalBookingPaymentReservation.Status.CANCELLED
        slot.refresh_from_db()
        assert slot.status == PersonalAvailabilitySlot.Status.PUBLISHED
        order = BankPaymentOrder.objects.for_club(club).get(id=created.json()["bank_payment_order_id"])
        assert order.status == BankPaymentOrder.Status.CANCELLED

    def test_parent_cannot_create_personal_payment_reservation_for_non_child(
        self,
        settings,
        club,
        parent_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        target_date = _future_date_obj(8)
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        other_child = StudentFactory(club=club, is_child=True, status=Student.Status.ACTIVE)
        tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=1)
        slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=_aware_datetime(target_date, time(14, 0)),
            ends_at=_aware_datetime(target_date, time(15, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )

        response = client.post(
            f"/personal-availability/{slot.id}/payment-reservations/",
            json={"child_student_id": other_child.id, "tariff_id": tariff.id},
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 404
        slot.refresh_from_db()
        assert slot.status == PersonalAvailabilitySlot.Status.PUBLISHED

    def test_student_personal_availability_options_are_safe_and_actionable(self, club, student_user):
        target_date = _future_date_obj(8)
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        bookable_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        blocked_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.MINI_GROUP)
        group_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
        tariff = TariffFactory(training_type=bookable_type)
        subscription = SubscriptionFactory(club=club, student=student, tariff=tariff)
        bookable_slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=bookable_type,
            starts_at=_aware_datetime(target_date, time(10, 0)),
            ends_at=_aware_datetime(target_date, time(11, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )
        blocked_slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=blocked_type,
            starts_at=_aware_datetime(target_date, time(12, 0)),
            ends_at=_aware_datetime(target_date, time(13, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )
        PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=group_type,
            starts_at=_aware_datetime(target_date, time(14, 0)),
            ends_at=_aware_datetime(target_date, time(15, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )
        PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=bookable_type,
            starts_at=_aware_datetime(target_date, time(16, 0)),
            ends_at=_aware_datetime(target_date, time(17, 0)),
            status=PersonalAvailabilitySlot.Status.BOOKED,
        )

        response = client.get(
            f"/personal-availability/options/?date={target_date.isoformat()}",
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 200
        options_by_slot = {item["slot_id"]: item for item in response.json()}
        assert set(options_by_slot) == {bookable_slot.id, blocked_slot.id}
        assert options_by_slot[bookable_slot.id]["booking_status"] == "can_book"
        assert options_by_slot[bookable_slot.id]["subscription_id"] == subscription.id
        assert options_by_slot[blocked_slot.id]["booking_status"] == "blocked"
        assert options_by_slot[blocked_slot.id]["reason_code"] == "subscription_not_available"

    def test_student_personal_availability_options_use_club_timezone_day_bounds(
        self,
        club,
        student_user,
    ):
        club.timezone = "Asia/Yekaterinburg"
        club.save(update_fields=["timezone"])
        target_date = _future_date_obj(8)
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
        tariff = TariffFactory(club=club, training_type=training_type)
        subscription = SubscriptionFactory(club=club, student=student, tariff=tariff)
        club_tz = ZoneInfo("Asia/Yekaterinburg")
        slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=timezone.make_aware(datetime.combine(target_date, time(0, 30)), club_tz),
            ends_at=timezone.make_aware(datetime.combine(target_date, time(1, 30)), club_tz),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )

        response = client.get(
            f"/personal-availability/options/?date={target_date.isoformat()}",
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 200
        data = response.json()
        assert [item["slot_id"] for item in data] == [slot.id]
        assert data[0]["date"] == target_date.isoformat()
        assert data[0]["booking_status"] == "can_book"
        assert data[0]["subscription_id"] == subscription.id

    def test_student_personal_availability_options_hide_same_day_past_slots(self, club, student_user):
        target_date = timezone.localdate()
        now = timezone.make_aware(datetime.combine(target_date, time(15, 0)))
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
        tariff = TariffFactory(club=club, training_type=training_type)
        subscription = SubscriptionFactory(club=club, student=student, tariff=tariff)
        past_slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=_aware_datetime(target_date, time(10, 0)),
            ends_at=_aware_datetime(target_date, time(11, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )
        future_slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=_aware_datetime(target_date, time(16, 0)),
            ends_at=_aware_datetime(target_date, time(17, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )

        with patch("apps.attendance.selectors.timezone.now", return_value=now):
            response = client.get(
                f"/personal-availability/options/?date={target_date.isoformat()}",
                **_auth_params(student_user, club, role="student"),
            )

        assert response.status_code == 200
        data = response.json()
        assert [item["slot_id"] for item in data] == [future_slot.id]
        assert data[0]["booking_status"] == "can_book"
        assert data[0]["subscription_id"] == subscription.id
        assert past_slot.id not in {item["slot_id"] for item in data}

    def test_student_personal_availability_options_hide_blocked_and_schedule_overlaps(self, club, student_user):
        target_date = _future_date_obj(8)
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
        tariff = TariffFactory(training_type=training_type)
        SubscriptionFactory(club=club, student=student, tariff=tariff)
        visible_slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=_aware_datetime(target_date, time(10, 0)),
            ends_at=_aware_datetime(target_date, time(11, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )
        blocked_slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=_aware_datetime(target_date, time(12, 0)),
            ends_at=_aware_datetime(target_date, time(13, 0)),
            status=PersonalAvailabilitySlot.Status.BLOCKED,
        )
        overlapped_slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=_aware_datetime(target_date, time(14, 0)),
            ends_at=_aware_datetime(target_date, time(15, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )
        ScheduleFactory(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=target_date,
            start_time=time(14, 30),
            end_time=time(15, 30),
        )

        response = client.get(
            f"/personal-availability/options/?date={target_date.isoformat()}",
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 200
        slot_ids = {item["slot_id"] for item in response.json()}
        assert slot_ids == {visible_slot.id}
        assert blocked_slot.id not in slot_ids
        assert overlapped_slot.id not in slot_ids

    def test_student_personal_availability_options_respect_schedule_exceptions(self, club, student_user):
        target_date = _future_date_obj(7)
        trainer = TrainerFactory(club=club)
        substitute = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        group_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        personal_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        TrainerLocationFactory(club=club, trainer=substitute, location=location)
        student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
        tariff = TariffFactory(training_type=personal_type)
        SubscriptionFactory(club=club, student=student, tariff=tariff)
        cancelled_schedule = ScheduleFactory(
            club=club,
            trainer=trainer,
            location=location,
            training_type=group_type,
            day_of_week=target_date.weekday(),
            start_time=time(10, 0),
            end_time=time(11, 0),
        )
        ScheduleExceptionFactory(
            schedule=cancelled_schedule,
            date=target_date,
            exception_type=ScheduleException.ExceptionType.CANCELLED,
        )
        substitute_schedule = ScheduleFactory(
            club=club,
            trainer=trainer,
            location=location,
            training_type=group_type,
            day_of_week=target_date.weekday(),
            start_time=time(12, 0),
            end_time=time(13, 0),
        )
        ScheduleExceptionFactory(
            schedule=substitute_schedule,
            date=target_date,
            exception_type=ScheduleException.ExceptionType.SUBSTITUTE,
            substitute_trainer=substitute,
        )
        cancelled_schedule_slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=personal_type,
            starts_at=_aware_datetime(target_date, time(10, 0)),
            ends_at=_aware_datetime(target_date, time(11, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )
        original_trainer_slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=personal_type,
            starts_at=_aware_datetime(target_date, time(12, 0)),
            ends_at=_aware_datetime(target_date, time(13, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )
        substitute_trainer_slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=substitute,
            location=location,
            training_type=personal_type,
            starts_at=_aware_datetime(target_date, time(12, 0)),
            ends_at=_aware_datetime(target_date, time(13, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )

        response = client.get(
            f"/personal-availability/options/?date={target_date.isoformat()}",
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 200
        slot_ids = {item["slot_id"] for item in response.json()}
        assert slot_ids == {cancelled_schedule_slot.id, original_trainer_slot.id}
        assert substitute_trainer_slot.id not in slot_ids

    def test_parent_personal_availability_options_require_own_child_scope(self, club, parent_user):
        target_date = _future_date_obj(8)
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        own_child = StudentFactory(
            club=club,
            parent_user=parent_user,
            is_child=True,
            status=Student.Status.ACTIVE,
        )
        other_child = StudentFactory(club=club, is_child=True, status=Student.Status.ACTIVE)
        tariff = TariffFactory(training_type=training_type)
        SubscriptionFactory(club=club, student=own_child, tariff=tariff)
        slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=_aware_datetime(target_date, time(10, 0)),
            ends_at=_aware_datetime(target_date, time(11, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )

        own_child_response = client.get(
            (
                f"/personal-availability/options/?date={target_date.isoformat()}"
                f"&child_student_id={own_child.id}"
            ),
            **_auth_params(parent_user, club, role="parent"),
        )
        other_child_response = client.get(
            (
                f"/personal-availability/options/?date={target_date.isoformat()}"
                f"&child_student_id={other_child.id}"
            ),
            **_auth_params(parent_user, club, role="parent"),
        )

        assert own_child_response.status_code == 200
        assert own_child_response.json()[0]["slot_id"] == slot.id
        assert other_child_response.status_code == 404


@pytest.mark.django_db
class TestTrainerPersonalAvailabilityAPI:
    def test_trainer_generates_lists_blocks_unblocks_and_cancels_own_slots(
        self,
        club,
        trainer_user,
    ):
        target_date = timezone.localdate() + timedelta(days=14)
        trainer = TrainerFactory(club=club, user=trainer_user)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)

        generate_response = client.post(
            "/personal-availability/slots/generate/",
            json={
                "date_from": target_date.isoformat(),
                "date_to": target_date.isoformat(),
                "weekdays": [target_date.weekday()],
                "start_time": "10:00:00",
                "end_time": "11:00:00",
                "location_id": location.id,
                "training_type_id": training_type.id,
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert generate_response.status_code == 200
        generated = generate_response.json()
        assert generated["skipped"] == []
        slot = generated["created"][0]
        assert slot["trainer_id"] == trainer.id
        assert slot["status"] == PersonalAvailabilitySlot.Status.PUBLISHED
        assert slot["can_block"] is True
        assert slot["can_unblock"] is False
        assert slot["can_cancel"] is True

        list_response = client.get(
            (
                "/personal-availability/slots/"
                f"?date_from={target_date.isoformat()}&date_to={target_date.isoformat()}"
            ),
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert list_response.status_code == 200
        assert [item["id"] for item in list_response.json()] == [slot["id"]]

        block_response = client.post(
            f"/personal-availability/slots/{slot['id']}/block/",
            json={"reason": "личные дела"},
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert block_response.status_code == 200
        assert block_response.json()["status"] == PersonalAvailabilitySlot.Status.BLOCKED
        assert block_response.json()["block_reason"] == "личные дела"
        assert block_response.json()["can_unblock"] is True

        unblock_response = client.post(
            f"/personal-availability/slots/{slot['id']}/unblock/",
            json={},
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert unblock_response.status_code == 200
        assert unblock_response.json()["status"] == PersonalAvailabilitySlot.Status.PUBLISHED
        assert unblock_response.json()["block_reason"] == ""

        cancel_response = client.post(
            f"/personal-availability/slots/{slot['id']}/cancel/",
            json={},
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert cancel_response.status_code == 200
        assert cancel_response.json()["status"] == PersonalAvailabilitySlot.Status.CANCELLED
        assert cancel_response.json()["can_block"] is False
        assert cancel_response.json()["can_cancel"] is False

    def test_trainer_generation_returns_skipped_schedule_conflicts(self, club, trainer_user):
        target_date = timezone.localdate() + timedelta(days=14)
        trainer = TrainerFactory(club=club, user=trainer_user)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        ScheduleFactory(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=target_date,
            start_time=time(10, 30),
            end_time=time(11, 30),
        )

        response = client.post(
            "/personal-availability/slots/generate/",
            json={
                "date_from": target_date.isoformat(),
                "date_to": target_date.isoformat(),
                "weekdays": [target_date.weekday()],
                "start_time": "10:00:00",
                "end_time": "11:00:00",
                "location_id": location.id,
                "training_type_id": training_type.id,
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        assert response.json()["created"] == []
        assert [item["reason_code"] for item in response.json()["skipped"]] == ["schedule_overlap"]

    def test_trainer_cannot_block_reserved_availability_slot(self, club, trainer_user):
        target_date = timezone.localdate() + timedelta(days=14)
        trainer = TrainerFactory(club=club, user=trainer_user)
        slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=LocationFactory(club=club),
            training_type=TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL),
            starts_at=_aware_datetime(target_date, time(10, 0)),
            ends_at=_aware_datetime(target_date, time(11, 0)),
            status=PersonalAvailabilitySlot.Status.HELD,
        )

        response = client.post(
            f"/personal-availability/slots/{slot.id}/block/",
            json={"reason": "занят"},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "personal_availability_slot_reserved"

    def test_trainer_cannot_manage_another_trainer_availability(self, club, trainer_user):
        target_date = timezone.localdate() + timedelta(days=14)
        current_trainer = TrainerFactory(club=club, user=trainer_user)
        other_trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=current_trainer, location=location)
        TrainerLocationFactory(club=club, trainer=other_trainer, location=location)
        other_slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=other_trainer,
            location=location,
            training_type=training_type,
            starts_at=_aware_datetime(target_date, time(12, 0)),
            ends_at=_aware_datetime(target_date, time(13, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )
        other_blocked_slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=other_trainer,
            location=location,
            training_type=training_type,
            starts_at=_aware_datetime(target_date, time(14, 0)),
            ends_at=_aware_datetime(target_date, time(15, 0)),
            status=PersonalAvailabilitySlot.Status.BLOCKED,
        )

        list_response = client.get(
            (
                "/personal-availability/slots/"
                f"?date_from={target_date.isoformat()}&date_to={target_date.isoformat()}"
                f"&trainer_id={other_trainer.id}"
            ),
            **_auth_params(trainer_user, club, role="trainer"),
        )
        generate_response = client.post(
            "/personal-availability/slots/generate/",
            json={
                "date_from": target_date.isoformat(),
                "date_to": target_date.isoformat(),
                "weekdays": [target_date.weekday()],
                "start_time": "10:00:00",
                "end_time": "11:00:00",
                "location_id": location.id,
                "training_type_id": training_type.id,
                "trainer_id": other_trainer.id,
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )
        block_response = client.post(
            f"/personal-availability/slots/{other_slot.id}/block/?trainer_id={other_trainer.id}",
            json={"reason": "spoof"},
            **_auth_params(trainer_user, club, role="trainer"),
        )
        unblock_response = client.post(
            f"/personal-availability/slots/{other_blocked_slot.id}/unblock/?trainer_id={other_trainer.id}",
            **_auth_params(trainer_user, club, role="trainer"),
        )
        cancel_response = client.post(
            f"/personal-availability/slots/{other_slot.id}/cancel/?trainer_id={other_trainer.id}",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert list_response.status_code == 403
        assert generate_response.status_code == 403
        assert block_response.status_code == 403
        assert unblock_response.status_code == 403
        assert cancel_response.status_code == 403

    def test_owner_must_scope_trainer_availability_and_can_list_selected_trainer(self, club, owner_user):
        target_date = timezone.localdate() + timedelta(days=14)
        trainer = TrainerFactory(club=club)
        other_trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        TrainerLocationFactory(club=club, trainer=other_trainer, location=location)
        selected_slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=_aware_datetime(target_date, time(10, 0)),
            ends_at=_aware_datetime(target_date, time(11, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )
        PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=other_trainer,
            location=location,
            training_type=training_type,
            starts_at=_aware_datetime(target_date, time(12, 0)),
            ends_at=_aware_datetime(target_date, time(13, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )

        missing_scope_response = client.get(
            (
                "/personal-availability/slots/"
                f"?date_from={target_date.isoformat()}&date_to={target_date.isoformat()}"
            ),
            **_auth_params(owner_user, club),
        )
        scoped_response = client.get(
            (
                "/personal-availability/slots/"
                f"?date_from={target_date.isoformat()}&date_to={target_date.isoformat()}"
                f"&trainer_id={trainer.id}"
            ),
            **_auth_params(owner_user, club),
        )

        assert missing_scope_response.status_code == 400
        assert scoped_response.status_code == 200
        assert [item["id"] for item in scoped_response.json()] == [selected_slot.id]

    def test_student_cannot_use_trainer_availability_calendar_endpoints(self, club, student_user):
        target_date = timezone.localdate() + timedelta(days=14)

        response = client.get(
            (
                "/personal-availability/slots/"
                f"?date_from={target_date.isoformat()}&date_to={target_date.isoformat()}"
            ),
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 403


@pytest.mark.django_db
class TestSelfServiceGuestBookingOptionsAPI:
    def test_student_guest_booking_options_are_safe_and_actionable(self, club, student_user):
        target_date = _future_date_obj(7)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        bookable_schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            group_name="Bookable group",
            training_type=training_type,
        )
        already_booked_schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            group_name="Already booked group",
            training_type=training_type,
        )
        personal_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            group_name="Private trainer slot",
            training_type=personal_type,
        )
        student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
        tariff = TariffFactory(training_type=training_type)
        SubscriptionFactory(club=club, student=student, tariff=tariff)
        ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=already_booked_schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=target_date,
        )

        response = client.get(
            f"/schedules/guest-booking-options/?date={target_date.isoformat()}",
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 200
        options_by_schedule = {item["schedule_id"]: item for item in response.json()}
        assert set(options_by_schedule) == {
            bookable_schedule.id,
            already_booked_schedule.id,
        }
        assert options_by_schedule[bookable_schedule.id]["booking_status"] == "can_book"
        assert options_by_schedule[bookable_schedule.id]["financial_status"] == "subscription"
        assert options_by_schedule[already_booked_schedule.id]["booking_status"] == "already_booked"
        assert options_by_schedule[already_booked_schedule.id]["reason_code"] == "already_booked"

    def test_student_guest_booking_options_block_same_day_past_sessions(self, club, student_user):
        target_date = timezone.localdate()
        now = timezone.make_aware(datetime.combine(target_date, time(15, 0)))
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        past_schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            start_time=time(10, 0),
            end_time=time(11, 0),
            training_type=training_type,
        )
        future_schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            start_time=time(16, 0),
            end_time=time(17, 0),
            training_type=training_type,
        )
        student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
        tariff = TariffFactory(training_type=training_type)
        SubscriptionFactory(club=club, student=student, tariff=tariff)

        with patch("apps.attendance.selectors.timezone.now", return_value=now):
            response = client.get(
                f"/schedules/guest-booking-options/?date={target_date.isoformat()}",
                **_auth_params(student_user, club, role="student"),
            )

        assert response.status_code == 200
        options_by_schedule = {item["schedule_id"]: item for item in response.json()}
        assert options_by_schedule[past_schedule.id]["booking_status"] == "blocked"
        assert options_by_schedule[past_schedule.id]["reason_code"] == "self_booking_past_session"
        assert options_by_schedule[future_schedule.id]["booking_status"] == "can_book"

    def test_student_guest_booking_options_block_without_subscription_or_dropin_policy(self, club, student_user):
        target_date = _future_date_obj(7)
        training_type = TrainingTypeFactory(
            club=club,
            kind=TrainingType.Kind.GROUP,
            drop_in_price=None,
            trial_free=False,
        )
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)

        response = client.get(
            f"/schedules/guest-booking-options/?date={target_date.isoformat()}",
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 200
        assert response.json() == [
            {
                "schedule_id": schedule.id,
                "date": target_date.isoformat(),
                "start_time": "10:00:00",
                "end_time": "11:00:00",
                "group_name": schedule.group_name,
                "trainer_name": f"{schedule.trainer.first_name} {schedule.trainer.last_name}",
                "location_name": schedule.location.name,
                "training_type_id": training_type.id,
                "training_type_name": training_type.name,
                "booking_status": "blocked",
                "reason_code": "drop_in_price_required",
                "financial_status": "blocked",
                "subscription_id": None,
                "drop_in_price": None,
            }
        ]

    def test_parent_guest_booking_options_require_own_child_scope(self, club, parent_user):
        target_date = _future_date_obj(7)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        own_child = StudentFactory(
            club=club,
            is_child=True,
            parent_user=parent_user,
            status=Student.Status.ACTIVE,
        )
        other_child = StudentFactory(club=club, is_child=True, status=Student.Status.ACTIVE)
        tariff = TariffFactory(training_type=training_type)
        SubscriptionFactory(club=club, student=own_child, tariff=tariff)

        own_child_response = client.get(
            (
                f"/schedules/guest-booking-options/?date={target_date.isoformat()}"
                f"&child_student_id={own_child.id}"
            ),
            **_auth_params(parent_user, club, role="parent"),
        )
        other_child_response = client.get(
            (
                f"/schedules/guest-booking-options/?date={target_date.isoformat()}"
                f"&child_student_id={other_child.id}"
            ),
            **_auth_params(parent_user, club, role="parent"),
        )

        assert own_child_response.status_code == 200
        assert own_child_response.json()[0]["schedule_id"] == schedule.id
        assert other_child_response.status_code == 404


@pytest.mark.django_db
class TestBookingCancellationAPI:
    def test_student_cancels_own_guest_booking(self, club, student_user):
        target_date = _future_date_obj(7)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
        tariff = TariffFactory(training_type=training_type)
        SubscriptionFactory(club=club, student=student, tariff=tariff)
        booking_response = client.post(
            f"/schedules/{schedule.id}/guest-bookings/",
            json={"date": target_date.isoformat()},
            **_auth_params(student_user, club, role="student"),
        )
        enrollment_id = booking_response.json()["enrollment_id"]

        response = client.post(
            f"/guest-bookings/{enrollment_id}/cancel/",
            json={"reason": "Не получается прийти"},
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 200
        assert response.json()["status"] == ScheduleEnrollment.Status.CANCELLED
        event = ScheduleBookingEvent.objects.for_club(club).get(
            enrollment_id=enrollment_id,
            event_type=ScheduleBookingEvent.EventType.GUEST_VISIT_CANCELLED,
        )
        assert event.origin == ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING
        assert event.actor_id == student_user.id
        assert event.metadata == {"reason": "Не получается прийти"}

    def test_parent_cannot_cancel_other_child_guest_booking(self, club, parent_user, owner_user):
        target_date = _future_date_obj(7)
        schedule = ScheduleFactory(club=club, day_of_week=target_date.weekday())
        other_child = StudentFactory(
            club=club,
            is_child=True,
            parent_user=owner_user,
            status=Student.Status.ACTIVE,
        )
        enrollment = ScheduleEnrollment.objects.create(
            club=club,
            student=other_child,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=target_date,
            ends_on=target_date,
            created_from=ScheduleEnrollment.CreatedFrom.STUDENT_SELF_BOOKING,
        )

        response = client.post(
            f"/guest-bookings/{enrollment.id}/cancel/",
            json={},
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 403
        enrollment.refresh_from_db()
        assert enrollment.status == ScheduleEnrollment.Status.ACTIVE

    def test_trainer_guest_booking_cancel_requires_reason(self, club, trainer_user):
        target_date = _future_date_obj(7)
        trainer = TrainerFactory(club=club, user=trainer_user)
        schedule = ScheduleFactory(
            club=club,
            trainer=trainer,
            day_of_week=target_date.weekday(),
        )
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        enrollment = ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=target_date,
            ends_on=target_date,
            created_from=ScheduleEnrollment.CreatedFrom.STUDENT_SELF_BOOKING,
        )

        missing_reason = client.post(
            f"/guest-bookings/{enrollment.id}/cancel/",
            json={},
            **_auth_params(trainer_user, club, role="trainer"),
        )
        response = client.post(
            f"/guest-bookings/{enrollment.id}/cancel/",
            json={"reason": "Тренер заболел"},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert missing_reason.status_code == 400
        assert response.status_code == 200
        assert response.json()["status"] == ScheduleEnrollment.Status.CANCELLED
        event = ScheduleBookingEvent.objects.for_club(club).get(
            enrollment=enrollment,
            event_type=ScheduleBookingEvent.EventType.GUEST_VISIT_CANCELLED,
        )
        assert event.origin == ScheduleBookingEvent.Origin.PLANNED_SESSION_ACTION
        assert event.metadata == {"reason": "Тренер заболел"}

    def test_owner_cancels_personal_booking_and_deactivates_schedule(self, club, owner_user):
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        tariff = TariffFactory(training_type=training_type)
        subscription = SubscriptionFactory(club=club, student=student, tariff=tariff)
        booking_response = client.post(
            f"/students/{student.id}/personal-bookings/",
            json={
                "starts_at": _future_datetime(8, hour=10),
                "ends_at": _future_datetime(8, hour=11),
                "trainer_id": trainer.id,
                "location_id": location.id,
                "training_type_id": training_type.id,
                "subscription_id": subscription.id,
            },
            **_auth_params(owner_user, club),
        )
        data = booking_response.json()

        response = client.post(
            f"/personal-bookings/{data['enrollment_id']}/cancel/",
            json={"reason": "Клиент попросил перенос"},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        assert response.json()["status"] == ScheduleEnrollment.Status.CANCELLED
        schedule = Schedule.objects.for_club(club).get(id=data["schedule_id"])
        assert schedule.is_active is False
        event = ScheduleBookingEvent.objects.for_club(club).get(
            enrollment_id=data["enrollment_id"],
            event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_CANCELLED,
        )
        assert event.metadata == {"reason": "Клиент попросил перенос"}

    def test_student_cancels_personal_availability_booking_and_reopens_slot(self, club, student_user):
        target_date = _future_date_obj(8)
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        student = StudentFactory(club=club, user=student_user, status=Student.Status.ACTIVE)
        tariff = TariffFactory(training_type=training_type)
        SubscriptionFactory(club=club, student=student, tariff=tariff)
        slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=_aware_datetime(target_date, time(10, 0)),
            ends_at=_aware_datetime(target_date, time(11, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )
        booking = client.post(
            f"/personal-availability/{slot.id}/book/",
            json={"idempotency_key": "student-cancel-reopen"},
            **_auth_params(student_user, club, role="student"),
        )

        response = client.post(
            f"/personal-bookings/{booking.json()['enrollment_id']}/cancel/",
            json={"reason": ""},
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 200
        assert response.json()["status"] == ScheduleEnrollment.Status.CANCELLED
        slot.refresh_from_db()
        assert slot.status == PersonalAvailabilitySlot.Status.PUBLISHED
        assert slot.booked_enrollment_id is None
        schedule = Schedule.objects.for_club(club).get(id=booking.json()["schedule_id"])
        assert schedule.is_active is False
        event = ScheduleBookingEvent.objects.for_club(club).get(
            enrollment_id=booking.json()["enrollment_id"],
            event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_CANCELLED,
        )
        assert event.origin == ScheduleBookingEvent.Origin.STUDENT_SELF_BOOKING

    def test_parent_cancels_child_personal_availability_booking_and_reopens_slot(self, club, parent_user):
        target_date = _future_date_obj(8)
        trainer = TrainerFactory(club=club)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        child = StudentFactory(
            club=club,
            parent_user=parent_user,
            is_child=True,
            status=Student.Status.ACTIVE,
        )
        tariff = TariffFactory(training_type=training_type)
        SubscriptionFactory(club=club, student=child, tariff=tariff)
        slot = PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            starts_at=_aware_datetime(target_date, time(12, 0)),
            ends_at=_aware_datetime(target_date, time(13, 0)),
            status=PersonalAvailabilitySlot.Status.PUBLISHED,
        )
        booking = client.post(
            f"/personal-availability/{slot.id}/book/",
            json={"child_student_id": child.id},
            **_auth_params(parent_user, club, role="parent"),
        )

        response = client.post(
            f"/personal-bookings/{booking.json()['enrollment_id']}/cancel/",
            json={"reason": ""},
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 200
        assert response.json()["status"] == ScheduleEnrollment.Status.CANCELLED
        slot.refresh_from_db()
        assert slot.status == PersonalAvailabilitySlot.Status.PUBLISHED
        assert slot.booked_enrollment_id is None
        event = ScheduleBookingEvent.objects.for_club(club).get(
            enrollment_id=booking.json()["enrollment_id"],
            event_type=ScheduleBookingEvent.EventType.PERSONAL_SESSION_CANCELLED,
        )
        assert event.origin == ScheduleBookingEvent.Origin.PARENT_SELF_BOOKING


# ──────────────────────────────────────────────
# Check-in API tests
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestKioskPhoneLookupAPI:
    def test_kiosk_phone_lookup_api(self, club):
        StudentFactory(club=club, phone="+79001234567", status="active")
        response = client.post(
            "/checkins/kiosk/lookup/",
            json={"phone_suffix": "4567"},
            **_kiosk_auth_params(club),
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["lookup_suffix"] == "4567"
        assert data[0]["lookup_suffixes"] == ["4567"]
        assert data[0]["masked_phone"].endswith("4567")
        assert "phone" not in data[0]
        assert "email" not in data[0]

    def test_kiosk_phone_lookup_invalid_suffix(self, club):
        response = client.post(
            "/checkins/kiosk/lookup/",
            json={"phone_suffix": "12"},
            **_kiosk_auth_params(club),
        )
        assert response.status_code == 400


@pytest.mark.django_db
class TestKioskCheckinAPI:
    @patch("apps.attendance.services.async_task")
    def test_kiosk_checkin_api(self, mock_async, club):
        student = StudentFactory(club=club, status="active")
        training_type = TrainingTypeFactory(club=club)
        schedule = ScheduleFactory(club=club, day_of_week=date.today().weekday(), training_type=training_type)
        ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=date.today(),
        )
        response = client.post(
            "/checkins/kiosk/",
            json={
                "student_id": student.id,
                "schedule_id": schedule.id,
                "training_type_id": training_type.id,
            },
            **_kiosk_auth_params(club),
        )
        assert response.status_code == 200
        data = response.json()
        assert "checkin_id" in data
        assert data["alerts"] == []  # Kiosk: no alerts

    @patch("apps.attendance.services.async_task")
    def test_kiosk_checkin_rejects_unenrolled_student_before_side_effects(self, mock_async, club):
        student = StudentFactory(club=club, status="active")
        training_type = TrainingTypeFactory(club=club)
        schedule = ScheduleFactory(club=club, day_of_week=date.today().weekday(), training_type=training_type)

        response = client.post(
            "/checkins/kiosk/",
            json={
                "student_id": student.id,
                "schedule_id": schedule.id,
                "training_type_id": training_type.id,
            },
            **_kiosk_auth_params(club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "student_schedule_ineligible"
        assert not Checkin.objects.filter(student=student, schedule=schedule).exists()
        assert not Debt.objects.filter(student=student).exists()
        assert not CheckinCascadeEvent.objects.filter(club=club).exists()


@pytest.mark.django_db
class TestKioskGuestBookAndCheckinAPI:
    def test_kiosk_guest_book_and_checkin_creates_booking_and_checkin(self, club):
        target_date = date.today()
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        tariff = TariffFactory(training_type=training_type)
        SubscriptionFactory(club=club, student=student, tariff=tariff)

        response = client.post(
            "/checkins/kiosk/guest-book-and-checkin/",
            json={
                "student_id": student.id,
                "schedule_id": schedule.id,
                "training_type_id": training_type.id,
                "checkin_date": target_date.isoformat(),
            },
            **_kiosk_auth_params(club),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["student_id"] == student.id
        assert data["checkin_id"]
        assert data["created"] is True
        assert data["duplicate"] is False
        assert data["is_debt"] is False
        assert data["subscription_id"] is not None
        assert data["subscription_effect"] == "deducted"
        assert data["debt_effect"] == "none"
        assert data["alerts"] == []
        enrollment = ScheduleEnrollment.objects.for_club(club).get(
            student=student,
            schedule=schedule,
        )
        assert enrollment.created_from == ScheduleEnrollment.CreatedFrom.GUEST_VISIT
        assert enrollment.starts_on == target_date
        assert enrollment.ends_on == target_date
        event = ScheduleBookingEvent.objects.for_club(club).get(enrollment=enrollment)
        assert event.event_type == ScheduleBookingEvent.EventType.GUEST_VISIT_BOOKED
        assert event.origin == ScheduleBookingEvent.Origin.WALK_IN_CHECKIN
        assert event.actor_id is None
        expected_idempotency_key = (
            f"kiosk-guest-booking-{student.id}-{schedule.id}-"
            f"{target_date.isoformat()}"
        )
        assert event.metadata == {
            "idempotency_key": expected_idempotency_key
        }
        checkin = Checkin.objects.for_club(club).get(id=data["checkin_id"])
        assert checkin.student == student
        assert checkin.schedule == schedule
        assert checkin.source == "kiosk"
        assert checkin.date == target_date
        assert not Debt.objects.for_club(club).filter(student=student).exists()
        assert CheckinCascadeEvent.objects.for_club(club).filter(checkin=checkin).exists()

    def test_kiosk_guest_book_and_checkin_duplicate_returns_existing_checkin(self, club):
        target_date = date.today()
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        tariff = TariffFactory(training_type=training_type)
        SubscriptionFactory(club=club, student=student, tariff=tariff)
        payload = {
            "student_id": student.id,
            "schedule_id": schedule.id,
            "training_type_id": training_type.id,
            "checkin_date": target_date.isoformat(),
        }

        first = client.post(
            "/checkins/kiosk/guest-book-and-checkin/",
            json=payload,
            **_kiosk_auth_params(club),
        )
        second = client.post(
            "/checkins/kiosk/guest-book-and-checkin/",
            json=payload,
            **_kiosk_auth_params(club),
        )

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["checkin_id"] == first.json()["checkin_id"]
        assert second.json()["created"] is False
        assert second.json()["duplicate"] is True
        assert ScheduleEnrollment.objects.for_club(club).filter(
            student=student,
            schedule=schedule,
            created_from=ScheduleEnrollment.CreatedFrom.GUEST_VISIT,
        ).count() == 1
        assert ScheduleBookingEvent.objects.for_club(club).filter(
            event_type=ScheduleBookingEvent.EventType.GUEST_VISIT_BOOKED,
        ).count() == 1
        assert Checkin.objects.for_club(club).filter(student=student, schedule=schedule).count() == 1

    def test_kiosk_guest_book_and_checkin_blocks_without_financial_eligibility(self, club):
        target_date = date.today()
        training_type = TrainingTypeFactory(
            club=club,
            kind=TrainingType.Kind.GROUP,
            drop_in_price=None,
            trial_free=False,
        )
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)

        response = client.post(
            "/checkins/kiosk/guest-book-and-checkin/",
            json={
                "student_id": student.id,
                "schedule_id": schedule.id,
                "training_type_id": training_type.id,
                "checkin_date": target_date.isoformat(),
            },
            **_kiosk_auth_params(club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "drop_in_price_required"
        assert not ScheduleEnrollment.objects.for_club(club).filter(student=student, schedule=schedule).exists()
        assert not Checkin.objects.for_club(club).filter(student=student, schedule=schedule).exists()
        assert not Debt.objects.for_club(club).filter(student=student).exists()
        assert not ScheduleBookingEvent.objects.for_club(club).exists()

    def test_kiosk_guest_book_and_checkin_allows_dropin_debt_policy(self, club):
        target_date = date.today()
        training_type = TrainingTypeFactory(
            club=club,
            kind=TrainingType.Kind.GROUP,
            drop_in_price=1500,
            trial_free=False,
        )
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)

        response = client.post(
            "/checkins/kiosk/guest-book-and-checkin/",
            json={
                "student_id": student.id,
                "schedule_id": schedule.id,
                "training_type_id": training_type.id,
                "checkin_date": target_date.isoformat(),
            },
            **_kiosk_auth_params(club),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["created"] is True
        assert data["is_debt"] is True
        assert data["subscription_id"] is None
        assert data["debt_effect"] == "created"
        assert ScheduleEnrollment.objects.for_club(club).filter(
            student=student,
            schedule=schedule,
            created_from=ScheduleEnrollment.CreatedFrom.GUEST_VISIT,
        ).count() == 1
        assert Checkin.objects.for_club(club).filter(student=student, schedule=schedule).count() == 1
        assert Debt.objects.for_club(club).filter(student=student).count() == 1

    def test_kiosk_guest_book_and_checkin_rejects_reactivation_public_device_booking(self, club):
        target_date = date.today()
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        student = StudentFactory(club=club, status=Student.Status.AT_RISK)
        tariff = TariffFactory(training_type=training_type)
        SubscriptionFactory(club=club, student=student, tariff=tariff)

        response = client.post(
            "/checkins/kiosk/guest-book-and-checkin/",
            json={
                "student_id": student.id,
                "schedule_id": schedule.id,
                "training_type_id": training_type.id,
                "checkin_date": target_date.isoformat(),
            },
            **_kiosk_auth_params(club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "student_ineligible"
        assert not ScheduleEnrollment.objects.for_club(club).filter(student=student, schedule=schedule).exists()

    def test_kiosk_guest_book_and_checkin_rejects_lost_public_device_booking(self, club):
        target_date = date.today()
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        student = StudentFactory(club=club, status=Student.Status.LOST)
        tariff = TariffFactory(training_type=training_type)
        SubscriptionFactory(club=club, student=student, tariff=tariff)

        response = client.post(
            "/checkins/kiosk/guest-book-and-checkin/",
            json={
                "student_id": student.id,
                "schedule_id": schedule.id,
                "training_type_id": training_type.id,
                "checkin_date": target_date.isoformat(),
            },
            **_kiosk_auth_params(club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "student_ineligible"
        assert not ScheduleEnrollment.objects.for_club(club).filter(student=student, schedule=schedule).exists()

    def test_kiosk_guest_book_and_checkin_rejects_churned_public_device_booking(self, club):
        target_date = date.today()
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        student = StudentFactory(club=club, status=Student.Status.CHURNED)
        tariff = TariffFactory(training_type=training_type)
        SubscriptionFactory(club=club, student=student, tariff=tariff)

        response = client.post(
            "/checkins/kiosk/guest-book-and-checkin/",
            json={
                "student_id": student.id,
                "schedule_id": schedule.id,
                "training_type_id": training_type.id,
                "checkin_date": target_date.isoformat(),
            },
            **_kiosk_auth_params(club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "student_ineligible"
        assert not ScheduleEnrollment.objects.for_club(club).filter(student=student, schedule=schedule).exists()

    def test_kiosk_guest_book_and_checkin_rejects_existing_permanent_member(self, club):
        target_date = date.today()
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        tariff = TariffFactory(training_type=training_type)
        SubscriptionFactory(club=club, student=student, tariff=tariff)
        ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=target_date,
        )

        response = client.post(
            "/checkins/kiosk/guest-book-and-checkin/",
            json={
                "student_id": student.id,
                "schedule_id": schedule.id,
                "training_type_id": training_type.id,
                "checkin_date": target_date.isoformat(),
            },
            **_kiosk_auth_params(club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "kiosk_guest_booking_not_available"
        assert ScheduleEnrollment.objects.for_club(club).filter(student=student, schedule=schedule).count() == 1


@pytest.mark.django_db
class TestKioskOptionsAPI:
    def test_kiosk_options_returns_checkin_and_guest_booking_actions_without_mutation(self, club):
        target_date = date.today()
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        enrolled_schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
            group_name="Own group",
        )
        guest_schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
            group_name="Guest group",
        )
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        tariff = TariffFactory(training_type=training_type)
        subscription = SubscriptionFactory(club=club, student=student, tariff=tariff)
        ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=enrolled_schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=target_date,
        )

        response = client.post(
            "/checkins/kiosk/options/",
            json={
                "student_id": student.id,
                "date": target_date.isoformat(),
            },
            **_kiosk_auth_params(club),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["student_id"] == student.id
        assert data["date"] == target_date.isoformat()
        options_by_schedule = {item["schedule_id"]: item for item in data["options"]}
        assert options_by_schedule[enrolled_schedule.id]["self_checkin_status"] == "can_checkin"
        assert options_by_schedule[enrolled_schedule.id]["financial_status"] == "subscription"
        assert options_by_schedule[enrolled_schedule.id]["subscription_id"] == subscription.id
        assert options_by_schedule[guest_schedule.id]["self_checkin_status"] == "can_book_guest_visit"
        assert options_by_schedule[guest_schedule.id]["financial_status"] == "subscription"
        assert Checkin.objects.for_club(club).count() == 0
        assert Debt.objects.for_club(club).count() == 0
        assert ScheduleEnrollment.objects.for_club(club).count() == 1

    @pytest.mark.parametrize(
        ("local_time", "expected_status"),
        [
            (time(20, 29, 59), "too_early"),
            (time(20, 30), "open"),
            (time(22, 0), "open"),
            (time(22, 0, 1), "closed"),
        ],
    )
    def test_kiosk_options_uses_club_timezone_for_checkin_window(
        self,
        club,
        local_time,
        expected_status,
    ):
        club.timezone = "Asia/Yekaterinburg"
        club.save(update_fields=["timezone"])
        target_date = date(2026, 7, 24)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            start_time=time(21, 0),
            end_time=time(22, 0),
            training_type=training_type,
        )
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        tariff = TariffFactory(training_type=training_type)
        SubscriptionFactory(club=club, student=student, tariff=tariff)
        ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=target_date,
            ends_on=target_date,
        )
        current_time = datetime.combine(
            target_date,
            local_time,
            tzinfo=ZoneInfo("Asia/Yekaterinburg"),
        )

        with patch("apps.attendance.selectors.timezone.now", return_value=current_time):
            response = client.post(
                "/checkins/kiosk/options/",
                json={
                    "student_id": student.id,
                    "date": target_date.isoformat(),
                },
                **_kiosk_auth_params(club),
            )

        assert response.status_code == 200
        option = response.json()["options"][0]
        assert option["checkin_window_status"] == expected_status
        assert option["checkin_opens_at"] == "2026-07-24T20:30:00+05:00"
        assert option["checkin_closes_at"] == "2026-07-24T22:00:00+05:00"

    def test_kiosk_options_blocks_expected_roster_for_closed_group_session(self, club):
        target_date = date.today()
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=target_date,
        )
        GroupSessionFactory(
            club=club,
            schedule=schedule,
            date=target_date,
            closed_at=timezone.now(),
        )

        response = client.post(
            "/checkins/kiosk/options/",
            json={
                "student_id": student.id,
                "date": target_date.isoformat(),
            },
            **_kiosk_auth_params(club),
        )

        assert response.status_code == 200
        option = response.json()["options"][0]
        assert option["schedule_id"] == schedule.id
        assert option["self_checkin_status"] == "blocked"
        assert option["reason_code"] == "group_session_closed"

    def test_kiosk_options_blocks_guest_booking_without_subscription_or_dropin_policy(self, club):
        target_date = date.today()
        training_type = TrainingTypeFactory(
            club=club,
            kind=TrainingType.Kind.GROUP,
            drop_in_price=None,
            trial_free=False,
        )
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)

        response = client.post(
            "/checkins/kiosk/options/",
            json={
                "student_id": student.id,
                "date": target_date.isoformat(),
            },
            **_kiosk_auth_params(club),
        )

        assert response.status_code == 200
        option = response.json()["options"][0]
        assert option["schedule_id"] == schedule.id
        assert option["self_checkin_status"] == "blocked"
        assert option["financial_status"] == "blocked"
        assert option["reason_code"] == "drop_in_price_required"

    def test_kiosk_options_blocks_guest_booking_for_churned_student(self, club):
        target_date = date.today()
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        student = StudentFactory(club=club, status=Student.Status.CHURNED)
        tariff = TariffFactory(training_type=training_type)
        SubscriptionFactory(club=club, student=student, tariff=tariff)

        response = client.post(
            "/checkins/kiosk/options/",
            json={
                "student_id": student.id,
                "date": target_date.isoformat(),
            },
            **_kiosk_auth_params(club),
        )

        assert response.status_code == 200
        option = response.json()["options"][0]
        assert option["schedule_id"] == schedule.id
        assert option["self_checkin_status"] == "blocked"
        assert option["financial_status"] == "subscription"
        assert option["reason_code"] == "student_ineligible"

    def test_kiosk_options_marks_existing_checkin_as_already_checked_in(self, club):
        target_date = date.today()
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            date=target_date,
        )

        response = client.post(
            "/checkins/kiosk/options/",
            json={
                "student_id": student.id,
                "date": target_date.isoformat(),
            },
            **_kiosk_auth_params(club),
        )

        assert response.status_code == 200
        option = response.json()["options"][0]
        assert option["self_checkin_status"] == "blocked"
        assert option["reason_code"] == "already_checked_in"
        assert option["existing_checkin_id"] is not None

    def test_kiosk_options_rejects_ineligible_student(self, club):
        target_date = date.today()
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        ScheduleFactory(
            club=club,
            day_of_week=target_date.weekday(),
            training_type=training_type,
        )
        lead = StudentFactory(club=club, status=Student.Status.LEAD)

        response = client.post(
            "/checkins/kiosk/options/",
            json={
                "student_id": lead.id,
                "date": target_date.isoformat(),
            },
            **_kiosk_auth_params(club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "student_ineligible"


@pytest.mark.django_db
class TestBatchCheckinAPI:
    @patch("apps.attendance.services.async_task")
    def test_batch_checkin_rejects_before_effective_end_without_side_effects(
        self,
        mock_async,
        club,
        owner_user,
    ):
        checkin_date = date.today()
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=checkin_date.weekday(),
            start_time=time(18, 0),
            end_time=time(19, 0),
        )
        tariff = TariffFactory(
            club=club,
            training_type=schedule.training_type,
            trainings_limit=5,
        )
        subscription = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            trainings_left=5,
            trainings_used=0,
        )
        ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=checkin_date,
        )
        current_time = datetime.combine(
            checkin_date,
            time(18, 30),
            tzinfo=ZoneInfo("Europe/Moscow"),
        )

        with patch("apps.attendance.selectors.timezone.now", return_value=current_time):
            response = client.post(
                "/checkins/batch/",
                json={
                    "schedule_id": schedule.id,
                    "date": checkin_date.isoformat(),
                    "present_student_ids": [student.id],
                    "training_type_id": schedule.training_type_id,
                    "topic_tags": [],
                    "notes": "Too early",
                },
                **_auth_params(owner_user, club),
            )

        assert response.status_code == 400
        assert response.json()["code"] == "session_close_not_allowed_yet"
        assert not Checkin.objects.for_club(club).filter(schedule=schedule, date=checkin_date).exists()
        assert not Debt.objects.for_club(club).filter(student=student).exists()
        assert not GroupSession.objects.for_club(club).filter(schedule=schedule, date=checkin_date).exists()
        assert not CheckinCascadeEvent.objects.for_club(club).exists()
        subscription.refresh_from_db()
        assert subscription.trainings_left == 5
        assert subscription.trainings_used == 0
        mock_async.assert_not_called()

    @patch("apps.attendance.services.async_task")
    def test_batch_checkin_empty_list_rejects_before_effective_end(
        self,
        mock_async,
        club,
        owner_user,
    ):
        checkin_date = date.today()
        schedule = ScheduleFactory(
            club=club,
            day_of_week=checkin_date.weekday(),
            start_time=time(18, 0),
            end_time=time(19, 0),
        )
        current_time = datetime.combine(
            checkin_date,
            time(18, 30),
            tzinfo=ZoneInfo("Europe/Moscow"),
        )

        with patch("apps.attendance.selectors.timezone.now", return_value=current_time):
            response = client.post(
                "/checkins/batch/",
                json={
                    "schedule_id": schedule.id,
                    "date": checkin_date.isoformat(),
                    "present_student_ids": [],
                    "training_type_id": schedule.training_type_id,
                    "topic_tags": [],
                    "notes": "Too early",
                },
                **_auth_params(owner_user, club),
            )

        assert response.status_code == 400
        assert response.json()["code"] == "session_close_not_allowed_yet"
        assert not GroupSession.objects.for_club(club).filter(schedule=schedule, date=checkin_date).exists()
        mock_async.assert_not_called()

    @patch("apps.attendance.services.async_task")
    def test_batch_checkin_uses_rescheduled_effective_end_time(
        self,
        mock_async,
        club,
        owner_user,
    ):
        source_date = _future_date_obj(7)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=source_date.weekday(),
            start_time=time(10, 0),
            end_time=time(11, 0),
        )
        effective_date = source_date + timedelta(days=1)
        ScheduleExceptionFactory(
            club=club,
            schedule=schedule,
            date=source_date,
            exception_type=ScheduleException.ExceptionType.RESCHEDULED,
            new_date=effective_date,
            new_start_time=time(20, 0),
            new_end_time=time(21, 0),
        )
        current_time = datetime.combine(
            effective_date,
            time(20, 30),
            tzinfo=ZoneInfo("Europe/Moscow"),
        )

        with patch("apps.attendance.selectors.timezone.now", return_value=current_time):
            response = client.post(
                "/checkins/batch/",
                json={
                    "schedule_id": schedule.id,
                    "date": effective_date.isoformat(),
                    "present_student_ids": [],
                    "training_type_id": schedule.training_type_id,
                    "topic_tags": [],
                    "notes": "Original time already passed",
                },
                **_auth_params(owner_user, club),
            )

        assert response.status_code == 400
        assert response.json()["code"] == "session_close_not_allowed_yet"
        assert not GroupSession.objects.for_club(club).filter(schedule=schedule, date=effective_date).exists()
        mock_async.assert_not_called()

    @patch("apps.attendance.services.async_task")
    def test_batch_checkin_allows_rescheduled_occurrence_after_effective_end(
        self,
        mock_async,
        club,
        owner_user,
    ):
        source_date = _future_date_obj(7)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=source_date.weekday(),
            start_time=time(10, 0),
            end_time=time(11, 0),
        )
        effective_date = source_date + timedelta(days=1)
        ScheduleExceptionFactory(
            club=club,
            schedule=schedule,
            date=source_date,
            exception_type=ScheduleException.ExceptionType.RESCHEDULED,
            new_date=effective_date,
            new_start_time=time(20, 0),
            new_end_time=time(21, 0),
        )
        current_time = datetime.combine(
            effective_date,
            time(21, 30),
            tzinfo=ZoneInfo("Europe/Moscow"),
        )

        with patch("apps.attendance.selectors.timezone.now", return_value=current_time):
            response = client.post(
                "/checkins/batch/",
                json={
                    "schedule_id": schedule.id,
                    "date": effective_date.isoformat(),
                    "present_student_ids": [],
                    "training_type_id": schedule.training_type_id,
                    "topic_tags": ["rescheduled"],
                    "notes": "Effective occurrence finished",
                },
                **_auth_params(owner_user, club),
            )

        assert response.status_code == 200
        session = GroupSession.objects.for_club(club).get(
            schedule=schedule,
            date=effective_date,
        )
        assert session.closed_by_id == owner_user.id
        assert session.close_source == GroupSession.CloseSource.BATCH
        assert session.topic_tags == ["rescheduled"]
        assert session.notes == "Effective occurrence finished"
        mock_async.assert_not_called()

    @patch("apps.attendance.api.batch_checkin")
    def test_batch_checkin_passes_authenticated_actor(self, mock_batch_checkin, club, owner_user):
        checkin_date = date.today()
        schedule = ScheduleFactory(club=club, day_of_week=checkin_date.weekday())
        mock_batch_checkin.return_value = {"checkins": [], "group_session_id": 1}

        response = client.post(
            "/checkins/batch/",
            json={
                "schedule_id": schedule.id,
                "date": checkin_date.isoformat(),
                "present_student_ids": [],
                "training_type_id": schedule.training_type_id,
                "topic_tags": [],
                "notes": "",
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        assert mock_batch_checkin.call_args.kwargs["actor_user_id"] == owner_user.id

    def test_batch_checkin_forbids_trainer_role(self, club, trainer_user):
        checkin_date = date.today()
        schedule = ScheduleFactory(club=club, day_of_week=checkin_date.weekday())

        response = client.post(
            "/checkins/batch/",
            json={
                "schedule_id": schedule.id,
                "date": checkin_date.isoformat(),
                "present_student_ids": [],
                "training_type_id": schedule.training_type_id,
                "topic_tags": [],
                "notes": "",
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403

    @patch("apps.attendance.services.async_task")
    def test_batch_checkin_api(self, mock_async, club, owner_user):
        checkin_date = date.today() - timedelta(days=7)
        student1 = StudentFactory(club=club, status="active")
        student2 = StudentFactory(club=club, status="active")
        schedule = ScheduleFactory(club=club, day_of_week=checkin_date.weekday())
        training_type = schedule.training_type
        for student in (student1, student2):
            ScheduleEnrollment.objects.create(
                club=club,
                student=student,
                schedule=schedule,
                status=ScheduleEnrollment.Status.ACTIVE,
                starts_on=checkin_date,
            )
        response = client.post(
            "/checkins/batch/",
            json={
                "schedule_id": schedule.id,
                "date": str(checkin_date),
                "present_student_ids": [student1.id, student2.id],
                "training_type_id": training_type.id,
                "topic_tags": ["sparring"],
                "notes": "Good session",
            },
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["checkins"]) == 2
        assert "group_session_id" in data
        # Batch: alerts ARE returned
        assert {item["student_id"] for item in data["checkins"]} == {student1.id, student2.id}
        for checkin_data in data["checkins"]:
            assert "alerts" in checkin_data

    @patch("apps.attendance.services.async_task")
    def test_batch_checkin_duplicate_keeps_service_contract(self, mock_async, club, owner_user):
        checkin_date = date.today() - timedelta(days=7)
        student = StudentFactory(club=club, status="active")
        schedule = ScheduleFactory(club=club, day_of_week=checkin_date.weekday())
        training_type = schedule.training_type
        ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=checkin_date,
        )
        payload = {
            "schedule_id": schedule.id,
            "date": str(checkin_date),
            "present_student_ids": [student.id],
            "training_type_id": training_type.id,
            "topic_tags": [],
            "notes": "",
        }

        first_response = client.post(
            "/checkins/batch/",
            json=payload,
            **_auth_params(owner_user, club),
        )
        second_response = client.post(
            "/checkins/batch/",
            json=payload,
            **_auth_params(owner_user, club),
        )

        assert first_response.status_code == 200
        assert first_response.json()["checkins"][0]["created"] is True
        assert first_response.json()["checkins"][0]["duplicate"] is False
        assert second_response.status_code == 200
        duplicate = second_response.json()["checkins"][0]
        assert duplicate["created"] is False
        assert duplicate["duplicate"] is True
        assert duplicate["subscription_effect"] == "none"
        assert duplicate["debt_effect"] == "none"

    @patch("apps.attendance.services.async_task")
    def test_batch_checkin_empty_list_rejects_non_occurring_schedule(self, mock_async, club, owner_user):
        checkin_date = date.today() - timedelta(days=7)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=(checkin_date.weekday() + 1) % 7,
        )

        response = client.post(
            "/checkins/batch/",
            json={
                "schedule_id": schedule.id,
                "date": str(checkin_date),
                "present_student_ids": [],
                "training_type_id": schedule.training_type_id,
                "topic_tags": [],
                "notes": "",
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "schedule_occurrence_not_found"
        assert not GroupSession.objects.for_club(club).filter(
            schedule=schedule,
            date=checkin_date,
        ).exists()
        mock_async.assert_not_called()

    @patch("apps.attendance.services.async_task")
    def test_batch_checkin_rejects_training_type_mismatch(self, mock_async, club, owner_user):
        checkin_date = date.today() - timedelta(days=7)
        student = StudentFactory(club=club, status="active")
        schedule_type = TrainingTypeFactory(club=club)
        wrong_type = TrainingTypeFactory(club=club)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=checkin_date.weekday(),
            training_type=schedule_type,
        )
        ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=checkin_date,
        )

        response = client.post(
            "/checkins/batch/",
            json={
                "schedule_id": schedule.id,
                "date": str(checkin_date),
                "present_student_ids": [student.id],
                "training_type_id": wrong_type.id,
                "topic_tags": [],
                "notes": "",
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "training_type_mismatch"
        assert not Checkin.objects.filter(student=student, schedule=schedule).exists()

    @patch("apps.attendance.services.async_task")
    def test_batch_checkin_rejects_ineligible_student(self, mock_async, club, owner_user):
        checkin_date = date.today() - timedelta(days=7)
        student = StudentFactory(club=club, status="lead")
        schedule = ScheduleFactory(club=club, day_of_week=checkin_date.weekday())
        training_type = schedule.training_type
        ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=checkin_date,
        )

        response = client.post(
            "/checkins/batch/",
            json={
                "schedule_id": schedule.id,
                "date": str(checkin_date),
                "present_student_ids": [student.id],
                "training_type_id": training_type.id,
                "topic_tags": [],
                "notes": "",
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "student_ineligible"
        assert not Checkin.objects.filter(student=student, schedule=schedule).exists()

    @patch("apps.attendance.services.async_task")
    def test_batch_checkin_rejects_unassigned_student_without_partial_checkins(
        self, mock_async, club, owner_user
    ):
        checkin_date = date.today() - timedelta(days=7)
        enrolled = StudentFactory(club=club, status="active")
        unassigned = StudentFactory(club=club, status="active")
        schedule = ScheduleFactory(club=club, day_of_week=checkin_date.weekday())
        ScheduleEnrollment.objects.create(
            club=club,
            student=enrolled,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=checkin_date,
        )

        response = client.post(
            "/checkins/batch/",
            json={
                "schedule_id": schedule.id,
                "date": str(checkin_date),
                "present_student_ids": [enrolled.id, unassigned.id],
                "training_type_id": schedule.training_type_id,
                "topic_tags": [],
                "notes": "",
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "student_schedule_ineligible"
        assert not Checkin.objects.filter(schedule=schedule).exists()
        assert not Debt.objects.filter(student__in=[enrolled, unassigned]).exists()
        assert not CheckinCascadeEvent.objects.filter(club=club).exists()
        mock_async.assert_not_called()

    @patch("apps.attendance.services.async_task")
    def test_batch_checkin_rejects_frozen_student(self, mock_async, club, owner_user):
        checkin_date = date.today() - timedelta(days=7)
        student = StudentFactory(club=club, status="active")
        schedule = ScheduleFactory(club=club, day_of_week=checkin_date.weekday())
        ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.FROZEN,
            starts_on=checkin_date,
        )

        response = client.post(
            "/checkins/batch/",
            json={
                "schedule_id": schedule.id,
                "date": str(checkin_date),
                "present_student_ids": [student.id],
                "training_type_id": schedule.training_type_id,
                "topic_tags": [],
                "notes": "",
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "enrollment_frozen"
        assert not Checkin.objects.filter(student=student, schedule=schedule).exists()
        assert not Debt.objects.filter(student=student).exists()
        assert not CheckinCascadeEvent.objects.filter(club=club).exists()
        mock_async.assert_not_called()

    @patch("apps.attendance.api.batch_checkin")
    def test_batch_checkin_rejects_cross_tenant_enrichment_result(
        self, mock_batch_checkin, club, other_club, owner_user
    ):
        other_checkin = CheckinFactory(club=other_club)
        schedule = ScheduleFactory(club=club)
        training_type = schedule.training_type
        mock_batch_checkin.return_value = {
            "checkins": [
                {
                    "checkin_id": other_checkin.id,
                    "is_debt": False,
                    "subscription_id": None,
                }
            ],
            "group_session_id": 1,
        }

        response = client.post(
            "/checkins/batch/",
            json={
                "schedule_id": schedule.id,
                "date": str(date.today()),
                "present_student_ids": [],
                "training_type_id": training_type.id,
                "topic_tags": [],
                "notes": "",
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "batch_checkin_tenant_mismatch"


@pytest.mark.django_db
class TestCancelCheckinAPI:
    @patch("apps.attendance.services.async_task")
    def test_cancel_checkin_api(self, mock_async, club, owner_user):
        student = StudentFactory(club=club, status="active")
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
        )
        response = client.post(
            f"/checkins/{checkin.id}/cancel/",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        assert response.json()["success"] is True

    @patch("apps.attendance.services.async_task")
    def test_checkin_tenant_isolation_api(self, mock_async, club, other_club, owner_user):
        """Cancelling a checkin from another club returns 404."""
        student = StudentFactory(club=other_club, status="active")
        schedule = ScheduleFactory(club=other_club)
        training_type = TrainingTypeFactory(club=other_club)
        checkin = CheckinFactory(
            club=other_club,
            student=student,
            schedule=schedule,
            training_type=training_type,
        )
        response = client.post(
            f"/checkins/{checkin.id}/cancel/",
            **_auth_params(owner_user, club),
        )
        # Should fail -- checkin doesn't belong to this club
        assert response.status_code == 404


@pytest.mark.django_db
class TestOfflineSyncAPI:
    @patch("apps.attendance.services.async_task")
    def test_offline_sync_api(self, mock_async, club):
        student1 = StudentFactory(club=club, status="active")
        student2 = StudentFactory(club=club, status="active")
        training_type = TrainingTypeFactory(club=club)
        schedule = ScheduleFactory(club=club, day_of_week=date.today().weekday(), training_type=training_type)
        for student in (student1, student2):
            ScheduleEnrollment.objects.create(
                club=club,
                student=student,
                schedule=schedule,
                status=ScheduleEnrollment.Status.ACTIVE,
                starts_on=date.today(),
            )
        response = client.post(
            "/checkins/sync/",
            json={
                "checkins": [
                    {
                        "student_id": student1.id,
                        "schedule_id": schedule.id,
                        "training_type_id": training_type.id,
                        "checkin_date": str(date.today()),
                        "client_id": "api-offline-1",
                    },
                    {
                        "student_id": student2.id,
                        "schedule_id": schedule.id,
                        "training_type_id": training_type.id,
                        "checkin_date": str(date.today()),
                        "client_id": "api-offline-2",
                    },
                ]
            },
            **_kiosk_auth_params(club),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["synced"] == 2
        assert data["failed"] == 0
        assert len(data["results"]) == 2
        assert all(result["retryable"] is False for result in data["results"])

    @pytest.mark.parametrize(
        "code",
        [
            "subscription_component_limit_exceeded",
            "subscription_component_credits_exhausted",
            "payroll_period_closed",
        ],
    )
    def test_offline_sync_marks_deterministic_business_rejections_terminal(
        self,
        code,
        club,
    ):
        item = {
            "student_id": 101,
            "schedule_id": 202,
            "training_type_id": 303,
            "checkin_date": str(date.today()),
            "client_id": f"terminal-{code}",
            "idempotency_key": f"terminal-{code}",
        }

        with patch(
            "apps.attendance.api.create_checkin",
            side_effect=BusinessLogicError("deterministic rejection", code=code),
        ):
            response = client.post(
                "/checkins/sync/",
                json={"checkins": [item]},
                **_kiosk_auth_params(club),
            )

        assert response.status_code == 200
        result = response.json()["results"][0]
        assert result == {
            "client_id": item["client_id"],
            "idempotency_key": item["idempotency_key"],
            "student_id": item["student_id"],
            "success": False,
            "checkin_id": None,
            "duplicate": False,
            "error": code,
            "retryable": False,
        }


@pytest.mark.django_db
class TestTodayCheckinsAPI:
    @patch("apps.attendance.services.async_task")
    def test_today_checkins_api(self, mock_async, club, owner_user):
        student = StudentFactory(club=club, status="active")
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            date=date.today(),
        )
        response = client.get(
            "/checkins/today/",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["student_id"] == student.id

    def test_trainer_cannot_list_all_today_checkins(self, club, trainer_user):
        response = client.get(
            "/checkins/today/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403


@pytest.mark.django_db
class TestScheduleByDateEndpoint:
    def test_by_date_returns_schedules(self, club, owner_user):
        today = date.today()
        response = client.get(
            f"/schedules/by-date/?date={today.isoformat()}",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        assert isinstance(response.json(), list)


@pytest.mark.django_db
class TestScheduleStudentsEndpoint:
    def test_schedule_students_returns_typed_list(self, club, owner_user):
        schedule = ScheduleFactory(club=club)
        student = StudentFactory(club=club, status="active")
        training_type = TrainingTypeFactory(club=club)
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            date=date.today(),
        )
        response = client.get(
            f"/schedules/{schedule.id}/students/",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)


@pytest.mark.django_db
class TestCancelCheckinTrainerDenied:
    """D-13: Trainer CANNOT cancel check-ins. Only owner/admin."""

    @patch("apps.attendance.services.async_task")
    def test_trainer_cannot_cancel_checkin(self, mock_async, club, trainer_user):
        """Trainer role should get 403 on cancel_checkin_endpoint."""
        student = StudentFactory(club=club, status="active")
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
        )
        response = client.post(
            f"/checkins/{checkin.id}/cancel/",
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert response.status_code == 403  # Should be forbidden for trainer
