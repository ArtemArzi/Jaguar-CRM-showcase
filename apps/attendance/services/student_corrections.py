"""One-student attendance commands; never close or mark a whole session."""

from dataclasses import asdict
from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.attendance.models import (
    Checkin,
    PersonalDropInBooking,
    ScheduleEnrollment,
    StudentAttendanceCorrection,
    TrainingGroupMembership,
)
from apps.attendance.services import checkin as common
from apps.attendance.services.training_group_memberships import lock_training_group_mutation_scope
from apps.attendance.training_group_roster import _enrollment_is_effective
from apps.billing.models import (
    OpeningEntitlementSnapshot,
    Payment,
    Subscription,
    SubscriptionComponent,
    SubscriptionCorrection,
    SubscriptionFreeze,
    Tariff,
    TariffComponent,
)
from apps.billing.service_modules.opening_attendance import assert_opening_attendance_not_covered
from apps.billing.service_modules.subscription_corrections import _authorize, _digest, _fail, _json, _snapshot
from apps.clubs.models import Club
from apps.clubs.timezones import club_local_day_start_by_id, club_localdate, club_zoneinfo
from apps.students.models import Student
from apps.trainers.models import Trainer
from apps.trainers.services import assert_trainer_payroll_date_open


def _roster_evidence(*, schedule, student, day):
    """Require dated membership/booking evidence, never inferred attendance."""
    if student.deleted_at is not None or student.status in {Student.Status.LEAD, Student.Status.LOST}:
        _fail("Ученик недоступен для отметки.", "student_ineligible")
    if schedule.training_group_id:
        rows = list(
            TrainingGroupMembership.objects.for_club(schedule.club_id)
            .filter(
                student=student,
                training_group_id=schedule.training_group_id,
                starts_on__lte=day,
            )
            .filter(Q(ends_on__isnull=True) | Q(ends_on__gte=day))
            .order_by("id")
        )
        # A terminal membership with a recorded end can prove its earlier
        # interval. The terminal day itself needs an explicit history review.
        eligible = [
            m
            for m in rows
            if m.status == "active" or (m.status in {"cancelled", "transferred"} and m.ends_on and day < m.ends_on)
        ]
        if len(eligible) != 1 or any(m.status == "frozen" for m in rows):
            _fail("Нужно сверить членство в группе на дату занятия.", "attendance_membership_needs_review")
        m = eligible[0]
        return {"membership_id": m.id, "status": m.status, "starts_on": m.starts_on, "ends_on": m.ends_on}
    rows = list(
        ScheduleEnrollment.objects.for_club(schedule.club_id)
        .filter(
            student=student,
            schedule=schedule,
        )
        .order_by("id")
    )
    eligible = [e for e in rows if e.starts_on is not None and _enrollment_is_effective(e, target_date=day)]
    if len(eligible) != 1 or eligible[0].status == "frozen":
        _fail("Нужно сверить запись ученика на дату занятия.", "attendance_membership_needs_review")
    e = eligible[0]
    return {"enrollment_id": e.id, "status": e.status, "starts_on": e.starts_on, "ends_on": e.ends_on}


