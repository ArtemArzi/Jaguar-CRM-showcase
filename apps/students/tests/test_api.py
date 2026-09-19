import io
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from ninja.testing import TestClient
from openpyxl import Workbook

from apps.attendance.models import (
    PersonalAvailabilitySlot,
    PersonalBookingPaymentReservation,
    PersonalDropInBooking,
    PersonalDropInPaymentLink,
    PersonalServiceTermsSnapshot,
    Schedule,
    ScheduleEnrollment,
)
from apps.attendance.selectors import (
    can_reschedule_student_personal_booking,
    get_student_upcoming_personal_bookings,
)
from apps.attendance.tests.factories import (
    CheckinFactory,
    GroupSessionFactory,
    ScheduleFactory,
    TrainingGroupFactory,
    TrainingGroupMembershipFactory,
)
from apps.billing.models import BankPaymentOrder, Payment, Subscription, TrainingType
from apps.billing.tests.factories import PaymentFactory, SubscriptionFactory, TariffFactory, TrainingTypeFactory
from apps.clubs.models import ClubMembership, ClubSettings
from apps.clubs.tests.factories import UserFactory
from apps.common.tests.helpers import make_auth_params as _auth_params
from apps.students.models import AccountAccess, Student, StudentNote
from apps.students.schemas import StudentDetailOut
from apps.students.tests.factories import StudentFactory
from apps.trainers.models import TrainerPackageAllocation
from apps.trainers.tests.factories import TrainerFactory, TrainerLocationFactory
from config.api import api

client = TestClient(api)


def _future_date_obj(days: int):
    return timezone.localdate() + timedelta(days=days)


def _future_datetime(days: int, *, hour: int, minute: int = 0) -> str:
    return f"{_future_date_obj(days).isoformat()}T{hour:02d}:{minute:02d}:00"


NON_CURRENT_PACKAGE_CASES = [
    ("expired", Subscription.Status.EXPIRED, 30, 8, False),
    ("pending", Subscription.Status.PENDING, 30, 8, False),
    ("frozen", Subscription.Status.FROZEN, 30, 8, False),
    ("past_expiry", Subscription.Status.ACTIVE, -1, 8, False),
    ("depleted", Subscription.Status.ACTIVE, 30, 0, False),
    ("soft_deleted", Subscription.Status.ACTIVE, 30, 8, True),
]


def _make_excel_bytes(rows: list[list]) -> bytes:
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _paid_active_subscription(*, club, student):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    tariff = TariffFactory(training_type=training_type, price=Decimal("5000"))
    return SubscriptionFactory(
        tariff=tariff,
        student=student,
        status=Subscription.Status.ACTIVE,
        paid_amount=Decimal("5000"),
    )


def _pending_manual_operational_admission(*, club, student, recorded_by):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    tariff = TariffFactory(club=club, training_type=training_type, price=Decimal("5000"))
    start_date = timezone.localdate()
    schedule = ScheduleFactory(
        club=club,
        training_type=training_type,
        day_of_week=start_date.weekday(),
    )
    enrollment = ScheduleEnrollment.objects.create(
        club=club,
        student=student,
        schedule=schedule,
        status=ScheduleEnrollment.Status.ACTIVE,
        starts_on=start_date,
        created_from=ScheduleEnrollment.CreatedFrom.PAID_CONVERSION,
    )
    subscription = SubscriptionFactory(
        club=club,
        student=student,
        tariff=tariff,
        status=Subscription.Status.PENDING,
        paid_amount=None,
        expires_at=None,
    )
    return PaymentFactory(
        club=club,
        student=student,
        tariff=tariff,
        subscription=subscription,
        payment_method=Payment.Method.CASH,
        status=Payment.Status.PENDING,
        recorded_by=recorded_by,
        target_schedule=schedule,
        target_start_date=start_date,
        target_group_name_snapshot=schedule.group_name,
        target_location_id_snapshot=schedule.location_id,
        target_location_name_snapshot=schedule.location.name,
        target_training_type_id_snapshot=training_type.id,
        target_training_type_kind_snapshot=TrainingType.Kind.GROUP,
        conversion_enrollment=enrollment,
    )


def _create_active_package_allocation(
    *,
    club,
    student,
    owner_trainer,
    status=Subscription.Status.ACTIVE,
    expires_at=None,
    trainings_left=None,
    deleted=False,
):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    tariff = TariffFactory(training_type=training_type, price=Decimal("5000"))
    subscription = SubscriptionFactory(
        tariff=tariff,
        student=student,
        status=status,
        expires_at=expires_at,
        trainings_left=tariff.trainings_limit if trainings_left is None else trainings_left,
        paid_amount=Decimal("5000"),
    )
    if deleted:
        subscription.soft_delete()
    TrainerPackageAllocation.objects.create(
        club=club,
        subscription=subscription,
        student=student,
        tariff=tariff,
        training_type=training_type,
        owner_trainer=owner_trainer,
        amount_snapshot=tariff.price,
        sessions_total_snapshot=tariff.trainings_limit,
        sessions_remaining_snapshot=subscription.trainings_left,
        is_active=True,
    )
    return subscription


@pytest.fixture(autouse=True)
def _bypass_jwt_auth(bypass_jwt_auth):
    pass


