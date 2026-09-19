from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from django.db.models import Case, CharField, Exists, F, OuterRef, Q, QuerySet, Value, When
from django.utils import timezone

from apps.attendance.models import ScheduleEnrollment, TrainingGroupMembership
from apps.billing.models import (
    Debt,
    Payment,
    Subscription,
    SubscriptionComponent,
    Tariff,
    TariffComponent,
    TrainingType,
)
from apps.billing.selectors import current_active_subscription_q
from apps.clubs.timezones import club_localdate
from apps.students.models import Student, StudentNote


@dataclass(frozen=True)
class ManualOperationalAdmission:
    payment_id: int
    recorded_by_id: int
    payment_status: str
    payment_method: str
    subscription_status: str | None
    enrollment_status: str
    group_label: str
    training_group_id: int | None
    group_membership_id: int | None
    start_date: date
    checkin_ready: bool
    account_access_eligible: bool
    covered_visit_count: int
    is_qualifying: bool

    def as_dict(self) -> dict:
        result = {
            "payment_id": self.payment_id,
            "payment_status": self.payment_status,
            "payment_method": self.payment_method,
            "subscription_status": self.subscription_status,
            "enrollment_status": self.enrollment_status,
            "group_label": self.group_label,
            "start_date": self.start_date,
            "checkin_ready": self.checkin_ready,
            "account_access_eligible": self.account_access_eligible,
            "covered_visit_count": self.covered_visit_count,
        }
        if self.training_group_id is not None:
            result["training_group_id"] = self.training_group_id
            result["group_membership_id"] = self.group_membership_id
        return result


@dataclass(frozen=True)
class ManualOperationalAdmissionV2:
    """Additive discriminated admission projection for pending manual families."""

    kind: str
    payment_id: int
    recorded_by_id: int
    payment_status: str
    payment_method: str
    subscription_status: str | None
    start_date: date
    checkin_ready: bool
    account_access_eligible: bool
    is_qualifying: bool
    group_label: str | None = None
    training_group_id: int | None = None
    group_membership_id: int | None = None
    enrollment_status: str | None = None
    booking_id: int | None = None
    session_id: int | None = None
    booking_state: str | None = None

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "payment_id": self.payment_id,
            "recorded_by_id": self.recorded_by_id,
            "payment_status": self.payment_status,
            "payment_method": self.payment_method,
            "subscription_status": self.subscription_status,
            "start_date": self.start_date,
            "checkin_ready": self.checkin_ready,
            "account_access_eligible": self.account_access_eligible,
            "is_qualifying": self.is_qualifying,
            "group_label": self.group_label,
            "training_group_id": self.training_group_id,
            "group_membership_id": self.group_membership_id,
            "enrollment_status": self.enrollment_status,
            "booking_id": self.booking_id,
            "session_id": self.session_id,
            "booking_state": self.booking_state,
        }