def _locked_preview(*, club_id, student_id, schedule_id, checkin_date, subscription_id, component_id, allow_debt):
    from apps.attendance.selectors import _kiosk_checkin_window
    from apps.attendance.services.subscription_lifecycle import assert_checkin_cancellation_entitlement

    lock_training_group_mutation_scope(club_id=club_id, no_key=True)
    club = Club.objects.get(id=club_id)
    if checkin_date > club_localdate(club):
        _fail("Будущее посещение отмечать нельзя.", "attendance_future_date")
    # Substitutes must join the sorted trainer prefix before student roots.
    from apps.attendance.models import Schedule, ScheduleException

    try:
        schedule_info = Schedule.objects.for_club(club_id).get(id=schedule_id)
    except Schedule.DoesNotExist:
        _fail("Занятие не найдено. Выберите доступное занятие.", "attendance_schedule_not_found")
    trainer_ids = set(
        ScheduleException.objects.for_club(club_id)
        .filter(
            schedule=schedule_info,
            substitute_trainer_id__isnull=False,
        )
        .values_list("substitute_trainer_id", flat=True)
    ) | {schedule_info.trainer_id}
    list(Trainer.objects.for_club(club_id).select_for_update().filter(id__in=trainer_ids).order_by("id"))
    schedule, students = common._lock_attendance_identity_scope(
        club_id=club_id,
        schedule_id=schedule_id,
        student_ids=[student_id],
    )
    student = students[student_id]
    occurrence = common._get_schedule_occurrence(schedule=schedule, checkin_date=checkin_date)
    if checkin_date == club_localdate(club) and not common._is_group_session_closed(
        schedule=schedule,
        checkin_date=checkin_date,
    ):
        window = _kiosk_checkin_window(
            club=club,
            effective_date=occurrence.effective_date,
            effective_start_time=occurrence.effective_start_time,
            effective_end_time=occurrence.effective_end_time,
            current_time=timezone.now(),
        )
        if window["checkin_window_status"] != "open":
            _fail("Отметка сейчас вне допустимого времени занятия.", "attendance_window_closed")
    roster = _roster_evidence(schedule=schedule, student=student, day=checkin_date)
    if (
        PersonalDropInBooking.objects.for_club(club_id)
        .filter(
            enrollment__student=student,
            enrollment__schedule=schedule,
            enrollment__starts_on=checkin_date,
        )
        .exists()
    ):
        _fail("Используйте исправление разового персонального занятия.", "attendance_exact_drop_in_required")
    if (
        Checkin.objects.for_club(club_id)
        .filter(
            student=student,
            schedule=schedule,
            date=checkin_date,
            deleted_at__isnull=True,
        )
        .exists()
    ):
        _fail("Посещение уже отмечено.", "attendance_already_recorded")
    trainer = Trainer.objects.for_club(club_id).get(id=occurrence.trainer_id)
    selected_source = common._selected_personal_booking_entitlement_evidence(
        club_id=club_id, student_id=student_id, schedule_id=schedule_id, checkin_date=checkin_date,
    )
    if selected_source[0] is not None and selected_source != (subscription_id, component_id):
        _fail("При записи выбран другой пакет. Нужно сверить основание.", "attendance_selected_source_conflict")
    assert_trainer_payroll_date_open(club_id=club_id, target_date=checkin_date)
    subscription = component = None
    financial = {"subscription_id": None, "component_id": None, "debt": True}
    if subscription_id is not None:
        payments = list(
            Payment.objects.for_club(club_id)
            .select_for_update()
            .filter(
                subscription_id=subscription_id,
            )
            .order_by("id")
        )
        subscription = (
            Subscription.objects.for_club(club_id)
            .select_for_update()
            .filter(
                id=subscription_id,
                student=student,
                deleted_at__isnull=True,
            )
            .first()
        )
        if subscription is None or subscription.status not in {"active", "expired"}:
            _fail("Выбранный абонемент недоступен.", "attendance_entitlement_unavailable")
        components = list(
            SubscriptionComponent.objects.for_club(club_id)
            .select_for_update()
            .filter(
                subscription=subscription,
            )
            .order_by("id")
        )
        component = next((c for c in components if c.id == component_id), None)
        if component is None or not component.is_active or component.training_type_id != schedule.training_type_id:
            _fail("Нужно сверить точный компонент абонемента.", "attendance_entitlement_needs_review")
        if component.scope == Tariff.Scope.LOCATION and component.location_id != schedule.location_id:
            _fail("Абонемент не действует в этом зале.", "attendance_entitlement_scope")
        finite = component.entitlement_kind == TariffComponent.EntitlementKind.FINITE_CREDITS
        if not finite and checkin_date < club_localdate(club):
            _fail("Исторический лимит нужно сверить отдельно.", "attendance_entitlement_needs_review")
        if finite and (not component.credits_left or component.credits_left < 1):
            _fail("В выбранном компоненте нет остатка.", "attendance_entitlement_exhausted")
        if component.entitlement_kind == TariffComponent.EntitlementKind.WEEKLY_LIMIT and not (
            common._component_has_weekly_capacity(component=component, checkin_date=checkin_date)
        ):
            _fail("Лимит посещений на эту неделю исчерпан.", "attendance_weekly_limit")
        assert_checkin_cancellation_entitlement(subscription=subscription, component=component)
        if (
            SubscriptionFreeze.objects.for_club(club_id)
            .filter(subscription=subscription)
            .exclude(status="rejected")
            .exists()
        ):
            _fail("Нужно сверить историческое покрытие заморозки.", "attendance_freeze_needs_review")
        snapshot = OpeningEntitlementSnapshot.objects.for_club(club_id).filter(subscription=subscription).first()
        starts_at = timezone.make_aware(
            datetime.combine(checkin_date, occurrence.effective_start_time), club_zoneinfo(club),
        )
        if checkin_date == club_localdate(club):
            # Current attendance uses the accepted effective right, including
            # S4 corrections. Original terms only prove historical coverage.
            covered = subscription.expires_at is None or starts_at < subscription.expires_at
        else:
            corrections = list(SubscriptionCorrection.objects.for_club(club_id).filter(
                subscription=subscription,
            ).order_by("created_at", "id"))
            expiry = subscription.expires_at
            if snapshot:
                expiry = club_local_day_start_by_id(club_id, snapshot.expires_on + timedelta(days=1))
            elif corrections:
                raw = corrections[0].before.get("expires_at")
                expiry = datetime.fromisoformat(raw) if raw else None
            for correction in corrections:
                if correction.created_at <= starts_at:
                    raw = correction.after.get("expires_at")
                    expiry = datetime.fromisoformat(raw) if raw else None
            covered = expiry is not None and starts_at < expiry
        if snapshot:
            covered = covered and snapshot.started_on <= checkin_date
        elif checkin_date < club_localdate(club):
            covered = covered and subscription.activated_at <= starts_at and any(
                p.status == "confirmed" and p.verified_at and p.verified_at <= starts_at for p in payments
            )
        else:
            covered = covered and club_localdate(club, subscription.activated_at) <= checkin_date and any(
                p.status == "confirmed" and p.verified_at and club_localdate(club, p.verified_at) <= checkin_date
                for p in payments
            )
        if not covered:
            _fail("Нужно сверить основание абонемента на дату занятия.", "attendance_entitlement_needs_review")
        financial = {
            "subscription_id": subscription.id,
            "component_id": component.id,
            "state": _snapshot(subscription, components),
            "payments": [(p.id, p.status, p.updated_at) for p in payments],
            "component_terms": {
                "kind": component.entitlement_kind, "weekly_limit": component.weekly_limit,
                "payout_policy": component.trainer_payout_policy_snapshot,
            },
        }
        from apps.attendance.services.enrollment import _active_personal_entitlement_reservations

        financial["reservations"] = list(_active_personal_entitlement_reservations(
            subscription=subscription,
        ).order_by("id").values("id", "metadata"))
    else:
        if component_id is not None or not allow_debt or schedule.training_type.drop_in_price is None:
            _fail("Выберите абонемент или явно подтвердите занятие в долг.", "attendance_debt_confirmation_required")
        # This explicit branch is the existing generic drop-in debt, not an
        # alternative route around a pending payment-owned admission.
        if Payment.objects.for_club(club_id).filter(student=student, status="pending").exists():
            _fail("Сначала завершите связанную оплату.", "attendance_pending_payment")
        financial["price"] = str(schedule.training_type.drop_in_price)
        if SubscriptionComponent.objects.for_club(club_id).filter(
            subscription__student=student, subscription__status="active",
            subscription__deleted_at__isnull=True, training_type_id=schedule.training_type_id,
            is_active=True,
        ).filter(common._active_through_date_q(
            field_name="subscription__expires_at", target_date=checkin_date, club_id=club_id,
        )).exists():
            _fail("Сначала выберите или сверьте действующий компонент.", "attendance_existing_component")
    assert_opening_attendance_not_covered(
        club_id=club_id,
        student_id=student_id,
        schedule=schedule,
        checkin_date=checkin_date,
        subscription=subscription,
    )
    financial["salary_snapshot"] = common._salary_snapshot_payload_for_checkin(
        checkin=Checkin(club=club, student=student, schedule=schedule,
                       training_type=schedule.training_type, trainer=trainer, location=schedule.location,
                       date=checkin_date, subscription=subscription, subscription_component=component),
        training_type=schedule.training_type, club_id=club_id,
    )
    if component and component.trainer_payout_policy_snapshot == Tariff.PayoutPolicy.ON_CHECKIN and (
        financial["salary_snapshot"]["rate_percent_snapshot"] is None
    ):
        _fail("Для тренера не задана ставка. Сначала уточните начисление.", "trainer_rate_not_set")
    evidence = _json(
        {
            "student_id": student_id,
            "student_status": student.status,
            "schedule_id": schedule.id,
            "day": checkin_date,
            "trainer_id": trainer.id,
            "occurrence": asdict(occurrence),
            "roster": roster,
            "financial": financial,
        }
    )
    return {"fingerprint": _digest(evidence), "evidence": evidence}, (
        club,
        schedule,
        student,
        trainer,
        subscription,
        component,
    )


