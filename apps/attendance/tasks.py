from __future__ import annotations

import logging
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.attendance.models import Checkin, CheckinCascadeEvent, GroupSession
from apps.billing.models import Tariff, TrainingType
from apps.common.exceptions import BusinessLogicError
from apps.grades.models import GradeProgressEvent, StudentGrade
from apps.grades.services import decrement_grade_progress, increment_grade_progress
from apps.trainers.models import TrainerEarning, TrainerEarningAdjustment

logger = logging.getLogger(__name__)

SALARY_SNAPSHOT_BASIS = "checkin_salary_snapshot"
SALARY_SNAPSHOT_QUEUE_PROVENANCE = "checkin_queue"
SALARY_SNAPSHOT_LEGACY_PROVENANCE = "legacy_backfill_current_state"


def _decimal_or_none(value) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _salary_snapshot_for_checkin(*, checkin_id: int, club_id: int) -> dict | None:
    event = (
        CheckinCascadeEvent.objects.for_club(club_id)
        .filter(
            checkin_id=checkin_id,
            effect=CheckinCascadeEvent.Effect.SALARY,
            expected=True,
        )
        .order_by("-created_at", "-id")
        .first()
    )
    if event is None:
        return None
    payload = event.payload or {}
    if payload.get("calculation_basis") != SALARY_SNAPSHOT_BASIS:
        raise BusinessLogicError(
            f"Снапшот расчета зарплаты отсутствует для checkin {checkin_id}",
            code="salary_snapshot_missing",
        )
    provenance = payload.get("snapshot_provenance")
    if provenance == SALARY_SNAPSHOT_LEGACY_PROVENANCE:
        raise BusinessLogicError(
            f"Исторический снапшот зарплаты требует ручной сверки (checkin {checkin_id})",
            code="salary_snapshot_legacy_reconciliation_required",
        )
    if provenance != SALARY_SNAPSHOT_QUEUE_PROVENANCE:
        raise BusinessLogicError(
            f"Источник снапшота зарплаты не подтвержден для checkin {checkin_id}",
            code="salary_snapshot_missing",
        )
    return payload