def _is_exact_manual_operational_admission(*, payment: Payment, student: Student) -> bool:
    """Return true only for the payment-owned group admission created by S2.

    The payment/enrollment link is provenance, not a broad pending-subscription
    hint. Keep all structural checks here so access, trainer scope, and cabinet
    reads cannot drift apart.
    """

    subscription = payment.subscription
    enrollment = payment.conversion_enrollment
    schedule = payment.target_schedule
    if subscription is None or enrollment is None or schedule is None:
        return False

    common = (
        payment.club_id == student.club_id
        and payment.student_id == student.id
        and payment.payment_method in {Payment.Method.CASH, Payment.Method.TRANSFER}
        and payment.target_start_date is not None
        and payment.tariff.training_type.kind == TrainingType.Kind.GROUP
        and payment.target_training_type_kind_snapshot == TrainingType.Kind.GROUP
        and schedule.club_id == payment.club_id
        and schedule.training_type_id == payment.tariff.training_type_id
        and enrollment.club_id == payment.club_id
        and enrollment.student_id == payment.student_id
        and enrollment.starts_on == payment.target_start_date
        and subscription.club_id == payment.club_id
        and subscription.student_id == payment.student_id
        and subscription.tariff_id == payment.tariff_id
    )
    if not common:
        return False

    membership = payment.conversion_group_membership
    if membership is not None:
        return (
            payment.group_membership_action_snapshot
            == Payment.GroupMembershipActionSnapshot.NEW_ADMISSION
            and payment.target_training_group_id == membership.training_group_id
            and payment.target_group_membership_id == membership.id
            and membership.club_id == payment.club_id
            and membership.student_id == payment.student_id
            and membership.starts_on == payment.target_start_date
            and membership.authority == TrainingGroupMembership.Authority.PAYMENT_OWNED
            and schedule.training_group_id == membership.training_group_id
            and enrollment.schedule_id == payment.target_schedule_id
            and enrollment.training_group_membership_id == membership.id
            and enrollment.created_from == ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION
        )
    return (
        enrollment.schedule_id == payment.target_schedule_id
        and enrollment.created_from == ScheduleEnrollment.CreatedFrom.PAID_CONVERSION
    )


def _has_current_compatible_target(*, club, payment: Payment) -> bool:
    """Match the current target checks used by confirmation and check-in."""

    schedule = payment.target_schedule
    target_start_date = payment.target_start_date
    if schedule is None or target_start_date is None:
        return False
    if (
        not schedule.is_active
        or schedule.one_time_date is not None
        or schedule.training_type_id != payment.tariff.training_type_id
        or schedule.training_type_id != payment.target_training_type_id_snapshot
        or schedule.location_id != payment.target_location_id_snapshot
        or (
            payment.tariff.scope == Tariff.Scope.LOCATION
            and payment.tariff.location_id != schedule.location_id
        )
    ):
        return False
    group = payment.target_training_group
    if group is not None:
        if (
            group.status != group.Status.ACTIVE
            or group.responsible_trainer_id is None
            or not group.responsible_trainer.is_active
            or schedule.training_group_id != group.id
        ):
            return False
    elif not schedule.trainer.is_active:
        return False

    from apps.attendance.selectors import get_schedule_occurrences_for_date

    return any(
        occurrence.schedule_id == schedule.id
        for occurrence in get_schedule_occurrences_for_date(
            club=club,
            target_date=target_start_date,
        )
    )


def _manual_operational_admission_payments(*, club, student: Student) -> QuerySet[Payment]:
    return (
        Payment.objects.for_club(club)
        .filter(
            student_id=student.id,
            deleted_at__isnull=True,
            payment_method__in=[Payment.Method.CASH, Payment.Method.TRANSFER],
            target_schedule__isnull=False,
            target_start_date__isnull=False,
            conversion_enrollment__isnull=False,
        )
        .select_related(
            "subscription",
            "tariff__training_type",
            "target_schedule__trainer",
            "target_training_group__responsible_trainer",
            "conversion_group_membership",
            "conversion_enrollment__schedule",
        )
        .order_by("-id")
    )


def _is_qualifying_manual_operational_admission(
    *,
    club,
    student: Student,
    payment: Payment,
    club_today: date,
) -> bool:
    subscription = payment.subscription
    enrollment = payment.conversion_enrollment
    if subscription is None or enrollment is None or payment.target_start_date is None:
        return False
    exclusive_end_date = payment.target_start_date + timedelta(
        days=payment.tariff.duration_days,
    )
    return (
        student.status == Student.Status.ACTIVE
        and payment.status == Payment.Status.PENDING
        and subscription.status == Subscription.Status.PENDING
        and subscription.deleted_at is None
        and enrollment.status == ScheduleEnrollment.Status.ACTIVE
        and club_today < exclusive_end_date
        and _has_current_compatible_target(club=club, payment=payment)
    )


