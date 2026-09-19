from __future__ import annotations

from django.db.models import Exists, F, OuterRef, Q
from django.utils import timezone
from ninja.errors import HttpError

from apps.billing.models import Payment, Subscription, TrainingType
from apps.clubs.models import ClubMembership
from apps.students.models import Student
from apps.students.selectors import has_qualifying_manual_operational_admission


def _active_package_subscription_filter(now=None) -> Q:
    now = now or timezone.now()
    return (
        Q(trainer_package_allocations__subscription__status=Subscription.Status.ACTIVE)
        & Q(trainer_package_allocations__subscription__deleted_at__isnull=True)
        & (
            Q(trainer_package_allocations__subscription__expires_at__isnull=True)
            | Q(trainer_package_allocations__subscription__expires_at__gt=now)
        )
        & (
            Q(trainer_package_allocations__subscription__trainings_left__isnull=True)
            | Q(trainer_package_allocations__subscription__trainings_left__gt=0)
        )
    )


def trainer_package_owner_student_scope_filter(trainer_id: int) -> Q:
    return Q(
        trainer_package_allocations__owner_trainer_id=trainer_id,
        trainer_package_allocations__is_active=True,
    ) & _active_package_subscription_filter()


def trainer_student_account_access_scope_filter(trainer_id: int) -> Q:
    return Q(assigned_trainer_id=trainer_id) | trainer_package_owner_student_scope_filter(trainer_id)


def trainer_open_group_enrollment_student_scope_filter(trainer_id: int) -> Q:
    return Q(
        schedule_enrollments__schedule__trainer_id=trainer_id,
        schedule_enrollments__schedule__is_active=True,
        schedule_enrollments__schedule__one_time_date__isnull=True,
        schedule_enrollments__schedule__training_type__kind="group",
        schedule_enrollments__status__in=["active", "frozen"],
        schedule_enrollments__created_from__in=[
            "manual",
            "paid_conversion",
            "group_projection",
            "import",
        ],
        schedule_enrollments__ends_on__isnull=True,
    )


def trainer_student_scope_filter(trainer_id: int) -> Q:
    return (
        trainer_student_account_access_scope_filter(trainer_id)
        | trainer_open_group_enrollment_student_scope_filter(trainer_id)
        | Q(
            checkins__trainer_id=trainer_id,
            checkins__deleted_at__isnull=True,
            checkins__cancelled_at__isnull=True,
        )
    )


def trainer_manual_operational_admission_scope_filter(*, club, trainer_id: int, user_id: int) -> Q:
    """Return only the recorder-owned exact pending manual admissions.

    The regular operational scope intentionally follows assignment, package
    ownership, group roster, and check-ins.  A new manual admission has none
    of those relationships yet, so its recorder needs this narrowly proven
    bridge until payment review reaches a terminal state.
    """

    from apps.attendance.models import (
        PersonalDropInBooking,
        PersonalDropInPaymentLink,
        PersonalServiceTermsSnapshot,
        ScheduleEnrollment,
        TrainingGroupMembership,
        complete_personal_terms_queryset,
    )
    from apps.trainers.models import Trainer

    if not Trainer.objects.for_club(club).filter(id=trainer_id, user_id=user_id, is_active=True).exists():
        return Q(pk__in=Student.objects.none())

    complete_terms = complete_personal_terms_queryset(
        PersonalServiceTermsSnapshot.objects.for_club(club).filter(
            booking_id=OuterRef("booking_id"),
            tariff_id_snapshot=OuterRef("payment__tariff_id"),
            payable_amount=OuterRef("payment__amount"),
        )
    )
    personal_student_ids = (
        PersonalDropInPaymentLink.objects.for_club(club)
        .filter(
            payment__recorded_by_id=user_id,
            payment__deleted_at__isnull=True,
            payment__payment_method__in=[Payment.Method.CASH, Payment.Method.TRANSFER],
            payment__status=Payment.Status.PENDING,
            payment__subscription__deleted_at__isnull=True,
            payment__subscription__status=Subscription.Status.PENDING,
            payment__subscription__student_id=F("payment__student_id"),
            payment__subscription__tariff_id=F("payment__tariff_id"),
            booking__state__in=[PersonalDropInBooking.State.SCHEDULED, PersonalDropInBooking.State.ATTENDED],
            booking__enrollment__student_id=F("payment__student_id"),
            booking__enrollment__status=ScheduleEnrollment.Status.ACTIVE,
        )
        .annotate(_has_complete_terms=Exists(complete_terms))
        .filter(_has_complete_terms=True)
        .values("payment__student_id")
    )
    group_student_ids = (
        Payment.objects.for_club(club)
        .filter(
            recorded_by_id=user_id,
            deleted_at__isnull=True,
            payment_method__in=[Payment.Method.CASH, Payment.Method.TRANSFER],
            status=Payment.Status.PENDING,
            subscription__deleted_at__isnull=True,
            subscription__status=Subscription.Status.PENDING,
            subscription__student_id=F("student_id"),
            subscription__tariff_id=F("tariff_id"),
            tariff__training_type__kind=TrainingType.Kind.GROUP,
            target_training_type_kind_snapshot=TrainingType.Kind.GROUP,
            target_schedule__is_active=True,
            target_schedule__one_time_date__isnull=True,
            target_schedule__training_type_id=F("tariff__training_type_id"),
            target_start_date__isnull=False,
            conversion_enrollment__student_id=F("student_id"),
            conversion_enrollment__schedule_id=F("target_schedule_id"),
            conversion_enrollment__starts_on=F("target_start_date"),
            conversion_enrollment__status=ScheduleEnrollment.Status.ACTIVE,
            conversion_enrollment__ends_on__isnull=True,
        )
        .filter(
            Q(
                conversion_group_membership__isnull=False,
                group_membership_action_snapshot=Payment.GroupMembershipActionSnapshot.NEW_ADMISSION,
                target_group_membership_id=F("conversion_group_membership_id"),
                target_training_group_id=F("conversion_group_membership__training_group_id"),
                conversion_group_membership__authority=TrainingGroupMembership.Authority.PAYMENT_OWNED,
                conversion_group_membership__student_id=F("student_id"),
                conversion_group_membership__starts_on=F("target_start_date"),
                conversion_enrollment__training_group_membership_id=F("conversion_group_membership_id"),
                conversion_enrollment__created_from=ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION,
            )
            | Q(
                conversion_group_membership__isnull=True,
                conversion_enrollment__created_from=ScheduleEnrollment.CreatedFrom.PAID_CONVERSION,
            )
        )
        .values("student_id")
    )
    candidate_ids = set(personal_student_ids.values_list("payment__student_id", flat=True))
    candidate_ids.update(group_student_ids.values_list("student_id", flat=True))
    qualifying_ids = [
        student.id
        for student in Student.objects.for_club(club).filter(
            id__in=candidate_ids,
            deleted_at__isnull=True,
        )
        if has_qualifying_manual_operational_admission(
            club=club,
            student=student,
            recorded_by_id=user_id,
        )
    ]
    return Q(status=Student.Status.ACTIVE, id__in=qualifying_ids)