@transaction.atomic
def preview_student_attendance(
    *,
    club_id,
    actor_user_id,
    student_id,
    schedule_id,
    checkin_date,
    subscription_id=None,
    component_id=None,
    allow_debt=False,
):
    _authorize(club_id=club_id, actor_user_id=actor_user_id)
    preview, _ = _locked_preview(
        club_id=club_id,
        student_id=student_id,
        schedule_id=schedule_id,
        checkin_date=checkin_date,
        subscription_id=subscription_id,
        component_id=component_id,
        allow_debt=allow_debt,
    )
    return preview


@transaction.atomic
def record_student_attendance(
    *,
    club_id,
    actor_user_id,
    student_id,
    schedule_id,
    checkin_date,
    expected_fingerprint,
    command_key,
    reason,
    channel="admin",
    subscription_id=None,
    component_id=None,
    allow_debt=False,
    notify_parent=False,
):
    _authorize(club_id=club_id, actor_user_id=actor_user_id)
    reason, command_key = reason.strip(), command_key.strip()
    if not reason or len(reason) > 1000 or not command_key or len(command_key) > 120 or channel not in {"admin", "cli"}:
        _fail("Укажите причину и ключ операции.", "attendance_command_invalid")
    payload = _digest(
        {
            "student": student_id,
            "schedule": schedule_id,
            "date": checkin_date,
            "subscription": subscription_id,
            "component": component_id,
            "allow_debt": allow_debt,
            "notify_parent": notify_parent,
            "reason": reason,
            "actor": actor_user_id,
            "channel": channel,
            "preview": expected_fingerprint,
            "action": "record",
        }
    )
    Club.objects.select_for_update(no_key=True).get(id=club_id)
    receipt = StudentAttendanceCorrection.objects.for_club(club_id).filter(command_key=command_key).first()
    if receipt:
        if receipt.payload_fingerprint != payload:
            _fail("Ключ уже использован для другой операции.", "attendance_key_conflict")
        return receipt
    if not settings.STUDENT_ADMIN_CORRECTIONS_ENABLED:
        _fail("Исправления временно отключены.", "student_corrections_disabled")
    preview, scope = _locked_preview(
        club_id=club_id,
        student_id=student_id,
        schedule_id=schedule_id,
        checkin_date=checkin_date,
        subscription_id=subscription_id,
        component_id=component_id,
        allow_debt=allow_debt,
    )
    if preview["fingerprint"] != expected_fingerprint:
        _fail("Данные изменились. Откройте предварительный результат заново.", "attendance_preview_stale")
    club, schedule, student, trainer, subscription, component = scope
    checkin = Checkin.objects.create(
        club_id=club_id,
        student=student,
        schedule=schedule,
        training_type=schedule.training_type,
        trainer=trainer,
        location=schedule.location,
        date=checkin_date,
        source="manual",
        notification_policy=(
            Checkin.NotificationPolicy.DATED_CORRECTION
            if notify_parent
            else Checkin.NotificationPolicy.SILENT_CORRECTION
        ),
    )
    common._deduct_subscription(
        student=student,
        club_id=club_id,
        schedule=schedule,
        club=club,
        training_type_id=schedule.training_type_id,
        location=schedule.location,
        checkin=checkin,
        checkin_date=checkin_date,
        subscription=subscription,
        subscription_component=component,
    )
    Student.objects.for_club(club_id).filter(id=student_id).filter(
        Q(last_visit_date__isnull=True) | Q(last_visit_date__lt=checkin_date),
    ).update(last_visit_date=checkin_date)
    if checkin_date == club_localdate(club) and student.status in {Student.Status.AT_RISK, Student.Status.CHURNED}:
        from apps.students.services import reactivate_student

        reactivate_student(student_id=student_id, club_id=club_id)
    events = common._record_checkin_cascade_events(
        checkin=checkin,
        student=student,
        training_type=schedule.training_type,
        club_id=club_id,
        trainer_id=trainer.id,
        skip_group_analytics=False,
    )
    receipt = StudentAttendanceCorrection.objects.create(
        club_id=club_id,
        checkin=checkin,
        actor_id=actor_user_id,
        action="record",
        channel=channel,
        reason=reason,
        command_key=command_key,
        payload_fingerprint=payload,
        before=preview["evidence"],
        after=_json(
            {
                "checkin_id": checkin.id,
                "subscription_id": checkin.subscription_id,
                "component_id": checkin.subscription_component_id,
                "is_debt": checkin.is_debt,
                "notification_policy": checkin.notification_policy,
                "credits_left": component.credits_left if component else None,
                "credits_used": component.credits_used if component else None,
            }
        ),
    )
    common._enqueue_checkin_tasks(cascade_events=events, defer_until_commit=True)
    return receipt