def _manual_personal_operational_admission_links(*, club, student: Student):
    """Return only complete exact personal payment links for one person."""

    from apps.attendance.models import PersonalDropInPaymentLink, PersonalServiceTermsSnapshot

    complete_terms = PersonalServiceTermsSnapshot.objects.for_club(club).filter(
        booking_id=OuterRef("booking_id"),
    )
    return (
        PersonalDropInPaymentLink.objects.for_club(club)
        .filter(
            booking__enrollment__student_id=student.id,
            payment__student_id=student.id,
            payment__deleted_at__isnull=True,
            payment__payment_method__in=[Payment.Method.CASH, Payment.Method.TRANSFER],
            payment__subscription__isnull=False,
        )
        .select_related(
            "payment__subscription",
            "payment__tariff",
            "booking__enrollment__schedule",
        )
        .annotate(_has_complete_terms=Exists(complete_terms))
        .order_by("-payment_id")
    )


def _is_exact_manual_personal_operational_admission(*, link, student: Student) -> bool:
    """Keep personal qualifying evidence as strict as the mutation owner."""

    from apps.attendance.models import (
        PersonalDropInBooking,
        PersonalServiceTermsSnapshot,
        complete_personal_terms_queryset,
    )

    payment = link.payment
    subscription = payment.subscription
    booking = link.booking
    enrollment = booking.enrollment
    terms = complete_personal_terms_queryset(
        PersonalServiceTermsSnapshot.objects.for_club(payment.club_id).filter(booking_id=booking.id)
    ).first()
    return bool(
        terms is not None
        and payment.club_id == student.club_id
        and payment.student_id == student.id
        and payment.payment_method in {Payment.Method.CASH, Payment.Method.TRANSFER}
        and subscription is not None
        and subscription.club_id == payment.club_id
        and subscription.student_id == student.id
        and subscription.tariff_id == payment.tariff_id
        and booking.club_id == payment.club_id
        and enrollment.club_id == payment.club_id
        and enrollment.student_id == student.id
        and booking.state in {PersonalDropInBooking.State.SCHEDULED, PersonalDropInBooking.State.ATTENDED}
        and terms.tariff_id_snapshot == payment.tariff_id
        and terms.payable_amount == payment.amount
    )


def _is_qualifying_manual_personal_operational_admission(*, club, student: Student, link, club_today: date) -> bool:
    payment = link.payment
    subscription = payment.subscription
    booking = link.booking
    enrollment = booking.enrollment
    if subscription is None or enrollment.starts_on is None:
        return False
    return bool(
        student.status == Student.Status.ACTIVE
        and payment.status == Payment.Status.PENDING
        and subscription.status == Subscription.Status.PENDING
        and subscription.deleted_at is None
        and enrollment.status == ScheduleEnrollment.Status.ACTIVE
        and club_today < enrollment.starts_on + timedelta(days=payment.tariff.duration_days)
        and _is_exact_manual_personal_operational_admission(link=link, student=student)
    )


def _build_manual_personal_operational_admission_v2(*, link, is_qualifying: bool) -> ManualOperationalAdmissionV2:
    payment = link.payment
    subscription = payment.subscription
    booking = link.booking
    enrollment = booking.enrollment
    assert subscription is not None
    assert enrollment.starts_on is not None
    return ManualOperationalAdmissionV2(
        kind="personal",
        payment_id=payment.id,
        recorded_by_id=payment.recorded_by_id,
        payment_status=payment.status,
        payment_method=payment.payment_method,
        subscription_status=(Subscription.Status.CANCELLED if subscription.deleted_at else subscription.status),
        start_date=enrollment.starts_on,
        checkin_ready=(is_qualifying and club_localdate(payment.club) >= enrollment.starts_on),
        account_access_eligible=is_qualifying,
        is_qualifying=is_qualifying,
        booking_id=booking.id,
        session_id=enrollment.schedule_id,
        booking_state=booking.state,
    )


