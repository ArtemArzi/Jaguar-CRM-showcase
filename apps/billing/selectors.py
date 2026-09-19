from __future__ import annotations

from datetime import date, datetime, time, timedelta
from io import BytesIO

from django.db import models
from django.db.models import Count, DateField, Prefetch, Q, QuerySet, Value
from django.utils import timezone

from apps.billing.models import (
    BankPaymentOrder,
    BankPaymentProviderEvent,
    Debt,
    Discount,
    Expense,
    Payment,
    PaymentRefund,
    PaymentRefundCase,
    Subscription,
    SubscriptionComponent,
    SubscriptionFreeze,
    Tariff,
    TariffComponent,
    TrainingType,
)
from apps.clubs.timezones import club_local_day_start, club_localdate
from apps.common.exceptions import BusinessLogicError
from apps.trainers.models import TrainerPackageAllocation

LIVE_BANK_PAYMENT_ORDER_STATUSES = {
    BankPaymentOrder.Status.CREATED,
    BankPaymentOrder.Status.PENDING,
    BankPaymentOrder.Status.AUTHORIZED,
}


def get_open_payment_refund_cases(*, club) -> QuerySet[PaymentRefundCase]:
    return (
        PaymentRefundCase.objects.for_club(club)
        .exclude(status=PaymentRefundCase.Status.RESOLVED)
        .select_related(
            "order",
            "order__payment",
            "order__payment__tariff",
            "order__student",
            "provider_event",
            "legacy_review_event",
        )
        .order_by("provider_refunded_at", "created_at", "id")
    )


def get_payment_refunds_requiring_payroll(*, club) -> QuerySet[PaymentRefund]:
    return (
        PaymentRefund.objects.for_club(club)
        .filter(status=PaymentRefund.Status.PAYROLL_ACTION_REQUIRED)
        .select_related(
            "payment",
            "payment__student",
            "order",
            "approved_by",
        )
        .order_by("accounting_date", "id")
    )


def _with_paid_amount(qs: QuerySet[Subscription]) -> QuerySet[Subscription]:
    return qs.select_related("payment")


def active_package_allocation_prefetch(*, club, lookup: str = "trainer_allocations") -> Prefetch:
    return Prefetch(
        lookup,
        queryset=TrainerPackageAllocation.objects.for_club(club)
        .filter(is_active=True)
        .select_related("owner_trainer"),
        to_attr="active_trainer_allocations",
    )


def active_tariff_components_prefetch(*, club, lookup: str = "components") -> Prefetch:
    return Prefetch(
        lookup,
        queryset=TariffComponent.objects.for_club(club)
        .filter(is_active=True)
        .select_related("training_type", "location")
        .order_by("sort_order", "id"),
        to_attr="active_components",
    )


def active_subscription_components_prefetch(
    *, club, booking_date: date | None = None, lookup: str = "components"
) -> Prefetch:
    queryset = (
        SubscriptionComponent.objects.for_club(club)
        .filter(is_active=True)
        .select_related("training_type", "location")
        .order_by("id")
    )
    if booking_date is not None:
        week_start = booking_date - timedelta(days=booking_date.weekday())
        week_end = week_start + timedelta(days=6)
        queryset = queryset.annotate(
            booking_week_used=Count(
                "checkins",
                filter=Q(
                    checkins__club=club,
                    checkins__date__gte=week_start,
                    checkins__date__lte=week_end,
                    checkins__deleted_at__isnull=True,
                    checkins__cancelled_at__isnull=True,
                ),
            )
        )
    return Prefetch(
        lookup,
        queryset=queryset,
        to_attr="active_booking_components",
    )


def subscription_component_presence_prefetch(*, club, lookup: str = "components") -> Prefetch:
    return Prefetch(
        lookup,
        queryset=SubscriptionComponent.objects.for_club(club).only("id", "subscription_id"),
        to_attr="subscription_component_presence",
    )


def pending_freezes_prefetch(*, club) -> Prefetch:
    return Prefetch(
        "freezes",
        queryset=SubscriptionFreeze.objects.for_club(club).filter(
            status=SubscriptionFreeze.FreezeStatus.PENDING,
        ),
        to_attr="pending_freezes",
    )


def applied_discounts_prefetch(*, club) -> Prefetch:
    return Prefetch(
        "applied_discounts",
        queryset=Discount.objects.for_club(club).only(
            "id",
            "name",
            "discount_type",
            "value",
        ),
        to_attr="prefetched_applied_discounts",
    )


