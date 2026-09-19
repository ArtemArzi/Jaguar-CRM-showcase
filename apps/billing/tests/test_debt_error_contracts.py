from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.attendance.models import (
    PersonalDropInBooking,
    PersonalDropInPaymentLink,
    ScheduleEnrollment,
)
from apps.attendance.tests.factories import CheckinFactory, ScheduleFactory
from apps.billing.models import (
    BankPaymentOrder,
    Debt,
    DebtLifecycleEvent,
    DebtSettlementEvent,
    DebtWriteOffEvent,
    Payment,
    Subscription,
    Tariff,
    TariffComponent,
    TrainingType,
)
from apps.billing.service_modules.debts import (
    _assert_personal_drop_in_payment_path,
    _attach_debts_to_subscription,
    _reserve_debts_for_payment,
    _validate_complete_personal_debt_terms,
    _validate_personal_drop_in_debt_tariff_contract,
    assert_payment_reservation_capacity,
    debt_state,
    record_debt_lifecycle_event,
    record_debt_settlement_events,
    write_off_debt,
)
from apps.billing.tests.factories import (
    DebtFactory,
    PaymentFactory,
    SubscriptionComponentFactory,
    SubscriptionFactory,
    TariffComponentFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.clubs.tests.factories import LocationFactory
from apps.common.exceptions import BusinessLogicError
from apps.students.tests.factories import StudentFactory
from apps.trainers.services import close_trainer_payroll_period
from apps.trainers.tests.factories import TrainerFactory, TrainerLocationFactory


def _assert_business_logic_error(call, *, code: str, message: str) -> None:
    with pytest.raises(BusinessLogicError) as exc_info:
        call()

    assert exc_info.value.code == code
    assert str(exc_info.value) == message


def _assert_validation_error(call, *, message_dict: dict[str, list[str]]) -> None:
    with pytest.raises(ValidationError) as exc_info:
        call()

    assert exc_info.value.message_dict == message_dict


def test_complete_personal_debt_terms_mismatch_has_no_mutation():
    debt = SimpleNamespace(
        id=1,
        required_tariff_id=10,
        tariff_price=Decimal("1000.00"),
    )
    terms = SimpleNamespace(
        tariff_id_snapshot=11,
        payable_amount=Decimal("1000.00"),
    )

    _assert_business_logic_error(
        lambda: _validate_complete_personal_debt_terms(
            debts=[debt],
            terms_by_debt={debt.id: terms},
        ),
        code="personal_terms_debt_mismatch",
        message="Personal booking debt does not match immutable terms",
    )


def _regular_subscription_context(*, club, trainings_left: int | None = 3):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP, drop_in_price=None)
    tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=trainings_left or 3)
    student = StudentFactory(club=club)
    subscription = SubscriptionFactory(
        club=club,
        student=student,
        tariff=tariff,
        trainings_left=trainings_left,
        trainings_used=0,
    )
    payment = PaymentFactory(club=club, student=student, tariff=tariff, subscription=subscription)
    return training_type, tariff, student, subscription, payment


def _open_debt(*, club, student, training_type, location=None, required_tariff=None):
    schedule = ScheduleFactory(
        club=club,
        training_type=training_type,
        location=location or LocationFactory(club=club),
    )
    checkin = CheckinFactory(
        club=club,
        student=student,
        schedule=schedule,
        training_type=training_type,
        trainer=schedule.trainer,
        location=schedule.location,
        subscription=None,
        is_debt=True,
    )
    return DebtFactory(
        club=club,
        student=student,
        checkin=checkin,
        required_tariff=required_tariff,
    )