def _manual_group_operational_admission_v2(*, admission: ManualOperationalAdmission) -> ManualOperationalAdmissionV2:
    return ManualOperationalAdmissionV2(
        kind="group",
        payment_id=admission.payment_id,
        recorded_by_id=admission.recorded_by_id,
        payment_status=admission.payment_status,
        payment_method=admission.payment_method,
        subscription_status=admission.subscription_status,
        start_date=admission.start_date,
        checkin_ready=admission.checkin_ready,
        account_access_eligible=admission.account_access_eligible,
        is_qualifying=admission.is_qualifying,
        group_label=admission.group_label,
        training_group_id=admission.training_group_id,
        group_membership_id=admission.group_membership_id,
        enrollment_status=admission.enrollment_status,
    )


def _build_manual_operational_admission(
    *,
    club,
    student: Student,
    payment: Payment,
    club_today: date,
    is_qualifying: bool,
) -> ManualOperationalAdmission:
    subscription = payment.subscription
    enrollment = payment.conversion_enrollment
    assert subscription is not None
    assert enrollment is not None
    assert payment.target_schedule is not None
    assert payment.target_start_date is not None

    covered_visit_count = 0
    if is_qualifying:
        covered_visit_count = Debt.objects.for_club(club).filter(
            student_id=student.id,
            settlement_payment_id=payment.id,
            resolved_at__isnull=True,
        ).count()

    return ManualOperationalAdmission(
        payment_id=payment.id,
        recorded_by_id=payment.recorded_by_id,
        payment_status=payment.status,
        payment_method=payment.payment_method,
        subscription_status=(
            Subscription.Status.CANCELLED if subscription.deleted_at is not None else subscription.status
        ),
        enrollment_status=enrollment.status,
        group_label=(
            payment.target_training_group.name
            if payment.target_training_group_id is not None
            else payment.target_group_name_snapshot or payment.target_schedule.group_name
        ),
        training_group_id=payment.target_training_group_id,
        group_membership_id=payment.conversion_group_membership_id,
        start_date=payment.target_start_date,
        checkin_ready=(is_qualifying and club_today >= payment.target_start_date),
        account_access_eligible=is_qualifying,
        covered_visit_count=covered_visit_count,
        is_qualifying=is_qualifying,
    )


def get_manual_operational_admissions(*, club, student: Student) -> list[ManualOperationalAdmission]:
    """Return every live admission, or the singular truthful fallback.

    Different training types may legitimately have concurrent pending
    admissions. A newer rejected or drifted row must not mask another live
    admission, while the newest exact non-qualifying row remains the truthful
    fallback when no qualifying admission exists. The queryset order makes the
    returned qualifying entries deterministic newest-first.
    """

    club_today = club_localdate(club)
    fallback: ManualOperationalAdmission | None = None
    admissions: list[ManualOperationalAdmission] = []
    for payment in _manual_operational_admission_payments(club=club, student=student):
        if not _is_exact_manual_operational_admission(payment=payment, student=student):
            continue
        is_qualifying = _is_qualifying_manual_operational_admission(
            club=club,
            student=student,
            payment=payment,
            club_today=club_today,
        )
        if is_qualifying:
            admissions.append(
                _build_manual_operational_admission(
                    club=club,
                    student=student,
                    payment=payment,
                    club_today=club_today,
                    is_qualifying=True,
                )
            )
        elif fallback is None:
            fallback = _build_manual_operational_admission(
                club=club,
                student=student,
                payment=payment,
                club_today=club_today,
                is_qualifying=False,
            )
    return admissions or ([fallback] if fallback else [])


def get_manual_operational_admission(*, club, student: Student) -> ManualOperationalAdmission | None:
    """Return the primary/newest admission for backward-compatible callers."""

    admissions = get_manual_operational_admissions(club=club, student=student)
    return admissions[0] if admissions else None