def calculate_salary(checkin_id: int, club_id: int) -> None:
    # Idempotency guard
    if TrainerEarning.objects.for_club(club_id).filter(checkin_id=checkin_id).exists():
        from apps.trainers.services import record_checkin_package_transfer

        record_checkin_package_transfer(checkin_id=checkin_id, club_id=club_id)
        logger.info("salary_already_calculated", extra={"checkin_id": checkin_id})
        return

    checkin = (
        Checkin.objects.select_related(
            "trainer",
            "subscription__tariff",
            "subscription_component",
            "training_type",
            "location",
        )
        .filter(
            id=checkin_id,
            club_id=club_id,
            deleted_at__isnull=True,
            cancelled_at__isnull=True,
        )
        .first()
    )
    if checkin is None:
        return

    # No earning for debt checkins
    if checkin.is_debt or not checkin.subscription:
        return

    if (
        checkin.subscription_component_id
        and checkin.subscription_component.trainer_payout_policy_snapshot
        in {Tariff.PayoutPolicy.ON_PAYMENT, Tariff.PayoutPolicy.NONE}
    ):
        logger.info(
            "salary_skipped_payout_policy",
            extra={
                "checkin_id": checkin_id,
                "club_id": club_id,
                "payout_policy": checkin.subscription_component.trainer_payout_policy_snapshot,
            },
        )
        return

    snapshot = _salary_snapshot_for_checkin(checkin_id=checkin_id, club_id=club_id)
    earning_type = snapshot["training_type_kind_snapshot"] if snapshot is not None else checkin.training_type.kind
    payout_policy = snapshot.get("payout_policy_snapshot") if snapshot is not None else ""

    # Sale-paid and disabled components never create per-checkin salary.
    if payout_policy in {Tariff.PayoutPolicy.ON_PAYMENT, Tariff.PayoutPolicy.NONE}:
        logger.info(
            "salary_skipped_payout_policy",
            extra={"checkin_id": checkin_id, "club_id": club_id, "payout_policy": payout_policy},
        )
        return

    # Legacy fallback: old GROUP rows were paid once at sale confirmation.
    if not payout_policy and earning_type == TrainingType.Kind.GROUP:
        logger.info(
            "salary_skipped_group_paid_on_sale",
            extra={"checkin_id": checkin_id, "club_id": club_id},
        )
        return

    if snapshot is None:
        raise BusinessLogicError(
            f"Снапшот расчета зарплаты отсутствует для checkin {checkin_id}",
            code="salary_snapshot_missing",
        )

    rate = _decimal_or_none(snapshot.get("rate_percent_snapshot"))
    tariff_price = _decimal_or_none(snapshot.get("subscription_price_snapshot"))
    component_id = snapshot.get("component_id_snapshot")
    component_paid_amount_basis = _decimal_or_none(snapshot.get("component_paid_amount_basis_snapshot"))
    trainer_id = int(snapshot["trainer_id_snapshot"])

    if rate is None:
        # Fail loud: owner sees the failed task in /admin/ instead of
        # silently under-paying the trainer.
        raise BusinessLogicError(
            f"Ставка не задана для тренера/локации/типа (checkin {checkin_id})",
            code="trainer_rate_not_set",
        )
    if tariff_price is None:
        raise BusinessLogicError(
            f"Цена абонемента не зафиксирована для расчета зарплаты (checkin {checkin_id})",
            code="salary_price_snapshot_missing",
        )

    amount = tariff_price * rate / 100

    from apps.trainers.services import (
        assert_trainer_payroll_date_open,
        lock_trainer_payroll_mutation_scope,
        record_checkin_package_transfer,
    )

    with transaction.atomic():
        lock_trainer_payroll_mutation_scope(club_id=club_id)
        if not Checkin.objects.for_club(club_id).filter(
            id=checkin_id, cancelled_at__isnull=True, deleted_at__isnull=True,
        ).exists():
            return
        if TrainerEarning.objects.for_club(club_id).filter(checkin_id=checkin_id).exists():
            record_checkin_package_transfer(checkin_id=checkin_id, club_id=club_id)
            logger.info("salary_already_calculated", extra={"checkin_id": checkin_id})
            return
        assert_trainer_payroll_date_open(club_id=club_id, target_date=checkin.date)
        try:
            earning = TrainerEarning.objects.create(
                club_id=club_id,
                trainer_id=trainer_id,
                checkin=checkin,
                subscription_component_id=component_id or checkin.subscription_component_id,
                earning_type=earning_type,
                amount=amount,
                rate_percent=rate,
                subscription_price=tariff_price,
                payout_policy_snapshot=payout_policy,
                component_id_snapshot=component_id,
                component_paid_amount_basis_snapshot=component_paid_amount_basis,
            )
            from apps.trainers.settlement_services import note_earning_created

            note_earning_created(earning=earning)
        except IntegrityError:
            if TrainerEarning.objects.for_club(club_id).filter(checkin_id=checkin_id).exists():
                record_checkin_package_transfer(checkin_id=checkin_id, club_id=club_id)
                logger.info("salary_race_skipped", extra={"checkin_id": checkin_id})
                return
            raise

    record_checkin_package_transfer(checkin_id=checkin_id, club_id=club_id)

    logger.info(
        "salary_calculated",
        extra={"checkin_id": checkin_id, "club_id": club_id},
    )


def update_grade_progress(checkin_id: int, club_id: int) -> None:
    checkin = (
        Checkin.objects.select_related("training_type").filter(
            id=checkin_id,
            club_id=club_id,
            deleted_at__isnull=True,
            cancelled_at__isnull=True,
        )
        .first()
    )
    if checkin is None:
        return

    grade_system_id = checkin.training_type.grade_system_id
    if grade_system_id is None:
        logger.info(
            "grade_progress_skipped_unmapped_training_type",
            extra={"checkin_id": checkin_id, "training_type_id": checkin.training_type_id},
        )
        return

    student_grades = StudentGrade.objects.for_club(club_id).filter(
        student_id=checkin.student_id,
        grade_system_id=grade_system_id,
    )
    for sg in student_grades:
        try:
            _, created = GradeProgressEvent.objects.get_or_create(
                club_id=club_id,
                student_grade=sg,
                checkin=checkin,
            )
        except IntegrityError:
            created = False

        if not created:
            logger.info(
                "grade_progress_already_counted",
                extra={"checkin_id": checkin_id, "student_grade_id": sg.id},
            )
            continue
        increment_grade_progress(
            club_id=club_id,
            student_id=checkin.student_id,
            grade_system_id=sg.grade_system_id,
        )
        # Mark this checkin as last counted (for exact-duplicate idempotency only)
        StudentGrade.objects.for_club(club_id).filter(id=sg.id).update(last_counted_checkin_id=checkin_id)

    logger.info(
        "grade_progress_updated",
        extra={"checkin_id": checkin_id, "student_id": checkin.student_id, "club_id": club_id},
    )