def get_current_trainer_id_for_user(*, club, user) -> int:
    from apps.trainers.models import Trainer
    from apps.trainers.selectors import get_trainer_for_user

    try:
        trainer = get_trainer_for_user(club=club, user=user)
    except Trainer.DoesNotExist:
        raise HttpError(403, "Trainer profile not found")
    return trainer.id


def actor_is_scoped_to_student(*, club, membership_role: str, user, student_id: int) -> bool:
    if membership_role != ClubMembership.Role.TRAINER:
        return True

    trainer_id = get_current_trainer_id_for_user(club=club, user=user)
    generally_scoped = (
        Student.objects.for_club(club)
        .filter(id=student_id, deleted_at__isnull=True)
        .filter(trainer_student_scope_filter(trainer_id))
        .exists()
    )
    if generally_scoped:
        return True

    return False


def actor_can_read_student_detail(*, club, membership_role: str, user, student_id: int) -> bool:
    if actor_is_scoped_to_student(
        club=club,
        membership_role=membership_role,
        user=user,
        student_id=student_id,
    ):
        return True
    if membership_role != ClubMembership.Role.TRAINER:
        return True

    student = Student.objects.for_club(club).filter(id=student_id, deleted_at__isnull=True).first()
    return bool(
        student
        and has_qualifying_manual_operational_admission(
            club=club,
            student=student,
            recorded_by_id=user.id,
        )
    )


def actor_can_manage_student_sensitive_actions(*, club, membership_role: str, user, student_id: int) -> bool:
    if membership_role != ClubMembership.Role.TRAINER:
        return True

    trainer_id = get_current_trainer_id_for_user(club=club, user=user)
    generally_scoped = (
        Student.objects.for_club(club)
        .filter(id=student_id, deleted_at__isnull=True)
        .filter(trainer_student_account_access_scope_filter(trainer_id))
        .exists()
    )
    if generally_scoped:
        return True

    student = Student.objects.for_club(club).filter(id=student_id, deleted_at__isnull=True).first()
    return bool(
        student
        and has_qualifying_manual_operational_admission(
            club=club,
            student=student,
            recorded_by_id=user.id,
        )
    )


def actor_can_manage_student_account_access(*, club, membership_role: str, user, student_id: int) -> bool:
    return actor_can_manage_student_sensitive_actions(
        club=club,
        membership_role=membership_role,
        user=user,
        student_id=student_id,
    )


def assert_actor_is_scoped_to_student(
    *,
    club,
    membership_role: str,
    user,
    student_id: int,
    message: str = "Access denied: not your student",
) -> None:
    if not actor_is_scoped_to_student(
        club=club,
        membership_role=membership_role,
        user=user,
        student_id=student_id,
    ):
        raise HttpError(403, message)