def get_manual_operational_admissions_v2(*, club, student: Student) -> list[ManualOperationalAdmissionV2]:
    """Return group and personal admissions without forcing personal into v1 shape."""

    club_today = club_localdate(club)
    group_admissions = [
        _manual_group_operational_admission_v2(admission=admission)
        for admission in get_manual_operational_admissions(club=club, student=student)
    ]
    personal_admissions: list[ManualOperationalAdmissionV2] = []
    personal_fallback: ManualOperationalAdmissionV2 | None = None
    for link in _manual_personal_operational_admission_links(club=club, student=student):
        if not _is_exact_manual_personal_operational_admission(link=link, student=student):
            continue
        qualifying = _is_qualifying_manual_personal_operational_admission(
            club=club,
            student=student,
            link=link,
            club_today=club_today,
        )
        projection = _build_manual_personal_operational_admission_v2(
            link=link,
            is_qualifying=qualifying,
        )
        if qualifying:
            personal_admissions.append(projection)
        elif personal_fallback is None:
            personal_fallback = projection
    admissions = [*group_admissions, *personal_admissions]
    if admissions:
        return sorted(admissions, key=lambda admission: admission.payment_id, reverse=True)
    return [personal_fallback] if personal_fallback else []


def has_qualifying_manual_operational_admission(
    *,
    club,
    student: Student,
    recorded_by_id: int | None = None,
) -> bool:
    """Return whether any exact live admission matches the optional recorder."""

    club_today = club_localdate(club)
    for payment in _manual_operational_admission_payments(club=club, student=student):
        if recorded_by_id is not None and payment.recorded_by_id != recorded_by_id:
            continue
        if not _is_exact_manual_operational_admission(payment=payment, student=student):
            continue
        if _is_qualifying_manual_operational_admission(
            club=club,
            student=student,
            payment=payment,
            club_today=club_today,
        ):
            return True
    for link in _manual_personal_operational_admission_links(club=club, student=student):
        if recorded_by_id is not None and link.payment.recorded_by_id != recorded_by_id:
            continue
        if _is_qualifying_manual_personal_operational_admission(
            club=club,
            student=student,
            link=link,
            club_today=club_today,
        ):
            return True
    return False


def has_paid_active_subscription(*, club, student_id: int) -> bool:
    return (
        Subscription.objects.for_club(club)
        .filter(
            current_active_subscription_q(),
            student_id=student_id,
            paid_amount__gt=0,
        )
        .exists()
    )


def is_account_access_eligible(*, club, student: Student) -> bool:
    if student.status != Student.Status.ACTIVE:
        return False
    if has_paid_active_subscription(club=club, student_id=student.id):
        return True
    return has_qualifying_manual_operational_admission(club=club, student=student)


def get_cabinet_financial_read_model(*, club, student: Student) -> dict:
    """Return the shared pending-manual financial state for student/parent views."""

    admissions = get_manual_operational_admissions(club=club, student=student)
    admissions_v2 = get_manual_operational_admissions_v2(club=club, student=student)
    qualifying_admissions = [admission for admission in admissions_v2 if admission.is_qualifying]
    covered_visits_by_payment: dict[int, list[dict]] = {
        admission.payment_id: [] for admission in qualifying_admissions
    }
    if qualifying_admissions:
        debts = (
            Debt.objects.for_club(club)
            .filter(
                student_id=student.id,
                settlement_payment_id__in=covered_visits_by_payment,
                resolved_at__isnull=True,
            )
            .select_related("checkin__training_type")
            .order_by("checkin__date", "created_at", "id")
        )
        for debt in debts:
            covered_visits_by_payment[debt.settlement_payment_id].append(
                {
                    "payment_id": debt.settlement_payment_id,
                    "debt_id": debt.id,
                    "checkin_id": debt.checkin_id,
                    "training_type_name": debt.checkin.training_type.name,
                    "checkin_date": debt.checkin.date,
                    "coverage_state": "covered_awaiting_confirmation",
                    "is_payable": False,
                }
            )

    covered_visits = [
        visit
        for admission in qualifying_admissions
        for visit in covered_visits_by_payment[admission.payment_id]
    ]
    admission = admissions[0] if admissions else None

    return {
        "operational_admission": admission.as_dict() if admission else None,
        "operational_admissions": [admission.as_dict() for admission in admissions],
        "operational_admission_v2": admissions_v2[0].as_dict() if admissions_v2 else None,
        "operational_admissions_v2": [admission.as_dict() for admission in admissions_v2],
        "covered_visits": covered_visits,
    }