def update_group_analytics(checkin_id: int, club_id: int) -> None:
    checkin = Checkin.objects.get(id=checkin_id, club_id=club_id)

    count = (
        Checkin.objects.for_club(club_id)
        .filter(
            schedule_id=checkin.schedule_id,
            date=checkin.date,
            deleted_at__isnull=True,
        )
        .count()
    )

    GroupSession.objects.update_or_create(
        club_id=club_id,
        schedule_id=checkin.schedule_id,
        date=checkin.date,
        defaults={
            "trainer_id": checkin.trainer_id,
            "attendee_count": count,
        },
    )

    logger.info(
        "group_analytics_updated",
        extra={"checkin_id": checkin_id, "count": count, "club_id": club_id},
    )


def log_parent_event(checkin_id: int, club_id: int) -> None:
    """Send push to parent when child checks in."""
    checkin = (
        Checkin.objects.select_related("student", "subscription__tariff")
        .filter(
            id=checkin_id,
            club_id=club_id,
            deleted_at__isnull=True,
            cancelled_at__isnull=True,
        )
        .first()
    )
    if checkin is None:
        return

    if not checkin.student.is_child or not checkin.student.parent_user_id:
        return

    if checkin.notification_policy == Checkin.NotificationPolicy.SILENT_CORRECTION or checkin.parent_notified_at:
        return
    dated = checkin.notification_policy == Checkin.NotificationPolicy.DATED_CORRECTION

    sub = checkin.subscription

    if sub and sub.tariff.trainings_limit:
        progress = f"{sub.trainings_used} из {sub.tariff.trainings_limit}"
    else:
        progress = None

    body = f"{checkin.student.first_name} был(а) на тренировке."
    if progress:
        body += f" {progress}."
    if dated:
        body = f"Уточнено посещение: {checkin.student.first_name}, тренировка {checkin.date:%d.%m.%Y}."

    from apps.notifications.routes import parent_child_url
    from apps.notifications.services import send_parent_notification

    sent = send_parent_notification(
        club=checkin.club,
        student=checkin.student,
        notification_type="parent_checkin",
        context={
            "name": checkin.student.first_name,
            "full_name": str(checkin.student),
            "progress": progress or "",
        },
        fallback_title="Уточнение посещения" if dated else "Чек-ин",
        fallback_body=body,
        url=parent_child_url(checkin.student_id),
        record_type=f"parent_checkin:{checkin.id}",
        **({"use_fallback_content": True} if dated else {}),
    )

    if sent:
        Checkin.objects.for_club(club_id).filter(id=checkin.id, parent_notified_at__isnull=True).update(
            parent_notified_at=timezone.now(),
        )
        # Cancellation may have committed while the push provider was sending.
        # Its reverse task then legitimately saw no arrival fact. Complete the
        # dated delivery with a fresh cancellation read so that interleaving
        # cannot permanently suppress the compensating message.
        if dated and Checkin.objects.for_club(club_id).filter(id=checkin.id).filter(
            Q(cancelled_at__isnull=False) | Q(deleted_at__isnull=False),
        ).exists():
            reverse_parent_checkin_push(checkin.id, club_id=club_id)
        logger.info(
            "parent_checkin_push_sent",
            extra={
                "checkin_id": checkin_id,
                "student_id": checkin.student_id,
                "parent_user_id": checkin.student.parent_user_id,
                "club_id": club_id,
            },
        )