def _personal_drop_in_context(*, club, owner_user, attended: bool):
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club)
    training_type = TrainingTypeFactory(
        club=club,
        kind=TrainingType.Kind.PERSONAL,
        drop_in_price=Decimal("1000.00"),
    )
    TrainerLocationFactory(club=club, trainer=trainer, location=location)
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        price=Decimal("1000.00"),
        trainings_limit=1,
        scope=Tariff.Scope.CLUB,
    )
    tariff_component = TariffComponentFactory(
        club=club,
        tariff=tariff,
        training_type=training_type,
        entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
        credits_total=1,
        scope=Tariff.Scope.CLUB,
        location=None,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
        paid_amount_basis=tariff.price,
    )
    student = StudentFactory(club=club)
    schedule = ScheduleFactory(
        club=club,
        trainer=trainer,
        location=location,
        training_type=training_type,
    )
    enrollment = ScheduleEnrollment.objects.create(
        club=club,
        student=student,
        schedule=schedule,
        status=ScheduleEnrollment.Status.ACTIVE,
        starts_on=date(2026, 6, 10),
        ends_on=date(2026, 6, 10),
        created_from=ScheduleEnrollment.CreatedFrom.PERSONAL_DROP_IN,
    )
    subscription = SubscriptionFactory(
        club=club,
        student=student,
        tariff=tariff,
        trainings_left=1,
        trainings_used=0,
    )
    SubscriptionComponentFactory(
        club=club,
        subscription=subscription,
        tariff_component=tariff_component,
        training_type=training_type,
        credits_total=1,
        credits_left=1,
        scope=Tariff.Scope.CLUB,
        location=None,
    )

    checkin = None
    debt = None
    state = PersonalDropInBooking.State.SCHEDULED
    if attended:
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            trainer=trainer,
            location=location,
            date=date(2026, 6, 10),
            subscription=None,
            is_debt=True,
        )
        debt = DebtFactory(
            club=club,
            student=student,
            checkin=checkin,
            required_tariff=tariff,
            tariff_price=tariff.price,
        )
        state = PersonalDropInBooking.State.ATTENDED
    booking = PersonalDropInBooking.objects.create(
        club=club,
        enrollment=enrollment,
        tariff=tariff,
        tariff_name_snapshot=tariff.name,
        price_snapshot=tariff.price,
        state=state,
        checkin=checkin,
        debt=debt,
        created_by=owner_user,
        idempotency_key=f"debt-error-{uuid4()}",
    )
    return {
        "booking": booking,
        "debt": debt,
        "location": location,
        "student": student,
        "subscription": subscription,
        "tariff": tariff,
        "training_type": training_type,
    }


def _assert_no_debt_audit(*, debt) -> None:
    assert not DebtLifecycleEvent.objects.for_club(debt.club_id).filter(debt=debt).exists()
    assert not DebtSettlementEvent.objects.for_club(debt.club_id).filter(debt=debt).exists()
    assert not DebtWriteOffEvent.objects.for_club(debt.club_id).filter(debt=debt).exists()


