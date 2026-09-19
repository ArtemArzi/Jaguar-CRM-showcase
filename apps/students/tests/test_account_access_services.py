from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.attendance.models import ScheduleEnrollment
from apps.attendance.tests.factories import ScheduleFactory
from apps.billing.models import Payment, Subscription, TrainingType
from apps.billing.tests.factories import (
    PaymentFactory,
    SubscriptionFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.clubs.models import ClubMembership
from apps.clubs.tests.factories import ClubFactory, ClubMembershipFactory, UserFactory
from apps.common.exceptions import BusinessLogicError
from apps.students.access_services import (
    open_account_access_for_student,
    open_parent_access,
    open_student_access,
    reset_account_access,
    reset_account_access_for_student,
)
from apps.students.models import AccountAccess, Student
from apps.students.tests.factories import StudentFactory

User = get_user_model()


def _paid_active_subscription(*, club, student):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    tariff = TariffFactory(training_type=training_type, price=Decimal("5000"))
    return SubscriptionFactory(
        tariff=tariff,
        student=student,
        status=Subscription.Status.ACTIVE,
        paid_amount=Decimal("5000"),
    )


def _pending_manual_operational_admission(
    *,
    club,
    student,
    recorded_by=None,
    start_date: date | None = None,
    duration_days: int = 30,
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
        payment_method=Payment.Method.CASH,
        status=Payment.Status.PENDING,
        target_schedule=schedule,
        target_start_date=start_date,
        conversion_enrollment=enrollment,
        target_group_name_snapshot=schedule.group_name,
        target_location_id_snapshot=schedule.location_id,
        target_location_name_snapshot=schedule.location.name,
        target_training_type_id_snapshot=training_type.id,
        target_training_type_kind_snapshot=TrainingType.Kind.GROUP,
        recorded_by=recorded_by or UserFactory(),
    )
    return payment


@pytest.mark.django_db
class TestOpenStudentAccess:
    def test_open_student_access_requires_paid_active_subscription(self, club):
        student = StudentFactory(
            club=club,
            is_child=False,
            status=Student.Status.ACTIVE,
            phone="8 (900) 123-45-67",
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            open_student_access(
                club_id=club.id,
                student_id=student.id,
                issued_by_id=None,
            )

        assert exc_info.value.code == "account_access_requires_paid_subscription"
        assert not AccountAccess.objects.for_club(club).filter(student=student).exists()

    def test_open_adult_student_access_creates_user_membership_and_one_time_password(self, club):
        issuer = UserFactory()
        student = StudentFactory(
            club=club,
            is_child=False,
            status=Student.Status.ACTIVE,
            phone="8 (900) 123-45-67",
        )
        _paid_active_subscription(club=club, student=student)

        result = open_student_access(
            club_id=club.id,
            student_id=student.id,
            issued_by_id=issuer.id,
        )

        student.refresh_from_db()
        assert result.user == student.user
        assert result.created_user is True
        assert result.created_membership is True
        assert result.created_access is True
        assert result.temporary_password is not None
        assert result.user.username == "+79001234567"
        assert result.user.check_password(result.temporary_password)
        assert ClubMembership.objects.filter(
            user=result.user,
            club=club,
            role=ClubMembership.Role.STUDENT,
            is_active=True,
        ).count() == 1
        access = AccountAccess.objects.for_club(club).get(
            student=student,
            user=result.user,
            role=ClubMembership.Role.STUDENT,
        )
        assert access.must_change_password is True
        assert access.temporary_credential_revealed_at is not None

    def test_open_student_access_is_idempotent_without_rotating_password(self, club):
        student = StudentFactory(
            club=club,
            is_child=False,
            status=Student.Status.ACTIVE,
            phone="+7 900 123-45-67",
        )
        _paid_active_subscription(club=club, student=student)

        first = open_student_access(
            club_id=club.id,
            student_id=student.id,
            issued_by_id=None,
        )
        second = open_student_access(
            club_id=club.id,
            student_id=student.id,
            issued_by_id=None,
        )

        assert second.user == first.user
        assert second.temporary_password is None
        assert User.objects.filter(username="+79001234567").count() == 1
        assert ClubMembership.objects.filter(user=first.user, club=club).count() == 1
        assert AccountAccess.objects.for_club(club).filter(
            student=student,
            role=ClubMembership.Role.STUDENT,
        ).count() == 1
        assert first.user.check_password(first.temporary_password)

    def test_open_student_access_reuses_same_role_user_without_duplicate_membership(self, club):
        user = UserFactory(username="+79001234567")
        ClubMembershipFactory(user=user, club=club, role=ClubMembership.Role.STUDENT)
        student = StudentFactory(
            club=club,
            is_child=False,
            status=Student.Status.ACTIVE,
            phone="8 900 123 45 67",
        )
        _paid_active_subscription(club=club, student=student)

        result = open_student_access(
            club_id=club.id,
            student_id=student.id,
            issued_by_id=None,
        )

        student.refresh_from_db()
        assert result.user == user
        assert student.user == user
        assert result.created_user is False
        assert result.created_membership is False
        assert result.temporary_password is None
        assert ClubMembership.objects.filter(user=user, club=club).count() == 1

    def test_open_student_access_requires_manual_review_for_same_club_role_conflict(self, club):
        user = UserFactory(username="+79001234567")
        ClubMembershipFactory(user=user, club=club, role=ClubMembership.Role.PARENT)
        student = StudentFactory(
            club=club,
            is_child=False,
            status=Student.Status.ACTIVE,
            phone="8 900 123 45 67",
        )
        _paid_active_subscription(club=club, student=student)

        with pytest.raises(BusinessLogicError) as exc_info:
            open_student_access(
                club_id=club.id,
                student_id=student.id,
                issued_by_id=None,
            )

        assert exc_info.value.code == "manual_review_required"
        student.refresh_from_db()
        assert student.user_id is None
        assert not AccountAccess.objects.for_club(club).filter(student=student).exists()

    def test_open_student_access_requires_manual_review_for_other_active_club(self, club):
        other_club = ClubFactory()
        user = UserFactory(username="+79001234567")
        ClubMembershipFactory(user=user, club=other_club, role=ClubMembership.Role.STUDENT)
        student = StudentFactory(
            club=club,
            is_child=False,
            status=Student.Status.ACTIVE,
            phone="8 900 123 45 67",
        )
        _paid_active_subscription(club=club, student=student)

        with pytest.raises(BusinessLogicError) as exc_info:
            open_student_access(
                club_id=club.id,
                student_id=student.id,
                issued_by_id=None,
            )

        assert exc_info.value.code == "manual_review_required"
        assert not ClubMembership.objects.filter(user=user, club=club).exists()

    def test_open_student_access_rejects_wrong_club_student_before_user_creation(self, club, other_club):
        student = StudentFactory(
            club=other_club,
            is_child=False,
            status=Student.Status.ACTIVE,
            phone="8 900 123 45 67",
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            open_student_access(
                club_id=club.id,
                student_id=student.id,
                issued_by_id=None,
            )

        assert exc_info.value.code == "student_not_found"
        assert not User.objects.filter(username="+79001234567").exists()

    def test_open_student_access_rejects_deleted_student(self, club):
        student = StudentFactory(
            club=club,
            is_child=False,
            status=Student.Status.ACTIVE,
            phone="8 900 123 45 67",
        )
        student.soft_delete()

        with pytest.raises(BusinessLogicError) as exc_info:
            open_student_access(
                club_id=club.id,
                student_id=student.id,
                issued_by_id=None,
            )

        assert exc_info.value.code == "student_not_found"
        assert not User.objects.filter(username="+79001234567").exists()

    def test_open_student_access_rejects_lost_student(self, club):
        student = StudentFactory(
            club=club,
            is_child=False,
            status=Student.Status.LOST,
            phone="8 900 123 45 67",
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            open_student_access(
                club_id=club.id,
                student_id=student.id,
                issued_by_id=None,
            )

        assert exc_info.value.code == "student_not_accessible"
        assert not User.objects.filter(username="+79001234567").exists()

    def test_open_student_access_rejects_child_branch(self, club):
        child = StudentFactory(
            club=club,
            is_child=True,
            status=Student.Status.ACTIVE,
            phone="8 900 123 45 67",
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            open_student_access(
                club_id=club.id,
                student_id=child.id,
                issued_by_id=None,
            )

        assert exc_info.value.code == "child_requires_parent_access"
        child.refresh_from_db()
        assert child.user_id is None


@pytest.mark.django_db
class TestPendingManualOperationalAdmissionAccess:
    def test_newer_rejected_admission_does_not_mask_older_live_access(self, club):
        student = StudentFactory(
            club=club,
            is_child=False,
            status=Student.Status.ACTIVE,
            phone="8 900 123 45 67",
        )
        live_payment = _pending_manual_operational_admission(club=club, student=student)
        rejected_payment = _pending_manual_operational_admission(club=club, student=student)
        rejected_payment.status = Payment.Status.REJECTED
        rejected_payment.save(update_fields=["status", "updated_at"])
        rejected_payment.subscription.soft_delete()
        rejected_payment.conversion_enrollment.status = ScheduleEnrollment.Status.CANCELLED
        rejected_payment.conversion_enrollment.ends_on = (
            rejected_payment.conversion_enrollment.starts_on
        )
        rejected_payment.conversion_enrollment.save(
            update_fields=["status", "ends_on", "updated_at"]
        )

        result = open_account_access_for_student(
            club_id=club.id,
            student_id=student.id,
            issued_by_id=live_payment.recorded_by_id,
        )

        assert rejected_payment.id > live_payment.id
        assert result.created_access is True
        assert result.temporary_password is not None

    def test_pre_start_admission_allows_explicit_access(self, club):
        student = StudentFactory(
            club=club,
            is_child=False,
            status=Student.Status.ACTIVE,
            phone="8 900 123 45 67",
        )
        payment = _pending_manual_operational_admission(
            club=club,
            student=student,
            start_date=timezone.localdate() + timedelta(days=1),
        )

        result = open_account_access_for_student(
            club_id=club.id,
            student_id=student.id,
            issued_by_id=payment.recorded_by_id,
        )

        assert result.created_access is True

    def test_account_access_expires_at_dst_aware_club_local_boundary(self, club):
        club.timezone = "America/New_York"
        club.save(update_fields=["timezone"])
        last_valid_student = StudentFactory(
            club=club,
            is_child=False,
            status=Student.Status.ACTIVE,
            phone="8 900 123 45 67",
        )
        expired_student = StudentFactory(
            club=club,
            is_child=False,
            status=Student.Status.ACTIVE,
            phone="8 900 123 45 68",
        )
        last_valid_payment = _pending_manual_operational_admission(
            club=club,
            student=last_valid_student,
            start_date=date(2026, 10, 31),
            duration_days=2,
        )
        expired_payment = _pending_manual_operational_admission(
            club=club,
            student=expired_student,
            start_date=date(2026, 10, 31),
            duration_days=2,
        )

        with patch(
            "apps.clubs.timezones.timezone.now",
            return_value=datetime(2026, 11, 2, 4, 59, 59, tzinfo=UTC),
        ):
            result = open_account_access_for_student(
                club_id=club.id,
                student_id=last_valid_student.id,
                issued_by_id=last_valid_payment.recorded_by_id,
            )
        assert result.created_access is True

        with patch(
            "apps.clubs.timezones.timezone.now",
            return_value=datetime(2026, 11, 2, 5, 0, tzinfo=UTC),
        ):
            with pytest.raises(BusinessLogicError) as exc_info:
                open_account_access_for_student(
                    club_id=club.id,
                    student_id=expired_student.id,
                    issued_by_id=expired_payment.recorded_by_id,
                )

        assert exc_info.value.code == "account_access_requires_paid_subscription"

    def test_open_access_accepts_exact_pending_manual_admission(self, club):
        student = StudentFactory(
            club=club,
            is_child=False,
            status=Student.Status.ACTIVE,
            phone="8 900 123 45 67",
        )
        payment = _pending_manual_operational_admission(club=club, student=student)

        result = open_account_access_for_student(
            club_id=club.id,
            student_id=student.id,
            issued_by_id=payment.recorded_by_id,
        )

        assert result.created_access is True
        assert result.temporary_password is not None
        assert result.user.check_password(result.temporary_password)

    @pytest.mark.parametrize(
        ("mutate", "expected_code"),
        [
            ("online", "account_access_requires_paid_subscription"),
            ("wrong_target", "account_access_requires_paid_subscription"),
            ("rejected", "account_access_requires_paid_subscription"),
            ("source_missing", "account_access_requires_paid_subscription"),
        ],
    )
    def test_open_access_rejects_nonqualifying_manual_rows(self, club, mutate, expected_code):
        student = StudentFactory(
            club=club,
            is_child=False,
            status=Student.Status.ACTIVE,
            phone="8 900 123 45 67",
        )
        payment = _pending_manual_operational_admission(club=club, student=student)
        if mutate == "online":
            payment.payment_method = Payment.Method.ONLINE
            payment.save(update_fields=["payment_method", "updated_at"])
        elif mutate == "wrong_target":
            payment.target_start_date += timedelta(days=1)
            payment.save(update_fields=["target_start_date", "updated_at"])
        elif mutate == "rejected":
            payment.status = Payment.Status.REJECTED
            payment.save(update_fields=["status", "updated_at"])
        else:
            payment.target_training_type_kind_snapshot = ""
            payment.save(update_fields=["target_training_type_kind_snapshot", "updated_at"])

        with pytest.raises(BusinessLogicError) as exc_info:
            open_account_access_for_student(
                club_id=club.id,
                student_id=student.id,
                issued_by_id=payment.recorded_by_id,
            )

        assert exc_info.value.code == expected_code
        assert not AccountAccess.objects.for_club(club).filter(student=student).exists()

    def test_open_access_rejects_foreign_manual_payment(self, club, other_club):
        student = StudentFactory(
            club=club,
            is_child=False,
            status=Student.Status.ACTIVE,
            phone="8 900 123 45 67",
        )
        _pending_manual_operational_admission(club=other_club, student=student)

        with pytest.raises(BusinessLogicError) as exc_info:
            open_account_access_for_student(
                club_id=club.id,
                student_id=student.id,
                issued_by_id=None,
            )

        assert exc_info.value.code == "account_access_requires_paid_subscription"

    def test_new_pending_admission_after_rejection_is_the_qualifying_row(self, club):
        student = StudentFactory(
            club=club,
            is_child=False,
            status=Student.Status.ACTIVE,
            phone="8 900 123 45 67",
        )
        rejected = _pending_manual_operational_admission(club=club, student=student)
        rejected.status = Payment.Status.REJECTED
        rejected.save(update_fields=["status", "updated_at"])
        rejected.subscription.soft_delete()
        rejected.conversion_enrollment.status = ScheduleEnrollment.Status.CANCELLED
        rejected.conversion_enrollment.ends_on = rejected.conversion_enrollment.starts_on
        rejected.conversion_enrollment.save(
            update_fields=["status", "ends_on", "updated_at"]
        )
        replacement = _pending_manual_operational_admission(club=club, student=student)

        result = open_account_access_for_student(
            club_id=club.id,
            student_id=student.id,
            issued_by_id=replacement.recorded_by_id,
        )

        assert result.created_access is True


@pytest.mark.django_db
class TestOpenAccountAccessForStudent:
    def test_open_account_access_requires_paid_active_subscription(self, club):
        student = StudentFactory(
            club=club,
            is_child=False,
            status=Student.Status.ACTIVE,
            phone="8 900 123 45 67",
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            open_account_access_for_student(
                club_id=club.id,
                student_id=student.id,
                issued_by_id=None,
            )

        assert exc_info.value.code == "account_access_requires_paid_subscription"
        assert not AccountAccess.objects.for_club(club).filter(student=student).exists()

    def test_open_account_access_for_paid_adult_returns_one_time_password(self, club):
        student = StudentFactory(
            club=club,
            is_child=False,
            status=Student.Status.ACTIVE,
            phone="8 900 123 45 67",
        )
        _paid_active_subscription(club=club, student=student)

        result = open_account_access_for_student(
            club_id=club.id,
            student_id=student.id,
            issued_by_id=None,
        )

        student.refresh_from_db()
        assert student.user_id == result.user.id
        assert result.created_access is True
        assert result.temporary_password is not None
        assert result.user.check_password(result.temporary_password)

    def test_open_account_access_for_child_requires_parent_phone_without_linked_parent(self, club):
        child = StudentFactory(
            club=club,
            is_child=True,
            status=Student.Status.ACTIVE,
            phone="8 900 123 45 67",
        )
        _paid_active_subscription(club=club, student=child)

        with pytest.raises(BusinessLogicError) as exc_info:
            open_account_access_for_student(
                club_id=club.id,
                student_id=child.id,
                issued_by_id=None,
            )

        assert exc_info.value.code == "parent_phone_required"
        child.refresh_from_db()
        assert child.parent_user_id is None

    def test_open_account_access_for_child_validates_parent_phone_before_paid_subscription(self, club):
        child = StudentFactory(
            club=club,
            is_child=True,
            status=Student.Status.ACTIVE,
            phone="8 900 123 45 67",
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            open_account_access_for_student(
                club_id=club.id,
                student_id=child.id,
                issued_by_id=None,
            )

        assert exc_info.value.code == "parent_phone_required"
        assert not AccountAccess.objects.for_club(club).filter(student=child).exists()

    def test_open_account_access_for_child_uses_existing_parent_without_parent_phone(self, club):
        parent = UserFactory(username="+79012223344")
        parent.set_unusable_password()
        parent.save(update_fields=["password"])
        child = StudentFactory(
            club=club,
            is_child=True,
            status=Student.Status.ACTIVE,
            phone="8 900 123 45 67",
            parent_user=parent,
        )
        _paid_active_subscription(club=club, student=child)

        result = open_account_access_for_student(
            club_id=club.id,
            student_id=child.id,
            issued_by_id=None,
        )

        parent.refresh_from_db()
        assert result.user == parent
        assert result.temporary_password is not None
        assert parent.check_password(result.temporary_password)
        assert ClubMembership.objects.filter(
            user=parent,
            club=club,
            role=ClubMembership.Role.PARENT,
            is_active=True,
        ).count() == 1

    def test_reset_account_access_rotates_temporary_password_and_marks_reset(self, club):
        student = StudentFactory(
            club=club,
            is_child=False,
            status=Student.Status.ACTIVE,
            phone="8 900 123 45 67",
        )
        _paid_active_subscription(club=club, student=student)
        opened = open_account_access_for_student(
            club_id=club.id,
            student_id=student.id,
            issued_by_id=None,
        )

        reset = reset_account_access_for_student(
            club_id=club.id,
            student_id=student.id,
            reset_by_id=None,
        )

        opened.user.refresh_from_db()
        assert reset.user == opened.user
        assert reset.temporary_password is not None
        assert reset.temporary_password != opened.temporary_password
        assert opened.user.check_password(reset.temporary_password)
        assert reset.access.status == AccountAccess.Status.RESET
        assert reset.access.reset_at is not None
        assert reset.access.must_change_password is True

    def test_direct_reset_account_access_requires_paid_active_subscription(self, club):
        student = StudentFactory(
            club=club,
            is_child=False,
            status=Student.Status.ACTIVE,
            phone="8 900 123 45 67",
        )
        subscription = _paid_active_subscription(club=club, student=student)
        opened = open_account_access_for_student(
            club_id=club.id,
            student_id=student.id,
            issued_by_id=None,
        )
        original_password_hash = opened.user.password
        subscription.status = Subscription.Status.EXPIRED
        subscription.save(update_fields=["status", "updated_at"])

        with pytest.raises(BusinessLogicError) as exc_info:
            reset_account_access(
                club_id=club.id,
                student_id=student.id,
                role=AccountAccess.Role.STUDENT,
                reset_by_id=None,
            )

        assert exc_info.value.code == "account_access_requires_paid_subscription"
        opened.user.refresh_from_db()
        opened.access.refresh_from_db()
        assert opened.user.password == original_password_hash
        assert opened.access.status == AccountAccess.Status.OPEN


@pytest.mark.django_db
class TestOpenParentAccess:
    def test_open_parent_access_requires_paid_active_subscription(self, club):
        child = StudentFactory(
            club=club,
            is_child=True,
            status=Student.Status.ACTIVE,
            phone="+79001111111",
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            open_parent_access(
                club_id=club.id,
                student_id=child.id,
                parent_phone="8 901 222 33 44",
                issued_by_id=None,
            )

        assert exc_info.value.code == "account_access_requires_paid_subscription"
        assert not AccountAccess.objects.for_club(club).filter(student=child).exists()

    def test_open_parent_access_for_child_links_parent_user(self, club):
        child = StudentFactory(
            club=club,
            is_child=True,
            status=Student.Status.ACTIVE,
            phone="+79001111111",
        )
        _paid_active_subscription(club=club, student=child)

        result = open_parent_access(
            club_id=club.id,
            student_id=child.id,
            parent_phone="8 (901) 222-33-44",
            issued_by_id=None,
        )

        child.refresh_from_db()
        assert child.user_id is None
        assert child.parent_user == result.user
        assert result.user.username == "+79012223344"
        assert result.temporary_password is not None
        assert result.user.check_password(result.temporary_password)
        assert ClubMembership.objects.filter(
            user=result.user,
            club=club,
            role=ClubMembership.Role.PARENT,
            is_active=True,
        ).count() == 1
        assert AccountAccess.objects.for_club(club).filter(
            student=child,
            user=result.user,
            role=ClubMembership.Role.PARENT,
        ).count() == 1

    def test_open_parent_access_is_idempotent_without_duplicate_membership(self, club):
        child = StudentFactory(
            club=club,
            is_child=True,
            status=Student.Status.ACTIVE,
            phone="+79001111111",
        )
        _paid_active_subscription(club=club, student=child)

        first = open_parent_access(
            club_id=club.id,
            student_id=child.id,
            parent_phone="8 901 222 33 44",
            issued_by_id=None,
        )
        second = open_parent_access(
            club_id=club.id,
            student_id=child.id,
            parent_phone="+7 901 222 33 44",
            issued_by_id=None,
        )

        assert second.user == first.user
        assert second.temporary_password is None
        assert User.objects.filter(username="+79012223344").count() == 1
        assert ClubMembership.objects.filter(user=first.user, club=club).count() == 1
        assert AccountAccess.objects.for_club(club).filter(
            student=child,
            role=ClubMembership.Role.PARENT,
        ).count() == 1

    def test_open_parent_access_rejects_non_child_branch(self, club):
        adult = StudentFactory(
            club=club,
            is_child=False,
            status=Student.Status.ACTIVE,
            phone="8 900 123 45 67",
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            open_parent_access(
                club_id=club.id,
                student_id=adult.id,
                parent_phone="8 901 222 33 44",
                issued_by_id=None,
            )

        assert exc_info.value.code == "not_child"
        adult.refresh_from_db()
        assert adult.parent_user_id is None

    def test_open_parent_access_requires_manual_review_for_role_conflict(self, club):
        user = UserFactory(username="+79012223344")
        ClubMembershipFactory(user=user, club=club, role=ClubMembership.Role.STUDENT)
        child = StudentFactory(
            club=club,
            is_child=True,
            status=Student.Status.ACTIVE,
            phone="+79001111111",
        )
        _paid_active_subscription(club=club, student=child)

        with pytest.raises(BusinessLogicError) as exc_info:
            open_parent_access(
                club_id=club.id,
                student_id=child.id,
                parent_phone="8 901 222 33 44",
                issued_by_id=None,
            )

        assert exc_info.value.code == "manual_review_required"
        child.refresh_from_db()
        assert child.parent_user_id is None
        assert not AccountAccess.objects.for_club(club).filter(student=child).exists()

    def test_open_parent_access_rejects_invalid_parent_phone(self, club):
        child = StudentFactory(
            club=club,
            is_child=True,
            status=Student.Status.ACTIVE,
            phone="+79001111111",
        )
        _paid_active_subscription(club=club, student=child)

        with pytest.raises(BusinessLogicError) as exc_info:
            open_parent_access(
                club_id=club.id,
                student_id=child.id,
                parent_phone="123",
                issued_by_id=None,
            )

        assert exc_info.value.code == "invalid_phone"
        assert not User.objects.filter(username="123").exists()

    def test_open_parent_access_does_not_silently_rotate_existing_access_password(self, club):
        user = UserFactory(username="+79012223344")
        user.set_unusable_password()
        user.save(update_fields=["password"])
        child = StudentFactory(
            club=club,
            is_child=True,
            status=Student.Status.ACTIVE,
            phone="+79001111111",
            parent_user=user,
        )
        _paid_active_subscription(club=club, student=child)
        AccountAccess.objects.create(
            club=club,
            student=child,
            user=user,
            role=ClubMembership.Role.PARENT,
            must_change_password=False,
        )

        result = open_parent_access(
            club_id=club.id,
            student_id=child.id,
            parent_phone="8 901 222 33 44",
            issued_by_id=None,
        )

        user.refresh_from_db()
        assert result.temporary_password is None
        assert not user.has_usable_password()
        assert result.created_membership is True
        assert ClubMembership.objects.filter(
            user=user,
            club=club,
            role=ClubMembership.Role.PARENT,
            is_active=True,
        ).count() == 1