def reverse_salary(checkin_id: int, club_id: int) -> None:
    active_earning_exists = TrainerEarning.objects.for_club(club_id).filter(
        checkin_id=checkin_id,
        earning_source=TrainerEarning.Source.CHECKIN,
        cancelled=False,
    ).exists()
    active_manual_correction_exists = TrainerEarningAdjustment.objects.for_club(club_id).filter(
        source_checkin_id=checkin_id,
        kind=TrainerEarningAdjustment.Kind.MANUAL_ADJUSTMENT,
        affects_payroll=True,
    ).exists()
    if not active_earning_exists and not active_manual_correction_exists:
        logger.info("salary_reverse_already_applied", extra={"checkin_id": checkin_id, "club_id": club_id})
        return

    checkin = Checkin._base_manager.filter(id=checkin_id, club_id=club_id).only("id", "date").first()
    if checkin is None:
        return

    from apps.trainers.services import (
        assert_trainer_payroll_date_open,
        lock_trainer_payroll_mutation_scope,
    )

    with transaction.atomic():
        lock_trainer_payroll_mutation_scope(club_id=club_id)
        assert_trainer_payroll_date_open(club_id=club_id, target_date=checkin.date)
        from apps.trainers.settlement_services import record_historical_settlement_change

        for earning in TrainerEarning.objects.for_club(club_id).filter(
            checkin_id=checkin_id, earning_source=TrainerEarning.Source.CHECKIN, cancelled=False,
        ).order_by("trainer_id", "id"):
            record_historical_settlement_change(
                club_id=club_id, trainer_id=earning.trainer_id, effective_on=checkin.date,
                event_key=f"earning:{earning.id}:cancelled", suggested_delta=-earning.amount,
                evidence={"earning_id": earning.id, "checkin_id": checkin_id},
            )
        # Defensive: only reverse per-checkin earnings; never touch sale earnings.
        TrainerEarning.objects.for_club(club_id).filter(
            checkin_id=checkin_id,
            earning_source=TrainerEarning.Source.CHECKIN,
        ).update(cancelled=True)
        from apps.trainers.services import (
            reverse_checkin_manual_corrections,
            reverse_checkin_package_transfer,
        )

        reverse_checkin_package_transfer(checkin_id=checkin_id, club_id=club_id)
        reverse_checkin_manual_corrections(checkin_id=checkin_id, club_id=club_id)
    logger.info("salary_reversed", extra={"checkin_id": checkin_id, "club_id": club_id})


def reverse_parent_checkin_push(checkin_id: int, club_id: int) -> None:
    """T4: compensating push when a child's checkin is cancelled."""
    checkin = Checkin._base_manager.filter(id=checkin_id, club_id=club_id).select_related("student").first()
    if checkin is None:
        return
    if not checkin.student.is_child or not checkin.student.parent_user_id:
        return
    if (
        checkin.notification_policy == Checkin.NotificationPolicy.SILENT_CORRECTION
        or checkin.parent_cancellation_notified_at
    ):
        return
    dated = checkin.notification_policy == Checkin.NotificationPolicy.DATED_CORRECTION
    if dated and checkin.parent_notified_at is None:
        return

    from apps.notifications.routes import parent_child_url
    from apps.notifications.services import send_parent_notification

    sent = send_parent_notification(
        club=checkin.club,
        student=checkin.student,
        notification_type="parent_checkin_cancelled",
        context={
            "name": checkin.student.first_name,
            "full_name": str(checkin.student),
        },
        fallback_title="Чек-ин отменён",
        fallback_body=(
            f"Отменено ранее внесённое посещение: {checkin.student.first_name}, тренировка {checkin.date:%d.%m.%Y}."
            if dated else f"Чек-ин {checkin.student.first_name} был отменён."
        ),
        url=parent_child_url(checkin.student_id),
        record_type=f"parent_checkin_cancelled:{checkin.id}",
        **({"use_fallback_content": True} if dated else {}),
    )
    if sent:
        Checkin.objects.for_club(club_id).filter(id=checkin.id, parent_cancellation_notified_at__isnull=True).update(
            parent_cancellation_notified_at=timezone.now(),
        )
        logger.info(
            "parent_checkin_reverse_push_sent",
            extra={
                "checkin_id": checkin_id,
                "student_id": checkin.student_id,
                "club_id": club_id,
            },
        )


def reverse_grade_progress(checkin_id: int, club_id: int) -> None:
    checkin = Checkin.objects.get(id=checkin_id, club_id=club_id)

    events = list(
        GradeProgressEvent.objects.for_club(club_id)
        .filter(checkin_id=checkin_id, student_grade__student_id=checkin.student_id)
        .select_related("student_grade")
    )
    if not events:
        logger.info(
            "grade_progress_reverse_skipped_not_counted",
            extra={"checkin_id": checkin_id, "student_id": checkin.student_id},
        )
        return

    for event in events:
        sg = event.student_grade
        if sg.promoted_at and event.created_at < sg.promoted_at:
            event.delete()
            logger.info(
                "grade_progress_reverse_skipped_prior_promotion",
                extra={"checkin_id": checkin_id, "student_grade_id": sg.id},
            )
            continue

        decrement_grade_progress(
            club_id=club_id,
            student_id=checkin.student_id,
            grade_system_id=sg.grade_system_id,
        )
        event.delete()

    logger.info(
        "grade_progress_reversed",
        extra={"checkin_id": checkin_id, "student_id": checkin.student_id, "club_id": club_id},
    )


def reverse_group_analytics(checkin_id: int, club_id: int) -> None:
    # Recount live -- same logic as update_group_analytics
    update_group_analytics(checkin_id, club_id)