@transaction.atomic
def cancel_student_attendance(*, club_id, actor_user_id, student_id, checkin_id, command_key, reason, channel="admin"):
    _authorize(club_id=club_id, actor_user_id=actor_user_id)
    reason, command_key = reason.strip(), command_key.strip()
    if not reason or len(reason) > 1000 or not command_key or len(command_key) > 120 or channel not in {"admin", "cli"}:
        _fail("Укажите причину и ключ операции.", "attendance_command_invalid")
    payload = _digest(
        {
            "student": student_id,
            "checkin": checkin_id,
            "reason": reason,
            "actor": actor_user_id,
            "channel": channel,
            "action": "cancel",
        }
    )
    # The shared cancellation owner and its nested salary reversal use this
    # same leading payroll lock. Acquire it before receipt/identity reads.
    lock_training_group_mutation_scope(club_id=club_id)
    receipt = StudentAttendanceCorrection.objects.for_club(club_id).filter(command_key=command_key).first()
    if receipt:
        if receipt.payload_fingerprint != payload:
            _fail("Ключ уже использован для другой операции.", "attendance_key_conflict")
        return receipt
    target = Checkin.objects.for_club(club_id).filter(id=checkin_id, student_id=student_id).first()
    if target is None:
        _fail("Посещение недоступно.", "attendance_target_unavailable")
    # Cancellation drains accepted attendance even when new corrections are off.
    if target.cancelled_at or target.deleted_at:
        _fail("Посещение уже отменено.", "attendance_already_cancelled")
    common.cancel_checkin(
        checkin_id=checkin_id,
        club_id=club_id,
        cancelled_by_user_id=actor_user_id,
        user_role="owner",
        reason=reason,
        channel=channel,
        command_key=command_key,
        payload_fingerprint=payload,
        _defer_async_until_commit=True,
    )
    return StudentAttendanceCorrection.objects.for_club(club_id).get(command_key=command_key)
