from __future__ import annotations

import hashlib
import logging
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.db import transaction

import apps.billing.service_modules.group_payments as group_payments
from apps.billing.models import (
    Debt,
    Discount,
    Payment,
    Subscription,
    SubscriptionComponent,
    Tariff,
    TrainingType,
)
from apps.billing.service_modules._shared import _money, _validate_positive_money
from apps.billing.service_modules.catalog import _validate_discount_value
from apps.billing.service_modules.debts import (
    _assert_personal_drop_in_payment_path,
    _reserve_debts_for_payment,
)
from apps.billing.service_modules.entitlements import (
    _create_subscription_components,
    _resolve_package_owner_trainer_id_for_components,
    _student_has_current_component_subscription,
)
from apps.billing.service_modules.tariff_components import (
    _ensure_tariff_components,
    _payment_preflight_tariff_components,
    _resolve_tariff_payout_policy,
)
from apps.clubs.models import Club
from apps.common.exceptions import BusinessLogicError
from apps.students.models import Student

logger = logging.getLogger(__name__)


def build_personal_command_fingerprint(
    *,
    student_id: int,
    terms_id: int,
    payment_method: str,
    booking_id: int | None = None,
    reservation_id: int | None = None,
    debt_ids: list[int] | None = None,
) -> str:
    """Return a non-PII immutable fingerprint for one personal command."""

    material = "|".join(
        (
            "personal-v1",
            str(student_id),
            str(terms_id),
            payment_method,
            str(booking_id or ""),
            str(reservation_id or ""),
            ",".join(str(debt_id) for debt_id in sorted(set(debt_ids or []))),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _lock_personal_terms_target(
    *,
    club_id: int,
    booking_id: int | None,
    reservation_id: int | None,
):
    """Lock an accepted personal target in D12 order before finance writes."""

    from apps.attendance.services.personal_locking import lock_complete_personal_scopes

    ordered_scope = lock_complete_personal_scopes(
        club_id=club_id,
        booking_ids=[booking_id] if booking_id is not None else [],
        reservation_ids=[reservation_id] if reservation_id is not None else [],
    )
    if ordered_scope is not None:
        from apps.attendance.models import PersonalServiceTermsSnapshot

        target = (
            ordered_scope.bookings_by_id[booking_id]
            if booking_id is not None
            else ordered_scope.reservations_by_id[reservation_id]
        )
        terms = (
            PersonalServiceTermsSnapshot.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(booking_id=booking_id, reservation_id=reservation_id)
            .first()
        )
        student_id = target.enrollment.student_id if booking_id is not None else target.student_id
        student = Student.objects.for_club(club_id).get(id=student_id)
        return target, terms, student

    from apps.attendance.models import (
        PersonalAvailabilitySlot,
        PersonalBookingPaymentReservation,
        PersonalDropInBooking,
        PersonalServiceTermsSnapshot,
        ScheduleEnrollment,
    )
    from apps.trainers.models import Trainer

    if booking_id is not None:
        preview = (
            PersonalDropInBooking.objects.for_club(club_id)
            .select_related("enrollment__schedule")
            .get(id=booking_id)
        )
        trainer_id = preview.enrollment.schedule.trainer_id
        student_id = preview.enrollment.student_id
        slot_id = (
            PersonalAvailabilitySlot.objects.for_club(club_id)
            .filter(booked_enrollment_id=preview.enrollment_id)
            .values_list("id", flat=True)
            .first()
        )
    else:
        preview = PersonalBookingPaymentReservation.objects.for_club(club_id).get(id=reservation_id)
        trainer_id = preview.trainer_id
        student_id = preview.student_id
        slot_id = preview.availability_slot_id

    Trainer.objects.for_club(club_id).select_for_update(of=("self",)).get(id=trainer_id)
    student = Student.objects.for_club(club_id).select_for_update(of=("self",)).get(id=student_id)
    if slot_id is not None:
        PersonalAvailabilitySlot.objects.for_club(club_id).select_for_update(of=("self",)).get(id=slot_id)

    if booking_id is not None:
        target = (
            PersonalDropInBooking.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .select_related("enrollment__schedule")
            .get(id=booking_id)
        )
        ScheduleEnrollment.objects.for_club(club_id).select_for_update(of=("self",)).get(
            id=target.enrollment_id
        )
        terms = (
            PersonalServiceTermsSnapshot.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(booking_id=target.id)
            .first()
        )
    else:
        target = (
            PersonalBookingPaymentReservation.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .get(id=reservation_id)
        )
        terms = (
            PersonalServiceTermsSnapshot.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .filter(reservation_id=target.id)
            .first()
        )
    return target, terms, student


def create_personal_terms_payment(
    *,
    club_id: int,
    booking_id: int | None = None,
    reservation_id: int | None = None,
    payment_method: str,
    recorded_by_id: int,
    command_idempotency_key: str,
    command_fingerprint: str,
    debt_ids: list[int] | None = None,
) -> Payment:
    """Create the exact personal financial family from complete frozen terms.

    This intentionally does not extend the generic payment API with a caller
    supplied amount.  The tariff FK is retained for history, while every money,
    entitlement, scope and payout snapshot below comes from the accepted terms.
    """

    if (booking_id is None) == (reservation_id is None):
        raise BusinessLogicError(
            "Exactly one personal intent is required",
            code="personal_terms_target_required",
        )
    if payment_method not in {Payment.Method.CASH, Payment.Method.TRANSFER, Payment.Method.ONLINE}:
        raise BusinessLogicError(
            "Personal terms payment requires cash, transfer, or online",
            code="personal_terms_payment_method_invalid",
        )
    command_key = command_idempotency_key.strip()
    if not command_key:
        raise BusinessLogicError(
            "Unified personal payment requires an idempotency key",
            code="idempotency_key_required",
        )
    if len(command_key) > 120 or len(command_fingerprint) != 64:
        raise BusinessLogicError(
            "Invalid unified command identity",
            code="invalid_command_identity",
        )

    from apps.attendance.models import is_complete_personal_terms

    with transaction.atomic():
        # Arbitrate every keyed unified command at the club root before either
        # request can enter its student-specific financial scope.
        Club.objects.select_for_update(of=("self",)).get(id=club_id)
        target, terms, student = _lock_personal_terms_target(
            club_id=club_id,
            booking_id=booking_id,
            reservation_id=reservation_id,
        )
        existing = (
            Payment.objects.for_club(club_id)
            .select_for_update(of=("self",))
            .select_related("subscription")
            .filter(command_idempotency_key=command_key)
            .first()
        )
        if existing is not None:
            if existing.command_fingerprint != command_fingerprint:
                raise BusinessLogicError(
                    "Idempotency key was already used for another command",
                    code="idempotency_conflict",
                )
            return existing

        if booking_id is not None:
            trainer_id = target.enrollment.schedule.trainer_id
            tariff_id = target.tariff_id
        else:
            trainer_id = target.trainer_id
            tariff_id = target.tariff_id

        if not is_complete_personal_terms(terms):
            raise BusinessLogicError(
                "Legacy personal intent requires the compatibility settlement owner",
                code="personal_terms_legacy_partial",
            )
        if terms.payable_amount is None or terms.payable_amount <= 0:
            raise BusinessLogicError(
                "Personal terms amount is incomplete",
                code="personal_terms_incomplete",
            )
        if terms.component_entitlement_kind != "finite_credits" or terms.component_credits_total != 1:
            raise BusinessLogicError(
                "Personal terms must contain one finite credit",
                code="personal_terms_incomplete",
            )

        subscription = Subscription.objects.create(
            club_id=club_id,
            student=student,
            tariff_id=tariff_id,
            paid_amount=terms.payable_amount,
            status=Subscription.Status.PENDING,
            trainings_left=1,
            expires_at=None,
            scope=terms.scope,
            location_id=terms.location_id_snapshot,
            trainer_payout_policy_snapshot=terms.tariff_trainer_payout_policy,
        )
        component = SubscriptionComponent(
            club_id=club_id,
            subscription=subscription,
            tariff_component=None,
            name_snapshot=terms.component_name_snapshot,
            training_type_id=terms.component_training_type_id_snapshot,
            entitlement_kind=terms.component_entitlement_kind,
            credits_total=1,
            credits_left=1,
            scope=terms.component_scope,
            location_id=terms.component_location_id_snapshot,
            trainer_payout_policy_snapshot=terms.component_trainer_payout_policy,
            paid_amount_basis_snapshot=terms.component_paid_amount_basis,
            unit_amount_basis_snapshot=terms.component_unit_amount_basis,
        )
        component.full_clean()
        component.save()
        payment = Payment(
            club_id=club_id,
            student=student,
            tariff_id=tariff_id,
            subscription=subscription,
            amount=terms.payable_amount,
            original_amount=terms.base_amount or terms.payable_amount,
            payment_method=payment_method,
            status=Payment.Status.PENDING,
            recorded_by_id=recorded_by_id,
            seller_trainer_id=trainer_id,
            package_owner_trainer_id=trainer_id,
            command_idempotency_key=command_key,
            command_fingerprint=command_fingerprint,
        )
        payment.full_clean()
        payment.save()
        # The accepted terms retain the durable discount evidence.  When the
        # catalog row still exists, retain the reporting relation too without
        # accepting a new discount authority on this financial path.
        if terms.discount_id_snapshot is not None:
            selected_discount = Discount.objects.for_club(club_id).filter(
                id=terms.discount_id_snapshot
            ).first()
            if selected_discount is not None:
                payment.applied_discounts.add(selected_discount)
        # Debt follows the payment/subscription family in the canonical order.
        # The billing debt owner locks and proves the exact booking association
        # before it stores settlement_payment and immutable reservation events.
        _reserve_debts_for_payment(
            payment=payment,
            subscription=subscription,
            club_id=club_id,
            debt_ids=debt_ids,
            personal_drop_in_booking_id=booking_id,
        )
        # Keep the manual-review notification tied to the durable financial
        # family.  The callback runs only after the coordinator's outer
        # transaction commits, so reviewers never see a half-created booking
        # or subscription.
        if payment_method != Payment.Method.ONLINE:
            from django_q.tasks import async_task

            transaction.on_commit(
                lambda payment_id=payment.id: async_task(
                    "apps.billing.tasks.notify_payment_verification",
                    payment_id,
                    club_id=club_id,
                )
            )
        return payment


def _validate_payment_method(payment_method: str) -> None:
    if payment_method not in Payment.Method.values:
        raise BusinessLogicError(
            "Некорректный способ оплаты",
            code="invalid_payment_method",
        )


def _apply_discounts(
    *,
    base_price: Decimal,
    club_id: int,
    discount_ids: list[int],
) -> tuple[Decimal, list[Discount]]:
    """Apply discounts from base price. Returns (final_amount, applied_discount_objects)."""
    if not discount_ids:
        return _money(base_price), []

    requested_ids = set(discount_ids)
    discounts = list(
        Discount.objects.for_club(club_id)
        .filter(id__in=requested_ids, is_active=True)
        .order_by("id")
    )
    found_ids = {discount.id for discount in discounts}
    if found_ids != requested_ids:
        raise BusinessLogicError(
            "Некорректные скидки для оплаты",
            code="invalid_discount_ids",
        )

    total_reduction = Decimal("0")

    for discount in discounts:
        _validate_discount_value(
            discount_type=discount.discount_type,
            value=discount.value,
        )
        if discount.discount_type == Discount.Type.PERCENT:
            total_reduction += base_price * discount.value / Decimal("100")
        else:
            total_reduction += discount.value

    result = _money(max(base_price - total_reduction, Decimal("0")))
    return result, discounts


def _find_existing_manual_operational_admission(
    *,
    club_id: int,
    student_id: int,
    tariff_id: int,
    payment_method: str,
    target_schedule_id: int,
    target_start_date: date,
    seller_trainer_id: int | None,
    package_owner_trainer_id: int | None,
    discount_ids: list[int] | None,
    debt_ids: list[int] | None,
    canonical_group_admission: bool = False,
    target_training_group_id: int | None = None,
) -> Payment | None:
    payments = (
        Payment.objects.for_club(club_id)
        .select_for_update()
        .filter(
            student_id=student_id,
            tariff_id=tariff_id,
            payment_method=payment_method,
            status=Payment.Status.PENDING,
            target_schedule_id=target_schedule_id,
            target_start_date=target_start_date,
            seller_trainer_id=seller_trainer_id,
            package_owner_trainer_id=package_owner_trainer_id,
            conversion_enrollment__isnull=False,
        )
    )
    if canonical_group_admission:
        payments = payments.filter(
            target_training_group_id=target_training_group_id,
            group_membership_action_snapshot=(
                Payment.GroupMembershipActionSnapshot.NEW_ADMISSION
            ),
        )
    payment = payments.order_by("id").first()
    if payment is None:
        return None

    requested_discount_ids = set(dict.fromkeys(discount_ids or []))
    existing_discount_ids = set(
        payment.applied_discounts.values_list("id", flat=True)
    )
    requested_debt_ids = set(dict.fromkeys(debt_ids or []))
    existing_debt_ids = set(
        Debt.objects.for_club(club_id)
        .filter(
            settlement_payment_id=payment.id,
            resolved_at__isnull=True,
        )
        .values_list("id", flat=True)
    )
    if (
        existing_discount_ids == requested_discount_ids
        and existing_debt_ids == requested_debt_ids
    ):
        return payment
    return None


def create_payment(
    *,
    club_id: int,
    student_id: int,
    tariff_id: int,
    payment_method: str,
    discount_ids: list[int] | None = None,
    debt_ids: list[int] | None = None,
    recorded_by_id: int,
    seller_trainer_id: int | None = None,
    package_owner_trainer_id: int | None = None,
    target_schedule_id: int | None = None,
    target_training_group_id: int | None = None,
    target_start_date: date | None = None,
    enforce_trainer_group_contract: bool = False,
    allow_renewal: bool = False,
    renewed_from_subscription_id: int | None = None,
    renewal_source_tariff_id: int | None = None,
    expected_target_tariff_id: int | None = None,
    expected_target_price: Decimal | str | None = None,
    renewal_chain_id=None,
    personal_drop_in_booking_id: int | None = None,
    create_manual_operational_admission: bool = False,
    mixed_v1_lead_only_admission: bool = False,
    command_idempotency_key: str | None = None,
    command_fingerprint: str | None = None,
    _locked_rollout_state=None,
    _locked_pre_create_validator=None,
) -> Payment:
    from apps.trainers.models import Trainer

    command_key = (command_idempotency_key or "").strip()
    if command_key and (len(command_key) > 120 or not command_fingerprint or len(command_fingerprint) != 64):
        raise BusinessLogicError("Некорректная идентичность команды", code="invalid_command_identity")
    if command_fingerprint and not command_key:
        raise BusinessLogicError("Некорректная идентичность команды", code="invalid_command_identity")
    if renewed_from_subscription_id is not None:
        from apps.billing.service_modules.renewals import _normalise_expected_renewal_offer

        (
            expected_target_tariff_id,
            expected_target_price,
        ) = _normalise_expected_renewal_offer(
            expected_target_tariff_id=expected_target_tariff_id,
            expected_target_price=expected_target_price,
        )
    # Exact replay is an immutable receipt lookup, not a fresh commercial
    # decision.  It deliberately precedes tariff, target and capability reads
    # so K1 remains replayable after those mutable surfaces change.
    if command_key:
        with transaction.atomic():
            Club.objects.select_for_update(of=("self",)).get(id=club_id)
            existing_command = (
                Payment.objects.for_club(club_id)
                .select_for_update(of=("self",))
                .select_related("subscription")
                .filter(command_idempotency_key=command_key)
                .first()
            )
            if existing_command is not None:
                if existing_command.command_fingerprint != command_fingerprint:
                    raise BusinessLogicError(
                        "Idempotency key was already used for another command",
                        code="idempotency_conflict",
                    )
                existing_command._command_replayed = True
                return existing_command

    renewal_source_preview = None
    if renewed_from_subscription_id is not None:
        # A renewal request names the historical source separately from its
        # accepted target. The source may already be archived after a price
        # revision, so do not ask the mutable active-tariff surface to resolve
        # this request before exact source/family arbitration below.
        renewal_source_preview = (
            Subscription.objects.for_club(club_id)
            .select_related("tariff__training_type", "tariff__location")
            .filter(
                id=renewed_from_subscription_id,
                student_id=student_id,
                deleted_at__isnull=True,
            )
            .first()
        )
        if renewal_source_preview is None:
            raise BusinessLogicError(
                "Абонемент для продления не найден",
                code="renewal_source_not_found",
            )
        if (
            renewal_source_tariff_id is not None
            and renewal_source_preview.tariff_id != renewal_source_tariff_id
        ):
            raise BusinessLogicError(
                "Источник продления изменился",
                code="renewal_source_mismatch",
            )
        tariff = renewal_source_preview.tariff
    else:
        tariff = (
            Tariff.objects.for_club(club_id)
            .select_related("training_type", "location")
            .get(id=tariff_id, is_active=True)
        )
    group_target_intent = tariff.training_type.kind == TrainingType.Kind.GROUP and (
        target_schedule_id is not None or target_training_group_id is not None
    )
    _validate_positive_money(tariff.price)
    components = _payment_preflight_tariff_components(tariff, club_id=club_id)
    _validate_payment_method(payment_method)
    Student.objects.for_club(club_id).only("id").get(id=student_id)
    target_schedule = group_payments._validate_group_conversion_target(
        club_id=club_id,
        tariff=tariff,
        target_schedule_id=target_schedule_id,
        target_start_date=target_start_date,
        seller_trainer_id=None,
    )
    if target_schedule is not None:
        seller_trainer_id = target_schedule.trainer_id

    # Validate seller_trainer belongs to this club
    if seller_trainer_id is not None:
        if (
            not Trainer.objects.for_club(club_id)
            .filter(id=seller_trainer_id)
            .exists()
        ):
            raise BusinessLogicError(
                "Тренер-продавец не найден",
                code="seller_trainer_not_found",
            )
    if package_owner_trainer_id is not None:
        if (
            not Trainer.objects.for_club(club_id)
            .filter(id=package_owner_trainer_id, is_active=True)
            .exists()
        ):
            raise BusinessLogicError(
                "Тренер-владелец пакета не найден",
                code="package_owner_trainer_not_found",
            )

    resolved_package_owner_trainer_id = (
        _resolve_package_owner_trainer_id_for_components(
            components=components,
            seller_trainer_id=seller_trainer_id,
            package_owner_trainer_id=package_owner_trainer_id,
        )
    )

    original_amount = tariff.price
    amount, applied_discounts = _apply_discounts(
        base_price=original_amount,
        club_id=club_id,
        discount_ids=discount_ids or [],
    )
    _validate_positive_money(amount)

    with transaction.atomic():
        # The club root is the command arbitration lock.  It precedes Student
        # so the unique payment key cannot race across different students.
        if command_key or group_target_intent:
            Club.objects.select_for_update(of=("self",)).get(id=club_id)
        # A command may have committed after the optimistic replay lookup and
        # before this transaction acquired the club arbitration lock.  Exact
        # K1 replay remains valid across protocol flips, so resolve it before
        # consulting the mutable protocol/offer validator.
        if command_key:
            existing_command = (
                Payment.objects.for_club(club_id)
                .select_for_update(of=("self",))
                .select_related("subscription")
                .filter(command_idempotency_key=command_key)
                .first()
            )
            if existing_command is not None:
                if existing_command.command_fingerprint != command_fingerprint:
                    raise BusinessLogicError(
                        "Idempotency key was already used for another command",
                        code="idempotency_conflict",
                    )
                existing_command._command_replayed = True
                return existing_command
        if renewal_source_preview is not None:
            from apps.billing.service_modules.renewals import _lock_renewal_catalog_scope

            _lock_renewal_catalog_scope(
                club_id=club_id,
                source_tariff=renewal_source_preview.tariff,
            )
        if _locked_pre_create_validator is not None:
            validated_rollout_state = _locked_pre_create_validator()
            if validated_rollout_state is not None:
                _locked_rollout_state = validated_rollout_state
        rollout_state = _locked_rollout_state
        if rollout_state is None and group_target_intent:
            from apps.attendance.services.training_group_memberships import (
                lock_training_group_mutation_scope,
            )

            rollout_state = lock_training_group_mutation_scope(club_id=club_id)
        if group_target_intent:
            from apps.attendance.models import Schedule
            from apps.attendance.services.training_group_memberships import (
                assert_training_group_new_writes_enabled,
            )

            mapped_target_group_id = (
                Schedule.objects.for_club(club_id)
                .filter(id=target_schedule_id)
                .values_list("training_group_id", flat=True)
                .first()
                if target_schedule_id is not None
                else None
            )
            if (
                target_training_group_id is not None
                or mapped_target_group_id is not None
            ):
                assert_training_group_new_writes_enabled()
        student = (
            Student.objects.for_club(club_id)
            .select_for_update()
            .get(id=student_id)
        )
        if mixed_v1_lead_only_admission and student.lead_status is not None:
            # ``lead_status`` is the canonical active-lead workspace fact;
            # it deliberately includes a completed-trial lead.
            create_manual_operational_admission = True
        canonical_group = None
        target_group_membership = None
        group_membership_action = ""
        if target_schedule is not None:
            target_schedule = group_payments._validate_group_conversion_target(
                club_id=club_id,
                tariff=tariff,
                target_schedule_id=target_schedule_id,
                target_start_date=target_start_date,
                seller_trainer_id=None,
                lock_schedule=True,
            )
            (
                canonical_group,
                target_group_membership,
                group_membership_action,
            ) = group_payments._resolve_canonical_group_payment_target(
                club_id=club_id,
                student_id=student.id,
                schedule=target_schedule,
                target_start_date=target_start_date,
                requested_training_group_id=target_training_group_id,
                rollout_state=rollout_state,
                lock=True,
            )
            if canonical_group is not None:
                seller_trainer_id = canonical_group.responsible_trainer_id
                if create_manual_operational_admission and payment_method in {
                    Payment.Method.CASH,
                    Payment.Method.TRANSFER,
                }:
                    resolved_package_owner_trainer_id = (
                        _resolve_package_owner_trainer_id_for_components(
                            components=components,
                            seller_trainer_id=seller_trainer_id,
                            package_owner_trainer_id=package_owner_trainer_id,
                        )
                    )
                    existing_payment = (
                        _find_existing_manual_operational_admission(
                            club_id=club_id,
                            student_id=student.id,
                            tariff_id=tariff.id,
                            payment_method=payment_method,
                            target_schedule_id=target_schedule.id,
                            target_start_date=target_start_date,
                            seller_trainer_id=seller_trainer_id,
                            package_owner_trainer_id=(
                                resolved_package_owner_trainer_id
                            ),
                            discount_ids=discount_ids,
                            debt_ids=debt_ids,
                            canonical_group_admission=True,
                            target_training_group_id=canonical_group.id,
                        )
                    )
                    if existing_payment is not None:
                        return existing_payment
                allow_renewal = (
                    allow_renewal
                    or group_membership_action
                    == Payment.GroupMembershipActionSnapshot.RENEWAL
                )
                if (
                    group_membership_action
                    == Payment.GroupMembershipActionSnapshot.RENEWAL
                ):
                    from apps.clubs.capabilities import is_unified_client_journey_enabled

                    if (
                        is_unified_client_journey_enabled(club=club_id)
                        and renewed_from_subscription_id is None
                    ):
                        raise BusinessLogicError(
                            "Контекстное продление группы требует точный исходный абонемент",
                            code="renewal_source_required",
                        )
                if (
                    group_membership_action
                    == Payment.GroupMembershipActionSnapshot.RENEWAL
                    and debt_ids
                ):
                    raise BusinessLogicError(
                        "A group renewal cannot reserve debt settlement before confirmation.",
                        code="group_renewal_debt_reservation_not_allowed",
                    )
        _assert_personal_drop_in_payment_path(
            club_id=club_id,
            student_id=student_id,
            tariff_id=tariff_id,
            personal_drop_in_booking_id=personal_drop_in_booking_id,
        )
        if enforce_trainer_group_contract:
            group_payments._validate_trainer_group_payment_contract(
                club_id=club_id,
                student=student,
                tariff=tariff,
                target_schedule_id=target_schedule_id,
                target_start_date=target_start_date,
                lock_enrollments=True,
            )
        manual_group_admission = (
            create_manual_operational_admission
            and payment_method
            in {
                Payment.Method.CASH,
                Payment.Method.TRANSFER,
            }
            and target_schedule is not None
            and group_membership_action
            != Payment.GroupMembershipActionSnapshot.RENEWAL
        )
        v2_manual_operational_admission = False
        if manual_group_admission:
            from apps.clubs.capabilities import (
                assert_v2_manual_admission_command_allowed,
                get_commercial_journey_capability,
            )
            from apps.clubs.models import ClubSettings

            # Keep the protocol decision stable through membership, payment and
            # evidence admission. A concurrent v2 rollback must block rather
            # than turning this exact canonical command into a legacy write.
            ClubSettings.objects.select_for_update(of=("self",)).filter(club_id=club_id).first()
            capability = get_commercial_journey_capability(club=club_id)
            if capability.protocol_version == "v2":
                # A v2 canonical target may never silently fall back to the
                # legacy lifecycle when the tenant manual gate changes.
                assert_v2_manual_admission_command_allowed(capability=capability)
                v2_manual_operational_admission = True
            elif capability.protocol_version != "v1":
                raise BusinessLogicError(
                    "Commercial journey command is unavailable for this client or tenant.",
                    code="commercial_journey_unavailable",
                )
            from apps.attendance.services.enrollment import (
                assert_paid_conversion_enrollment_available,
            )

            existing_payment = _find_existing_manual_operational_admission(
                club_id=club_id,
                student_id=student.id,
                tariff_id=tariff.id,
                payment_method=payment_method,
                target_schedule_id=target_schedule.id,
                target_start_date=target_start_date,
                seller_trainer_id=seller_trainer_id,
                package_owner_trainer_id=resolved_package_owner_trainer_id,
                discount_ids=discount_ids,
                debt_ids=debt_ids,
            )
            if existing_payment is not None:
                return existing_payment
            assert_paid_conversion_enrollment_available(
                club_id=club_id,
                student_id=student.id,
                schedule_id=target_schedule.id,
            )
            if not settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED:
                raise BusinessLogicError(
                    "Операционная запись по ручной оплате временно недоступна",
                    code="manual_operational_admission_disabled",
                )
            if Payment.objects.for_club(club_id).filter(
                payment_method__in=[
                    Payment.Method.CASH,
                    Payment.Method.TRANSFER,
                ],
                status=Payment.Status.PENDING,
                target_schedule__isnull=False,
                conversion_enrollment__isnull=True,
            ).exists():
                raise BusinessLogicError(
                    "Сначала завершите старые ожидающие ручные оплаты",
                    code="legacy_unlinked_manual_payment_exists",
                )

        components = (
            _payment_preflight_tariff_components(tariff, club_id=club_id)
            if renewed_from_subscription_id is not None
            else _ensure_tariff_components(tariff, club_id=club_id)
        )
        resolved_package_owner_trainer_id = (
            _resolve_package_owner_trainer_id_for_components(
                components=components,
                seller_trainer_id=seller_trainer_id,
                package_owner_trainer_id=package_owner_trainer_id,
            )
        )
        if (
            _student_has_current_component_subscription(
                club_id=club_id,
                student_id=student_id,
                components=components,
            )
            and not allow_renewal
        ):
            raise BusinessLogicError(
                "У ученика уже есть активный абонемент этого типа",
                code="active_subscription_exists",
            )

        renewed_from = None
        if renewed_from_subscription_id is not None:
            from apps.billing.service_modules.renewals import (
                get_renewal_offer,
                lock_and_validate_exact_renewal_source,
                validate_expected_renewal_offer,
                validate_locked_renewal_source,
            )

            renewed_from = lock_and_validate_exact_renewal_source(
                club_id=club_id,
                student_id=student_id,
                renewed_from_subscription_id=renewed_from_subscription_id,
                validate=False,
            )
            from apps.billing.service_modules.renewals import find_existing_renewal_payment_family

            existing_renewal_family = find_existing_renewal_payment_family(
                club_id=club_id,
                source_subscription_id=renewed_from.id,
                payment_method=payment_method,
                target_schedule_id=target_schedule_id,
                target_training_group_id=target_training_group_id,
                target_start_date=target_start_date,
                discount_ids=discount_ids,
                debt_ids=debt_ids,
            )
            if existing_renewal_family is not None:
                if (
                    command_key
                    and existing_renewal_family.command_idempotency_key != command_key
                ):
                    raise BusinessLogicError(
                        "У этого абонемента уже есть ожидающее или подтверждённое продление",
                        code="renewal_source_finalized_successor",
                    )
                if command_key:
                    existing_renewal_family._command_replayed = True
                return existing_renewal_family
            if (
                renewal_source_tariff_id is not None
                and renewed_from.tariff_id != renewal_source_tariff_id
            ):
                raise BusinessLogicError(
                    "Источник продления изменился",
                    code="renewal_source_mismatch",
                )
            offer = get_renewal_offer(
                club_id=club_id,
                source_tariff=renewed_from.tariff,
                lock=True,
            )
            validate_expected_renewal_offer(
                offer=offer,
                expected_target_tariff_id=expected_target_tariff_id,
                expected_target_price=expected_target_price,
            )
            if offer.target_tariff_id != tariff_id:
                raise BusinessLogicError(
                    "Тариф продления изменился",
                    code="renewal_tariff_mismatch",
                )
            if not offer.target_tariff.is_active:
                raise BusinessLogicError(
                    "Тариф продления больше недоступен",
                    code="renewal_offer_stale",
                )
            # The source remains the eligibility authority.  The target was
            # resolved under the same Club/Student/source locks above.
            tariff = offer.target_tariff
            _validate_positive_money(tariff.price)
            components = _payment_preflight_tariff_components(tariff, club_id=club_id)
            original_amount = tariff.price
            amount, applied_discounts = _apply_discounts(
                base_price=original_amount,
                club_id=club_id,
                discount_ids=discount_ids or [],
            )
            _validate_positive_money(amount)
            components = _ensure_tariff_components(tariff, club_id=club_id)
            resolved_package_owner_trainer_id = (
                _resolve_package_owner_trainer_id_for_components(
                    components=components,
                    seller_trainer_id=seller_trainer_id,
                    package_owner_trainer_id=package_owner_trainer_id,
                )
            )
            validate_locked_renewal_source(source=renewed_from)
            if renewal_chain_id is None:
                from uuid import uuid4

                renewal_chain_id = renewed_from.renewal_chain_id or uuid4()
            if renewed_from.renewal_chain_id != renewal_chain_id:
                renewed_from.renewal_chain_id = renewal_chain_id
                renewed_from.save(update_fields=["renewal_chain_id", "updated_at"])
            if Subscription.objects.for_club(club_id).filter(
                renewed_from_id=renewed_from.id,
                status=Subscription.Status.PENDING,
                deleted_at__isnull=True,
            ).exists():
                raise BusinessLogicError(
                    "У этого абонемента уже есть ожидающее продление",
                    code="renewal_pending_exists",
                )

        subscription = Subscription.objects.create(
            club_id=club_id,
            student_id=student_id,
            tariff=tariff,
            paid_amount=amount,
            status=Subscription.Status.PENDING,
            trainings_left=tariff.trainings_limit,
            expires_at=None,  # Timer starts on payment confirmation, not creation
            scope=tariff.scope,
            location=tariff.location,
            trainer_payout_policy_snapshot=_resolve_tariff_payout_policy(tariff),
            renewed_from=renewed_from,
            renewal_chain_id=renewal_chain_id,
        )
        _create_subscription_components(
            subscription=subscription,
            club_id=club_id,
            paid_amount=amount,
        )

        payment = Payment.objects.create(
            club_id=club_id,
            student_id=student_id,
            tariff=tariff,
            subscription=subscription,
            amount=amount,
            original_amount=original_amount,
            payment_method=payment_method,
            status=Payment.Status.PENDING,
            command_idempotency_key=command_key or None,
            command_fingerprint=command_fingerprint or "",
            recorded_by_id=recorded_by_id,
            seller_trainer_id=seller_trainer_id,
            package_owner_trainer_id=resolved_package_owner_trainer_id,
            target_schedule=target_schedule,
            target_training_group=canonical_group,
            target_group_membership=target_group_membership,
            group_membership_action_snapshot=group_membership_action,
            renewal_source_tariff_name_snapshot=(
                renewed_from.tariff.name if renewed_from is not None else ""
            ),
            target_start_date=(
                target_start_date if target_schedule is not None else None
            ),
        )
        if target_schedule is not None:
            group_payments._snapshot_group_conversion_target(
                payment,
                target_schedule,
                canonical_group=canonical_group,
            )
            payment.save(
                update_fields=[
                    "target_group_name_snapshot",
                    "target_location_id_snapshot",
                    "target_location_name_snapshot",
                    "target_trainer_id_snapshot",
                    "target_trainer_name_snapshot",
                    "target_training_type_id_snapshot",
                    "target_training_type_kind_snapshot",
                    "sale_trainer_id_snapshot",
                    "sale_attribution_source",
                    "updated_at",
                ]
            )

        if applied_discounts:
            payment.applied_discounts.set(applied_discounts)
        _reserve_debts_for_payment(
            payment=payment,
            subscription=subscription,
            club_id=club_id,
            debt_ids=debt_ids,
            personal_drop_in_booking_id=personal_drop_in_booking_id,
        )
        if manual_group_admission:
            from apps.leads.services import admit_lead_for_manual_operational_admission
            from apps.students.operational_admission_contracts import ManualOperationalAdmissionEvidence

            if canonical_group is not None:
                group_payments._link_payment_owned_group_membership(
                    payment=payment,
                    club_id=club_id,
                    actor_user_id=recorded_by_id,
                    scope_locked=rollout_state is not None,
                )
            else:
                from apps.attendance.services.enrollment import (
                    create_paid_conversion_enrollment,
                )

                conversion_enrollment = create_paid_conversion_enrollment(
                    club_id=club_id,
                    student_id=student.id,
                    schedule_id=target_schedule.id,
                    starts_on=target_start_date,
                )
                payment.conversion_enrollment = conversion_enrollment
                payment.save(
                    update_fields=[
                        "conversion_enrollment",
                        "updated_at",
                    ]
                )
            if canonical_group is not None and student.lead_status is not None:
                if v2_manual_operational_admission:
                    admit_lead_for_manual_operational_admission(
                        evidence=ManualOperationalAdmissionEvidence(
                            club_id=club_id,
                            student_id=student.id,
                            payment_id=payment.id,
                            origin="group",
                            actor_user_id=recorded_by_id,
                        )
                    )
                else:
                    from apps.leads.services import snooze_lead_for_pending_group_payment

                    snooze_lead_for_pending_group_payment(
                        club_id=club_id,
                        student_id=student.id,
                        payment_id=payment.id,
                        actor_user_id=recorded_by_id,
                    )
            else:
                # Legacy non-canonical paid-conversion rows retain their
                # compatibility helper. New canonical admissions never
                # downgrade to that broad student-id-only path.
                from apps.leads.services import convert_lead_for_manual_operational_admission

                convert_lead_for_manual_operational_admission(
                    club_id=club_id, student_id=student.id, actor_user_id=recorded_by_id
                )

    # Antifraud: cash/transfer payments must be manually verified by owner.
    # Online payments are confirmed by the provider webhook through BankPaymentOrder.
    if payment_method != Payment.Method.ONLINE:
        from django_q.tasks import async_task

        async_task(
            "apps.billing.tasks.notify_payment_verification",
            payment.id,
            club_id=club_id,
        )

    logger.info(
        "payment_created",
        extra={
            "id": payment.id,
            "student_id": student_id,
            "club_id": club_id,
        },
    )
    return payment
