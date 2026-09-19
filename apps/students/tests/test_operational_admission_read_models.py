from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone
from ninja.testing import TestClient

from apps.attendance.models import ScheduleEnrollment
from apps.attendance.tests.factories import CheckinFactory, ScheduleFactory
from apps.billing.models import Payment, Subscription, TrainingType
from apps.billing.tests.factories import (
    DebtFactory,
    PaymentFactory,
    SubscriptionFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.common.tests.helpers import make_auth_params as _auth_params
from apps.students.models import Student
from apps.students.selectors import (
    get_cabinet_financial_read_model,
    get_manual_operational_admission,
    is_account_access_eligible,
)
from apps.students.tests.factories import StudentFactory
from config.api import api

client = TestClient(api)


@pytest.fixture(autouse=True)
def _bypass_jwt_auth(bypass_jwt_auth):
    pass


def _pending_admission_with_covered_visit(
    *,
    club,
    student,
    start_date: date | None = None,
    duration_days: int = 30,
    group_name: str | None = None,
):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        price=Decimal("5000"),
        duration_days=duration_days,
    )
    start_date = start_date or timezone.localdate()
    schedule = ScheduleFactory(
        club=club,
        training_type=training_type,
        day_of_week=start_date.weekday(),
        **({"group_name": group_name} if group_name is not None else {}),
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
    payment = PaymentFactory(
        club=club,
        student=student,
        tariff=tariff,
        subscription=subscription,
        payment_method=Payment.Method.TRANSFER,
        status=Payment.Status.PENDING,
        target_schedule=schedule,
        target_start_date=start_date,
        target_group_name_snapshot=schedule.group_name,
        target_location_id_snapshot=schedule.location_id,
        target_location_name_snapshot=schedule.location.name,
        target_training_type_id_snapshot=training_type.id,
        target_training_type_kind_snapshot=TrainingType.Kind.GROUP,
        conversion_enrollment=enrollment,
    )
    checkin = CheckinFactory(
        club=club,
        student=student,
        schedule=schedule,
        training_type=training_type,
        date=start_date,
        is_debt=True,
        subscription=None,
    )
    debt = DebtFactory(
        club=club,
        student=student,
        checkin=checkin,
        tariff_price=None,
        settlement_payment=payment,
    )
    return payment, subscription, enrollment, debt


@pytest.mark.django_db
class TestOperationalAdmissionCabinetReadModels:
    def test_student_and_parent_cabinets_return_canonical_group_identity(
        self,
        club,
        student_user,
        parent_user,
    ):
        from apps.attendance.models import TrainingGroupMembership
        from apps.attendance.tests.factories import TrainingGroupFactory

        start_date = timezone.localdate()
        student = StudentFactory(
            club=club,
            user=student_user,
            parent_user=parent_user,
            is_child=True,
            status=Student.Status.ACTIVE,
        )
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(club=club, training_type=training_type, duration_days=30)
        group = TrainingGroupFactory(club=club, training_type=training_type)
        schedule = ScheduleFactory(
            club=club,
            training_group=group,
            training_type=training_type,
            trainer=group.responsible_trainer,
            location=group.location,
            day_of_week=start_date.weekday(),
        )
        membership = TrainingGroupMembership.objects.create(
            club=club,
            student=student,
            training_group=group,
            starts_on=start_date,
            source=TrainingGroupMembership.Source.PAID_CONVERSION,
            authority=TrainingGroupMembership.Authority.PAYMENT_OWNED,
        )
        enrollment = ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            training_group_membership=membership,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=start_date,
            created_from=ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION,
        )
        subscription = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.PENDING,
            paid_amount=None,
            expires_at=None,
        )
        payment = PaymentFactory(
            club=club,
            student=student,
            tariff=tariff,
            subscription=subscription,
            payment_method=Payment.Method.CASH,
            status=Payment.Status.PENDING,
            target_schedule=schedule,
            target_training_group=group,
            target_group_membership=membership,
            conversion_group_membership=membership,
            conversion_enrollment=enrollment,
            group_membership_action_snapshot=Payment.GroupMembershipActionSnapshot.NEW_ADMISSION,
            target_start_date=start_date,
            target_group_name_snapshot="Stale schedule label",
            target_location_id_snapshot=group.location_id,
            target_location_name_snapshot=group.location.name,
            target_training_type_id_snapshot=training_type.id,
            target_training_type_kind_snapshot=TrainingType.Kind.GROUP,
        )

        student_response = client.get(
            "/students/me/financial-state/",
            **_auth_params(student_user, club, role="student"),
        )
        parent_response = client.get(
            f"/parents/children/{student.id}/",
            **_auth_params(parent_user, club, role="parent"),
        )

        assert student_response.status_code == parent_response.status_code == 200
        student_admission = student_response.json()["operational_admission"]
        parent_admission = parent_response.json()["financial_state"]["operational_admission"]
        assert student_admission["payment_id"] == payment.id
        assert student_admission["group_label"] == group.name
        assert student_admission["training_group_id"] == group.id
        assert student_admission["group_membership_id"] == membership.id
        assert parent_admission == student_admission

    def test_newer_terminal_admission_does_not_mask_older_qualifying_admission(self, club):
        student = StudentFactory(club=club, status="active")
        live_payment, _live_subscription, _live_enrollment, _live_debt = (
            _pending_admission_with_covered_visit(club=club, student=student)
        )
        terminal_payment, terminal_subscription, terminal_enrollment, _terminal_debt = (
            _pending_admission_with_covered_visit(club=club, student=student)
        )
        terminal_payment.status = Payment.Status.REJECTED
        terminal_payment.save(update_fields=["status", "updated_at"])
        terminal_subscription.soft_delete()
        terminal_enrollment.status = ScheduleEnrollment.Status.CANCELLED
        terminal_enrollment.ends_on = terminal_enrollment.starts_on
        terminal_enrollment.save(update_fields=["status", "ends_on", "updated_at"])

        admission = get_manual_operational_admission(club=club, student=student)
        cabinet = get_cabinet_financial_read_model(club=club, student=student)

        assert terminal_payment.id > live_payment.id
        assert admission is not None
        assert admission.payment_id == live_payment.id
        assert admission.is_qualifying is True
        assert cabinet["operational_admission"]["payment_id"] == live_payment.id
        assert [item["payment_id"] for item in cabinet["operational_admissions"]] == [
            live_payment.id,
        ]
        assert is_account_access_eligible(club=club, student=student) is True

    def test_multiple_live_admissions_choose_newest_qualifying_deterministically(self, club):
        student = StudentFactory(club=club, status="active")
        first_payment, *_ = _pending_admission_with_covered_visit(club=club, student=student)
        second_payment, *_ = _pending_admission_with_covered_visit(club=club, student=student)

        admission = get_manual_operational_admission(club=club, student=student)
        cabinet = get_cabinet_financial_read_model(club=club, student=student)

        assert second_payment.id > first_payment.id
        assert admission is not None
        assert admission.payment_id == second_payment.id
        assert admission.is_qualifying is True
        assert cabinet["operational_admission"]["payment_id"] == second_payment.id
        assert [item["payment_id"] for item in cabinet["operational_admissions"]] == [
            second_payment.id,
            first_payment.id,
        ]
        assert is_account_access_eligible(club=club, student=student) is True

    def test_student_financial_api_shows_each_live_admission_and_its_covered_visit(
        self,
        club,
        student_user,
    ):
        student = StudentFactory(club=club, user=student_user, status="active")
        first_payment, _first_subscription, _first_enrollment, first_debt = (
            _pending_admission_with_covered_visit(
                club=club,
                student=student,
                group_name="Boxing group",
            )
        )
        second_payment, _second_subscription, _second_enrollment, second_debt = (
            _pending_admission_with_covered_visit(
                club=club,
                student=student,
                group_name="BJJ group",
            )
        )

        state_response = client.get(
            "/students/me/financial-state/",
            **_auth_params(student_user, club, role="student"),
        )
        debts_response = client.get(
            "/students/me/debts/",
            **_auth_params(student_user, club, role="student"),
        )

        assert state_response.status_code == debts_response.status_code == 200
        state = state_response.json()
        assert state["operational_admission"]["payment_id"] == second_payment.id
        assert [admission["payment_id"] for admission in state["operational_admissions"]] == [
            second_payment.id,
            first_payment.id,
        ]
        assert [admission["group_label"] for admission in state["operational_admissions"]] == [
            "BJJ group",
            "Boxing group",
        ]
        assert [
            (visit["payment_id"], visit["debt_id"])
            for visit in state["covered_visits"]
        ] == [
            (second_payment.id, second_debt.id),
            (first_payment.id, first_debt.id),
        ]
        assert all(visit["is_payable"] is False for visit in state["covered_visits"])
        assert debts_response.json() == []

    def test_parent_financial_api_shows_each_live_admission_and_its_covered_visit(
        self,
        club,
        parent_user,
    ):
        child = StudentFactory(
            club=club,
            parent_user=parent_user,
            is_child=True,
            status="active",
        )
        first_payment, _first_subscription, _first_enrollment, first_debt = (
            _pending_admission_with_covered_visit(
                club=club,
                student=child,
                group_name="Kids boxing",
            )
        )
        second_payment, _second_subscription, _second_enrollment, second_debt = (
            _pending_admission_with_covered_visit(
                club=club,
                student=child,
                group_name="Kids BJJ",
            )
        )

        response = client.get(
            f"/parents/children/{child.id}/",
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 200
        state = response.json()["financial_state"]
        assert state["operational_admission"]["payment_id"] == second_payment.id
        assert [admission["payment_id"] for admission in state["operational_admissions"]] == [
            second_payment.id,
            first_payment.id,
        ]
        assert [
            (visit["payment_id"], visit["debt_id"])
            for visit in state["covered_visits"]
        ] == [
            (second_payment.id, second_debt.id),
            (first_payment.id, first_debt.id),
        ]
        assert response.json()["open_debts"] == []

    def test_staff_and_student_use_club_local_last_valid_and_exclusive_end(
        self,
        club,
        owner_user,
        student_user,
    ):
        club.timezone = "Asia/Yekaterinburg"
        club.save(update_fields=["timezone"])
        student = StudentFactory(club=club, user=student_user, status="active")
        _pending_admission_with_covered_visit(
            club=club,
            student=student,
            start_date=date(2026, 7, 20),
            duration_days=2,
        )

        with patch(
            "apps.clubs.timezones.timezone.now",
            return_value=datetime(2026, 7, 21, 18, 59, 59, tzinfo=UTC),
        ):
            staff = client.get(
                f"/students/{student.id}/",
                **_auth_params(owner_user, club, role="owner"),
            )
            cabinet = client.get(
                "/students/me/financial-state/",
                **_auth_params(student_user, club, role="student"),
            )

        assert staff.status_code == cabinet.status_code == 200
        assert staff.json()["operational_admission"]["checkin_ready"] is True
        assert staff.json()["operational_admission"]["account_access_eligible"] is True
        assert cabinet.json()["operational_admission"]["checkin_ready"] is True

        with patch(
            "apps.clubs.timezones.timezone.now",
            return_value=datetime(2026, 7, 21, 19, 0, tzinfo=UTC),
        ):
            staff = client.get(
                f"/students/{student.id}/",
                **_auth_params(owner_user, club, role="owner"),
            )
            cabinet = client.get(
                "/students/me/financial-state/",
                **_auth_params(student_user, club, role="student"),
            )

        assert staff.json()["operational_admission"]["checkin_ready"] is False
        assert staff.json()["operational_admission"]["account_access_eligible"] is False
        assert cabinet.json()["operational_admission"]["checkin_ready"] is False
        assert cabinet.json()["operational_admission"]["account_access_eligible"] is False

    def test_parent_uses_dst_aware_exclusive_end(self, club, parent_user):
        club.timezone = "America/New_York"
        club.save(update_fields=["timezone"])
        child = StudentFactory(
            club=club,
            parent_user=parent_user,
            is_child=True,
            status="active",
        )
        _pending_admission_with_covered_visit(
            club=club,
            student=child,
            start_date=date(2026, 10, 31),
            duration_days=2,
        )

        with patch(
            "apps.clubs.timezones.timezone.now",
            return_value=datetime(2026, 11, 2, 4, 59, 59, tzinfo=UTC),
        ):
            last_valid = client.get(
                f"/parents/children/{child.id}/",
                **_auth_params(parent_user, club, role="parent"),
            )
        with patch(
            "apps.clubs.timezones.timezone.now",
            return_value=datetime(2026, 11, 2, 5, 0, tzinfo=UTC),
        ):
            expired = client.get(
                f"/parents/children/{child.id}/",
                **_auth_params(parent_user, club, role="parent"),
            )

        assert last_valid.status_code == expired.status_code == 200
        assert last_valid.json()["financial_state"]["operational_admission"]["checkin_ready"] is True
        assert expired.json()["financial_state"]["operational_admission"]["checkin_ready"] is False
        assert (
            expired.json()["financial_state"]["operational_admission"]["account_access_eligible"]
            is False
        )

    @pytest.mark.parametrize(
        "mutation",
        [
            "wrong_training_type_snapshot",
            "wrong_location_snapshot",
            "inactive_schedule",
            "missing_target_occurrence",
        ],
    )
    def test_current_target_incompatibility_removes_readiness(self, club, mutation):
        student = StudentFactory(club=club, status="active")
        payment, _subscription, _enrollment, _debt = _pending_admission_with_covered_visit(
            club=club,
            student=student,
        )
        schedule = payment.target_schedule
        if mutation == "wrong_training_type_snapshot":
            payment.target_training_type_id_snapshot = schedule.training_type_id + 10_000
            payment.save(update_fields=["target_training_type_id_snapshot", "updated_at"])
        elif mutation == "wrong_location_snapshot":
            payment.target_location_id_snapshot = schedule.location_id + 10_000
            payment.save(update_fields=["target_location_id_snapshot", "updated_at"])
        elif mutation == "inactive_schedule":
            schedule.is_active = False
            schedule.save(update_fields=["is_active", "updated_at"])
        else:
            schedule.day_of_week = (payment.target_start_date.weekday() + 1) % 7
            schedule.save(update_fields=["day_of_week", "updated_at"])

        admission = get_manual_operational_admission(club=club, student=student)

        assert admission is not None
        assert admission.is_qualifying is False
        assert admission.checkin_ready is False
        assert admission.account_access_eligible is False

    def test_student_financial_state_shows_covered_visit_without_open_debt(self, club, student_user):
        student = StudentFactory(club=club, user=student_user, status="active")
        payment, _subscription, _enrollment, debt = _pending_admission_with_covered_visit(
            club=club,
            student=student,
        )

        state_response = client.get(
            "/students/me/financial-state/",
            **_auth_params(student_user, club, role="student"),
        )
        debts_response = client.get(
            "/students/me/debts/",
            **_auth_params(student_user, club, role="student"),
        )

        assert state_response.status_code == 200
        state = state_response.json()
        assert state["operational_admission"] == {
            "payment_id": payment.id,
            "payment_status": Payment.Status.PENDING,
            "payment_method": Payment.Method.TRANSFER,
            "subscription_status": Subscription.Status.PENDING,
            "enrollment_status": ScheduleEnrollment.Status.ACTIVE,
            "group_label": payment.target_group_name_snapshot,
            "training_group_id": None,
            "group_membership_id": None,
            "start_date": payment.target_start_date.isoformat(),
            "checkin_ready": True,
            "account_access_eligible": True,
            "covered_visit_count": 1,
        }
        assert state["operational_admissions"] == [state["operational_admission"]]
        assert state["covered_visits"] == [
            {
                "payment_id": payment.id,
                "debt_id": debt.id,
                "checkin_id": debt.checkin_id,
                "training_type_name": debt.checkin.training_type.name,
                "checkin_date": debt.checkin.date.isoformat(),
                "coverage_state": "covered_awaiting_confirmation",
                "is_payable": False,
            }
        ]
        assert "provider_payment_url" not in state["operational_admission"]
        assert debts_response.status_code == 200
        assert debts_response.json() == []

    def test_parent_profile_uses_same_pending_covered_state(self, club, parent_user):
        child = StudentFactory(
            club=club,
            parent_user=parent_user,
            is_child=True,
            status="active",
        )
        payment, _subscription, _enrollment, debt = _pending_admission_with_covered_visit(
            club=club,
            student=child,
        )

        response = client.get(
            f"/parents/children/{child.id}/",
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 200
        state = response.json()["financial_state"]
        assert state["operational_admission"]["payment_id"] == payment.id
        assert state["operational_admissions"] == [state["operational_admission"]]
        assert state["operational_admission"]["account_access_eligible"] is True
        assert state["operational_admission"]["enrollment_status"] == ScheduleEnrollment.Status.ACTIVE
        assert state["covered_visits"] == [
            {
                "payment_id": payment.id,
                "debt_id": debt.id,
                "checkin_id": debt.checkin_id,
                "training_type_name": debt.checkin.training_type.name,
                "checkin_date": debt.checkin.date.isoformat(),
                "coverage_state": "covered_awaiting_confirmation",
                "is_payable": False,
            }
        ]
        assert response.json()["open_debts"] == []

    def test_rejection_keeps_terminal_truth_and_releases_one_payable_debt(self, club, student_user):
        student = StudentFactory(club=club, user=student_user, status="active")
        payment, subscription, enrollment, debt = _pending_admission_with_covered_visit(
            club=club,
            student=student,
        )
        payment.status = Payment.Status.REJECTED
        payment.save(update_fields=["status", "updated_at"])
        subscription.soft_delete()
        enrollment.status = ScheduleEnrollment.Status.CANCELLED
        enrollment.ends_on = enrollment.starts_on
        enrollment.save(update_fields=["status", "ends_on", "updated_at"])
        debt.settlement_payment = None
        debt.save(update_fields=["settlement_payment", "updated_at"])

        state_response = client.get(
            "/students/me/financial-state/",
            **_auth_params(student_user, club, role="student"),
        )
        debts_response = client.get(
            "/students/me/debts/",
            **_auth_params(student_user, club, role="student"),
        )

        assert state_response.status_code == debts_response.status_code == 200
        state = state_response.json()
        assert state["operational_admission"]["payment_status"] == Payment.Status.REJECTED
        assert state["operational_admission"]["subscription_status"] == Subscription.Status.CANCELLED
        assert state["operational_admission"]["enrollment_status"] == ScheduleEnrollment.Status.CANCELLED
        assert state["operational_admission"]["checkin_ready"] is False
        assert state["operational_admission"]["account_access_eligible"] is False
        assert state["operational_admission"]["covered_visit_count"] == 0
        assert state["operational_admissions"] == [state["operational_admission"]]
        assert state["covered_visits"] == []
        assert [item["id"] for item in debts_response.json()] == [debt.id]

    def test_confirmation_keeps_truthful_terminal_state_without_covered_or_open_debt(
        self,
        club,
        student_user,
    ):
        student = StudentFactory(club=club, user=student_user, status="active")
        payment, subscription, enrollment, debt = _pending_admission_with_covered_visit(
            club=club,
            student=student,
        )
        payment.status = Payment.Status.CONFIRMED
        payment.save(update_fields=["status", "updated_at"])
        subscription.status = Subscription.Status.ACTIVE
        subscription.paid_amount = Decimal("5000")
        subscription.save(update_fields=["status", "paid_amount", "updated_at"])
        debt.resolved_at = timezone.now()
        debt.save(update_fields=["resolved_at", "updated_at"])

        state_response = client.get(
            "/students/me/financial-state/",
            **_auth_params(student_user, club, role="student"),
        )
        debts_response = client.get(
            "/students/me/debts/",
            **_auth_params(student_user, club, role="student"),
        )

        assert state_response.status_code == debts_response.status_code == 200
        state = state_response.json()
        admission = state["operational_admission"]
        assert admission["payment_status"] == Payment.Status.CONFIRMED
        assert admission["subscription_status"] == Subscription.Status.ACTIVE
        assert admission["enrollment_status"] == enrollment.status
        assert admission["account_access_eligible"] is False
        assert state["operational_admissions"] == [admission]
        assert state["covered_visits"] == []
        assert debts_response.json() == []

    @pytest.mark.parametrize("terminal", ["confirmed", "rejected"])
    def test_parent_profile_reports_confirmed_and_rejected_terminal_truth(
        self,
        club,
        parent_user,
        terminal,
    ):
        child = StudentFactory(
            club=club,
            parent_user=parent_user,
            is_child=True,
            status="active",
        )
        payment, subscription, enrollment, debt = _pending_admission_with_covered_visit(
            club=club,
            student=child,
        )
        if terminal == "confirmed":
            payment.status = Payment.Status.CONFIRMED
            payment.save(update_fields=["status", "updated_at"])
            subscription.status = Subscription.Status.ACTIVE
            subscription.paid_amount = Decimal("5000")
            subscription.save(update_fields=["status", "paid_amount", "updated_at"])
            debt.resolved_at = timezone.now()
            debt.save(update_fields=["resolved_at", "updated_at"])
            expected_payment = Payment.Status.CONFIRMED
            expected_subscription = Subscription.Status.ACTIVE
            expected_enrollment = ScheduleEnrollment.Status.ACTIVE
            expected_open_debts = []
        else:
            payment.status = Payment.Status.REJECTED
            payment.save(update_fields=["status", "updated_at"])
            subscription.soft_delete()
            enrollment.status = ScheduleEnrollment.Status.CANCELLED
            enrollment.ends_on = enrollment.starts_on
            enrollment.save(update_fields=["status", "ends_on", "updated_at"])
            debt.settlement_payment = None
            debt.save(update_fields=["settlement_payment", "updated_at"])
            expected_payment = Payment.Status.REJECTED
            expected_subscription = Subscription.Status.CANCELLED
            expected_enrollment = ScheduleEnrollment.Status.CANCELLED
            expected_open_debts = [debt.id]

        response = client.get(
            f"/parents/children/{child.id}/",
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 200
        profile = response.json()
        admission = profile["financial_state"]["operational_admission"]
        assert admission["payment_status"] == expected_payment
        assert admission["subscription_status"] == expected_subscription
        assert admission["enrollment_status"] == expected_enrollment
        assert admission["account_access_eligible"] is False
        assert profile["financial_state"]["operational_admissions"] == [admission]
        assert profile["financial_state"]["covered_visits"] == []
        assert [item["id"] for item in profile["open_debts"]] == expected_open_debts