@pytest.mark.django_db
class TestListStudents:
    def test_list_students_owner(self, club, owner_user):
        StudentFactory(club=club)
        response = client.get("/students/", **_auth_params(owner_user, club))
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1

    def test_list_students_tenant_isolation(self, club, other_club, owner_user):
        StudentFactory(club=club)
        StudentFactory(club=other_club)
        response = client.get("/students/", **_auth_params(owner_user, club))
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1

    @pytest.mark.parametrize(
        "query",
        [
            "8912",
            "912 345",
            "+7 (912) 345-67",
            "34567",
        ],
    )
    def test_list_students_searches_normalized_phone_fragments(self, club, owner_user, query):
        student = StudentFactory(club=club, phone="+79123456789")
        StudentFactory(club=club, phone="+79990001122")

        response = client.get(f"/students/?q={query}", **_auth_params(owner_user, club))

        assert response.status_code == 200
        assert [item["id"] for item in response.json()["items"]] == [student.id]

    def test_list_students_searches_guardian_phone_without_tenant_leak(
        self,
        club,
        other_club,
        owner_user,
    ):
        student = StudentFactory(
            club=club,
            phone="",
            guardian_phone="+79876543210",
            is_child=True,
        )
        StudentFactory(
            club=other_club,
            phone="",
            guardian_phone="+79876543210",
            is_child=True,
        )

        response = client.get("/students/?q=76543", **_auth_params(owner_user, club))

        assert response.status_code == 200
        assert [item["id"] for item in response.json()["items"]] == [student.id]

    def test_trainer_phone_search_preserves_operational_scope(self, club, trainer_user):
        trainer = TrainerFactory(club=club, user=trainer_user)
        assigned = StudentFactory(
            club=club,
            assigned_trainer=trainer,
            phone="+79123450001",
        )
        StudentFactory(club=club, phone="+79123450002")

        response = client.get(
            "/students/?q=912345",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        assert [item["id"] for item in response.json()["items"]] == [assigned.id]

    def test_trainer_list_students_is_limited_to_operational_scope(self, club, trainer_user):
        trainer = TrainerFactory(club=club, user=trainer_user)
        assigned = StudentFactory(club=club, assigned_trainer=trainer)
        unassigned = StudentFactory(club=club)
        active_package_student = StudentFactory(club=club)
        inactive_package_student = StudentFactory(club=club)
        checked_in_student = StudentFactory(club=club)
        cancelled_checkin_student = StudentFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        tariff = TariffFactory(training_type=training_type, price=Decimal("5000"))
        active_subscription = SubscriptionFactory(tariff=tariff, student=active_package_student)
        inactive_subscription = SubscriptionFactory(tariff=tariff, student=inactive_package_student)
        schedule = ScheduleFactory(club=club, trainer=trainer, training_type=training_type)
        TrainerPackageAllocation.objects.create(
            club=club,
            subscription=active_subscription,
            student=active_package_student,
            tariff=tariff,
            training_type=training_type,
            owner_trainer=trainer,
            amount_snapshot=tariff.price,
            sessions_total_snapshot=tariff.trainings_limit,
            sessions_remaining_snapshot=active_subscription.trainings_left,
            is_active=True,
        )
        TrainerPackageAllocation.objects.create(
            club=club,
            subscription=inactive_subscription,
            student=inactive_package_student,
            tariff=tariff,
            training_type=training_type,
            owner_trainer=trainer,
            amount_snapshot=tariff.price,
            sessions_total_snapshot=tariff.trainings_limit,
            sessions_remaining_snapshot=inactive_subscription.trainings_left,
            is_active=False,
        )
        CheckinFactory(
            club=club,
            student=checked_in_student,
            schedule=schedule,
            trainer=trainer,
            training_type=training_type,
        )
        CheckinFactory(
            club=club,
            student=cancelled_checkin_student,
            schedule=schedule,
            trainer=trainer,
            training_type=training_type,
            cancelled_at=timezone.now(),
        )

        response = client.get("/students/", **_auth_params(trainer_user, club, role="trainer"))

        assert response.status_code == 200
        student_ids = {item["id"] for item in response.json()["items"]}
        assert assigned.id in student_ids
        assert active_package_student.id in student_ids
        assert checked_in_student.id in student_ids
        assert unassigned.id not in student_ids
        assert inactive_package_student.id not in student_ids
        assert cancelled_checkin_student.id not in student_ids

    def test_trainer_list_includes_own_pending_manual_operational_admission(self, club, trainer_user):
        TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(club=club, status=Student.Status.ACTIVE, assigned_trainer=None)
        _pending_manual_operational_admission(
            club=club,
            student=student,
            recorded_by=trainer_user,
        )

        response = client.get("/students/", **_auth_params(trainer_user, club, role="trainer"))

        assert response.status_code == 200
        assert student.id in {item["id"] for item in response.json()["items"]}

    def test_inactive_trainer_profile_cannot_list_students(self, club, trainer_user):
        trainer = TrainerFactory(club=club, user=trainer_user, is_active=False)
        StudentFactory(club=club, assigned_trainer=trainer)

        response = client.get("/students/", **_auth_params(trainer_user, club, role="trainer"))

        assert response.status_code == 403

    def test_open_permanent_group_enrollment_grants_read_only_trainer_scope(
        self,
        club,
        trainer_user,
    ):
        trainer = TrainerFactory(club=club, user=trainer_user)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        today = timezone.localdate()
        active_schedule = ScheduleFactory(
            club=club,
            trainer=trainer,
            training_type=training_type,
            day_of_week=today.weekday(),
        )
        inactive_schedule = ScheduleFactory(
            club=club,
            trainer=trainer,
            training_type=training_type,
            is_active=False,
        )
        one_time_schedule = ScheduleFactory(
            club=club,
            trainer=trainer,
            training_type=training_type,
            one_time_date=today + timedelta(days=7),
        )
        canonical_group = TrainingGroupFactory(
            club=club,
            training_type=training_type,
            responsible_trainer=trainer,
        )
        projected_schedule = ScheduleFactory(
            club=club,
            training_group=canonical_group,
            trainer=trainer,
            location=canonical_group.location,
            training_type=training_type,
            day_of_week=today.weekday(),
        )
        future_member = StudentFactory(club=club, status=Student.Status.ACTIVE)
        projected_member = StudentFactory(club=club, status=Student.Status.ACTIVE)
        ended_member = StudentFactory(club=club, status=Student.Status.ACTIVE)
        trial_guest = StudentFactory(club=club)
        inactive_schedule_member = StudentFactory(club=club, status=Student.Status.ACTIVE)
        one_time_member = StudentFactory(club=club, status=Student.Status.ACTIVE)
        ScheduleEnrollment.objects.create(
            club=club,
            student=future_member,
            schedule=active_schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=today + timedelta(days=7),
            ends_on=None,
            created_from=ScheduleEnrollment.CreatedFrom.PAID_CONVERSION,
        )
        ScheduleEnrollment.objects.create(
            club=club,
            student=ended_member,
            schedule=active_schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=today - timedelta(days=30),
            ends_on=today - timedelta(days=1),
            created_from=ScheduleEnrollment.CreatedFrom.MANUAL,
        )
        ScheduleEnrollment.objects.create(
            club=club,
            student=trial_guest,
            schedule=active_schedule,
            status=ScheduleEnrollment.Status.TRIAL,
            starts_on=today,
            ends_on=today,
            created_from=ScheduleEnrollment.CreatedFrom.LEAD_BOOKING,
        )
        ScheduleEnrollment.objects.create(
            club=club,
            student=inactive_schedule_member,
            schedule=inactive_schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=today,
            ends_on=None,
            created_from=ScheduleEnrollment.CreatedFrom.PAID_CONVERSION,
        )
        ScheduleEnrollment.objects.create(
            club=club,
            student=one_time_member,
            schedule=one_time_schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=one_time_schedule.one_time_date,
            ends_on=None,
            created_from=ScheduleEnrollment.CreatedFrom.PAID_CONVERSION,
        )
        projected_membership = TrainingGroupMembershipFactory(
            club=club,
            student=projected_member,
            training_group=canonical_group,
            starts_on=today,
        )
        ScheduleEnrollment.objects.create(
            club=club,
            student=projected_member,
            schedule=projected_schedule,
            training_group_membership=projected_membership,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=today,
            created_from=ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION,
        )

        listing = client.get(
            "/students/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert listing.status_code == 200
        visible_ids = {item["id"] for item in listing.json()["items"]}
        assert future_member.id in visible_ids
        assert projected_member.id in visible_ids
        assert ended_member.id not in visible_ids
        assert trial_guest.id not in visible_ids
        assert inactive_schedule_member.id not in visible_ids
        assert one_time_member.id not in visible_ids

        detail = client.get(
            f"/students/{future_member.id}/",
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert detail.status_code == 200
        assert detail.json()["can_manage_sensitive_actions"] is False
        assert detail.json()["can_manage_account_access"] is False


@pytest.mark.django_db
class TestCreateStudent:
    def test_create_student_endpoint(self, club, owner_user):
        response = client.post(
            "/students/",
            json={
                "first_name": "Ivan",
                "last_name": "Petrov",
                "phone": "+79001234567",
            },
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 201
        data = response.json()
        assert data["first_name"] == "Ivan"
        assert data["status"] == "lead"
        student = Student.objects.for_club(club).get(id=data["id"])
        assert student.lead_status == Student.LeadStatus.NEW

    def test_trainer_create_student_assigns_current_trainer_and_keeps_record_visible(self, club, trainer_user):
        trainer = TrainerFactory(club=club, user=trainer_user)

        response = client.post(
            "/students/",
            json={
                "first_name": "Trainer",
                "last_name": "Lead",
                "phone": "+79001234568",
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 201
        created_id = response.json()["id"]
        student = Student.objects.for_club(club).get(id=created_id)
        assert student.assigned_trainer_id == trainer.id
        assert student.status == Student.Status.LEAD
        assert student.lead_status == Student.LeadStatus.NEW

        detail = client.get(
            f"/students/{created_id}/",
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert detail.status_code == 200

        listing = client.get("/students/", **_auth_params(trainer_user, club, role="trainer"))
        assert listing.status_code == 200
        visible_ids = {item["id"] for item in listing.json()["items"]}
        assert created_id in visible_ids

        leads = client.get("/leads/?scope=mine", **_auth_params(trainer_user, club, role="trainer"))
        assert leads.status_code == 200
        lead_ids = {item["id"] for item in leads.json()["items"]}
        assert created_id in lead_ids

    def test_create_student_duplicate_returns_open_existing_action(self, club, owner_user):
        existing = StudentFactory(
            club=club,
            first_name="Existing",
            phone="+79001234570",
        )

        response = client.post(
            "/students/",
            json={
                "first_name": "Duplicate",
                "last_name": "Person",
                "phone": "+79001234570",
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 409
        data = response.json()
        assert data["code"] == "duplicate_phone"
        assert data["duplicate_scope"] == "club"
        assert data["can_open_existing"] is True
        assert data["existing_student"]["id"] == existing.id

    def test_create_child_student_keeps_guardian_phone(self, club, owner_user):
        response = client.post(
            "/students/",
            json={
                "first_name": "Child",
                "last_name": "Person",
                "phone": "+79001234571",
                "is_child": True,
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 201
        data = response.json()
        assert data["phone"] == ""
        assert data["guardian_phone"] == "+79001234571"

    def test_create_same_child_without_birth_date_keeps_legacy_duplicate_response(self, club, owner_user):
        payload = {
            "first_name": "Masha",
            "last_name": "Petrova",
            "phone": "",
            "guardian_phone": "+79001234572",
            "is_child": True,
        }
        first = client.post("/students/", json=payload, **_auth_params(owner_user, club))
        duplicate = client.post("/students/", json=payload, **_auth_params(owner_user, club))

        assert first.status_code == 201
        assert duplicate.status_code == 409
        assert duplicate.json()["code"] == "duplicate_phone"

    def test_create_same_named_child_siblings_with_distinct_birth_dates(self, club, owner_user):
        shared_identity = {
            "first_name": "Masha",
            "last_name": "Petrova",
            "phone": "",
            "guardian_phone": "+79001234573",
            "is_child": True,
        }
        first = client.post(
            "/students/",
            json={**shared_identity, "date_of_birth": "2017-05-06"},
            **_auth_params(owner_user, club),
        )
        sibling = client.post(
            "/students/",
            json={**shared_identity, "date_of_birth": "2018-05-06"},
            **_auth_params(owner_user, club),
        )

        assert first.status_code == sibling.status_code == 201
        assert Student.objects.for_club(club).filter(guardian_phone="+79001234573").count() == 2

    def test_inactive_trainer_profile_cannot_create_student(self, club, trainer_user):
        TrainerFactory(club=club, user=trainer_user, is_active=False)

        response = client.post(
            "/students/",
            json={
                "first_name": "Inactive",
                "last_name": "Trainer",
                "phone": "+79001234569",
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403
        assert not Student.objects.for_club(club).filter(phone="+79001234569").exists()

    def test_create_student_permission_denied(self, club, student_user):
        response = client.post(
            "/students/",
            json={
                "first_name": "Ivan",
                "last_name": "Petrov",
                "phone": "+79001234567",
            },
            **_auth_params(student_user, club, role="student"),
        )
        assert response.status_code == 403


@pytest.mark.django_db
class TestStudentDetail:
    def test_get_student_detail_endpoint(self, club, owner_user):
        student = StudentFactory(club=club)
        response = client.get(
            f"/students/{student.id}/",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == student.id
        assert "contraindications" in data
        assert "notes" in data
        assert data["can_manage_sensitive_actions"] is True

    def test_detail_schema_fails_closed_without_permission_projection(self, club):
        student = StudentFactory(club=club)

        assert StudentDetailOut.model_fields["can_manage_sensitive_actions"].default is False
        assert StudentDetailOut.resolve_can_manage_sensitive_actions(student) is False


@pytest.mark.django_db
class TestAccountAccessEndpoint:
    def test_owner_opens_adult_access_and_detail_does_not_expose_password(self, club, owner_user):
        student = StudentFactory(
            club=club,
            is_child=False,
            status=Student.Status.ACTIVE,
            phone="8 900 123 45 67",
        )
        _paid_active_subscription(club=club, student=student)

        response = client.post(
            f"/students/{student.id}/account-access/open/",
            json={},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 201
        data = response.json()
        assert data["username"] == "+79001234567"
        assert data["temporary_password"]
        student.refresh_from_db()
        assert student.user_id is not None
        assert AccountAccess.objects.for_club(club).filter(student=student).count() == 1

        detail = client.get(f"/students/{student.id}/", **_auth_params(owner_user, club))
        assert detail.status_code == 200
        detail_data = detail.json()
        assert detail_data["account_access"]["username"] == "+79001234567"
        assert detail_data["has_parent_user"] is False
        assert detail_data["can_manage_account_access"] is True
        assert detail_data["can_manage_feedback"] is True
        assert "temporary_password" not in detail.content.decode()

    def test_repeated_open_is_idempotent_without_rotating_password(self, club, owner_user):
        student = StudentFactory(
            club=club,
            is_child=False,
            status=Student.Status.ACTIVE,
            phone="8 900 123 45 67",
        )
        _paid_active_subscription(club=club, student=student)

        first = client.post(
            f"/students/{student.id}/account-access/open/",
            json={},
            **_auth_params(owner_user, club),
        )
        second = client.post(
            f"/students/{student.id}/account-access/open/",
            json={},
            **_auth_params(owner_user, club),
        )

        assert first.status_code == 201
        assert second.status_code == 200
        assert second.json()["temporary_password"] is None
        assert AccountAccess.objects.for_club(club).filter(student=student).count() == 1

    def test_reset_access_returns_new_one_time_password(self, club, owner_user):
        student = StudentFactory(
            club=club,
            is_child=False,
            status=Student.Status.ACTIVE,
            phone="8 900 123 45 67",
        )
        _paid_active_subscription(club=club, student=student)
        opened = client.post(
            f"/students/{student.id}/account-access/open/",
            json={},
            **_auth_params(owner_user, club),
        )

        reset = client.post(
            f"/students/{student.id}/account-access/reset/",
            **_auth_params(owner_user, club),
        )

        assert opened.status_code == 201
        assert reset.status_code == 200
        assert reset.json()["temporary_password"]
        assert reset.json()["temporary_password"] != opened.json()["temporary_password"]
        access = AccountAccess.objects.for_club(club).get(student=student)
        assert access.status == AccountAccess.Status.RESET
        assert access.reset_by_id == owner_user.id

    def test_child_access_uses_saved_guardian_phone_then_links_parent(self, club, owner_user):
        child = StudentFactory(
            club=club,
            is_child=True,
            status=Student.Status.ACTIVE,
            phone="",
            guardian_phone="+79001234567",
        )
        _paid_active_subscription(club=club, student=child)

        response = client.post(
            f"/students/{child.id}/account-access/open/",
            json={},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 201
        child.refresh_from_db()
        assert child.user_id is None
        assert child.parent_user_id is not None
        detail = client.get(f"/students/{child.id}/", **_auth_params(owner_user, club))
        assert detail.status_code == 200
        assert detail.json()["has_parent_user"] is True
        assert ClubMembership.objects.filter(
            user_id=child.parent_user_id,
            club=club,
            role=ClubMembership.Role.PARENT,
            is_active=True,
        ).count() == 1

    def test_trainer_can_open_only_scoped_student_access(self, club, trainer_user):
        trainer = TrainerFactory(club=club, user=trainer_user)
        assigned = StudentFactory(
            club=club,
            is_child=False,
            status=Student.Status.ACTIVE,
            phone="8 900 123 45 67",
            assigned_trainer=trainer,
        )
        unassigned = StudentFactory(
            club=club,
            is_child=False,
            status=Student.Status.ACTIVE,
            phone="8 900 765 43 21",
        )
        _paid_active_subscription(club=club, student=assigned)
        _paid_active_subscription(club=club, student=unassigned)

        allowed = client.post(
            f"/students/{assigned.id}/account-access/open/",
            json={},
            **_auth_params(trainer_user, club, role="trainer"),
        )
        denied = client.post(
            f"/students/{unassigned.id}/account-access/open/",
            json={},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert allowed.status_code == 201
        assert denied.status_code == 403
        assert not AccountAccess.objects.for_club(club).filter(student=unassigned).exists()

    def test_payment_recording_trainer_can_open_exact_pending_admission_access(self, club, trainer_user):
        trainer = TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(
            club=club,
            is_child=False,
            status=Student.Status.ACTIVE,
            phone="8 900 123 45 67",
        )
        payment = _pending_manual_operational_admission(
            club=club,
            student=student,
            recorded_by=trainer_user,
        )

        detail = client.get(
            f"/students/{student.id}/",
            **_auth_params(trainer_user, club, role="trainer"),
        )
        opened = client.post(
            f"/students/{student.id}/account-access/open/",
            json={},
            **_auth_params(trainer_user, club, role="trainer"),
        )
        forbidden_update = client.put(
            f"/students/{student.id}/",
            json={"first_name": "Scope leak"},
            **_auth_params(trainer_user, club, role="trainer"),
        )
        forbidden_notes = client.get(
            f"/students/{student.id}/notes/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert trainer.user_id == payment.recorded_by_id
        assert detail.status_code == 200
        detail_data = detail.json()
        assert detail_data["can_manage_account_access"] is True
        assert detail_data["account_access_eligible"] is True
        assert detail_data["operational_admission"]["payment_id"] == payment.id
        assert detail_data["operational_admission"]["payment_status"] == Payment.Status.PENDING
        assert opened.status_code == 201
        assert opened.json()["temporary_password"]
        assert forbidden_update.status_code == 403
        assert forbidden_notes.status_code == 403
        student.refresh_from_db()
        assert student.first_name != "Scope leak"

    def test_each_payment_recorder_keeps_exact_scope_with_two_live_admissions(self, club):
        recorder_a = UserFactory()
        recorder_b = UserFactory()
        unrelated = UserFactory()
        TrainerFactory(club=club, user=recorder_a)
        TrainerFactory(club=club, user=recorder_b)
        TrainerFactory(club=club, user=unrelated)
        student = StudentFactory(
            club=club,
            is_child=False,
            status=Student.Status.ACTIVE,
            phone="8 900 123 45 67",
        )
        payment_a = _pending_manual_operational_admission(
            club=club,
            student=student,
            recorded_by=recorder_a,
        )
        payment_b = _pending_manual_operational_admission(
            club=club,
            student=student,
            recorded_by=recorder_b,
        )

        detail_a = client.get(
            f"/students/{student.id}/",
            **_auth_params(recorder_a, club, role="trainer"),
        )
        detail_b = client.get(
            f"/students/{student.id}/",
            **_auth_params(recorder_b, club, role="trainer"),
        )
        unrelated_detail = client.get(
            f"/students/{student.id}/",
            **_auth_params(unrelated, club, role="trainer"),
        )
        opened = client.post(
            f"/students/{student.id}/account-access/open/",
            json={},
            **_auth_params(recorder_a, club, role="trainer"),
        )
        reset = client.post(
            f"/students/{student.id}/account-access/reset/",
            json={},
            **_auth_params(recorder_b, club, role="trainer"),
        )
        forbidden_update_a = client.put(
            f"/students/{student.id}/",
            json={"first_name": "Scope leak A"},
            **_auth_params(recorder_a, club, role="trainer"),
        )
        forbidden_update_b = client.put(
            f"/students/{student.id}/",
            json={"first_name": "Scope leak B"},
            **_auth_params(recorder_b, club, role="trainer"),
        )

        assert payment_b.id > payment_a.id
        assert detail_a.status_code == detail_b.status_code == 200
        assert detail_a.json()["operational_admission"]["payment_id"] == payment_b.id
        assert detail_b.json()["operational_admission"]["payment_id"] == payment_b.id
        assert detail_a.json()["account_access_eligible"] is True
        assert detail_b.json()["account_access_eligible"] is True
        assert opened.status_code == 201
        assert reset.status_code == 200
        assert unrelated_detail.status_code == 403
        assert forbidden_update_a.status_code == forbidden_update_b.status_code == 403
        student.refresh_from_db()
        assert student.first_name not in {"Scope leak A", "Scope leak B"}

    def test_unrelated_trainer_cannot_open_pending_admission_access(self, club, trainer_user):
        recording_trainer_user = UserFactory()
        TrainerFactory(club=club, user=recording_trainer_user)
        TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(
            club=club,
            is_child=False,
            status=Student.Status.ACTIVE,
            phone="8 900 123 45 67",
        )
        _pending_manual_operational_admission(
            club=club,
            student=student,
            recorded_by=recording_trainer_user,
        )

        detail = client.get(
            f"/students/{student.id}/",
            **_auth_params(trainer_user, club, role="trainer"),
        )
        opened = client.post(
            f"/students/{student.id}/account-access/open/",
            json={},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert detail.status_code == 403
        assert opened.status_code == 403

    def test_trainer_package_owner_can_open_student_access_and_read_detail(self, club, trainer_user):
        trainer = TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(
            club=club,
            is_child=False,
            status=Student.Status.ACTIVE,
            phone="8 900 123 45 67",
        )
        _create_active_package_allocation(club=club, student=student, owner_trainer=trainer)

        access_response = client.post(
            f"/students/{student.id}/account-access/open/",
            json={},
            **_auth_params(trainer_user, club, role="trainer"),
        )
        detail_response = client.get(
            f"/students/{student.id}/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert access_response.status_code == 201
        detail_data = detail_response.json()
        assert detail_response.status_code == 200
        assert detail_data["id"] == student.id
        assert detail_data["can_manage_sensitive_actions"] is True
        assert detail_data["can_manage_account_access"] is True
        assert detail_data["can_manage_feedback"] is True
        assert detail_data["account_access"]["username"] == "+79001234567"

    @pytest.mark.parametrize(
        ("_case", "status", "expires_delta_days", "trainings_left", "deleted"),
        NON_CURRENT_PACKAGE_CASES,
    )
    def test_trainer_package_owner_without_current_subscription_cannot_manage_student_access(
        self,
        club,
        trainer_user,
        _case,
        status,
        expires_delta_days,
        trainings_left,
        deleted,
    ):
        trainer = TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(
            club=club,
            is_child=False,
            status=Student.Status.ACTIVE,
            phone="8 900 123 45 67",
        )
        _create_active_package_allocation(
            club=club,
            student=student,
            owner_trainer=trainer,
            status=status,
            expires_at=timezone.now() + timedelta(days=expires_delta_days),
            trainings_left=trainings_left,
            deleted=deleted,
        )

        detail_response = client.get(
            f"/students/{student.id}/",
            **_auth_params(trainer_user, club, role="trainer"),
        )
        access_response = client.post(
            f"/students/{student.id}/account-access/open/",
            json={},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert detail_response.status_code == 403
        assert access_response.status_code == 403
        assert not AccountAccess.objects.for_club(club).filter(student=student).exists()

    def test_trainer_package_owner_can_reset_student_access(self, club, owner_user, trainer_user):
        trainer = TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(
            club=club,
            is_child=False,
            status=Student.Status.ACTIVE,
            phone="8 900 123 45 67",
        )
        _create_active_package_allocation(club=club, student=student, owner_trainer=trainer)
        opened = client.post(
            f"/students/{student.id}/account-access/open/",
            json={},
            **_auth_params(owner_user, club),
        )

        reset = client.post(
            f"/students/{student.id}/account-access/reset/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert opened.status_code == 201
        assert reset.status_code == 200
        assert reset.json()["temporary_password"]
        assert reset.json()["temporary_password"] != opened.json()["temporary_password"]
        access = AccountAccess.objects.for_club(club).get(student=student)
        assert access.status == AccountAccess.Status.RESET
        assert access.reset_by_id == trainer_user.id

    def test_trainer_detail_denies_unscoped_student(self, club, owner_user, trainer_user):
        trainer = TrainerFactory(club=club, user=trainer_user)
        assigned = StudentFactory(
            club=club,
            is_child=False,
            status=Student.Status.ACTIVE,
            phone="8 900 123 45 67",
            assigned_trainer=trainer,
        )
        unassigned = StudentFactory(
            club=club,
            is_child=False,
            status=Student.Status.ACTIVE,
            phone="8 900 765 43 21",
        )
        _paid_active_subscription(club=club, student=assigned)
        _paid_active_subscription(club=club, student=unassigned)

        client.post(
            f"/students/{assigned.id}/account-access/open/",
            json={},
            **_auth_params(owner_user, club),
        )
        client.post(
            f"/students/{unassigned.id}/account-access/open/",
            json={},
            **_auth_params(owner_user, club),
        )

        assigned_detail = client.get(
            f"/students/{assigned.id}/",
            **_auth_params(trainer_user, club, role="trainer"),
        )
        unassigned_detail = client.get(
            f"/students/{unassigned.id}/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert assigned_detail.status_code == 200
        assert assigned_detail.json()["account_access"]["username"] == "+79001234567"
        assert assigned_detail.json()["can_manage_sensitive_actions"] is True
        assert assigned_detail.json()["can_manage_account_access"] is True
        assert assigned_detail.json()["can_manage_feedback"] is True
        assert unassigned_detail.status_code == 403

    def test_trainer_detail_allows_actual_checkin_trainer_without_account_access_management(
        self,
        club,
        trainer_user,
    ):
        trainer = TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(
            club=club,
            is_child=False,
            status=Student.Status.ACTIVE,
            phone="8 900 555 44 33",
        )
        _paid_active_subscription(club=club, student=student)
        schedule = ScheduleFactory(club=club, trainer=trainer)
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            trainer=trainer,
            training_type=schedule.training_type,
        )

        detail = client.get(
            f"/students/{student.id}/",
            **_auth_params(trainer_user, club, role="trainer"),
        )
        open_access = client.post(
            f"/students/{student.id}/account-access/open/",
            json={},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert detail.status_code == 200
        assert detail.json()["id"] == student.id
        assert detail.json()["can_manage_sensitive_actions"] is False
        assert detail.json()["can_manage_account_access"] is False
        assert detail.json()["can_manage_feedback"] is False
        assert detail.json()["account_access"] is None
        assert open_access.status_code == 403


@pytest.mark.django_db
class TestPersonalBookingEndpoint:
    def test_flag_on_rejects_legacy_personal_booking_create_route(self, club, owner_user, settings):
        settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
        ClubSettings.objects.update_or_create(
            club=club,
            defaults={"unified_client_journey_enabled": True},
        )
        trainer = TrainerFactory(club=club)
        location = club.locations.create(name="Main Hall")
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        tariff = TariffFactory(training_type=training_type)
        subscription = SubscriptionFactory(club=club, student=student, tariff=tariff)

        response = client.post(
            f"/students/{student.id}/personal-bookings/",
            json={
                "starts_at": _future_datetime(7, hour=10),
                "ends_at": _future_datetime(7, hour=11),
                "trainer_id": trainer.id,
                "location_id": location.id,
                "training_type_id": training_type.id,
                "subscription_id": subscription.id,
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "unified_personal_command_required"
        assert not ScheduleEnrollment.objects.for_club(club).exists()

    def test_owner_books_personal_session_for_student(self, club, owner_user):
        trainer = TrainerFactory(club=club)
        location = club.locations.create(name="Main Hall")
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        tariff = TariffFactory(training_type=training_type)
        subscription = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            expires_at=None,
        )

        response = client.post(
            f"/students/{student.id}/personal-bookings/",
            json={
                "starts_at": _future_datetime(7, hour=10),
                "ends_at": _future_datetime(7, hour=11),
                "trainer_id": trainer.id,
                "location_id": location.id,
                "training_type_id": training_type.id,
                "subscription_id": subscription.id,
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 201
        data = response.json()
        expected_date = _future_date_obj(7).isoformat()
        assert data["student_id"] == student.id
        assert data["trainer_id"] == trainer.id
        assert data["location_id"] == location.id
        assert data["training_type_id"] == training_type.id
        assert data["created"] is True
        assert data["created_from"] == ScheduleEnrollment.CreatedFrom.PERSONAL_BOOKING
        schedule = Schedule.objects.for_club(club).get(id=data["schedule_id"])
        enrollment = ScheduleEnrollment.objects.for_club(club).get(id=data["enrollment_id"])
        assert schedule.one_time_date.isoformat() == expected_date
        assert schedule.training_type.kind == TrainingType.Kind.PERSONAL
        assert enrollment.student_id == student.id
        assert enrollment.schedule_id == schedule.id
        assert enrollment.starts_on.isoformat() == expected_date
        assert enrollment.ends_on.isoformat() == expected_date

    def test_list_personal_bookings_returns_only_upcoming_personal_sessions(self, club, owner_user):
        trainer = TrainerFactory(club=club)
        location = club.locations.create(name="Main Hall")
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        tariff = TariffFactory(training_type=training_type)
        subscription = SubscriptionFactory(club=club, student=student, tariff=tariff)

        past_date = timezone.localdate() - timedelta(days=1)
        past_schedule = ScheduleFactory(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=past_date,
        )
        ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=past_schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=past_date,
            ends_on=past_date,
            created_from=ScheduleEnrollment.CreatedFrom.PERSONAL_BOOKING,
        )
        future = client.post(
            f"/students/{student.id}/personal-bookings/",
            json={
                "starts_at": _future_datetime(7, hour=10),
                "ends_at": _future_datetime(7, hour=11),
                "trainer_id": trainer.id,
                "location_id": location.id,
                "training_type_id": training_type.id,
                "subscription_id": subscription.id,
            },
            **_auth_params(owner_user, club),
        )
        self_booked = client.post(
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
        assert self_booked.status_code == 201
        ScheduleEnrollment.objects.for_club(club).filter(
            id=self_booked.json()["enrollment_id"],
        ).update(created_from=ScheduleEnrollment.CreatedFrom.STUDENT_SELF_BOOKING)

        response = client.get(
            f"/students/{student.id}/personal-bookings/",
            **_auth_params(owner_user, club),
        )

        assert future.status_code == 201
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        assert data[0]["enrollment_id"] == future.json()["enrollment_id"]
        assert data[1]["enrollment_id"] == self_booked.json()["enrollment_id"]
        assert data[0]["student_id"] == student.id
        assert data[0]["trainer_id"] == trainer.id
        assert data[0]["location_name"] == "Main Hall"
        assert data[0]["training_type_name"] == training_type.name
        assert data[0]["starts_at"].startswith(_future_datetime(7, hour=10))
        assert data[0]["status"] == ScheduleEnrollment.Status.ACTIVE
        assert data[1]["created_from"] == ScheduleEnrollment.CreatedFrom.STUDENT_SELF_BOOKING

    def test_list_personal_bookings_projects_exact_reschedule_capability(
        self,
        club,
        trainer_user,
    ):
        trainer = TrainerFactory(club=club, user=trainer_user)
        foreign_trainer = TrainerFactory(club=club)
        location = club.locations.create(name="Main Hall")
        personal_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        mini_group_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.MINI_GROUP)
        student = StudentFactory(
            club=club,
            status=Student.Status.ACTIVE,
            assigned_trainer=trainer,
        )
        tariff = TariffFactory(club=club, training_type=personal_type, price=Decimal("2000.00"))
        target_date = _future_date_obj(7)
        booking_index = 0

        def create_exact_enrollment(
            *,
            trainer_for_schedule,
            training_type,
            created_from=ScheduleEnrollment.CreatedFrom.PERSONAL_BOOKING,
        ):
            nonlocal booking_index
            booking_date = target_date + timedelta(days=booking_index)
            booking_index += 1
            schedule = ScheduleFactory(
                club=club,
                trainer=trainer_for_schedule,
                location=location,
                training_type=training_type,
                one_time_date=booking_date,
                day_of_week=booking_date.weekday(),
            )
            enrollment = ScheduleEnrollment.objects.create(
                club=club,
                student=student,
                schedule=schedule,
                status=ScheduleEnrollment.Status.ACTIVE,
                starts_on=booking_date,
                ends_on=booking_date,
                created_from=created_from,
            )
            return schedule, enrollment

        _, eligible = create_exact_enrollment(
            trainer_for_schedule=trainer,
            training_type=personal_type,
        )
        _, mini_group = create_exact_enrollment(
            trainer_for_schedule=trainer,
            training_type=mini_group_type,
        )
        _, foreign_trainer_booking = create_exact_enrollment(
            trainer_for_schedule=foreign_trainer,
            training_type=personal_type,
        )
        checked_in_schedule, checked_in = create_exact_enrollment(
            trainer_for_schedule=trainer,
            training_type=personal_type,
        )
        CheckinFactory(
            club=club,
            student=student,
            schedule=checked_in_schedule,
            date=checked_in_schedule.one_time_date,
        )
        group_session_schedule, group_session = create_exact_enrollment(
            trainer_for_schedule=trainer,
            training_type=personal_type,
        )
        GroupSessionFactory(
            club=club,
            schedule=group_session_schedule,
            date=group_session_schedule.one_time_date,
        )
        pending_schedule, pending_reservation = create_exact_enrollment(
            trainer_for_schedule=trainer,
            training_type=personal_type,
        )
        PersonalBookingPaymentReservation.objects.create(
            club=club,
            student=student,
            trainer=trainer,
            location=location,
            training_type=personal_type,
            tariff=tariff,
            schedule=pending_schedule,
            enrollment=pending_reservation,
            starts_at=timezone.now() + timedelta(days=7),
            ends_at=timezone.now() + timedelta(days=7, hours=1),
            expires_at=timezone.now() + timedelta(hours=1),
            status=PersonalBookingPaymentReservation.Status.PENDING_PAYMENT,
            created_by=trainer_user,
        )
        booked_reservation_schedule, booked_reservation = create_exact_enrollment(
            trainer_for_schedule=trainer,
            training_type=personal_type,
        )
        booked_reservation_record = PersonalBookingPaymentReservation.objects.create(
            club=club,
            student=student,
            trainer=trainer,
            location=location,
            training_type=personal_type,
            tariff=tariff,
            schedule=booked_reservation_schedule,
            enrollment=booked_reservation,
            starts_at=timezone.now() + timedelta(days=14),
            ends_at=timezone.now() + timedelta(days=14, hours=1),
            expires_at=timezone.now() + timedelta(days=1),
            status=PersonalBookingPaymentReservation.Status.BOOKED,
            created_by=trainer_user,
        )
        PersonalServiceTermsSnapshot.objects.create(
            club=club,
            reservation=booked_reservation_record,
            terms_version=PersonalServiceTermsSnapshot.TermsVersion.COMPLETE_V1,
        )
        _, complete_drop_in = create_exact_enrollment(
            trainer_for_schedule=trainer,
            training_type=personal_type,
            created_from=ScheduleEnrollment.CreatedFrom.PERSONAL_DROP_IN,
        )
        complete_drop_in_record = PersonalDropInBooking.objects.create(
            club=club,
            enrollment=complete_drop_in,
            tariff=tariff,
            tariff_name_snapshot=tariff.name,
            price_snapshot=tariff.price,
            state=PersonalDropInBooking.State.SCHEDULED,
            created_by=trainer_user,
            idempotency_key="student-booking-reschedule-capability-drop-in",
        )
        PersonalServiceTermsSnapshot.objects.create(
            club=club,
            booking=complete_drop_in_record,
            terms_version=PersonalServiceTermsSnapshot.TermsVersion.COMPLETE_V1,
        )
        source_starts_at = timezone.now() + timedelta(days=7)
        PersonalAvailabilitySlot.objects.create(
            club=club,
            trainer=trainer,
            location=location,
            training_type=personal_type,
            starts_at=source_starts_at,
            ends_at=source_starts_at + timedelta(hours=1),
            status=PersonalAvailabilitySlot.Status.BOOKED,
            booked_enrollment=eligible,
        )

        response = client.get(
            f"/students/{student.id}/personal-bookings/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        capability_by_enrollment = {
            item["enrollment_id"]: item["can_reschedule"] for item in response.json()
        }
        assert capability_by_enrollment == {
            eligible.id: True,
            mini_group.id: False,
            foreign_trainer_booking.id: False,
            checked_in.id: False,
            group_session.id: False,
            pending_reservation.id: False,
            booked_reservation.id: True,
            complete_drop_in.id: True,
        }

        loaded_bookings = list(
            get_student_upcoming_personal_bookings(
                club=club,
                student_id=student.id,
            )
        )
        with CaptureQueriesContext(connection) as captured:
            loaded_capabilities = {
                enrollment.id: can_reschedule_student_personal_booking(
                    enrollment=enrollment,
                    can_manage=enrollment.schedule.trainer_id == trainer.id,
                )
                for enrollment in loaded_bookings
            }

        assert len(captured) == 0
        assert loaded_capabilities == capability_by_enrollment

    def test_terminal_drop_in_reports_no_future_payment_due(self, club, owner_user):
        trainer = TrainerFactory(club=club)
        location = club.locations.create(name="Main Hall")
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        tariff = TariffFactory(
            club=club,
            training_type=training_type,
            price=Decimal("2000.00"),
            trainings_limit=1,
        )
        target_date = _future_date_obj(7)
        schedule = ScheduleFactory(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=target_date,
            day_of_week=target_date.weekday(),
        )
        enrollment = ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.CANCELLED,
            starts_on=target_date,
            ends_on=target_date,
            created_from=ScheduleEnrollment.CreatedFrom.PERSONAL_DROP_IN,
        )
        booking = PersonalDropInBooking.objects.create(
            club=club,
            enrollment=enrollment,
            tariff=tariff,
            tariff_name_snapshot=tariff.name,
            price_snapshot=tariff.price,
            state=PersonalDropInBooking.State.NO_SHOW,
            created_by=owner_user,
            idempotency_key="student-api-terminal-drop-in",
        )

        response = client.get(
            f"/students/{student.id}/personal-bookings/",
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        item = next(row for row in response.json() if row["booking_id"] == booking.id)
        assert item["attendance_state"] == PersonalDropInBooking.State.NO_SHOW
        assert item["financial_state"] == "not_due"
        assert item["next_action_label"] is None

    def test_reassigned_trainer_sees_other_trainers_drop_in_booking_read_only(
        self,
        club,
        owner_user,
        trainer_user,
    ):
        owning_trainer = TrainerFactory(club=club, user=trainer_user)
        foreign_trainer_user = UserFactory()
        foreign_trainer = TrainerFactory(club=club, user=foreign_trainer_user)
        location = club.locations.create(name="Main Hall")
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        student = StudentFactory(
            club=club,
            status=Student.Status.ACTIVE,
            assigned_trainer=owning_trainer,
        )
        tariff = TariffFactory(
            club=club,
            training_type=training_type,
            price=Decimal("2000.00"),
            trainings_limit=1,
        )
        target_date = _future_date_obj(7)
        schedule = ScheduleFactory(
            club=club,
            trainer=owning_trainer,
            location=location,
            training_type=training_type,
            one_time_date=target_date,
            day_of_week=target_date.weekday(),
        )
        enrollment = ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=target_date,
            ends_on=target_date,
            created_from=ScheduleEnrollment.CreatedFrom.PERSONAL_DROP_IN,
        )
        booking = PersonalDropInBooking.objects.create(
            club=club,
            enrollment=enrollment,
            tariff=tariff,
            tariff_name_snapshot=tariff.name,
            price_snapshot=tariff.price,
            state=PersonalDropInBooking.State.SCHEDULED,
            created_by=trainer_user,
            idempotency_key="student-api-trainer-redaction",
        )
        subscription = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.PENDING,
        )
        payment = PaymentFactory(
            club=club,
            student=student,
            tariff=tariff,
            subscription=subscription,
            payment_method=Payment.Method.ONLINE,
            status=Payment.Status.PENDING,
            recorded_by=trainer_user,
        )
        order = BankPaymentOrder.objects.create(
            club=club,
            payment=payment,
            subscription=subscription,
            student=student,
            provider=BankPaymentOrder.Provider.MOCK,
            source=BankPaymentOrder.Source.TRAINER,
            status=BankPaymentOrder.Status.PENDING,
            amount_snapshot=payment.amount,
            purpose_snapshot="Personal booking",
            provider_payment_url="https://pay.example.test/personal-booking",
            expires_at=timezone.now() + timedelta(hours=1),
            created_by=trainer_user,
        )
        PersonalDropInPaymentLink.objects.create(
            club=club,
            booking=booking,
            payment=payment,
            bank_payment_order=order,
            created_by=trainer_user,
            idempotency_key="student-api-trainer-redaction-payment",
        )

        owning_response = client.get(
            f"/students/{student.id}/personal-bookings/",
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert owning_response.status_code == 200
        owning_item = next(item for item in owning_response.json() if item["booking_id"] == booking.id)
        assert owning_item["can_manage"] is True
        assert owning_item["payment_id"] == payment.id
        assert owning_item["bank_payment_order_id"] == order.id
        assert owning_item["provider_payment_url"] == order.provider_payment_url
        assert owning_item["can_cancel_payment"] is True

        student.assigned_trainer = foreign_trainer
        student.save(update_fields=["assigned_trainer", "updated_at"])

        foreign_response = client.get(
            f"/students/{student.id}/personal-bookings/",
            **_auth_params(foreign_trainer_user, club, role="trainer"),
        )
        assert foreign_response.status_code == 200
        foreign_item = next(item for item in foreign_response.json() if item["booking_id"] == booking.id)
        assert foreign_item["can_manage"] is False
        assert foreign_item["payment_id"] is None
        assert foreign_item["bank_payment_order_id"] is None
        assert foreign_item["provider_payment_url"] == ""
        assert foreign_item["can_cancel_payment"] is False
        assert foreign_item["can_cancel"] is False
        assert foreign_item["can_mark_no_show"] is False
        assert foreign_item["next_action_label"] is None

        owner_response = client.get(
            f"/students/{student.id}/personal-bookings/",
            **_auth_params(owner_user, club),
        )
        assert owner_response.status_code == 200
        owner_item = next(item for item in owner_response.json() if item["booking_id"] == booking.id)
        assert owner_item["can_manage"] is True
        assert owner_item["payment_id"] == payment.id
        assert owner_item["bank_payment_order_id"] == order.id
        assert owner_item["provider_payment_url"] == order.provider_payment_url

    def test_list_personal_bookings_defaults_to_club_local_today(self, monkeypatch, club, owner_user):
        club.timezone = "Asia/Yekaterinburg"
        club.save(update_fields=["timezone"])
        monkeypatch.setattr(
            "apps.clubs.timezones.timezone.now",
            lambda: datetime(2026, 6, 28, 20, 30, tzinfo=UTC),
        )
        trainer = TrainerFactory(club=club)
        location = club.locations.create(name="Main Hall")
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        club_local_yesterday = date(2026, 6, 28)
        schedule = ScheduleFactory(
            club=club,
            trainer=trainer,
            location=location,
            training_type=training_type,
            one_time_date=club_local_yesterday,
            day_of_week=club_local_yesterday.weekday(),
        )
        enrollment = ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=club_local_yesterday,
            ends_on=club_local_yesterday,
            created_from=ScheduleEnrollment.CreatedFrom.PERSONAL_BOOKING,
        )

        response = client.get(
            f"/students/{student.id}/personal-bookings/",
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        assert all(item["enrollment_id"] != enrollment.id for item in response.json())

    def test_owner_personal_booking_requires_trainer_id(self, club, owner_user):
        location = club.locations.create(name="Main Hall")
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)

        response = client.post(
            f"/students/{student.id}/personal-bookings/",
            json={
                "starts_at": _future_datetime(7, hour=10),
                "ends_at": _future_datetime(7, hour=11),
                "location_id": location.id,
                "training_type_id": training_type.id,
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400

    def test_trainer_personal_booking_uses_current_trainer_not_payload(self, club, trainer_user):
        trainer = TrainerFactory(club=club, user=trainer_user)
        other_trainer = TrainerFactory(club=club)
        location = club.locations.create(name="Main Hall")
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        student = StudentFactory(
            club=club,
            status=Student.Status.ACTIVE,
            assigned_trainer=trainer,
        )
        tariff = TariffFactory(training_type=training_type)
        subscription = SubscriptionFactory(club=club, student=student, tariff=tariff)

        response = client.post(
            f"/students/{student.id}/personal-bookings/",
            json={
                "starts_at": _future_datetime(7, hour=10),
                "ends_at": _future_datetime(7, hour=11),
                "trainer_id": other_trainer.id,
                "location_id": location.id,
                "training_type_id": training_type.id,
                "subscription_id": subscription.id,
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 201
        assert response.json()["trainer_id"] == trainer.id
        schedule = Schedule.objects.for_club(club).get(id=response.json()["schedule_id"])
        assert schedule.trainer_id == trainer.id

    def test_trainer_personal_payment_reservation_list_hides_cancel_after_expiry(
        self,
        settings,
        club,
        trainer_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        trainer = TrainerFactory(club=club, user=trainer_user)
        location = club.locations.create(name="Main Hall")
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=trainer, location=location)
        student = StudentFactory(
            club=club,
            status=Student.Status.ACTIVE,
            assigned_trainer=trainer,
        )
        tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=1)
        created = client.post(
            f"/students/{student.id}/personal-booking-payment-reservations/",
            json={
                "starts_at": "2099-04-07T10:00:00",
                "ends_at": "2099-04-07T11:00:00",
                "location_id": location.id,
                "training_type_id": training_type.id,
                "tariff_id": tariff.id,
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert created.status_code == 201, created.json()
        reservation = PersonalBookingPaymentReservation.objects.for_club(club).get(
            id=created.json()["id"]
        )
        reservation.expires_at = timezone.now() - timedelta(minutes=1)
        reservation.save(update_fields=["expires_at", "updated_at"])

        response = client.get(
            f"/students/{student.id}/personal-booking-payment-reservations/?status=pending_payment",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == reservation.id
        assert data[0]["can_cancel"] is False

    def test_reassigned_trainer_cannot_list_or_cancel_another_trainers_personal_payment_reservation(
        self,
        settings,
        club,
        owner_user,
        trainer_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        owning_trainer = TrainerFactory(club=club, user=trainer_user)
        foreign_trainer_user = UserFactory()
        foreign_trainer = TrainerFactory(club=club, user=foreign_trainer_user)
        location = club.locations.create(name="Main Hall")
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TrainerLocationFactory(club=club, trainer=owning_trainer, location=location)
        student = StudentFactory(
            club=club,
            status=Student.Status.ACTIVE,
            assigned_trainer=owning_trainer,
        )
        tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=1)
        created = client.post(
            f"/students/{student.id}/personal-booking-payment-reservations/",
            json={
                "starts_at": _future_datetime(8, hour=10),
                "ends_at": _future_datetime(8, hour=11),
                "location_id": location.id,
                "training_type_id": training_type.id,
                "tariff_id": tariff.id,
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert created.status_code == 201, created.json()
        reservation_id = created.json()["id"]
        reservation = PersonalBookingPaymentReservation.objects.for_club(club).get(id=reservation_id)
        assert reservation.trainer_id == owning_trainer.id
        assert reservation.bank_payment_order.source == BankPaymentOrder.Source.TRAINER

        scope_date = timezone.localdate() - timedelta(days=1)
        scope_schedule = ScheduleFactory(
            club=club,
            trainer=owning_trainer,
            location=location,
            training_type=training_type,
            one_time_date=scope_date,
            day_of_week=scope_date.weekday(),
        )
        CheckinFactory(
            club=club,
            student=student,
            schedule=scope_schedule,
            trainer=owning_trainer,
            location=location,
            training_type=training_type,
            date=scope_date,
        )

        student.assigned_trainer = foreign_trainer
        student.save(update_fields=["assigned_trainer", "updated_at"])

        listed = client.get(
            f"/students/{student.id}/personal-booking-payment-reservations/?status=open_actionable",
            **_auth_params(foreign_trainer_user, club, role="trainer"),
        )
        assert listed.status_code == 200
        assert listed.json() == []

        cancelled = client.post(
            f"/students/{student.id}/personal-booking-payment-reservations/{reservation_id}/cancel/",
            json={},
            **_auth_params(foreign_trainer_user, club, role="trainer"),
        )
        assert cancelled.status_code == 403
        reservation.refresh_from_db()
        reservation.bank_payment_order.refresh_from_db()
        reservation.payment.refresh_from_db()
        reservation.subscription.refresh_from_db()
        assert reservation.status == PersonalBookingPaymentReservation.Status.PENDING_PAYMENT
        assert reservation.bank_payment_order.status == BankPaymentOrder.Status.PENDING
        assert reservation.payment.status == Payment.Status.PENDING
        assert reservation.subscription.status == Subscription.Status.PENDING

        owning_list = client.get(
            f"/students/{student.id}/personal-booking-payment-reservations/",
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert owning_list.status_code == 200
        assert [item["id"] for item in owning_list.json()] == [reservation.id]

        owning_cancel = client.post(
            f"/students/{student.id}/personal-booking-payment-reservations/{reservation_id}/cancel/",
            json={},
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert owning_cancel.status_code == 200

        owner_list = client.get(
            f"/students/{student.id}/personal-booking-payment-reservations/",
            **_auth_params(owner_user, club),
        )
        assert owner_list.status_code == 200
        assert [item["id"] for item in owner_list.json()] == [reservation.id]

    def test_trainer_cannot_list_unscoped_student_personal_bookings(self, club, trainer_user):
        TrainerFactory(club=club, user=trainer_user)
        unassigned = StudentFactory(club=club, status=Student.Status.ACTIVE)

        response = client.get(
            f"/students/{unassigned.id}/personal-bookings/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403

    def test_trainer_cannot_book_unscoped_student_personal_session(self, club, trainer_user):
        trainer = TrainerFactory(club=club, user=trainer_user)
        location = club.locations.create(name="Main Hall")
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        unassigned = StudentFactory(club=club, status=Student.Status.ACTIVE)

        response = client.post(
            f"/students/{unassigned.id}/personal-bookings/",
            json={
                "starts_at": _future_datetime(7, hour=10),
                "ends_at": _future_datetime(7, hour=11),
                "trainer_id": trainer.id,
                "location_id": location.id,
                "training_type_id": training_type.id,
            },
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403
        assert not Schedule.objects.for_club(club).filter(one_time_date=_future_date_obj(7)).exists()


@pytest.mark.django_db
class TestUpdateStudent:
    def test_update_student_endpoint(self, club, owner_user):
        student = StudentFactory(club=club)
        response = client.put(
            f"/students/{student.id}/",
            json={"first_name": "Updated"},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        assert response.json()["first_name"] == "Updated"

    def test_update_child_guardian_phone_endpoint(self, club, owner_user):
        student = StudentFactory(
            club=club,
            is_child=True,
            phone="",
            guardian_phone="+79001111111",
        )

        response = client.put(
            f"/students/{student.id}/",
            json={"guardian_phone": "+7 (900) 222-22-22"},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        assert response.json()["guardian_phone"] == "+79002222222"
        student.refresh_from_db()
        assert student.guardian_phone == "+79002222222"

    def test_trainer_can_update_scoped_student(self, club, trainer_user):
        trainer = TrainerFactory(club=club, user=trainer_user)
        scoped_student = StudentFactory(club=club, assigned_trainer=trainer, first_name="Before")

        response = client.put(
            f"/students/{scoped_student.id}/",
            json={"first_name": "After", "source": "referral"},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["first_name"] == "After"
        assert data["source"] == "referral"
        scoped_student.refresh_from_db()
        assert scoped_student.first_name == "After"
        assert scoped_student.source == "referral"

    def test_trainer_cannot_update_unscoped_student(self, club, trainer_user):
        TrainerFactory(club=club, user=trainer_user)
        unassigned = StudentFactory(club=club)

        response = client.put(
            f"/students/{unassigned.id}/",
            json={"first_name": "Leaked"},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403
        unassigned.refresh_from_db()
        assert unassigned.first_name != "Leaked"

    def test_trainer_update_duplicate_phone_returns_friendly_error_without_mutation(self, club, trainer_user):
        trainer = TrainerFactory(club=club, user=trainer_user)
        scoped_student = StudentFactory(
            club=club,
            assigned_trainer=trainer,
            phone="+79001111111",
        )
        StudentFactory(club=club, phone="+79002222222")

        response = client.put(
            f"/students/{scoped_student.id}/",
            json={"phone": "+7 (900) 222-22-22"},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "duplicate_phone"
        scoped_student.refresh_from_db()
        assert scoped_student.phone == "+79001111111"


@pytest.mark.django_db
class TestTransitionStatus:
    def test_transition_status_endpoint(self, club, owner_user):
        student = StudentFactory(club=club, status=Student.Status.LEAD, lead_status=Student.LeadStatus.NEW)
        response = client.post(
            f"/students/{student.id}/transition/",
            json={"new_status": "trial"},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        assert response.json()["status"] == "trial"
        student.refresh_from_db()
        assert student.lead_status is None

    def test_transition_status_invalid(self, club, owner_user):
        student = StudentFactory(club=club, status=Student.Status.LEAD)
        response = client.post(
            f"/students/{student.id}/transition/",
            json={"new_status": "churned"},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 400
        assert response.json()["code"] == "invalid_transition"


@pytest.mark.django_db
class TestImportStudents:
    def test_import_students_endpoint(self, club, owner_user):
        from django.core.files.uploadedfile import SimpleUploadedFile

        excel_bytes = _make_excel_bytes(
            [
                ["Name", "Phone"],
                ["Ivan Petrov", "+79001111111"],
                ["Petr Sidorov", "+79002222222"],
            ]
        )
        uploaded = SimpleUploadedFile(
            "students.xlsx",
            excel_bytes,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response = client.post(
            "/students/import/",
            FILES={"file": uploaded},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["created"] == 2
        assert data["skipped"] == 0
        assert set(
            Student.objects.for_club(club)
            .filter(phone__in={"+79001111111", "+79002222222"})
            .values_list("lead_status", flat=True)
        ) == {Student.LeadStatus.NEW}

    def test_import_students_permission(self, club, trainer_user):
        from django.core.files.uploadedfile import SimpleUploadedFile

        excel_bytes = _make_excel_bytes([["Name", "Phone"], ["Test", "+79001111111"]])
        uploaded = SimpleUploadedFile("students.xlsx", excel_bytes)
        response = client.post(
            "/students/import/",
            FILES={"file": uploaded},
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert response.status_code == 403


@pytest.mark.django_db
class TestDeleteStudent:
    def test_delete_student_soft(self, club, owner_user):
        student = StudentFactory(club=club)
        response = client.delete(
            f"/students/{student.id}/",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 204
        student.refresh_from_db()
        assert student.deleted_at is not None


@pytest.mark.django_db
class TestStudentNotesEndpoint:
    def test_add_note_returns_201(self, club, owner_user):
        student = StudentFactory(club=club)
        response = client.post(
            f"/students/{student.id}/notes/",
            json={"text": "Good progress today"},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 201
        data = response.json()
        assert data["text"] == "Good progress today"
        assert "author_email" in data
        assert "created_at" in data

    def test_add_note_tenant_isolated(self, club, other_club, owner_user):
        student = StudentFactory(club=other_club)
        response = client.post(
            f"/students/{student.id}/notes/",
            json={"text": "Cross-tenant attempt"},
            **_auth_params(owner_user, club),
        )
        assert response.status_code in (404, 403)

    def test_trainer_cannot_list_or_add_notes_for_unscoped_student(self, club, trainer_user):
        TrainerFactory(club=club, user=trainer_user)
        unassigned = StudentFactory(club=club)

        list_response = client.get(
            f"/students/{unassigned.id}/notes/",
            **_auth_params(trainer_user, club, role="trainer"),
        )
        add_response = client.post(
            f"/students/{unassigned.id}/notes/",
            json={"text": "Should not be visible"},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert list_response.status_code == 403
        assert add_response.status_code == 403
        assert not StudentNote.objects.for_club(club).filter(student=unassigned).exists()

    def test_list_notes_returns_notes(self, club, owner_user):
        student = StudentFactory(club=club)
        response = client.get(
            f"/students/{student.id}/notes/",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200

    def test_student_checkins_returns_attendance(self, club, owner_user):
        student = StudentFactory(club=club)
        response = client.get(
            f"/students/{student.id}/checkins/",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200

    def test_trainer_cannot_read_unscoped_student_checkins(self, club, trainer_user):
        TrainerFactory(club=club, user=trainer_user)
        unassigned = StudentFactory(club=club)

        response = client.get(
            f"/students/{unassigned.id}/checkins/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403

    def test_student_checkins_use_actual_checkin_trainer(self, club, owner_user):
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        substitute = TrainerFactory(club=club, first_name="Alex", last_name="Backup")
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            trainer=substitute,
        )

        response = client.get(
            f"/students/{student.id}/checkins/",
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        assert response.json()[0]["trainer_name"] == "Alex Backup"