def get_students(*, club) -> QuerySet[Student]:
    return Student.objects.for_club(club).filter(deleted_at__isnull=True).order_by("-created_at")


COMMERCIAL_SEGMENT_FORMER = "former"
COMMERCIAL_SEGMENT_AT_RISK = "at_risk"
COMMERCIAL_SEGMENT_PENDING_ADMISSION = "pending_admission"
COMMERCIAL_SEGMENT_ACTIVE_ENTITLEMENT = "active_entitlement"
COMMERCIAL_SEGMENT_NO_CRM_ENTITLEMENT = "no_crm_entitlement"
COMMERCIAL_SEGMENTS = {
    COMMERCIAL_SEGMENT_FORMER,
    COMMERCIAL_SEGMENT_AT_RISK,
    COMMERCIAL_SEGMENT_PENDING_ADMISSION,
    COMMERCIAL_SEGMENT_ACTIVE_ENTITLEMENT,
    COMMERCIAL_SEGMENT_NO_CRM_ENTITLEMENT,
}


def get_student_workspace(*, club) -> QuerySet[Student]:
    """Return only people proven to belong to the Students workspace."""

    return (
        Student.objects.for_club(club)
        .filter(
            deleted_at__isnull=True,
            lead_status__isnull=True,
            became_student_at__isnull=False,
        )
        .order_by("-created_at", "-id")
    )


def with_commercial_segment(*, queryset: QuerySet[Student], club) -> QuerySet[Student]:
    """Annotate the mutually-exclusive D3 commercial segment without N+1 reads."""

    now = timezone.now()
    today = club_localdate(club)
    pending_admission = Payment.objects.for_club(club).filter(
        student_id=OuterRef("pk"),
        deleted_at__isnull=True,
        status=Payment.Status.PENDING,
        payment_method__in=[Payment.Method.CASH, Payment.Method.TRANSFER],
        subscription__isnull=False,
        subscription__deleted_at__isnull=True,
        subscription__status=Subscription.Status.PENDING,
        subscription__student_id=OuterRef("pk"),
        tariff__training_type__kind=TrainingType.Kind.GROUP,
        target_training_type_kind_snapshot=TrainingType.Kind.GROUP,
        target_schedule__isnull=False,
        target_schedule__is_active=True,
        target_schedule__one_time_date__isnull=True,
        target_schedule__training_type_id=F("tariff__training_type_id"),
        target_start_date__isnull=False,
        conversion_enrollment__isnull=False,
        conversion_enrollment__student_id=OuterRef("pk"),
        conversion_enrollment__schedule_id=F("target_schedule_id"),
        conversion_enrollment__status=ScheduleEnrollment.Status.ACTIVE,
        conversion_enrollment__starts_on=F("target_start_date"),
    ).filter(
        Q(subscription__expires_at__isnull=True) | Q(subscription__expires_at__gt=now),
        Q(conversion_enrollment__ends_on__isnull=True)
        | Q(conversion_enrollment__ends_on__gte=today),
    ).filter(
        Q(conversion_group_membership__isnull=False)
        | Q(conversion_enrollment__created_from=ScheduleEnrollment.CreatedFrom.PAID_CONVERSION)
    )
    active_entitlement = SubscriptionComponent.objects.for_club(club).filter(
        subscription__student_id=OuterRef("pk"),
        subscription__deleted_at__isnull=True,
        subscription__status=Subscription.Status.ACTIVE,
        is_active=True,
    ).filter(
        Q(subscription__expires_at__isnull=True) | Q(subscription__expires_at__gt=now),
    ).filter(
        Q(
            entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
            credits_left__gt=0,
        )
        | Q(entitlement_kind=TariffComponent.EntitlementKind.UNLIMITED)
        | Q(
            entitlement_kind=TariffComponent.EntitlementKind.WEEKLY_LIMIT,
            weekly_limit__gt=0,
        )
    )
    return queryset.annotate(
        _has_pending_admission=Exists(pending_admission),
        _has_active_entitlement=Exists(active_entitlement),
    ).annotate(
        _commercial_segment=Case(
            When(
                status__in=[Student.Status.CHURNED, Student.Status.LOST],
                then=Value(COMMERCIAL_SEGMENT_FORMER),
            ),
            When(status=Student.Status.AT_RISK, then=Value(COMMERCIAL_SEGMENT_AT_RISK)),
            When(
                _has_pending_admission=True,
                then=Value(COMMERCIAL_SEGMENT_PENDING_ADMISSION),
            ),
            When(
                _has_active_entitlement=True,
                then=Value(COMMERCIAL_SEGMENT_ACTIVE_ENTITLEMENT),
            ),
            default=Value(COMMERCIAL_SEGMENT_NO_CRM_ENTITLEMENT),
            output_field=CharField(),
        )
    )