def current_active_subscription_q(now=None) -> Q:
    now = now or timezone.now()
    return (
        Q(status=Subscription.Status.ACTIVE, deleted_at__isnull=True)
        & (Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        & (Q(trainings_left__isnull=True) | Q(trainings_left__gt=0))
    )


def get_active_training_types(*, club) -> QuerySet[TrainingType]:
    return TrainingType.objects.for_club(club).filter(is_active=True).select_related("grade_system")


def _with_booking_date_context(
    qs: QuerySet[Subscription], *, booking_date: date | None
) -> QuerySet[Subscription]:
    if booking_date is None:
        return qs
    return qs.annotate(
        booking_date_context=Value(booking_date, output_field=DateField()),
    )


def get_club_subscriptions(
    *, club, student_id: int | None = None, booking_date: date | None = None
) -> QuerySet[Subscription]:
    qs = (
        _with_paid_amount(
            Subscription.objects.for_club(club)
            .filter(deleted_at__isnull=True)
            .select_related("tariff", "tariff__training_type")
            .prefetch_related(
                active_package_allocation_prefetch(club=club),
                active_tariff_components_prefetch(club=club, lookup="tariff__components"),
                subscription_component_presence_prefetch(club=club),
                active_subscription_components_prefetch(club=club, booking_date=booking_date),
                pending_freezes_prefetch(club=club),
            )
        )
    )
    if student_id:
        qs = qs.filter(student_id=student_id)
    return _with_booking_date_context(qs, booking_date=booking_date)


def get_subscription_by_id(*, club, subscription_id: int) -> Subscription:
    return (
        _with_paid_amount(
            Subscription.objects.for_club(club)
            .select_related("tariff", "tariff__training_type")
            .prefetch_related(
                active_package_allocation_prefetch(club=club),
                active_tariff_components_prefetch(club=club, lookup="tariff__components"),
                subscription_component_presence_prefetch(club=club),
                active_subscription_components_prefetch(club=club),
                pending_freezes_prefetch(club=club),
            )
        ).get(id=subscription_id, deleted_at__isnull=True)
    )


def get_tariffs(*, club) -> QuerySet[Tariff]:
    return (
        Tariff.objects.for_club(club)
        .filter(is_active=True)
        .select_related("training_type")
        .prefetch_related(active_tariff_components_prefetch(club=club))
    )


def _trainer_group_target_student_is_eligible(student) -> bool:
    from apps.students.models import Student

    if student.status in {Student.Status.LOST, Student.Status.CHURNED}:
        return False
    if student.status == Student.Status.LEAD:
        return student.lead_status in Student.LeadStatus.values
    return student.status in {
        Student.Status.TRIAL,
        Student.Status.ACTIVE,
        Student.Status.AT_RISK,
    }


def _open_permanent_group_schedule_ids(*, club, student_id: int, training_type_id: int) -> set[int]:
    from apps.attendance.models import ScheduleEnrollment

    return set(
        ScheduleEnrollment.objects.for_club(club)
        .filter(
            student_id=student_id,
            schedule__training_type_id=training_type_id,
            schedule__one_time_date__isnull=True,
            status__in=[
                ScheduleEnrollment.Status.ACTIVE,
                ScheduleEnrollment.Status.FROZEN,
            ],
            created_from__in=[
                ScheduleEnrollment.CreatedFrom.MANUAL,
                ScheduleEnrollment.CreatedFrom.PAID_CONVERSION,
                ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION,
                ScheduleEnrollment.CreatedFrom.IMPORT,
            ],
            ends_on__isnull=True,
        )
        .values_list("schedule_id", flat=True)
    )


def get_group_enrollment_options(
    *,
    club,
    student_id: int,
    tariff_id: int,
    enforce_trainer_contract: bool = False,
    canonical_cards_enabled: bool = False,
) -> list[dict]:
    from apps.attendance.models import Schedule, ScheduleEnrollment, ScheduleException, TrainingGroup
    from apps.attendance.selectors import get_schedule_occurrences_for_date
    from apps.students.models import Student

    student = (
        Student.objects.for_club(club)
        .filter(id=student_id, deleted_at__isnull=True)
        .first()
    )
    if student is None:
        raise BusinessLogicError("Ученик не найден", code="student_not_found")

    tariff = (
        Tariff.objects.for_club(club)
        .filter(id=tariff_id, is_active=True)
        .select_related("training_type", "location")
        .first()
    )
    if tariff is None:
        raise BusinessLogicError("Тариф не найден", code="tariff_not_found")
    if tariff.training_type.kind != TrainingType.Kind.GROUP:
        raise BusinessLogicError(
            "Выберите групповой тариф",
            code="group_tariff_required",
        )
    if enforce_trainer_contract and not _trainer_group_target_student_is_eligible(student):
        raise BusinessLogicError(
            "Для потерянного или ушедшего ученика решение принимает владелец или администратор",
            code="trainer_group_student_not_eligible",
        )

    schedules = (
        Schedule.objects.for_club(club)
        .filter(
            is_active=True,
            one_time_date__isnull=True,
            trainer__is_active=True,
            training_type_id=tariff.training_type_id,
            training_type__kind=TrainingType.Kind.GROUP,
        )
        .filter(
            Q(training_group__isnull=True)
            | Q(
                training_group__status=TrainingGroup.Status.ACTIVE,
                training_group__responsible_trainer__is_active=True,
            )
        )
        .select_related("trainer", "location", "training_type", "training_group__responsible_trainer")
    )
    if tariff.scope == Tariff.Scope.LOCATION:
        schedules = schedules.filter(location_id=tariff.location_id)

    if enforce_trainer_contract:
        permanent_schedule_ids = _open_permanent_group_schedule_ids(
            club=club,
            student_id=student_id,
            training_type_id=tariff.training_type_id,
        )
        if permanent_schedule_ids:
            schedules = schedules.filter(id__in=permanent_schedule_ids)

    schedule_list = list(schedules.order_by("group_name", "id"))
    if not schedule_list:
        return []

    date_from = club_localdate(club)
    date_to = date_from + timedelta(days=27)
    candidate_ids = {schedule.id for schedule in schedule_list}
    # A recommendation is computed *after* compatibility filtering.  A newer
    # trial in another type/location must not suppress the latest candidate
    # canonical group.  For canonical cards, legacy slots are not candidates.
    recommendation_schedule_ids = {
        schedule.id
        for schedule in schedule_list
        if not canonical_cards_enabled or schedule.training_group_id is not None
    }
    trial_evidence = list(
        ScheduleEnrollment.objects.for_club(club)
        .filter(
            student_id=student_id,
            schedule_id__in=recommendation_schedule_ids,
            created_from=ScheduleEnrollment.CreatedFrom.LEAD_BOOKING,
            status__in=[
                ScheduleEnrollment.Status.ACTIVE,
                ScheduleEnrollment.Status.TRIAL,
                ScheduleEnrollment.Status.FROZEN,
            ],
            starts_on__isnull=False,
        )
        .values("id", "schedule_id", "starts_on", "ends_on", "trial_at")
    )
    latest_trial_schedule_id: int | None = None
    latest_trial_rank: tuple[date, float, int, int] | None = None
    for enrollment in trial_evidence:
        trial_at = enrollment["trial_at"]
        rank = (
            trial_at.date() if trial_at is not None else enrollment["starts_on"],
            trial_at.timestamp() if trial_at is not None else float("-inf"),
            enrollment["schedule_id"],
            enrollment["id"],
        )
        if latest_trial_rank is None or rank > latest_trial_rank:
            latest_trial_rank = rank
            latest_trial_schedule_id = enrollment["schedule_id"]

    # A completed check-in on the persisted lead-booking interval is stronger
    # later trial evidence.  We never consider arbitrary paid check-ins on the
    # same schedule, and the rank has stable schedule/enrollment tie-breakers.
    if trial_evidence:
        from apps.attendance.models import Checkin

        for checkin in (
            Checkin.objects.for_club(club)
            .filter(
                student_id=student_id,
                schedule_id__in={item["schedule_id"] for item in trial_evidence},
                deleted_at__isnull=True,
                cancelled_at__isnull=True,
            )
            .only("id", "schedule_id", "date")
        ):
            for enrollment in trial_evidence:
                if enrollment["schedule_id"] != checkin.schedule_id:
                    continue
                if checkin.date < enrollment["starts_on"]:
                    continue
                if enrollment["ends_on"] is not None and checkin.date > enrollment["ends_on"]:
                    continue
                rank = (checkin.date, float("inf"), checkin.schedule_id, enrollment["id"])
                if latest_trial_rank is None or rank > latest_trial_rank:
                    latest_trial_rank = rank
                    latest_trial_schedule_id = checkin.schedule_id
    candidate_weekdays = {schedule.day_of_week for schedule in schedule_list}
    rescheduled_dates = set(
        ScheduleException.objects.for_club(club)
        .filter(
            schedule_id__in=candidate_ids,
            exception_type=ScheduleException.ExceptionType.RESCHEDULED,
            new_date__range=(date_from, date_to),
        )
        .values_list("new_date", flat=True)
    )
    dates_to_check = []
    target_date = date_from
    while target_date <= date_to:
        if target_date.weekday() in candidate_weekdays or target_date in rescheduled_dates:
            dates_to_check.append(target_date)
        target_date += timedelta(days=1)

    occurrences_by_schedule: dict[int, list[object]] = {
        schedule_id: [] for schedule_id in candidate_ids
    }
    for target_date in dates_to_check:
        occurrences = get_schedule_occurrences_for_date(
            club=club,
            target_date=target_date,
        )
        for occurrence in occurrences:
            if occurrence.schedule_id in candidate_ids:
                occurrences_by_schedule[occurrence.schedule_id].append(occurrence)

    legacy_options: list[dict] = []
    mapped_options: dict[int, list[tuple[object, dict]]] = {}
    for schedule in schedule_list:
        schedule_occurrences = occurrences_by_schedule[schedule.id]
        if not schedule_occurrences:
            continue
        first_occurrence = schedule_occurrences[0]
        option = {
            "schedule_id": schedule.id,
            "group_name": first_occurrence.group_name,
            "trainer_id": first_occurrence.trainer_id,
            "trainer_name": first_occurrence.trainer_name,
            "location_id": first_occurrence.location_id,
            "location_name": first_occurrence.location_name,
            "training_type_id": schedule.training_type_id,
            "training_type_name": schedule.training_type.name,
            "day_of_week": schedule.day_of_week,
            "start_time": first_occurrence.effective_start_time,
            "end_time": first_occurrence.effective_end_time,
            "next_occurrence_date": first_occurrence.effective_date,
            "occurrence_dates": [
                occurrence.effective_date for occurrence in schedule_occurrences
            ],
            "upcoming_occurrences": [
                {
                    "schedule_id": schedule.id,
                    "date": occurrence.effective_date,
                    "start_time": occurrence.effective_start_time,
                    "end_time": occurrence.effective_end_time,
                    "trainer_id": occurrence.trainer_id,
                    "trainer_name": occurrence.trainer_name,
                    "location_id": occurrence.location_id,
                    "location_name": occurrence.location_name,
                    "is_rescheduled": occurrence.is_rescheduled,
                    "is_substitute": occurrence.is_substitute,
                }
                for occurrence in schedule_occurrences
            ],
            "_occurrences": schedule_occurrences,
            "is_latest_trial_group": schedule.id == latest_trial_schedule_id,
        }
        if schedule.training_group_id is None or not canonical_cards_enabled:
            option.pop("_occurrences")
            legacy_options.append(option)
        else:
            mapped_options.setdefault(schedule.training_group_id, []).append((schedule, option))

    membership_ids_by_group: dict[int, int] = {}
    if mapped_options:
        from apps.attendance.models import TrainingGroupMembership

        for membership in (
            TrainingGroupMembership.objects.for_club(club)
            .filter(
                student_id=student_id,
                training_group_id__in=mapped_options.keys(),
                ends_on__isnull=True,
                status__in=[
                    TrainingGroupMembership.Status.ACTIVE,
                    TrainingGroupMembership.Status.FROZEN,
                ],
            )
            .order_by("training_group_id", "id")
            .only("id", "training_group_id")
        ):
            membership_ids_by_group.setdefault(membership.training_group_id, membership.id)

    # A group card may advertise renewal only with the one exact current chain
    # leaf.  Natural expiry/exhaustion is still renewable, but an early-closed,
    # pending, or already-finalized source is not.  Multiple viable sources are
    # deliberately ambiguous: callers receive a RENEWAL card with no source and
    # must fail closed instead of selecting one by tariff/client convention.
    renewal_source_id: int | None = None
    if membership_ids_by_group:
        from apps.billing.service_modules.renewals import is_exact_renewal_source_actionable

        exact_sources = [
            source.id
            for source in (
                Subscription.objects.for_club(club)
                .filter(
                    student_id=student_id,
                    tariff_id=tariff.id,
                    deleted_at__isnull=True,
                    status__in=[Subscription.Status.ACTIVE, Subscription.Status.EXPIRED],
                )
                .order_by("id")
            )
            if is_exact_renewal_source_actionable(source=source)
        ]
        if len(exact_sources) == 1:
            renewal_source_id = exact_sources[0]

    canonical_cards: list[dict] = []
    for group_id, entries in mapped_options.items():
        entries.sort(key=lambda entry: (entry[1]["next_occurrence_date"], entry[0].start_time, entry[0].id))
        first_schedule, first_option = entries[0]
        group = first_schedule.training_group
        occurrences = []
        for schedule, option in entries:
            occurrences.extend(
                {
                    "schedule_id": schedule.id,
                    "date": occurrence.effective_date,
                    "start_time": occurrence.effective_start_time,
                    "end_time": occurrence.effective_end_time,
                    "trainer_id": occurrence.trainer_id,
                    "trainer_name": occurrence.trainer_name,
                    "location_id": occurrence.location_id,
                    "location_name": occurrence.location_name,
                    "is_rescheduled": occurrence.is_rescheduled,
                    "is_substitute": occurrence.is_substitute,
                }
                for occurrence in option.pop("_occurrences")
            )
        occurrences.sort(key=lambda item: (item["date"], item["start_time"], item["schedule_id"]))
        canonical_cards.append(
            {
                **first_option,
                "group_name": group.name,
                "trainer_id": group.responsible_trainer_id,
                "trainer_name": str(group.responsible_trainer),
                "training_group_id": group_id,
                "responsible_trainer_id": group.responsible_trainer_id,
                "responsible_trainer_name": str(group.responsible_trainer),
                "slot_schedule_ids": [schedule.id for schedule, _option in entries],
                "weekly_schedule": [
                    {
                        "schedule_id": schedule.id,
                        "day_of_week": schedule.day_of_week,
                        "start_time": schedule.start_time,
                        "end_time": schedule.end_time,
                        "trainer_id": schedule.trainer_id,
                        "trainer_name": str(schedule.trainer),
                        "location_id": schedule.location_id,
                        "location_name": schedule.location.name,
                    }
                    for schedule, _option in entries
                ],
                "upcoming_occurrences": occurrences,
                "occurrence_dates": sorted({item["date"] for item in occurrences}),
                "next_occurrence_date": occurrences[0]["date"],
                "target_group_membership_id": membership_ids_by_group.get(group_id),
                "group_membership_action": (
                    Payment.GroupMembershipActionSnapshot.RENEWAL
                    if group_id in membership_ids_by_group
                    else Payment.GroupMembershipActionSnapshot.NEW_ADMISSION
                ),
                "renewed_from_subscription_id": (
                    renewal_source_id if group_id in membership_ids_by_group else None
                ),
                "is_latest_trial_group": any(
                    option["is_latest_trial_group"] for _schedule, option in entries
                ),
                "is_canonical_group_card": True,
            }
        )

    return sorted(
        [*legacy_options, *canonical_cards],
        key=lambda option: (
            not option["is_latest_trial_group"],
            option["next_occurrence_date"],
            option["group_name"],
            option["schedule_id"],
        ),
    )


def get_payments(
    *,
    club,
    student_id: int | None = None,
    status: str | None = None,
) -> QuerySet[Payment]:
    qs = (
        Payment.objects.for_club(club)
        .filter(deleted_at__isnull=True)
        .select_related(
            "tariff",
            "tariff__training_type",
            "seller_trainer",
            "package_owner_trainer",
            "subscription",
            "target_schedule",
            "target_training_group__responsible_trainer",
        )
        .prefetch_related(
            applied_discounts_prefetch(club=club),
            active_tariff_components_prefetch(club=club, lookup="tariff__components"),
            active_package_allocation_prefetch(
                club=club,
                lookup="subscription__trainer_allocations",
            ),
        )
    )
    if student_id:
        qs = qs.filter(student_id=student_id)
    if status:
        qs = qs.filter(status=status)
    return qs.order_by("-created_at")


def get_payment_by_id(*, club, payment_id: int) -> Payment:
    return get_payments(club=club).get(id=payment_id)


def _finance_workspace_payment_context_q(context: str) -> Q:
    if context == "renewal":
        return Q(subscription__renewed_from_id__isnull=False)
    if context == "group":
        return Q(subscription__renewed_from_id__isnull=True) & (
            Q(target_training_group_id__isnull=False)
            | Q(
                target_schedule_id__isnull=False,
                tariff__training_type__kind=TrainingType.Kind.GROUP,
            )
        )
    if context == "personal":
        return Q(
            subscription__renewed_from_id__isnull=True,
            tariff__training_type__kind=TrainingType.Kind.PERSONAL,
        )
    if context == "other":
        return (
            Q(subscription__renewed_from_id__isnull=True)
            & Q(target_training_group_id__isnull=True)
            & Q(target_schedule_id__isnull=True)
            & ~Q(tariff__training_type__kind=TrainingType.Kind.PERSONAL)
        )
    return Q()


def _filter_finance_workspace_dates(
    qs: QuerySet,
    *,
    club,
    date_from: date | None,
    date_to: date | None,
    field_name: str = "created_at",
) -> QuerySet:
    date_from, date_to = _normalize_finance_workspace_dates(date_from, date_to)
    if date_from is not None:
        qs = qs.filter(**{f"{field_name}__gte": club_local_day_start(club, date_from)})
    if date_to is not None:
        qs = qs.filter(
            **{
                f"{field_name}__lt": club_local_day_start(
                    club,
                    date_to + timedelta(days=1),
                )
            }
        )
    return qs


def _normalize_finance_workspace_dates(
    date_from: date | None,
    date_to: date | None,
) -> tuple[date | None, date | None]:
    if date_from is not None and date_to is not None and date_from > date_to:
        return date_to, date_from
    return date_from, date_to


def _filter_finance_workspace_payments(
    qs: QuerySet[Payment],
    *,
    club,
    trainer_id: int | None = None,
    payment_method: str = "",
    context: str = "",
    date_from: date | None = None,
    date_to: date | None = None,
) -> QuerySet[Payment]:
    if trainer_id is not None:
        qs = qs.filter(
            Q(seller_trainer_id=trainer_id)
            | Q(package_owner_trainer_id=trainer_id)
            | Q(target_trainer_id_snapshot=trainer_id)
        )
    if payment_method in Payment.Method.values:
        qs = qs.filter(payment_method=payment_method)
    if context:
        qs = qs.filter(_finance_workspace_payment_context_q(context))
    return _filter_finance_workspace_dates(
        qs,
        club=club,
        date_from=date_from,
        date_to=date_to,
    )


def _finance_workspace_payment_queryset(*, club) -> QuerySet[Payment]:
    return (
        Payment.objects.for_club(club)
        .filter(deleted_at__isnull=True)
        .annotate(
            applied_discount_amount=models.ExpressionWrapper(
                models.F("original_amount") - models.F("amount"),
                output_field=models.DecimalField(max_digits=12, decimal_places=2),
            )
        )
        .select_related(
            "student",
            "tariff",
            "tariff__training_type",
            "recorded_by",
            "verified_by",
            "seller_trainer",
            "package_owner_trainer",
            "subscription",
            "subscription__renewed_from",
            "target_schedule",
            "target_training_group",
            "target_training_group__responsible_trainer",
            "target_group_membership",
            "personal_drop_in_payment_link",
            "personal_drop_in_payment_link__booking",
            "personal_drop_in_payment_link__booking__terms_snapshot",
            "personal_drop_in_payment_link__booking__enrollment",
            "personal_drop_in_payment_link__booking__enrollment__schedule",
            "personal_drop_in_payment_link__booking__enrollment__schedule__trainer",
            "personal_drop_in_payment_link__booking__enrollment__schedule__location",
            "personal_drop_in_payment_link__booking__enrollment__schedule__training_type",
        )
        .prefetch_related(
            applied_discounts_prefetch(club=club),
            active_package_allocation_prefetch(
                club=club,
                lookup="subscription__trainer_allocations",
            ),
        )
    )


def get_manual_payment_review_queue(
    *,
    club,
    trainer_id: int | None = None,
    payment_method: str = "",
    context: str = "",
    date_from: date | None = None,
    date_to: date | None = None,
) -> QuerySet[Payment]:
    """Pending cash/transfer rows that owner/admin may confirm or reject."""

    qs = _finance_workspace_payment_queryset(club=club).filter(
        status=Payment.Status.PENDING,
        payment_method__in=[Payment.Method.CASH, Payment.Method.TRANSFER],
    )
    return _filter_finance_workspace_payments(
        qs,
        club=club,
        trainer_id=trainer_id,
        payment_method=payment_method,
        context=context,
        date_from=date_from,
        date_to=date_to,
    ).order_by("created_at", "id")


def get_payment_history(
    *,
    club,
    status: str = "",
    trainer_id: int | None = None,
    payment_method: str = "",
    context: str = "",
    date_from: date | None = None,
    date_to: date | None = None,
) -> QuerySet[Payment]:
    """Terminal payment history; never an owner action queue."""

    qs = _finance_workspace_payment_queryset(club=club)
    if status != "all":
        qs = qs.filter(status__in=[Payment.Status.CONFIRMED, Payment.Status.REJECTED])
    if status in [Payment.Status.CONFIRMED, Payment.Status.REJECTED]:
        qs = qs.filter(status=status)
    return _filter_finance_workspace_payments(
        qs,
        club=club,
        trainer_id=trainer_id,
        payment_method=payment_method,
        context=context,
        date_from=date_from,
        date_to=date_to,
    ).order_by("-created_at", "-id")


def _get_online_payment_workspace_orders(
    *,
    club,
    status: str,
    trainer_id: int | None = None,
    context: str = "",
    date_from: date | None = None,
    date_to: date | None = None,
) -> QuerySet[BankPaymentOrder]:
    """Filter provider-backed workspace rows through their immutable payment context."""

    payment_ids = _filter_finance_workspace_payments(
        _finance_workspace_payment_queryset(club=club),
        club=club,
        trainer_id=trainer_id,
        payment_method=Payment.Method.ONLINE,
        context=context,
        date_from=None,
        date_to=None,
    ).order_by().values("id")
    qs = (
        get_bank_payment_orders(club=club, status=status)
        .filter(payment_id__in=payment_ids)
        .select_related(
            "payment__recorded_by",
            "payment__seller_trainer",
            "payment__target_schedule",
            "payment__target_training_group",
            "payment__subscription__renewed_from",
            "personal_payment_reservation",
            "personal_payment_reservation__trainer",
            "personal_payment_reservation__location",
            "personal_payment_reservation__training_type",
            "personal_payment_reservation__terms_snapshot",
            "personal_payment_reservation__schedule",
            "payment__personal_drop_in_payment_link",
            "payment__personal_drop_in_payment_link__booking",
            "payment__personal_drop_in_payment_link__booking__terms_snapshot",
            "payment__personal_drop_in_payment_link__booking__enrollment",
            "payment__personal_drop_in_payment_link__booking__enrollment__schedule",
            "payment__personal_drop_in_payment_link__booking__enrollment__schedule__trainer",
            "payment__personal_drop_in_payment_link__booking__enrollment__schedule__location",
            "payment__personal_drop_in_payment_link__booking__enrollment__schedule__training_type",
        )
        .prefetch_related(
            Prefetch(
                "provider_events",
                queryset=(
                    BankPaymentProviderEvent.objects.for_club(club)
                    .only(
                        "id",
                        "club_id",
                        "order_id",
                        "provider",
                        "provider_operation_id",
                        "provider_payment_link_id",
                        "provider_status",
                        "normalized_status_snapshot",
                        "provider_paid_at_snapshot",
                        "received_at",
                        "processed_at",
                        "processing_status",
                    )
                    .order_by("-received_at", "-id")
                ),
                to_attr="workspace_provider_events",
            )
        )
    )
    return _filter_finance_workspace_dates(
        qs,
        club=club,
        date_from=date_from,
        date_to=date_to,
    )


def get_online_payment_review_queue(
    *,
    club,
    trainer_id: int | None = None,
    context: str = "",
    date_from: date | None = None,
    date_to: date | None = None,
) -> QuerySet[BankPaymentOrder]:
    """Provider-backed exceptions only; generic Payment verification is forbidden."""

    return _get_online_payment_workspace_orders(
        club=club,
        status=BankPaymentOrder.Status.MANUAL_REVIEW,
        trainer_id=trainer_id,
        context=context,
        date_from=date_from,
        date_to=date_to,
    )


def get_online_refund_action_queue(
    *,
    club,
    trainer_id: int | None = None,
    context: str = "",
    date_from: date | None = None,
    date_to: date | None = None,
) -> QuerySet[PaymentRefundCase]:
    payment_ids = _filter_finance_workspace_payments(
        _finance_workspace_payment_queryset(club=club),
        club=club,
        trainer_id=trainer_id,
        payment_method=Payment.Method.ONLINE,
        context=context,
        date_from=None,
        date_to=None,
    ).values("id")
    qs = get_open_payment_refund_cases(club=club).exclude(
        order__status=BankPaymentOrder.Status.MANUAL_REVIEW,
    ).filter(order__payment_id__in=payment_ids)
    return _filter_finance_workspace_dates(
        qs,
        club=club,
        date_from=date_from,
        date_to=date_to,
        field_name="provider_refunded_at",
    )


def get_online_refund_payroll_action_queue(
    *,
    club,
    trainer_id: int | None = None,
    context: str = "",
    date_from: date | None = None,
    date_to: date | None = None,
) -> QuerySet[PaymentRefund]:
    payment_ids = _filter_finance_workspace_payments(
        _finance_workspace_payment_queryset(club=club),
        club=club,
        trainer_id=trainer_id,
        payment_method=Payment.Method.ONLINE,
        context=context,
        date_from=None,
        date_to=None,
    ).values("id")
    qs = get_payment_refunds_requiring_payroll(club=club).filter(
        payment_id__in=payment_ids,
    )
    date_from, date_to = _normalize_finance_workspace_dates(date_from, date_to)
    if date_from is not None:
        qs = qs.filter(accounting_date__gte=date_from)
    if date_to is not None:
        qs = qs.filter(accounting_date__lte=date_to)
    return qs


def get_live_online_payment_workspace_orders(
    *,
    club,
    trainer_id: int | None = None,
    context: str = "",
    date_from: date | None = None,
    date_to: date | None = None,
) -> QuerySet[BankPaymentOrder]:
    return _get_online_payment_workspace_orders(
        club=club,
        status="live",
        trainer_id=trainer_id,
        context=context,
        date_from=date_from,
        date_to=date_to,
    )


def get_bank_payment_orders(
    *,
    club,
    student_id: int | None = None,
    status: str | None = None,
) -> QuerySet[BankPaymentOrder]:
    qs = (
        BankPaymentOrder.objects.for_club(club)
        .select_related(
            "payment",
            "payment__tariff",
            "payment__tariff__training_type",
            "subscription",
            "renewed_from_subscription",
            "renewed_from_subscription__tariff",
            "student",
            "created_by",
        )
        .prefetch_related(
            Prefetch(
                "payment__settled_debts",
                queryset=Debt.objects.for_club(club)
                .filter(resolved_at__isnull=True)
                .only("id", "settlement_payment_id"),
                to_attr="open_settled_debts",
            )
        )
        .order_by("-created_at", "-id")
    )
    if student_id is not None:
        qs = qs.filter(student_id=student_id)
    if status == "live":
        qs = qs.filter(status__in=LIVE_BANK_PAYMENT_ORDER_STATUSES)
    elif status == "recent":
        qs = qs.filter(expires_at__gte=timezone.now() - timedelta(hours=24))
    elif status:
        qs = qs.filter(status=status)
    return qs


def get_bank_payment_order_by_id(*, club, order_id: int) -> BankPaymentOrder:
    return get_bank_payment_orders(club=club).get(id=order_id)


def get_active_discounts(*, club) -> QuerySet[Discount]:
    return Discount.objects.for_club(club).filter(is_active=True)


def get_subscription_freezes(*, club, subscription_id: int) -> QuerySet[SubscriptionFreeze]:
    return (
        SubscriptionFreeze.objects.for_club(club)
        .filter(subscription_id=subscription_id)
        .order_by("-starts_at")
    )


def get_expenses(*, club) -> QuerySet[Expense]:
    return Expense.objects.for_club(club).filter(deleted_at__isnull=True).order_by("-date")


def get_student_subscriptions(
    *, club, student_id: int, booking_date: date | None = None
) -> QuerySet[Subscription]:
    qs = (
        _with_paid_amount(
            Subscription.objects.for_club(club)
            .filter(student_id=student_id, deleted_at__isnull=True)
            .select_related("tariff", "tariff__training_type")
            .prefetch_related(
                active_package_allocation_prefetch(club=club),
                active_tariff_components_prefetch(club=club, lookup="tariff__components"),
                subscription_component_presence_prefetch(club=club),
                active_subscription_components_prefetch(club=club, booking_date=booking_date),
                pending_freezes_prefetch(club=club),
            )
        )
    )
    return _with_booking_date_context(qs, booking_date=booking_date)


def get_pending_subscription_freezes(*, club) -> QuerySet[SubscriptionFreeze]:
    return (
        SubscriptionFreeze.objects.for_club(club)
        .filter(status=SubscriptionFreeze.FreezeStatus.PENDING)
        .select_related(
            "subscription",
            "subscription__student",
            "subscription__tariff",
            "frozen_by",
        )
        .order_by("created_at", "id")
    )


def get_open_debts(*, club, student_id: int | None = None) -> QuerySet[Debt]:
    qs = (
        Debt.objects.for_club(club)
        .filter(resolved_at__isnull=True, settlement_payment__isnull=True)
        .select_related("student", "checkin", "checkin__training_type", "personal_drop_in_booking")
    )
    if student_id is not None:
        qs = qs.filter(student_id=student_id)
    return qs.order_by("-created_at", "-id")


def get_student_open_debts(*, club, student_id: int) -> QuerySet[Debt]:
    return get_open_debts(club=club, student_id=student_id)


def get_active_subscription(
    *,
    club_id: int,
    student_id: int,
    training_type_id: int,
    location,
) -> Subscription | None:
    now = timezone.now()
    qs = (
        Subscription.objects.for_club(club_id)
        .filter(
            student_id=student_id,
            status__in=[Subscription.Status.ACTIVE, Subscription.Status.PENDING],
            tariff__training_type_id=training_type_id,
            deleted_at__isnull=True,
        )
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        .filter(Q(trainings_left__isnull=True) | Q(trainings_left__gt=0))
        .select_related("tariff")
        .select_for_update()
    )

    # Prefer location-scoped subscription, fall back to club-scoped
    location_sub = qs.filter(scope=Tariff.Scope.LOCATION, location=location).first()
    if location_sub:
        return location_sub

    return qs.filter(scope=Tariff.Scope.CLUB).first()


def get_debtors(
    *,
    club,
    group_id: int | None = None,
    trainer_id: int | None = None,
    location_id: int | None = None,
    student_status: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> QuerySet[Debt]:
    qs = (
        Debt.objects.for_club(club)
        .filter(resolved_at__isnull=True, settlement_payment__isnull=True)
        .select_related(
            "student",
            "checkin__schedule",
            "checkin__trainer",
            "checkin__location",
            "personal_drop_in_booking",
        )
    )
    if group_id is not None:
        qs = qs.filter(checkin__schedule_id=group_id)
    if trainer_id is not None:
        qs = qs.filter(checkin__trainer_id=trainer_id)
    if location_id is not None:
        qs = qs.filter(checkin__location_id=location_id)
    if student_status is not None:
        qs = qs.filter(student__status=student_status)
    if date_from is not None:
        dt_from = timezone.make_aware(datetime.combine(date_from, time.min))
        qs = qs.filter(created_at__gte=dt_from)
    if date_to is not None:
        dt_to = timezone.make_aware(datetime.combine(date_to + timedelta(days=1), time.min))
        qs = qs.filter(created_at__lt=dt_to)
    return qs.order_by("-created_at")


def apply_debtor_quick_filter(debtors: QuerySet[Debt], *, quick_filter: str) -> QuerySet[Debt]:
    if quick_filter == "week":
        week_ago = timezone.now() - timedelta(days=7)
        return debtors.filter(created_at__lte=week_ago)
    if quick_filter == "large":
        return debtors.filter(tariff_price__gte=5000)
    if quick_filter == "unverified":
        return debtors.filter(resolution_type="")
    return debtors


def export_debtors_excel(
    *,
    club,
    group_id: int | None = None,
    trainer_id: int | None = None,
    location_id: int | None = None,
    student_status: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    quick_filter: str = "",
) -> bytes:
    import openpyxl

    debtors = apply_debtor_quick_filter(
        get_debtors(
            club=club,
            group_id=group_id,
            trainer_id=trainer_id,
            location_id=location_id,
            student_status=student_status,
            date_from=date_from,
            date_to=date_to,
        ),
        quick_filter=quick_filter,
    )
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Debtors"
    ws.append(["Student", "Phone", "Trainer", "Group", "Location", "Amount", "Date", "Reason"])

    for debt in debtors.iterator(chunk_size=500):
        checkin = debt.checkin
        ws.append(
            [
                str(debt.student) if debt.student else "",
                debt.student.phone if debt.student else "",
                str(checkin.trainer) if checkin else "",
                checkin.schedule.group_name if checkin and checkin.schedule else "",
                str(checkin.location) if checkin else "",
                str(debt.tariff_price) if debt.tariff_price else "",
                debt.created_at.strftime("%Y-%m-%d"),
                debt.reason,
            ]
        )

    output = BytesIO()
    wb.save(output)
    return output.getvalue()