@pytest.mark.django_db
class TestDebtErrorContracts:
    def test_debt_state_returns_exact_open_reserved_and_resolved_values(self, club):
        training_type, _tariff, student, _subscription, payment = _regular_subscription_context(club=club)
        debt = _open_debt(club=club, student=student, training_type=training_type)

        assert debt_state(debt) == "open"
        debt.settlement_payment = payment
        assert debt_state(debt) == "reserved"
        debt.resolved_at = timezone.now()
        debt.resolution_type = ""
        assert debt_state(debt) == "resolved:unknown"
        debt.resolution_type = "writeoff"
        assert debt_state(debt) == "resolved:writeoff"

    def test_lifecycle_event_returns_none_and_persists_exact_snapshot(self, club):
        training_type, _tariff, student, _subscription, _payment = _regular_subscription_context(club=club)
        debt = _open_debt(club=club, student=student, training_type=training_type)

        result = record_debt_lifecycle_event(
            club_id=club.id,
            debt=debt,
            event_type=DebtLifecycleEvent.EventType.ATTACHED,
            previous_state="open",
            new_state="resolved:subscription",
            reason="contract evidence",
        )

        assert result is None
        event = DebtLifecycleEvent.objects.for_club(club.id).get(debt=debt)
        assert event.event_type == DebtLifecycleEvent.EventType.ATTACHED
        assert event.previous_state == "open"
        assert event.new_state == "resolved:subscription"
        assert event.debt_id_snapshot == debt.id

    def test_lifecycle_event_rejects_invalid_event_type_without_audit(self, club):
        training_type, _tariff, student, _subscription, _payment = _regular_subscription_context(club=club)
        debt = _open_debt(club=club, student=student, training_type=training_type)

        _assert_validation_error(
            lambda: record_debt_lifecycle_event(
                club_id=club.id,
                debt=debt,
                event_type="invalid-event",
                previous_state="open",
                new_state="reserved",
            ),
            message_dict={"event_type": ["Значения 'invalid-event' нет среди допустимых вариантов."]},
        )

        assert not DebtLifecycleEvent.objects.for_club(club.id).filter(debt=debt).exists()

    def test_lifecycle_event_rejects_foreign_debt_without_audit(self, club, other_club):
        training_type = TrainingTypeFactory(club=other_club)
        student = StudentFactory(club=other_club)
        debt = _open_debt(club=other_club, student=student, training_type=training_type)

        _assert_validation_error(
            lambda: record_debt_lifecycle_event(
                club_id=club.id,
                debt=debt,
                event_type=DebtLifecycleEvent.EventType.ATTACHED,
                previous_state="open",
                new_state="resolved:subscription",
            ),
            message_dict={"debt": ["Debt must belong to the same club as lifecycle event."]},
        )

        assert not DebtLifecycleEvent.objects.filter(debt=debt).exists()

    def test_lifecycle_event_rejects_foreign_payment_and_subscription_without_audit(
        self,
        club,
        other_club,
    ):
        training_type, _tariff, student, _subscription, _payment = _regular_subscription_context(club=club)
        debt = _open_debt(club=club, student=student, training_type=training_type)
        foreign_subscription = SubscriptionFactory(club=other_club)
        foreign_payment = PaymentFactory(
            club=other_club,
            student=foreign_subscription.student,
            tariff=foreign_subscription.tariff,
            subscription=foreign_subscription,
        )

        _assert_validation_error(
            lambda: record_debt_lifecycle_event(
                club_id=club.id,
                debt=debt,
                event_type=DebtLifecycleEvent.EventType.RESERVED,
                previous_state="open",
                new_state="reserved",
                payment_id=foreign_payment.id,
            ),
            message_dict={"payment": ["Payment must belong to the same club as lifecycle event."]},
        )
        _assert_validation_error(
            lambda: record_debt_lifecycle_event(
                club_id=club.id,
                debt=debt,
                event_type=DebtLifecycleEvent.EventType.ATTACHED,
                previous_state="open",
                new_state="resolved:subscription",
                subscription_id=foreign_subscription.id,
            ),
            message_dict={
                "subscription": ["Subscription must belong to the same club as lifecycle event."]
            },
        )

        assert not DebtLifecycleEvent.objects.for_club(club.id).filter(debt=debt).exists()

    def test_lifecycle_event_rejects_missing_optional_foreign_keys_without_audit(
        self,
        club,
    ):
        training_type, _tariff, student, _subscription, _payment = _regular_subscription_context(club=club)
        debt = _open_debt(club=club, student=student, training_type=training_type)
        missing_id = 999999

        _assert_validation_error(
            lambda: record_debt_lifecycle_event(
                club_id=club.id,
                debt=debt,
                event_type=DebtLifecycleEvent.EventType.ATTACHED,
                previous_state="open",
                new_state="resolved:subscription",
                actor_user_id=missing_id,
            ),
            message_dict={
                "actor": [
                    f'Значение "{missing_id}" не является допустимым для поля "id" '
                    "объекта типа пользователь"
                ]
            },
        )
        with pytest.raises(Payment.DoesNotExist) as payment_exc:
            record_debt_lifecycle_event(
                club_id=club.id,
                debt=debt,
                event_type=DebtLifecycleEvent.EventType.RESERVED,
                previous_state="open",
                new_state="reserved",
                payment_id=missing_id,
            )
        assert str(payment_exc.value) == "Payment matching query does not exist."

        with pytest.raises(Subscription.DoesNotExist) as subscription_exc:
            record_debt_lifecycle_event(
                club_id=club.id,
                debt=debt,
                event_type=DebtLifecycleEvent.EventType.ATTACHED,
                previous_state="open",
                new_state="resolved:subscription",
                subscription_id=missing_id,
            )
        assert str(subscription_exc.value) == "Subscription matching query does not exist."

        assert not DebtLifecycleEvent.objects.for_club(club.id).filter(debt=debt).exists()

    def test_lifecycle_event_integrity_error_at_save_escapes_without_audit(
        self,
        club,
        monkeypatch,
    ):
        training_type, _tariff, student, _subscription, _payment = _regular_subscription_context(club=club)
        debt = _open_debt(club=club, student=student, training_type=training_type)

        def fail_save(_event):
            raise IntegrityError("forced lifecycle save failure")

        monkeypatch.setattr(DebtLifecycleEvent, "save", fail_save)

        with pytest.raises(IntegrityError, match="^forced lifecycle save failure$"):
            record_debt_lifecycle_event(
                club_id=club.id,
                debt=debt,
                event_type=DebtLifecycleEvent.EventType.ATTACHED,
                previous_state="open",
                new_state="resolved:subscription",
            )

        assert not DebtLifecycleEvent.objects.for_club(club.id).filter(debt=debt).exists()

    def test_payment_path_requires_booking_payment_without_booking_id(self, club, owner_user):
        context = _personal_drop_in_context(club=club, owner_user=owner_user, attended=False)
        booking = context["booking"]

        _assert_business_logic_error(
            lambda: _assert_personal_drop_in_payment_path(
                club_id=club.id,
                student_id=context["student"].id,
                tariff_id=context["tariff"].id,
            ),
            code="personal_drop_in_use_booking_payment",
            message="Используйте оплату из карточки разовой персоналки",
        )

        booking.refresh_from_db()
        assert booking.state == PersonalDropInBooking.State.SCHEDULED
        assert PersonalDropInBooking.objects.for_club(club.id).count() == 1

    def test_payment_path_rejects_non_actionable_booking_id(self, club, owner_user):
        context = _personal_drop_in_context(club=club, owner_user=owner_user, attended=False)
        booking = context["booking"]

        _assert_business_logic_error(
            lambda: _assert_personal_drop_in_payment_path(
                club_id=club.id,
                student_id=context["student"].id,
                tariff_id=context["tariff"].id,
                personal_drop_in_booking_id=booking.id + 100000,
            ),
            code="personal_drop_in_payment_not_actionable",
            message="Разовая персоналка недоступна для этой оплаты",
        )

        booking.refresh_from_db()
        assert booking.state == PersonalDropInBooking.State.SCHEDULED
        assert PersonalDropInBooking.objects.for_club(club.id).count() == 1

    @pytest.mark.parametrize("existing_origin", ["payment_link", "bank_order"])
    def test_payment_path_rejects_existing_pending_origin(
        self,
        club,
        owner_user,
        existing_origin,
    ):
        context = _personal_drop_in_context(club=club, owner_user=owner_user, attended=False)
        booking = context["booking"]
        payment = PaymentFactory(
            club=club,
            student=context["student"],
            tariff=context["tariff"],
            subscription=context["subscription"],
            status=Payment.Status.PENDING,
        )
        if existing_origin == "payment_link":
            PersonalDropInPaymentLink.objects.create(
                club=club,
                booking=booking,
                payment=payment,
                created_by=owner_user,
                idempotency_key="debt-contract-live-link",
            )
        else:
            BankPaymentOrder.objects.create(
                club=club,
                payment=payment,
                subscription=context["subscription"],
                student=context["student"],
                provider=BankPaymentOrder.Provider.MOCK,
                source=BankPaymentOrder.Source.OWNER,
                status=BankPaymentOrder.Status.PENDING,
                amount_snapshot=payment.amount,
                purpose_snapshot="Personal drop-in",
                provider_payment_url="https://pay.example.test/drop-in",
                personal_drop_in_booking_id_snapshot=booking.id,
                expires_at=timezone.now() + timedelta(hours=1),
                created_by=owner_user,
            )

        _assert_business_logic_error(
            lambda: _assert_personal_drop_in_payment_path(
                club_id=club.id,
                student_id=context["student"].id,
                tariff_id=context["tariff"].id,
                personal_drop_in_booking_id=booking.id,
            ),
            code="personal_drop_in_payment_pending",
            message="Разовая персоналка уже ожидает оплату",
        )

    def test_drop_in_debt_tariff_mismatch_has_no_mutation(self, club, owner_user):
        context = _personal_drop_in_context(club=club, owner_user=owner_user, attended=True)
        debt = context["debt"]
        other_tariff = TariffFactory(club=club, training_type=context["training_type"])
        debt.required_tariff = other_tariff
        debt.save(update_fields=["required_tariff", "updated_at"])

        _assert_business_logic_error(
            lambda: _validate_personal_drop_in_debt_tariff_contract(
                club_id=club.id,
                subscription=context["subscription"],
                debts=[debt],
            ),
            code="drop_in_debt_tariff_mismatch",
            message="Выбранный долг за персоналку можно закрыть только его исходным тарифом",
        )

        debt.refresh_from_db()
        assert debt.required_tariff_id == other_tariff.id
        assert debt.resolved_at is None
        _assert_no_debt_audit(debt=debt)

    def test_drop_in_debt_changed_tariff_price_has_no_mutation(self, club, owner_user):
        context = _personal_drop_in_context(club=club, owner_user=owner_user, attended=True)
        booking = context["booking"]
        debt = context["debt"]
        booking.price_snapshot = Decimal("999.00")
        booking.save(update_fields=["price_snapshot", "updated_at"])

        _assert_business_logic_error(
            lambda: _validate_personal_drop_in_debt_tariff_contract(
                club_id=club.id,
                subscription=context["subscription"],
                debts=[debt],
            ),
            code="drop_in_tariff_contract_changed",
            message="Тариф персоналки изменился после записи и не может закрыть этот долг",
        )

        booking.refresh_from_db()
        debt.refresh_from_db()
        assert booking.price_snapshot == Decimal("999.00")
        assert debt.resolved_at is None
        _assert_no_debt_audit(debt=debt)

    def test_attach_rejects_missing_selected_debt_without_mutation(self, club):
        _training_type, _tariff, student, subscription, _payment = _regular_subscription_context(club=club)

        _assert_business_logic_error(
            lambda: _attach_debts_to_subscription(
                subscription=subscription,
                club_id=club.id,
                student_id=student.id,
                resolution_type="subscription",
                debt_ids=[999999],
                attach_all_matching=False,
            ),
            code="debt_not_found",
            message="Долг не найден",
        )

        subscription.refresh_from_db()
        assert subscription.trainings_left == 3
        assert not DebtLifecycleEvent.objects.for_club(club.id).exists()
        assert not DebtSettlementEvent.objects.for_club(club.id).exists()

    def test_attach_rejects_debts_over_subscription_limit_without_mutation(self, club):
        training_type, tariff, student, subscription, _payment = _regular_subscription_context(
            club=club,
            trainings_left=1,
        )
        first = _open_debt(
            club=club,
            student=student,
            training_type=training_type,
            required_tariff=tariff,
        )
        second = _open_debt(
            club=club,
            student=student,
            training_type=training_type,
            required_tariff=tariff,
        )

        _assert_business_logic_error(
            lambda: _attach_debts_to_subscription(
                subscription=subscription,
                club_id=club.id,
                student_id=student.id,
                resolution_type="subscription",
                debt_ids=[first.id, second.id],
                attach_all_matching=False,
            ),
            code="debt_exceeds_subscription_limit",
            message="Выбранных долгов больше, чем тренировок в абонементе",
        )

        first.refresh_from_db()
        second.refresh_from_db()
        assert first.resolved_at is None and second.resolved_at is None
        assert first.settlement_payment_id is None and second.settlement_payment_id is None
        _assert_no_debt_audit(debt=first)
        _assert_no_debt_audit(debt=second)

    def test_attach_rejects_unmatched_subscription_component_without_mutation(self, club):
        training_type, tariff, student, subscription, _payment = _regular_subscription_context(club=club)
        covered_location = LocationFactory(club=club)
        other_location = LocationFactory(club=club)
        tariff_component = TariffComponentFactory(
            club=club,
            tariff=tariff,
            training_type=training_type,
            scope=Tariff.Scope.LOCATION,
            location=covered_location,
            credits_total=1,
        )
        SubscriptionComponentFactory(
            club=club,
            subscription=subscription,
            tariff_component=tariff_component,
            training_type=training_type,
            scope=Tariff.Scope.LOCATION,
            location=covered_location,
            credits_total=1,
            credits_left=1,
        )
        debt = _open_debt(
            club=club,
            student=student,
            training_type=training_type,
            location=other_location,
            required_tariff=tariff,
        )

        _assert_business_logic_error(
            lambda: _attach_debts_to_subscription(
                subscription=subscription,
                club_id=club.id,
                student_id=student.id,
                resolution_type="subscription",
                debt_ids=[debt.id],
                attach_all_matching=False,
            ),
            code="subscription_component_not_found",
            message="Нет подходящего компонента абонемента для закрытия долга",
        )

        debt.refresh_from_db()
        assert debt.resolved_at is None
        assert debt.checkin.subscription_id is None
        _assert_no_debt_audit(debt=debt)

    def test_attach_late_drop_in_requires_payment_and_rolls_back(self, club, owner_user):
        context = _personal_drop_in_context(club=club, owner_user=owner_user, attended=True)
        debt = context["debt"]
        close_trainer_payroll_period(
            club_id=club.id,
            period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 10),
            reason="Debt error contract",
            actor_user_id=owner_user.id,
        )

        def attempt():
            with transaction.atomic():
                _attach_debts_to_subscription(
                    subscription=context["subscription"],
                    club_id=club.id,
                    student_id=context["student"].id,
                    resolution_type="subscription",
                    debt_ids=[debt.id],
                    attach_all_matching=False,
                )

        _assert_business_logic_error(
            attempt,
            code="drop_in_payment_required",
            message="Late drop-in settlement requires its payment",
        )

        debt.refresh_from_db()
        debt.checkin.refresh_from_db()
        assert debt.resolved_at is None
        assert debt.checkin.subscription_id is None
        _assert_no_debt_audit(debt=debt)

    def test_capacity_rejects_debts_over_finite_subscription_limit_without_mutation(self, club):
        training_type, tariff, student, subscription, payment = _regular_subscription_context(
            club=club,
            trainings_left=1,
        )
        first = _open_debt(club=club, student=student, training_type=training_type, required_tariff=tariff)
        second = _open_debt(club=club, student=student, training_type=training_type, required_tariff=tariff)

        _assert_business_logic_error(
            lambda: assert_payment_reservation_capacity(
                payment=payment,
                subscription=subscription,
                club_id=club.id,
                debts=[first, second],
            ),
            code="debt_exceeds_subscription_limit",
            message="Выбранных долгов больше, чем тренировок в абонементе",
        )

        assert first.settlement_payment_id is None and second.settlement_payment_id is None
        _assert_no_debt_audit(debt=first)
        _assert_no_debt_audit(debt=second)

    def test_capacity_success_returns_none_without_mutation(self, club):
        training_type, tariff, student, subscription, payment = _regular_subscription_context(club=club)
        debt = _open_debt(club=club, student=student, training_type=training_type, required_tariff=tariff)

        result = assert_payment_reservation_capacity(
            payment=payment,
            subscription=subscription,
            club_id=club.id,
            debts=[debt],
        )

        assert result is None
        debt.refresh_from_db()
        assert debt.settlement_payment_id is None
        _assert_no_debt_audit(debt=debt)

    def test_capacity_rejects_exhausted_component_without_mutation(self, club):
        training_type, tariff, student, subscription, payment = _regular_subscription_context(club=club)
        tariff_component = TariffComponentFactory(
            club=club,
            tariff=tariff,
            training_type=training_type,
            credits_total=1,
        )
        SubscriptionComponentFactory(
            club=club,
            subscription=subscription,
            tariff_component=tariff_component,
            training_type=training_type,
            credits_total=1,
            credits_left=0,
        )
        debt = _open_debt(club=club, student=student, training_type=training_type, required_tariff=tariff)

        _assert_business_logic_error(
            lambda: assert_payment_reservation_capacity(
                payment=payment,
                subscription=subscription,
                club_id=club.id,
                debts=[debt],
            ),
            code="subscription_component_limit_exceeded",
            message="Лимит компонента абонемента исчерпан",
        )

        debt.refresh_from_db()
        assert debt.settlement_payment_id is None
        _assert_no_debt_audit(debt=debt)

    def test_reserve_rejects_missing_selected_debt_without_mutation(self, club):
        _training_type, _tariff, _student, subscription, payment = _regular_subscription_context(club=club)

        _assert_business_logic_error(
            lambda: _reserve_debts_for_payment(
                payment=payment,
                subscription=subscription,
                club_id=club.id,
                debt_ids=[999999],
            ),
            code="debt_not_found",
            message="Долг не найден",
        )

        assert not DebtSettlementEvent.objects.for_club(club.id).exists()
        assert not DebtLifecycleEvent.objects.for_club(club.id).exists()

    def test_reserve_requires_personal_booking_payment_without_mutation(self, club, owner_user):
        context = _personal_drop_in_context(club=club, owner_user=owner_user, attended=True)
        debt = context["debt"]
        payment = PaymentFactory(
            club=club,
            student=context["student"],
            tariff=context["tariff"],
            subscription=context["subscription"],
        )

        _assert_business_logic_error(
            lambda: _reserve_debts_for_payment(
                payment=payment,
                subscription=context["subscription"],
                club_id=club.id,
                debt_ids=[debt.id],
            ),
            code="personal_drop_in_use_booking_payment",
            message="Используйте оплату из карточки разовой персоналки",
        )

        debt.refresh_from_db()
        assert debt.settlement_payment_id is None
        _assert_no_debt_audit(debt=debt)

    def test_reserve_rejects_debts_over_subscription_limit_without_mutation(self, club):
        training_type, tariff, student, subscription, payment = _regular_subscription_context(
            club=club,
            trainings_left=1,
        )
        first = _open_debt(club=club, student=student, training_type=training_type, required_tariff=tariff)
        second = _open_debt(club=club, student=student, training_type=training_type, required_tariff=tariff)

        _assert_business_logic_error(
            lambda: _reserve_debts_for_payment(
                payment=payment,
                subscription=subscription,
                club_id=club.id,
                debt_ids=[first.id, second.id],
            ),
            code="debt_exceeds_subscription_limit",
            message="Выбранных долгов больше, чем тренировок в абонементе",
        )

        first.refresh_from_db()
        second.refresh_from_db()
        assert first.settlement_payment_id is None and second.settlement_payment_id is None
        _assert_no_debt_audit(debt=first)
        _assert_no_debt_audit(debt=second)

    def test_settlement_events_reject_foreign_payment_without_audit(self, club, other_club):
        _training_type, tariff, student, _subscription, payment = _regular_subscription_context(club=club)
        foreign_payment = PaymentFactory(
            club=other_club,
            student=StudentFactory(club=other_club),
            tariff=TariffFactory(club=other_club),
        )
        debt = _open_debt(club=club, student=student, training_type=tariff.training_type, required_tariff=tariff)

        _assert_business_logic_error(
            lambda: record_debt_settlement_events(
                club_id=club.id,
                payment=foreign_payment,
                debt_ids=[debt.id],
                event_type=DebtSettlementEvent.EventType.RESERVED,
            ),
            code="payment_not_found",
            message="Оплата не найдена",
        )

        assert not DebtSettlementEvent.objects.for_club(club.id).filter(debt=debt).exists()
        assert payment.club_id == club.id

    def test_settlement_events_success_returns_none_and_persists_one_event_per_debt(self, club):
        training_type, tariff, student, _subscription, payment = _regular_subscription_context(club=club)
        first = _open_debt(club=club, student=student, training_type=training_type, required_tariff=tariff)
        second = _open_debt(club=club, student=student, training_type=training_type, required_tariff=tariff)

        result = record_debt_settlement_events(
            club_id=club.id,
            payment=payment,
            debt_ids=[first.id, first.id, second.id],
            event_type=DebtSettlementEvent.EventType.RESERVED,
        )

        assert result is None
        assert set(
            DebtSettlementEvent.objects.for_club(club.id).values_list(
                "debt_id",
                "payment_id",
                "event_type",
            )
        ) == {
            (first.id, payment.id, DebtSettlementEvent.EventType.RESERVED),
            (second.id, payment.id, DebtSettlementEvent.EventType.RESERVED),
        }

    def test_settlement_events_reject_missing_debt_without_audit(self, club):
        _training_type, _tariff, _student, _subscription, payment = _regular_subscription_context(club=club)

        _assert_business_logic_error(
            lambda: record_debt_settlement_events(
                club_id=club.id,
                payment=payment,
                debt_ids=[999999],
                event_type=DebtSettlementEvent.EventType.RESERVED,
            ),
            code="debt_not_found",
            message="Долг не найден",
        )

        assert not DebtSettlementEvent.objects.for_club(club.id).exists()

    def test_write_off_requires_reason_without_mutation(self, club, owner_user):
        training_type, _tariff, student, _subscription, _payment = _regular_subscription_context(club=club)
        debt = _open_debt(club=club, student=student, training_type=training_type)

        _assert_business_logic_error(
            lambda: write_off_debt(
                debt_id=debt.id,
                club_id=club.id,
                written_off_by_id=owner_user.id,
                reason="   ",
            ),
            code="writeoff_reason_required",
            message="Write-off reason is required",
        )

        debt.refresh_from_db()
        assert debt.resolved_at is None
        _assert_no_debt_audit(debt=debt)

    def test_write_off_rejects_resolved_debt_without_mutation(self, club, owner_user):
        training_type, _tariff, student, _subscription, _payment = _regular_subscription_context(club=club)
        debt = _open_debt(club=club, student=student, training_type=training_type)
        debt.resolved_at = timezone.now()
        debt.resolution_type = "payment"
        debt.save(update_fields=["resolved_at", "resolution_type", "updated_at"])

        _assert_business_logic_error(
            lambda: write_off_debt(
                debt_id=debt.id,
                club_id=club.id,
                written_off_by_id=owner_user.id,
                reason="Duplicate write-off",
            ),
            code="debt_already_resolved",
            message="Долг уже закрыт",
        )

        debt.refresh_from_db()
        assert debt.resolution_type == "payment"
        _assert_no_debt_audit(debt=debt)

    def test_write_off_rejects_pending_payment_reservation_without_mutation(self, club, owner_user):
        training_type, tariff, student, _subscription, pending_payment = _regular_subscription_context(club=club)
        debt = _open_debt(club=club, student=student, training_type=training_type, required_tariff=tariff)
        debt.settlement_payment = pending_payment
        debt.save(update_fields=["settlement_payment", "updated_at"])

        _assert_business_logic_error(
            lambda: write_off_debt(
                debt_id=debt.id,
                club_id=club.id,
                written_off_by_id=owner_user.id,
                reason="Bypass attempt",
            ),
            code="debt_payment_pending",
            message="Долг уже привязан к ожидающей оплате",
        )

        debt.refresh_from_db()
        assert debt.settlement_payment_id == pending_payment.id
        assert debt.resolved_at is None
        _assert_no_debt_audit(debt=debt)

    def test_write_off_rejects_foreign_debt_without_mutation(self, club, other_club, owner_user):
        training_type = TrainingTypeFactory(club=other_club, kind=TrainingType.Kind.GROUP, drop_in_price=None)
        student = StudentFactory(club=other_club)
        debt = _open_debt(club=other_club, student=student, training_type=training_type)

        with pytest.raises(Debt.DoesNotExist) as exc_info:
            write_off_debt(
                debt_id=debt.id,
                club_id=club.id,
                written_off_by_id=owner_user.id,
                reason="Wrong tenant attempt",
            )
        assert str(exc_info.value) == "Debt matching query does not exist."

        debt.refresh_from_db()
        assert debt.resolved_at is None
        _assert_no_debt_audit(debt=debt)

    def test_write_off_rejects_closed_payroll_with_exact_dynamic_message(self, club, owner_user):
        training_type, _tariff, student, _subscription, _payment = _regular_subscription_context(club=club)
        checkin = CheckinFactory(
            club=club,
            student=student,
            training_type=training_type,
            subscription=None,
            is_debt=True,
            date=date(2026, 6, 10),
        )
        debt = DebtFactory(club=club, student=student, checkin=checkin)
        close_trainer_payroll_period(
            club_id=club.id,
            period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 10),
            reason="Closed payroll",
            actor_user_id=owner_user.id,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            write_off_debt(
                debt_id=debt.id,
                club_id=club.id,
                written_off_by_id=owner_user.id,
                reason="Late write-off",
            )

        assert exc_info.value.code == "payroll_period_closed"
        assert re.fullmatch(
            r"^Период выплат закрыт: 2026-06-01 — 2026-06-10\. Новые изменения выплат за этот период недоступны\.$",
            str(exc_info.value),
        )
        debt.refresh_from_db()
        assert debt.resolved_at is None
        _assert_no_debt_audit(debt=debt)

    def test_write_off_missing_actor_validation_rolls_back_debt_and_audits(self, club):
        training_type, _tariff, student, _subscription, _payment = _regular_subscription_context(club=club)
        debt = _open_debt(club=club, student=student, training_type=training_type)
        missing_id = 999999

        _assert_validation_error(
            lambda: write_off_debt(
                debt_id=debt.id,
                club_id=club.id,
                written_off_by_id=missing_id,
                reason="Missing actor",
            ),
            message_dict={
                "written_off_by": [
                    f'Значение "{missing_id}" не является допустимым для поля "id" '
                    "объекта типа пользователь"
                ]
            },
        )

        debt.refresh_from_db()
        assert debt.resolved_at is None
        assert debt.resolution_type == ""
        _assert_no_debt_audit(debt=debt)

    def test_write_off_duplicate_audit_validation_rolls_back_debt_and_preserves_existing_audit(
        self,
        club,
        owner_user,
    ):
        training_type, _tariff, student, _subscription, _payment = _regular_subscription_context(club=club)
        debt = _open_debt(club=club, student=student, training_type=training_type)
        existing_event = DebtWriteOffEvent.objects.create(
            club=club,
            debt=debt,
            written_off_by=owner_user,
            reason="Existing audit evidence",
            decided_at=timezone.now(),
            amount_snapshot=debt.tariff_price,
            debt_id_snapshot=debt.id,
            student_id_snapshot=debt.student_id,
            student_name_snapshot=str(debt.student),
            checkin_id_snapshot=debt.checkin_id,
            debt_reason_snapshot=debt.reason,
        )

        _assert_validation_error(
            lambda: write_off_debt(
                debt_id=debt.id,
                club_id=club.id,
                written_off_by_id=owner_user.id,
                reason="Duplicate audit",
            ),
            message_dict={
                "__all__": [
                    "Debt write off event с такими значениями полей Club и Debt уже существует."
                ]
            },
        )

        debt.refresh_from_db()
        assert debt.resolved_at is None
        assert debt.resolution_type == ""
        assert list(
            DebtWriteOffEvent.objects.for_club(club.id).filter(debt=debt).values_list("id", flat=True)
        ) == [existing_event.id]
        assert not DebtLifecycleEvent.objects.for_club(club.id).filter(debt=debt).exists()