def filter_by_commercial_segment(
    *, queryset: QuerySet[Student], commercial_segment: str
) -> QuerySet[Student]:
    if commercial_segment not in COMMERCIAL_SEGMENTS:
        return queryset.none()
    return queryset.filter(_commercial_segment=commercial_segment)


def filter_students_by_query(*, queryset: QuerySet[Student], query: str) -> QuerySet[Student]:
    normalized_query = query.strip()
    if not normalized_query:
        return queryset

    terms = normalized_query.split()
    search_filter = Q(first_name__icontains=normalized_query) | Q(
        last_name__icontains=normalized_query
    )
    if len(terms) >= 2:
        first, second = terms[0], terms[1]
        search_filter |= Q(first_name__icontains=first, last_name__icontains=second) | Q(
            first_name__icontains=second,
            last_name__icontains=first,
        )

    digits = "".join(character for character in normalized_query if character.isdigit())
    if len(digits) >= 2:
        phone_fragments = {digits}
        if digits.startswith("8"):
            phone_fragments.add(f"7{digits[1:]}")
        elif len(digits) == 10:
            phone_fragments.add(f"7{digits}")
        for fragment in phone_fragments:
            search_filter |= Q(phone__contains=fragment) | Q(guardian_phone__contains=fragment)

    return queryset.filter(search_filter)


def get_student_by_id(*, club, student_id: int) -> Student:
    return Student.objects.for_club(club).get(id=student_id, deleted_at__isnull=True)


def get_student_detail(*, club, student_id: int) -> Student:
    student = (
        with_commercial_segment(
            queryset=Student.objects.for_club(club),
            club=club,
        )
        .prefetch_related("notes__author", "account_accesses__user")
        .get(id=student_id, deleted_at__isnull=True)
    )
    student._operational_admission = get_manual_operational_admission(club=club, student=student)
    admissions_v2 = get_manual_operational_admissions_v2(club=club, student=student)
    student._operational_admission_v2 = admissions_v2[0] if admissions_v2 else None
    student._account_access_eligible = is_account_access_eligible(club=club, student=student)
    return student


def get_student_by_user(*, club, user_id: int) -> Student:
    return Student.objects.for_club(club).get(user_id=user_id, deleted_at__isnull=True)


def get_student_notes(*, club, student_id: int) -> QuerySet[StudentNote]:
    return (
        StudentNote.objects.for_club(club)
        .filter(student_id=student_id)
        .select_related("author")
        .order_by("-created_at")
    )
