import importlib
import io
import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.apps import apps as django_apps
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, migrations, transaction
from django.test import override_settings
from django.utils import timezone

from apps.attendance.models import Checkin, ScheduleEnrollment
from apps.attendance.services import cancel_checkin, create_checkin
from apps.attendance.tests.rollout import update_training_group_rollout_state_for_test
from apps.billing.models import (
    BankPaymentOrder,
    Debt,
    DebtSettlementEvent,
    DebtWriteOffEvent,
    Discount,
    Expense,
    Payment,
    Subscription,
    SubscriptionComponent,
    SubscriptionFreeze,
    Tariff,
    TariffComponent,
    TrainingType,
)
from apps.billing.service_modules.payment_creation import _apply_discounts
from apps.billing.services import (
    approve_freeze,
    cancel_bank_payment_order,
    create_bank_payment_order,
    create_discount,
    create_expense,
    create_payment,
    create_subscription,
    create_tariff,
    create_training_type,
    delete_expense,
    freeze_subscription,
    process_bank_payment_webhook,
    reject_freeze,
    unfreeze_subscription,
    update_discount,
    update_expense,
    update_tariff,
    update_training_type,
    verify_payment,
    write_off_debt,
)
from apps.billing.tasks import notify_payment_verification
from apps.billing.tests.factories import (
    DebtFactory,
    DiscountFactory,
    ExpenseFactory,
    PaymentFactory,
    SubscriptionComponentFactory,
    SubscriptionFactory,
    SubscriptionFreezeFactory,
    TariffComponentFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.clubs.models import Club, ClubMembership, ClubSettings
from apps.clubs.tests.factories import ClubFactory, ClubSettingsFactory, LocationFactory, UserFactory
from apps.common.exceptions import BusinessLogicError
from apps.grades.tests.factories import GradeSystemFactory
from apps.leads.tests.factories import LeadFactory
from apps.students.models import AccountAccess, Student
from apps.students.tests.factories import StudentFactory


def _enable_legacy_manual_group_admission(*, club, settings, manual_admission_enabled: bool) -> None:
    """Make historical generic-payment fixtures explicit about their v1 capability."""

    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = manual_admission_enabled
    ClubSettings.objects.update_or_create(
        club=club,
        defaults={
            "unified_client_journey_enabled": True,
            "commercial_journey_protocol_version": ClubSettings.CommercialJourneyProtocol.V1,
        },
    )


@pytest.mark.django_db
class TestPaymentVerificationTask:
    @patch("apps.notifications.services.send_push_to_user")
    def test_notifies_active_owner_and_admin_with_review_actions(self, mock_push, club):
        owner_user = UserFactory()
        admin_user = UserFactory()
        inactive_admin_user = UserFactory()
        trainer_user = UserFactory()
        ClubMembership.objects.create(user=owner_user, club=club, role=ClubMembership.Role.OWNER)
        ClubMembership.objects.create(user=admin_user, club=club, role=ClubMembership.Role.ADMIN)
        ClubMembership.objects.create(
            user=inactive_admin_user,
            club=club,
            role=ClubMembership.Role.ADMIN,
            is_active=False,
        )
        ClubMembership.objects.create(user=trainer_user, club=club, role=ClubMembership.Role.TRAINER)

        tariff = TariffFactory(club=club)
        student = StudentFactory(club=club)
        payment = PaymentFactory(
            club=club,
            student=student,
            tariff=tariff,
            amount=Decimal("5000"),
        )

        notify_payment_verification(payment_id=payment.id, club_id=club.id)

        assert mock_push.call_count == 2
        notified_user_ids = {call.kwargs["user_id"] for call in mock_push.call_args_list}
        assert notified_user_ids == {owner_user.id, admin_user.id}
        for call in mock_push.call_args_list:
            assert call.kwargs["title"] == "Верификация оплаты"
            assert call.kwargs["url"] == "/dashboard/billing/"
            assert call.kwargs["actions"] == [
                {"action": "confirm", "title": "Подтвердить"},
                {"action": "reject", "title": "Отклонить"},
            ]
            assert call.kwargs["data"] == {
                "payment_id": payment.id,
                "tag": f"payment-verify-{payment.id}",
            }

    @pytest.mark.parametrize(
        ("payment_method", "expected_suffix"),
        [
            (Payment.Method.CASH, "руб. наличными"),
            (Payment.Method.TRANSFER, "руб. перевод"),
        ],
    )
    @patch("apps.notifications.services.send_push_to_user")
    def test_uses_recorded_payment_method_in_copy(
        self,
        mock_push,
        payment_method,
        expected_suffix,
        club,
    ):
        owner_user = UserFactory()
        ClubMembership.objects.create(user=owner_user, club=club, role=ClubMembership.Role.OWNER)
        payment = PaymentFactory(
            club=club,
            payment_method=payment_method,
            amount=Decimal("5000"),
        )

        notify_payment_verification(payment_id=payment.id, club_id=club.id)

        assert mock_push.call_count == 1
        assert mock_push.call_args.kwargs["body"].endswith(expected_suffix)


@pytest.mark.django_db
class TestCreateTrainingType:
    def test_create_training_type(self, club):
        tt = create_training_type(club_id=club.id, name="Group", slug="group")
        assert tt.club_id == club.id
        assert tt.name == "Group"
        assert tt.slug == "group"
        assert tt.kind == TrainingType.Kind.GROUP
        assert tt.is_active is True
        assert tt.grade_system_id is None

    def test_create_training_type_can_link_grade_system(self, club):
        grade_system = GradeSystemFactory(club=club, discipline="BJJ")

        tt = create_training_type(
            club_id=club.id,
            name="BJJ",
            slug="bjj",
            kind=TrainingType.Kind.PERSONAL,
            grade_system_id=grade_system.id,
        )

        assert tt.kind == TrainingType.Kind.PERSONAL
        assert tt.grade_system_id == grade_system.id

    def test_create_training_type_rejects_invalid_kind(self, club):
        with pytest.raises(BusinessLogicError) as exc_info:
            create_training_type(
                club_id=club.id,
                name="Invalid",
                slug="invalid",
                kind="solo",
            )

        assert exc_info.value.code == "invalid_training_type_kind"

    def test_create_training_type_rejects_foreign_grade_system(self, club, other_club):
        foreign_grade_system = GradeSystemFactory(club=other_club, discipline="BJJ")

        with pytest.raises(BusinessLogicError) as exc_info:
            create_training_type(
                club_id=club.id,
                name="BJJ",
                slug="bjj",
                grade_system_id=foreign_grade_system.id,
            )

        assert exc_info.value.code == "grade_system_not_found"

    def test_training_type_unique_slug_per_club(self, club):
        create_training_type(club_id=club.id, name="Group", slug="group")
        with pytest.raises(IntegrityError):
            create_training_type(club_id=club.id, name="Group 2", slug="group")

    def test_update_training_type_can_change_grade_system(self, club):
        old_system = GradeSystemFactory(club=club, discipline="BJJ")
        new_system = GradeSystemFactory(club=club, discipline="Boxing")
        tt = TrainingTypeFactory(club=club, grade_system=old_system)

        updated = update_training_type(
            training_type_id=tt.id,
            club_id=club.id,
            kind=TrainingType.Kind.MINI_GROUP,
            grade_system_id=new_system.id,
        )

        assert updated.kind == TrainingType.Kind.MINI_GROUP
        assert updated.grade_system_id == new_system.id

    def test_update_training_type_rejects_foreign_grade_system(self, club, other_club):
        foreign_grade_system = GradeSystemFactory(club=other_club, discipline="BJJ")
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)

        with pytest.raises(BusinessLogicError) as exc_info:
            update_training_type(
                training_type_id=tt.id,
                club_id=club.id,
                grade_system_id=foreign_grade_system.id,
            )

        assert exc_info.value.code == "grade_system_not_found"

    def test_update_training_type_rejects_kind_change_after_use(self, club):
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        TariffFactory(training_type=tt)

        with pytest.raises(BusinessLogicError) as exc_info:
            update_training_type(
                training_type_id=tt.id,
                club_id=club.id,
                kind=TrainingType.Kind.GROUP,
            )

        assert exc_info.value.code == "training_type_kind_locked"
        tt.refresh_from_db()
        assert tt.kind == TrainingType.Kind.PERSONAL


@pytest.mark.django_db
class TestCreateTariff:
    def test_create_tariff(self, club):
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = create_tariff(
            club_id=club.id,
            name="8 sessions",
            training_type_id=tt.id,
            price=Decimal("5000"),
            trainings_limit=8,
            duration_days=30,
            scope="club",
        )
        assert tariff.name == "8 sessions"
        assert tariff.price == Decimal("5000")
        assert tariff.trainings_limit == 8
        assert tariff.duration_days == 30
        assert tariff.scope == "club"
        assert tariff.location is None

    def test_create_tariff_unlimited(self, club):
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = create_tariff(
            club_id=club.id,
            name="Unlimited",
            training_type_id=tt.id,
            price=Decimal("10000"),
            trainings_limit=None,
            duration_days=30,
            scope="club",
        )
        assert tariff.trainings_limit is None

    def test_create_tariff_location_scope(self, club):
        tt = TrainingTypeFactory(club=club)
        location = LocationFactory(club=club)
        tariff = create_tariff(
            club_id=club.id,
            name="Location tariff",
            training_type_id=tt.id,
            price=Decimal("4000"),
            trainings_limit=8,
            duration_days=30,
            scope="location",
            location_id=location.id,
        )
        assert tariff.scope == "location"
        assert tariff.location == location

    def test_create_tariff_location_scope_requires_location(self, club):
        tt = TrainingTypeFactory(club=club)
        with pytest.raises(BusinessLogicError, match="Location is required"):
            create_tariff(
                club_id=club.id,
                name="Bad tariff",
                training_type_id=tt.id,
                price=Decimal("4000"),
                trainings_limit=8,
                duration_days=30,
                scope="location",
                location_id=None,
            )

    def test_create_tariff_rejects_unknown_scope(self, club):
        tt = TrainingTypeFactory(club=club)

        with pytest.raises(BusinessLogicError) as exc_info:
            create_tariff(
                club_id=club.id,
                name="Bad scope",
                training_type_id=tt.id,
                price=Decimal("4000"),
                trainings_limit=8,
                duration_days=30,
                scope="locaiton",
            )

        assert exc_info.value.code == "invalid_tariff_scope"

    @pytest.mark.parametrize("price", [Decimal("0"), Decimal("-1")])
    def test_create_tariff_rejects_non_positive_price(self, club, price):
        tt = TrainingTypeFactory(club=club)

        with pytest.raises(BusinessLogicError) as exc_info:
            create_tariff(
                club_id=club.id,
                name="Bad price",
                training_type_id=tt.id,
                price=price,
                trainings_limit=8,
                duration_days=30,
            )

        assert exc_info.value.code == "invalid_money_amount"

    def test_update_tariff_rejects_non_positive_price(self, club):
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(club=club, training_type=tt, price=Decimal("5000"))

        with pytest.raises(BusinessLogicError) as exc_info:
            update_tariff(
                tariff_id=tariff.id,
                club_id=club.id,
                price=Decimal("0"),
            )

        assert exc_info.value.code == "invalid_money_amount"


@pytest.mark.django_db
class TestCreateSubscription:
    def test_current_component_subscription_uses_strict_point_in_time_expiry(self, club):
        from apps.billing.service_modules.entitlements import (
            _student_has_current_component_subscription,
        )

        training_type = TrainingTypeFactory(club=club)
        tariff = TariffFactory(club=club, training_type=training_type)
        component = TariffComponentFactory(club=club, tariff=tariff, training_type=training_type)
        student = StudentFactory(club=club)
        effective_at = timezone.now()
        subscription = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            expires_at=effective_at,
        )
        SubscriptionComponentFactory(
            club=club,
            subscription=subscription,
            tariff_component=component,
            credits_total=1,
            credits_left=1,
        )

        assert not _student_has_current_component_subscription(
            club_id=club.id,
            student_id=student.id,
            components=[component],
            effective_at=effective_at,
        )

        subscription.expires_at = effective_at + timedelta(seconds=1)
        subscription.save(update_fields=["expires_at", "updated_at"])

        assert _student_has_current_component_subscription(
            club_id=club.id,
            student_id=student.id,
            components=[component],
            effective_at=effective_at,
        )

    def test_create_subscription(self, club):
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt, trainings_limit=8, duration_days=30)
        student = StudentFactory(club=club)

        sub = create_subscription(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
        )

        assert sub.status == Subscription.Status.ACTIVE
        assert sub.trainings_left == 8
        assert sub.trainings_used == 0
        assert sub.scope == tariff.scope
        assert sub.expires_at is not None

    def test_subscription_price_from_tariff(self, club):
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt, price=Decimal("7000"))
        student = StudentFactory(club=club)

        sub = create_subscription(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
        )

        # Price comes from tariff, not from input
        assert sub.tariff.price == Decimal("7000")

    def test_create_subscription_snapshots_paid_amount_for_history(self, club):
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt, price=Decimal("7000"))
        student = StudentFactory(club=club)

        sub = create_subscription(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
        )
        tariff.price = Decimal("9000")
        tariff.save(update_fields=["price"])

        sub.refresh_from_db()
        assert sub.paid_amount == Decimal("7000")

    def test_create_subscription_unlimited(self, club):
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt, trainings_limit=None)
        student = StudentFactory(club=club)

        sub = create_subscription(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
        )

        assert sub.trainings_left is None

    def test_create_subscription_with_recorder_preserves_transfer_in_confirmed_payment_audit(
        self,
        club,
        owner_user,
    ):
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=tt, price=Decimal("7000"))
        student = StudentFactory(club=club)

        sub = create_subscription(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            recorded_by_id=owner_user.id,
            payment_method=Payment.Method.TRANSFER,
        )

        payment = Payment.objects.get(subscription=sub)
        assert payment.status == Payment.Status.CONFIRMED
        assert payment.amount == Decimal("7000")
        assert payment.original_amount == Decimal("7000")
        assert payment.recorded_by_id == owner_user.id
        assert payment.verified_by_id == owner_user.id
        assert payment.payment_method == Payment.Method.TRANSFER
        assert payment.seller_trainer_id is None
        sub.refresh_from_db()
        assert sub.paid_amount == payment.amount

    def test_confirmed_subscription_requires_explicit_manual_payment_method_before_side_effects(
        self,
        club,
        owner_user,
    ):
        tariff = TariffFactory(
            club=club,
            training_type=TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP),
        )
        student = StudentFactory(club=club)

        with pytest.raises(BusinessLogicError) as exc_info:
            create_subscription(
                club_id=club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                recorded_by_id=owner_user.id,
            )

        assert exc_info.value.code == "manual_payment_method_required"
        assert not Subscription.objects.for_club(club).filter(student=student).exists()
        assert not Payment.objects.for_club(club).filter(student=student).exists()

    @pytest.mark.parametrize(
        ("payment_method", "expected_code"),
        [
            (Payment.Method.ONLINE, "online_payment_requires_bank_order"),
            ("crypto", "invalid_payment_method"),
        ],
    )
    def test_confirmed_subscription_rejects_non_manual_method_without_financial_or_debt_effects(
        self,
        payment_method,
        expected_code,
        club,
        owner_user,
    ):
        from apps.attendance.tests.factories import CheckinFactory

        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(club=club, training_type=training_type)
        student = StudentFactory(club=club)
        checkin = CheckinFactory(
            club=club,
            student=student,
            training_type=training_type,
            subscription=None,
            is_debt=True,
        )
        debt = DebtFactory(club=club, student=student, checkin=checkin)

        with pytest.raises(BusinessLogicError) as exc_info:
            create_subscription(
                club_id=club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                recorded_by_id=owner_user.id,
                payment_method=payment_method,
                debt_ids=[debt.id],
            )

        assert exc_info.value.code == expected_code
        checkin.refresh_from_db()
        debt.refresh_from_db()
        assert not Subscription.objects.for_club(club).filter(student=student).exists()
        assert not Payment.objects.for_club(club).filter(student=student).exists()
        assert not DebtSettlementEvent.objects.for_club(club).filter(debt=debt).exists()
        assert checkin.subscription_id is None
        assert checkin.is_debt is True
        assert debt.resolved_at is None
        assert debt.settlement_payment_id is None

    @patch("django_q.tasks.async_task")
    def test_create_subscription_uses_club_local_day_for_payroll_close(
        self,
        _mock_async,
        club,
        owner_user,
    ):
        from apps.trainers.services import close_trainer_payroll_period
        from apps.trainers.tests.factories import TrainerFactory, TrainerLocationFactory

        club.timezone = "Asia/Yekaterinburg"
        club.save(update_fields=["timezone"])
        closed_local_date = date(2026, 7, 16)
        verified_at = datetime(2026, 7, 15, 20, 30, tzinfo=UTC)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(
            club=club,
            training_type=training_type,
            scope=Tariff.Scope.LOCATION,
            location=location,
            trainer_payout_policy=Tariff.PayoutPolicy.ON_PAYMENT,
        )
        trainer = TrainerFactory(club=club)
        TrainerLocationFactory(
            club=club,
            trainer=trainer,
            location=location,
            rate_group=Decimal("20.00"),
        )
        student = StudentFactory(club=club)
        close_trainer_payroll_period(
            club_id=club.id,
            period_start=closed_local_date,
            period_end=closed_local_date,
            reason="Closed",
            actor_user_id=owner_user.id,
        )

        with patch(
            "apps.billing.service_modules.subscriptions.timezone",
            wraps=timezone,
        ) as service_timezone:
            service_timezone.now.return_value = verified_at
            service_timezone.localdate.return_value = date(2026, 7, 15)
            with pytest.raises(BusinessLogicError) as exc_info:
                create_subscription(
                    club_id=club.id,
                    student_id=student.id,
                    tariff_id=tariff.id,
                    seller_trainer_id=trainer.id,
                    recorded_by_id=owner_user.id,
                    payment_method=Payment.Method.CASH,
                )

        assert exc_info.value.code == "payroll_period_closed"
        assert not Subscription.objects.for_club(club).filter(student=student).exists()
        assert not Payment.objects.for_club(club).filter(student=student).exists()

    @patch("django_q.tasks.async_task")
    def test_create_subscription_reuses_one_effective_verified_timestamp(
        self,
        _mock_async,
        club,
        owner_user,
    ):
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(
            club=club,
            training_type=training_type,
            trainer_payout_policy=Tariff.PayoutPolicy.ON_PAYMENT,
            duration_days=30,
        )
        student = StudentFactory(club=club)
        effective_verified_at = datetime(2026, 7, 15, 18, 30, tzinfo=UTC)
        later_timestamp = effective_verified_at + timedelta(minutes=5)
        latest_timestamp = later_timestamp + timedelta(minutes=5)

        with patch(
            "apps.billing.service_modules.subscriptions.timezone",
            wraps=timezone,
        ) as service_timezone:
            service_timezone.now.side_effect = [
                effective_verified_at,
                later_timestamp,
                latest_timestamp,
            ]
            subscription = create_subscription(
                club_id=club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                recorded_by_id=owner_user.id,
                payment_method=Payment.Method.CASH,
            )

        payment = Payment.objects.for_club(club).get(subscription=subscription)
        assert service_timezone.now.call_count == 1
        assert payment.verified_at == effective_verified_at
        assert subscription.expires_at == effective_verified_at + timedelta(days=30)

    def test_create_subscription_for_adult_lead_converts_without_opening_student_access(
        self,
        club,
        owner_user,
    ):
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=tt, price=Decimal("7000"))
        lead = LeadFactory(
            club=club,
            is_child=False,
            phone="8 900 123 45 67",
            status=Student.Status.TRIAL,
            lead_status=Student.LeadStatus.TRIAL_DONE,
        )

        create_subscription(
            club_id=club.id,
            student_id=lead.id,
            tariff_id=tariff.id,
            recorded_by_id=owner_user.id,
            payment_method=Payment.Method.CASH,
        )

        lead.refresh_from_db()
        assert lead.status == Student.Status.ACTIVE
        assert lead.lead_status is None
        assert lead.user_id is None
        assert not AccountAccess.objects.for_club(club).filter(student=lead).exists()

    def test_create_subscription_requires_package_owner_for_personal_sale(self, club, owner_user):
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)

        with pytest.raises(BusinessLogicError) as exc_info:
            create_subscription(
                club_id=club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                recorded_by_id=owner_user.id,
                payment_method=Payment.Method.CASH,
            )

        assert exc_info.value.code == "package_owner_trainer_required"

    def test_create_subscription_with_package_owner_creates_personal_allocation(self, club, owner_user):
        from apps.billing.models import TrainingType
        from apps.trainers.models import TrainerPackageAllocation
        from apps.trainers.tests.factories import TrainerFactory

        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        tariff = TariffFactory(training_type=tt, price=Decimal("7000"), trainings_limit=7)
        student = StudentFactory(club=club)
        trainer = TrainerFactory(club=club)

        sub = create_subscription(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            seller_trainer_id=trainer.id,
            package_owner_trainer_id=trainer.id,
            recorded_by_id=owner_user.id,
            payment_method=Payment.Method.CASH,
        )

        allocation = TrainerPackageAllocation.objects.get(subscription=sub)
        assert allocation.owner_trainer_id == trainer.id
        assert Payment.objects.get(subscription=sub).package_owner_trainer_id == trainer.id
        assert allocation.payment == Payment.objects.get(subscription=sub)
        assert allocation.training_type == tt
        assert allocation.sessions_total_snapshot == 7
        assert allocation.sessions_remaining_snapshot == 7
        assert allocation.amount_snapshot == Decimal("7000.00")

    @patch("django_q.tasks.async_task")
    def test_create_subscription_package_allocation_snapshots_remaining_after_debt_settlement(
        self,
        mock_async,
        club,
        owner_user,
    ):
        from apps.attendance.tests.factories import CheckinFactory
        from apps.trainers.models import TrainerPackageAllocation
        from apps.trainers.tests.factories import TrainerFactory

        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        tariff = TariffFactory(training_type=tt, trainings_limit=7)
        student = StudentFactory(club=club)
        trainer = TrainerFactory(club=club)
        checkin = CheckinFactory(
            club=club,
            student=student,
            training_type=tt,
            subscription=None,
            is_debt=True,
        )
        debt = DebtFactory(club=club, student=student, checkin=checkin)

        sub = create_subscription(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            package_owner_trainer_id=trainer.id,
            recorded_by_id=owner_user.id,
            payment_method=Payment.Method.CASH,
            debt_ids=[debt.id],
        )

        allocation = TrainerPackageAllocation.objects.get(subscription=sub)
        assert allocation.sessions_total_snapshot == 7
        assert allocation.sessions_remaining_snapshot == 6
        mock_async.assert_called_once_with(
            "apps.attendance.tasks.calculate_salary",
            checkin.id,
            club_id=club.id,
        )

    @patch("django_q.tasks.async_task")
    def test_create_subscription_does_not_settle_existing_matching_debt_by_default(
        self,
        mock_async,
        club,
        owner_user,
    ):
        from apps.attendance.tests.factories import CheckinFactory

        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(
            training_type=tt,
            trainings_limit=8,
            trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
        )
        student = StudentFactory(club=club)
        checkin = CheckinFactory(
            club=club,
            student=student,
            training_type=tt,
            subscription=None,
            is_debt=True,
        )
        debt = DebtFactory(club=club, student=student, checkin=checkin)

        sub = create_subscription(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            recorded_by_id=owner_user.id,
            payment_method=Payment.Method.CASH,
        )

        sub.refresh_from_db()
        checkin.refresh_from_db()
        debt.refresh_from_db()
        assert sub.trainings_left == 8
        assert sub.trainings_used == 0
        assert checkin.subscription_id is None
        assert checkin.is_debt is True
        assert debt.resolved_at is None
        assert debt.resolution_type == ""
        mock_async.assert_not_called()

    @patch("django_q.tasks.async_task")
    def test_create_subscription_settles_selected_matching_debt(self, mock_async, club, owner_user):
        from apps.attendance.tests.factories import CheckinFactory

        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(
            training_type=tt,
            trainings_limit=8,
            trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
        )
        student = StudentFactory(club=club)
        checkin = CheckinFactory(
            club=club,
            student=student,
            training_type=tt,
            subscription=None,
            is_debt=True,
        )
        debt = DebtFactory(club=club, student=student, checkin=checkin)

        sub = create_subscription(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            recorded_by_id=owner_user.id,
            payment_method=Payment.Method.CASH,
            debt_ids=[debt.id],
        )

        sub.refresh_from_db()
        checkin.refresh_from_db()
        debt.refresh_from_db()
        assert sub.trainings_left == 7
        assert sub.trainings_used == 1
        assert checkin.subscription_id == sub.id
        assert checkin.is_debt is False
        assert debt.resolved_at is not None
        assert debt.resolution_type == "subscription"
        payment = Payment.objects.for_club(club).get(subscription=sub)
        assert debt.settlement_payment_id == payment.id
        event = DebtSettlementEvent.objects.for_club(club.id).get(payment=payment, debt=debt)
        assert event.event_type == DebtSettlementEvent.EventType.CONFIRMED
        mock_async.assert_called_once_with(
            "apps.attendance.tasks.calculate_salary",
            checkin.id,
            club_id=club.id,
        )

    def test_create_subscription_rejects_selected_debt_without_payment(self, club):
        from apps.attendance.tests.factories import CheckinFactory

        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(
            training_type=tt,
            trainings_limit=8,
            trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
        )
        student = StudentFactory(club=club)
        checkin = CheckinFactory(
            club=club,
            student=student,
            training_type=tt,
            subscription=None,
            is_debt=True,
        )
        debt = DebtFactory(club=club, student=student, checkin=checkin)

        with pytest.raises(BusinessLogicError) as exc_info:
            create_subscription(
                club_id=club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                debt_ids=[debt.id],
            )

        assert exc_info.value.code == "debt_settlement_requires_payment"
        checkin.refresh_from_db()
        debt.refresh_from_db()
        assert not Subscription.objects.for_club(club).filter(student=student, tariff=tariff).exists()
        assert Payment.objects.for_club(club).count() == 0
        assert DebtSettlementEvent.objects.for_club(club.id).count() == 0
        assert checkin.subscription_id is None
        assert checkin.is_debt is True
        assert debt.resolved_at is None
        assert debt.settlement_payment_id is None

    def test_create_subscription_rejects_debt_settlement_for_closed_payroll_period(
        self,
        club,
        owner_user,
    ):
        from apps.attendance.tests.factories import CheckinFactory
        from apps.trainers.services import close_trainer_payroll_period
        from apps.trainers.tests.factories import TrainerFactory

        target_date = date(2026, 6, 10)
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        tariff = TariffFactory(training_type=tt, trainings_limit=8)
        student = StudentFactory(club=club)
        package_owner = TrainerFactory(club=club)
        checkin = CheckinFactory(
            club=club,
            student=student,
            training_type=tt,
            subscription=None,
            is_debt=True,
            date=target_date,
        )
        debt = DebtFactory(club=club, student=student, checkin=checkin)
        close_trainer_payroll_period(
            club_id=club.id,
            period_start=target_date.replace(day=1),
            period_end=target_date,
            reason="Closed",
            actor_user_id=owner_user.id,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            create_subscription(
                club_id=club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                package_owner_trainer_id=package_owner.id,
                recorded_by_id=owner_user.id,
                payment_method=Payment.Method.CASH,
                debt_ids=[debt.id],
            )

        checkin.refresh_from_db()
        debt.refresh_from_db()
        assert exc_info.value.code == "payroll_period_closed"
        assert not Subscription.objects.filter(club=club, student=student, tariff=tariff).exists()
        assert checkin.subscription_id is None
        assert checkin.is_debt is True
        assert debt.resolved_at is None
        assert debt.resolution_type == ""

    @patch("django_q.tasks.async_task")
    def test_create_subscription_does_not_settle_other_location_debt(self, mock_async, club, owner_user):
        from apps.attendance.tests.factories import CheckinFactory, ScheduleFactory

        location_a = LocationFactory(club=club, name="A")
        location_b = LocationFactory(club=club, name="B")
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(
            training_type=tt,
            trainings_limit=8,
            scope=Tariff.Scope.LOCATION,
            location=location_a,
        )
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club, training_type=tt, location=location_b)
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=tt,
            location=location_b,
            subscription=None,
            is_debt=True,
        )
        debt = DebtFactory(club=club, student=student, checkin=checkin)

        sub = create_subscription(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            recorded_by_id=owner_user.id,
            payment_method=Payment.Method.CASH,
        )

        sub.refresh_from_db()
        checkin.refresh_from_db()
        debt.refresh_from_db()
        assert sub.trainings_left == 8
        assert sub.trainings_used == 0
        assert checkin.subscription_id is None
        assert checkin.is_debt is True
        assert debt.resolved_at is None
        mock_async.assert_not_called()

    def test_create_subscription_rejects_existing_current_subscription_same_policy(self, club):
        tt = TrainingTypeFactory(club=club)
        first_tariff = TariffFactory(club=club, training_type=tt, trainings_limit=8)
        second_tariff = TariffFactory(club=club, training_type=tt, trainings_limit=12)
        student = StudentFactory(club=club)
        create_subscription(
            club_id=club.id,
            student_id=student.id,
            tariff_id=first_tariff.id,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            create_subscription(
                club_id=club.id,
                student_id=student.id,
                tariff_id=second_tariff.id,
            )

        assert exc_info.value.code == "active_subscription_exists"

    def test_create_subscription_allows_same_policy_when_previous_active_is_depleted(self, club):
        tt = TrainingTypeFactory(club=club)
        first_tariff = TariffFactory(club=club, training_type=tt, trainings_limit=8)
        second_tariff = TariffFactory(club=club, training_type=tt, trainings_limit=12)
        student = StudentFactory(club=club)
        previous = create_subscription(
            club_id=club.id,
            student_id=student.id,
            tariff_id=first_tariff.id,
        )
        previous.trainings_left = 0
        previous.trainings_used = 8
        previous.save(update_fields=["trainings_left", "trainings_used"])

        sub = create_subscription(
            club_id=club.id,
            student_id=student.id,
            tariff_id=second_tariff.id,
        )

        assert sub.id != previous.id
        assert sub.tariff_id == second_tariff.id
        assert sub.trainings_left == 12

    def test_create_subscription_allows_different_training_type(self, club):
        first_tt = TrainingTypeFactory(club=club)
        second_tt = TrainingTypeFactory(club=club)
        first_tariff = TariffFactory(club=club, training_type=first_tt)
        second_tariff = TariffFactory(club=club, training_type=second_tt)
        student = StudentFactory(club=club)
        create_subscription(
            club_id=club.id,
            student_id=student.id,
            tariff_id=first_tariff.id,
        )

        sub = create_subscription(
            club_id=club.id,
            student_id=student.id,
            tariff_id=second_tariff.id,
        )

        assert sub.tariff_id == second_tariff.id


@pytest.mark.django_db
class TestApplyDiscounts:
    def test_apply_discounts_percent(self, club):
        discount = DiscountFactory(club=club, discount_type="percent", value=Decimal("15"))
        amount, discounts = _apply_discounts(base_price=Decimal("5000"), club_id=club.id, discount_ids=[discount.id])
        assert amount == Decimal("4250")
        assert len(discounts) == 1

    def test_apply_discounts_fractional_percent_rounds_final_amount_to_cents(self, club):
        discount = DiscountFactory(club=club, discount_type="percent", value=Decimal("12.34"))

        amount, discounts = _apply_discounts(
            base_price=Decimal("999.99"),
            club_id=club.id,
            discount_ids=[discount.id],
        )

        assert amount == Decimal("876.59")
        assert discounts == [discount]

    def test_apply_discounts_fixed(self, club):
        discount = DiscountFactory(club=club, discount_type="fixed", value=Decimal("500"))
        amount, discounts = _apply_discounts(base_price=Decimal("5000"), club_id=club.id, discount_ids=[discount.id])
        assert amount == Decimal("4500")
        assert len(discounts) == 1

    def test_apply_discounts_combined(self, club):
        d_percent = DiscountFactory(club=club, discount_type="percent", value=Decimal("15"))
        d_fixed = DiscountFactory(club=club, discount_type="fixed", value=Decimal("500"))
        amount, discounts = _apply_discounts(
            base_price=Decimal("5000"),
            club_id=club.id,
            discount_ids=[d_percent.id, d_fixed.id],
        )
        # 5000 - 750 (15%) - 500 (fixed) = 3750
        assert amount == Decimal("3750")
        assert len(discounts) == 2

    def test_apply_discounts_floor_zero(self, club):
        d_huge = DiscountFactory(club=club, discount_type="fixed", value=Decimal("99999"))
        amount, _ = _apply_discounts(base_price=Decimal("5000"), club_id=club.id, discount_ids=[d_huge.id])
        assert amount == Decimal("0")

    def test_apply_discounts_empty_list(self, club):
        amount, discounts = _apply_discounts(base_price=Decimal("5000"), club_id=club.id, discount_ids=[])
        assert amount == Decimal("5000")
        assert discounts == []


@pytest.mark.django_db
class TestPendingManualAdmissionReconciliation:
    def _create_admission(
        self,
        *,
        club,
        owner_user,
        target_start_date,
        duration_days=3,
        canonical_group=False,
        student_status=Student.Status.ACTIVE,
    ):
        from apps.attendance.models import TrainingGroup, TrainingGroupRolloutState
        from apps.attendance.tests.factories import ScheduleFactory
        from apps.trainers.tests.factories import TrainerFactory

        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP, drop_in_price=None)
        schedule_kwargs = {}
        target_training_group_id = None
        if canonical_group:
            location = LocationFactory(club=club)
            trainer = TrainerFactory(club=club)
            group = TrainingGroup.objects.create(
                club=club,
                name="Audit reconciliation group",
                training_type=training_type,
                location=location,
                responsible_trainer=trainer,
                status=TrainingGroup.Status.ACTIVE,
            )
            rollout, _created = TrainingGroupRolloutState.objects.for_club(club).get_or_create(
                club=club,
                defaults={"mode": TrainingGroupRolloutState.Mode.OFF},
            )
            update_training_group_rollout_state_for_test(
                TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
                mode=TrainingGroupRolloutState.Mode.ACTIVE,
            )
            schedule_kwargs = {
                "training_group": group,
                "location": location,
                "trainer": trainer,
            }
            target_training_group_id = group.id
        schedule = ScheduleFactory(
            club=club,
            training_type=training_type,
            day_of_week=target_start_date.weekday(),
            one_time_date=None,
            **schedule_kwargs,
        )
        tariff = TariffFactory(
            club=club,
            training_type=training_type,
            price=Decimal("4000.00"),
            trainings_limit=3,
            duration_days=duration_days,
        )
        student = StudentFactory(
            club=club,
            status=student_status,
            **(
                {"lead_status": Student.LeadStatus.NEW}
                if student_status == Student.Status.LEAD
                else {}
            ),
        )
        ClubSettings.objects.update_or_create(
            club=club,
            defaults={
                "unified_client_journey_enabled": True,
                "commercial_journey_protocol_version": "v2",
            },
        )
        with override_settings(
            UNIFIED_CLIENT_JOURNEY_ENABLED=True,
            MANUAL_OPERATIONAL_ADMISSION_ENABLED=True,
            TRAINING_GROUP_NEW_WRITES_ENABLED=True,
        ), patch("django_q.tasks.async_task"):
            payment = create_payment(
                club_id=club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                payment_method=Payment.Method.CASH,
                recorded_by_id=owner_user.id,
                target_training_group_id=target_training_group_id,
                target_schedule_id=schedule.id,
                target_start_date=target_start_date,
                create_manual_operational_admission=True,
            )
        return student, schedule, tariff, payment

    def _create_component_admission(
        self,
        *,
        club,
        owner_user,
        component_specs,
        selected_date_offset=-7,
        duration_days=30,
    ):
        from apps.attendance.tests.factories import ScheduleFactory

        target_start_date = timezone.localdate() + timedelta(days=7)
        target_start_date += timedelta(days=(-target_start_date.weekday()) % 7)
        location = LocationFactory(club=club)
        training_type = TrainingTypeFactory(
            club=club,
            kind=TrainingType.Kind.GROUP,
            drop_in_price=None,
        )
        schedule = ScheduleFactory(
            club=club,
            location=location,
            training_type=training_type,
            day_of_week=target_start_date.weekday(),
            one_time_date=None,
        )
        components = []
        for name, entitlement_kind, limit, scope, paid_basis in component_specs:
            components.append(
                {
                    "name": name,
                    "training_type_id": training_type.id,
                    "entitlement_kind": entitlement_kind,
                    "credits_total": limit if entitlement_kind == "finite_credits" else None,
                    "weekly_limit": limit if entitlement_kind == "weekly_limit" else None,
                    "scope": scope,
                    "location_id": location.id if scope == Tariff.Scope.LOCATION else None,
                    "trainer_payout_policy": Tariff.PayoutPolicy.NONE,
                    "paid_amount_basis": paid_basis,
                }
            )
        tariff = create_tariff(
            club_id=club.id,
            name="Pending component admission",
            training_type_id=training_type.id,
            price=sum((item[4] for item in component_specs), Decimal("0.00")),
            trainings_limit=sum(
                item[2] for item in component_specs if item[1] == "finite_credits"
            )
            or None,
            duration_days=duration_days,
            components=components,
        )
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        selected_checkin = Checkin.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            trainer=schedule.trainer,
            location=location,
            date=target_start_date + timedelta(days=selected_date_offset),
            source=Checkin.Source.MANUAL,
            is_debt=True,
        )
        selected_debt = Debt.objects.create(
            club=club,
            student=student,
            checkin=selected_checkin,
            tariff_price=Decimal("1000.00"),
            reason="no_subscription",
        )
        ClubSettings.objects.update_or_create(
            club=club,
            defaults={
                "unified_client_journey_enabled": True,
                "commercial_journey_protocol_version": "v2",
            },
        )
        with override_settings(
            UNIFIED_CLIENT_JOURNEY_ENABLED=True,
            MANUAL_OPERATIONAL_ADMISSION_ENABLED=True,
        ), patch("django_q.tasks.async_task"):
            payment = create_payment(
                club_id=club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                payment_method=Payment.Method.CASH,
                recorded_by_id=owner_user.id,
                debt_ids=[selected_debt.id],
                target_schedule_id=schedule.id,
                target_start_date=target_start_date,
                create_manual_operational_admission=True,
            )
        return student, schedule, tariff, payment, selected_checkin, target_start_date

    @patch("apps.attendance.services.async_task")
    @patch("django_q.tasks.async_task")
    def test_confirmation_reuses_stable_location_first_component_allocation_for_selected_and_later_visits(
        self,
        _mock_payment_async,
        _mock_checkin_async,
        club,
        owner_user,
    ):
        student, schedule, tariff, payment, selected_checkin, target_start_date = self._create_component_admission(
            club=club,
            owner_user=owner_user,
            component_specs=[
                ("location-first", "finite_credits", 1, Tariff.Scope.LOCATION, Decimal("1000.00")),
                ("club-first", "finite_credits", 2, Tariff.Scope.CLUB, Decimal("2000.00")),
                ("club-second", "finite_credits", 1, Tariff.Scope.CLUB, Decimal("1000.00")),
            ],
        )
        later_checkin_ids = []
        for visit_date in (
            target_start_date,
            target_start_date + timedelta(days=7),
            target_start_date + timedelta(days=14),
        ):
            result = create_checkin(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                training_type_id=tariff.training_type_id,
                source=Checkin.Source.KIOSK,
                checkin_date=visit_date,
            )
            later_checkin_ids.append(result["checkin_id"])

        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )

        component_ids = list(
            payment.subscription.components.order_by("id").values_list("id", flat=True)
        )
        all_checkins = [
            Checkin.objects.for_club(club).get(id=selected_checkin.id),
            *[Checkin.objects.for_club(club).get(id=checkin_id) for checkin_id in later_checkin_ids],
        ]
        assert [checkin.subscription_component_id for checkin in all_checkins] == [
            component_ids[0],
            component_ids[1],
            component_ids[1],
            component_ids[2],
        ]
        assert all(checkin.subscription_id == payment.subscription_id for checkin in all_checkins)

    @patch("apps.attendance.services.async_task")
    def test_finite_capacity_overflow_rejects_pending_visit_without_partial_reservation(
        self,
        _mock_checkin_async,
        club,
        owner_user,
    ):
        student, schedule, tariff, payment, _selected_checkin, target_start_date = self._create_component_admission(
            club=club,
            owner_user=owner_user,
            component_specs=[
                ("one-credit", "finite_credits", 1, Tariff.Scope.CLUB, Decimal("4000.00")),
            ],
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            create_checkin(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                training_type_id=tariff.training_type_id,
                source=Checkin.Source.KIOSK,
                checkin_date=target_start_date,
            )

        assert exc_info.value.code == "subscription_component_limit_exceeded"
        assert not Debt.objects.for_club(club).filter(
            settlement_payment=payment,
            checkin__date=target_start_date,
        ).exists()

    @patch("apps.attendance.services.async_task")
    def test_weekly_capacity_overflow_rejects_pending_visit_without_partial_reservation(
        self,
        _mock_checkin_async,
        club,
        owner_user,
    ):
        student, schedule, tariff, payment, _selected_checkin, target_start_date = self._create_component_admission(
            club=club,
            owner_user=owner_user,
            component_specs=[
                ("one-per-week", "weekly_limit", 1, Tariff.Scope.CLUB, Decimal("4000.00")),
            ],
            selected_date_offset=1,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            create_checkin(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                training_type_id=tariff.training_type_id,
                source=Checkin.Source.KIOSK,
                checkin_date=target_start_date,
            )

        assert exc_info.value.code == "subscription_component_limit_exceeded"
        assert not Debt.objects.for_club(club).filter(
            settlement_payment=payment,
            checkin__date=target_start_date,
        ).exists()

    @patch("django_q.tasks.async_task")
    def test_confirmation_revalidates_exact_reserved_debts_before_any_payment_mutation(
        self,
        _mock_payment_async,
        club,
        owner_user,
    ):
        student, schedule, tariff, payment, _selected_checkin, target_start_date = self._create_component_admission(
            club=club,
            owner_user=owner_user,
            component_specs=[
                ("one-credit", "finite_credits", 1, Tariff.Scope.CLUB, Decimal("4000.00")),
            ],
        )
        overflow_checkin = Checkin.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            training_type_id=tariff.training_type_id,
            trainer=schedule.trainer,
            location=schedule.location,
            date=target_start_date,
            source=Checkin.Source.KIOSK,
            is_debt=True,
        )
        Debt.objects.create(
            club=club,
            student=student,
            checkin=overflow_checkin,
            tariff_price=None,
            required_tariff=tariff,
            settlement_payment=payment,
            reason="pending_manual_admission",
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            verify_payment(
                payment_id=payment.id,
                club_id=club.id,
                verified_by_id=owner_user.id,
                action="confirm",
            )

        payment.refresh_from_db()
        assert exc_info.value.code == "subscription_component_limit_exceeded"
        assert payment.status == Payment.Status.PENDING

    @patch("django_q.tasks.async_task")
    def test_dst_changing_club_anchors_expiry_and_delayed_confirmation_to_local_midnight(
        self,
        _mock_payment_async,
        club,
        owner_user,
    ):
        from apps.attendance.selectors import get_locked_pending_manual_admission_payment
        from apps.clubs.timezones import club_local_day_start

        club.timezone = "America/New_York"
        club.save(update_fields=["timezone"])
        target_start_date = date(2027, 3, 14)
        student, schedule, tariff, payment = self._create_admission(
            club=club,
            owner_user=owner_user,
            target_start_date=target_start_date,
            duration_days=2,
        )
        with transaction.atomic():
            assert get_locked_pending_manual_admission_payment(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                target_date=target_start_date,
            ).id == payment.id
            assert get_locked_pending_manual_admission_payment(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                target_date=target_start_date + timedelta(days=1),
            ).id == payment.id
            assert get_locked_pending_manual_admission_payment(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                target_date=target_start_date + timedelta(days=2),
            ) is None

        expected_expiry = club_local_day_start(club, target_start_date + timedelta(days=2))
        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
            verified_at=expected_expiry,
        )

        payment.subscription.refresh_from_db()
        assert payment.subscription.expires_at == expected_expiry
        assert payment.subscription.status == Subscription.Status.EXPIRED

    def test_sold_duration_days_cannot_change_before_or_after_operational_confirmation(
        self,
        club,
        owner_user,
    ):
        from apps.attendance.selectors import get_locked_pending_manual_admission_payment

        target_start_date = timezone.localdate() + timedelta(days=7)
        _student, schedule, tariff, payment = self._create_admission(
            club=club,
            owner_user=owner_user,
            target_start_date=target_start_date,
            duration_days=2,
        )
        expected_expiry = timezone.make_aware(
            datetime.combine(target_start_date + timedelta(days=2), datetime.min.time()),
            ZoneInfo(club.timezone),
        )

        with pytest.raises(BusinessLogicError) as pending_exc:
            update_tariff(
                tariff_id=tariff.id,
                club_id=club.id,
                duration_days=30,
            )
        with transaction.atomic():
            assert get_locked_pending_manual_admission_payment(
                club_id=club.id,
                student_id=payment.student_id,
                schedule_id=schedule.id,
                target_date=target_start_date + timedelta(days=1),
            ).id == payment.id
            assert get_locked_pending_manual_admission_payment(
                club_id=club.id,
                student_id=payment.student_id,
                schedule_id=schedule.id,
                target_date=target_start_date + timedelta(days=2),
            ) is None

        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
            verified_at=expected_expiry - timedelta(seconds=1),
        )

        with pytest.raises(BusinessLogicError) as active_exc:
            update_tariff(
                tariff_id=tariff.id,
                club_id=club.id,
                duration_days=30,
            )
        tariff.refresh_from_db()
        payment.subscription.refresh_from_db()
        assert pending_exc.value.code == "tariff_contract_change_blocked"
        assert active_exc.value.code == "tariff_contract_change_blocked"
        assert tariff.duration_days == 2
        assert payment.subscription.expires_at == expected_expiry

    @pytest.mark.parametrize("drift", ["terminal", "schedule"])
    def test_confirmation_rejects_drifted_or_terminal_payment_owned_enrollment(
        self,
        club,
        owner_user,
        drift,
    ):
        from apps.attendance.tests.factories import ScheduleFactory

        target_start_date = timezone.localdate() + timedelta(days=7)
        _student, schedule, tariff, payment = self._create_admission(
            club=club,
            owner_user=owner_user,
            target_start_date=target_start_date,
        )
        if drift == "terminal":
            ScheduleEnrollment.objects.for_club(club).filter(
                id=payment.conversion_enrollment_id
            ).update(
                status=ScheduleEnrollment.Status.CANCELLED,
                ends_on=target_start_date,
            )
        else:
            replacement_schedule = ScheduleFactory(
                club=club,
                training_type=tariff.training_type,
                day_of_week=target_start_date.weekday(),
                one_time_date=None,
            )
            ScheduleEnrollment.objects.for_club(club).filter(
                id=payment.conversion_enrollment_id
            ).update(schedule=replacement_schedule)

        with pytest.raises(BusinessLogicError) as exc_info:
            verify_payment(
                payment_id=payment.id,
                club_id=club.id,
                verified_by_id=owner_user.id,
                action="confirm",
            )

        payment.refresh_from_db()
        payment.subscription.refresh_from_db()
        assert exc_info.value.code == "payment_conversion_enrollment_invalid"
        assert payment.status == Payment.Status.PENDING
        assert payment.subscription.status == Subscription.Status.PENDING

    @patch("apps.attendance.services.async_task")
    @patch("django_q.tasks.async_task")
    def test_confirm_reconciles_exact_pending_visit_and_anchors_exclusive_expiry(
        self,
        _mock_payment_async,
        _mock_checkin_async,
        club,
        owner_user,
    ):
        target_start_date = timezone.localdate() + timedelta(days=7)
        student, schedule, tariff, payment = self._create_admission(
            club=club,
            owner_user=owner_user,
            target_start_date=target_start_date,
        )
        checkin_result = create_checkin(
            club_id=club.id,
            student_id=student.id,
            schedule_id=schedule.id,
            training_type_id=tariff.training_type_id,
            source="manual",
            checkin_date=target_start_date,
        )
        debt = Debt.objects.for_club(club).get(checkin_id=checkin_result["checkin_id"])
        expected_expiry = timezone.make_aware(
            datetime.combine(target_start_date + timedelta(days=tariff.duration_days), datetime.min.time()),
            ZoneInfo(club.timezone),
        )

        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
            verified_at=expected_expiry - timedelta(seconds=1),
        )

        payment.refresh_from_db()
        payment.subscription.refresh_from_db()
        debt.refresh_from_db()
        checkin = debt.checkin
        assert payment.status == Payment.Status.CONFIRMED
        assert payment.subscription.expires_at == expected_expiry
        assert payment.subscription.status == Subscription.Status.ACTIVE
        assert debt.resolved_at is not None
        assert debt.settlement_payment_id == payment.id
        assert checkin.subscription_id == payment.subscription_id
        assert list(
            debt.settlement_events.filter(payment=payment).values_list("event_type", flat=True)
        ) == [DebtSettlementEvent.EventType.RESERVED, DebtSettlementEvent.EventType.CONFIRMED]

    @patch("apps.attendance.services.async_task")
    @patch("django_q.tasks.async_task")
    def test_delayed_confirm_marks_operational_subscription_expired_and_reject_closes_only_owned_enrollment(
        self,
        _mock_payment_async,
        _mock_checkin_async,
        club,
        owner_user,
    ):
        target_start_date = timezone.localdate() + timedelta(days=7)
        student, schedule, tariff, payment = self._create_admission(
            club=club,
            owner_user=owner_user,
            target_start_date=target_start_date,
            duration_days=1,
        )
        expiry = timezone.make_aware(
            datetime.combine(target_start_date + timedelta(days=1), datetime.min.time()),
            ZoneInfo(club.timezone),
        )
        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
            verified_at=expiry,
        )
        payment.subscription.refresh_from_db()
        assert payment.subscription.status == Subscription.Status.EXPIRED

        student, schedule, tariff, rejected_payment = self._create_admission(
            club=club,
            owner_user=owner_user,
            target_start_date=target_start_date,
        )
        checkin_result = create_checkin(
            club_id=club.id,
            student_id=student.id,
            schedule_id=schedule.id,
            training_type_id=tariff.training_type_id,
            source="manual",
            checkin_date=target_start_date,
        )
        debt = Debt.objects.for_club(club).get(checkin_id=checkin_result["checkin_id"])
        verify_payment(
            payment_id=rejected_payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="reject",
            rejection_reason="operator rejected",
        )

        rejected_payment.refresh_from_db()
        rejected_payment.conversion_enrollment.refresh_from_db()
        debt.refresh_from_db()
        student.refresh_from_db()
        assert rejected_payment.conversion_enrollment.status == "cancelled"
        assert rejected_payment.conversion_enrollment.ends_on >= rejected_payment.conversion_enrollment.starts_on
        assert debt.settlement_payment_id is None
        assert debt.resolved_at is None
        assert student.status == "active"

    def test_operational_admission_audit_is_aggregate_only(self, club, owner_user):
        target_start_date = timezone.localdate() + timedelta(days=7)
        student, schedule, tariff, payment = self._create_admission(
            club=club,
            owner_user=owner_user,
            target_start_date=target_start_date,
        )
        payment.conversion_enrollment.starts_on = target_start_date + timedelta(days=1)
        payment.conversion_enrollment.save(update_fields=["starts_on", "updated_at"])
        duplicate_subscription = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.PENDING,
            expires_at=None,
        )
        PaymentFactory(
            club=club,
            student=student,
            tariff=tariff,
            subscription=duplicate_subscription,
            payment_method=Payment.Method.TRANSFER,
            status=Payment.Status.PENDING,
            recorded_by=owner_user,
            target_schedule=schedule,
            target_start_date=target_start_date,
            conversion_enrollment=payment.conversion_enrollment,
            target_training_type_id_snapshot=schedule.training_type_id,
            target_location_id_snapshot=schedule.location_id,
        )
        stdout = io.StringIO()
        call_command("audit_operational_admissions", club_id=club.id, stdout=stdout)
        report = json.loads(stdout.getvalue())

        assert report["club_id"] == club.id
        assert report["operational_admission_payment_count"] == 2
        assert report["payment_status_counts"] == {"pending": 2}
        assert report["invalid_state_counts"]["target_start_mismatch"] == 2
        assert report["invalid_state_counts"]["ambiguous_pending_admission"] == 2
        assert "student" not in report

    def test_operational_admission_audit_reports_terminal_and_incompatible_reservations(
        self,
        club,
        other_club,
        owner_user,
    ):
        from apps.attendance.tests.factories import CheckinFactory, ScheduleFactory
        from apps.clubs.tests.factories import LocationFactory

        target_start_date = timezone.localdate() + timedelta(days=7)
        student, schedule, tariff, payment = self._create_admission(
            club=club,
            owner_user=owner_user,
            target_start_date=target_start_date,
        )
        ScheduleEnrollment.objects.for_club(club).filter(
            id=payment.conversion_enrollment_id
        ).update(
            status=ScheduleEnrollment.Status.CANCELLED,
            ends_on=target_start_date,
        )
        incompatible_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        incompatible_tariff = TariffFactory(club=club, training_type=incompatible_type)
        incompatible_schedule = ScheduleFactory(
            club=club,
            training_type=incompatible_type,
            location=LocationFactory(club=club),
            day_of_week=target_start_date.weekday(),
        )
        incompatible_student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        incompatible_checkin = CheckinFactory(
            club=club,
            student=incompatible_student,
            schedule=incompatible_schedule,
            trainer=incompatible_schedule.trainer,
            location=incompatible_schedule.location,
            training_type=incompatible_type,
            date=target_start_date,
        )
        incompatible_debt = DebtFactory(
            club=club,
            student=incompatible_student,
            checkin=incompatible_checkin,
            required_tariff=incompatible_tariff,
            settlement_payment=payment,
        )
        mismatched_event_payment = PaymentFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Payment.Status.PENDING,
        )
        DebtSettlementEvent.objects.create(
            club=club,
            debt=incompatible_debt,
            payment=mismatched_event_payment,
            event_type=DebtSettlementEvent.EventType.RESERVED,
        )

        foreign_student = StudentFactory(club=other_club, status=Student.Status.ACTIVE)
        foreign_type = TrainingTypeFactory(club=other_club, kind=TrainingType.Kind.GROUP)
        foreign_schedule = ScheduleFactory(
            club=other_club,
            training_type=foreign_type,
            day_of_week=target_start_date.weekday(),
        )
        foreign_checkin = CheckinFactory(
            club=other_club,
            student=foreign_student,
            schedule=foreign_schedule,
            trainer=foreign_schedule.trainer,
            location=foreign_schedule.location,
            training_type=foreign_type,
            date=target_start_date,
        )
        DebtFactory(
            club=other_club,
            student=foreign_student,
            checkin=foreign_checkin,
            required_tariff=TariffFactory(club=other_club, training_type=foreign_type),
            settlement_payment=payment,
        )

        stdout = io.StringIO()
        call_command("audit_operational_admissions", club_id=club.id, stdout=stdout)
        report = json.loads(stdout.getvalue())

        invalid = report["invalid_state_counts"]
        assert report["operational_admission_payment_count"] == 1
        assert invalid["pending_terminal_enrollment"] == 1
        assert invalid["debt_owner_mismatch"] == 1
        assert invalid["debt_tariff_mismatch"] == 1
        assert invalid["debt_schedule_training_type_mismatch"] == 1
        assert invalid["debt_schedule_location_mismatch"] == 1
        assert invalid["debt_reservation_payment_mismatch"] == 1
        assert invalid["foreign_debt_reservation"] == 1
        assert "student" not in stdout.getvalue()

    def test_operational_admission_audit_accepts_released_then_rereserved_history(
        self,
        club,
        owner_user,
    ):
        from apps.attendance.tests.factories import CheckinFactory

        target_start_date = timezone.localdate() + timedelta(days=7)
        student, schedule, tariff, current_payment = self._create_admission(
            club=club,
            owner_user=owner_user,
            target_start_date=target_start_date,
        )
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            trainer=schedule.trainer,
            location=schedule.location,
            training_type=schedule.training_type,
            date=target_start_date,
        )
        debt = DebtFactory(
            club=club,
            student=student,
            checkin=checkin,
            required_tariff=tariff,
            settlement_payment=current_payment,
            resolved_at=None,
        )
        previous_payment = PaymentFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Payment.Status.REJECTED,
        )
        DebtSettlementEvent.objects.create(
            club=club,
            debt=debt,
            payment=previous_payment,
            event_type=DebtSettlementEvent.EventType.RESERVED,
        )
        DebtSettlementEvent.objects.create(
            club=club,
            debt=debt,
            payment=previous_payment,
            event_type=DebtSettlementEvent.EventType.REJECTED,
        )
        DebtSettlementEvent.objects.create(
            club=club,
            debt=debt,
            payment=current_payment,
            event_type=DebtSettlementEvent.EventType.RESERVED,
        )

        stdout = io.StringIO()
        call_command("audit_operational_admissions", club_id=club.id, stdout=stdout)
        invalid = json.loads(stdout.getvalue())["invalid_state_counts"]

        assert "debt_reservation_payment_mismatch" not in invalid
        assert "debt_reservation_lifecycle_missing" not in invalid

    def test_operational_admission_audit_all_clubs_cleanly_covers_every_club(self, club, owner_user):
        target_start_date = timezone.localdate() + timedelta(days=7)
        self._create_admission(
            club=club,
            owner_user=owner_user,
            target_start_date=target_start_date,
            canonical_group=True,
            student_status=Student.Status.LEAD,
        )

        stdout = io.StringIO()
        call_command("audit_operational_admissions", "--all-clubs", "--fail-on-invalid", stdout=stdout)
        report = json.loads(stdout.getvalue())

        assert report["scope"] == "all_clubs"
        assert report["total_club_count"] == Club.objects.count()
        assert report["audited_club_count"] == report["total_club_count"]
        assert report["aggregate_invalid_state_counts"] == {}
        assert report["legacy_unlinked_pending_manual_count"] == 0
        assert report["reconciliation_required_count"] == 0
        assert report["clean"] is True
        allowed_club_report_keys = {
            "club_id",
            "operational_admission_payment_count",
            "payment_status_counts",
            "subscription_status_counts",
            "enrollment_status_counts",
            "canonical_group_payment_count",
            "canonical_group_action_counts",
            "payment_owned_group_membership_count",
            "payment_owned_group_projection_count",
            "deferred_provider_event_count",
            "invalid_state_counts",
            "legacy_unlinked_pending_manual_count",
            "reconciliation_required_count",
            "manual_admission_origin_counts",
            "pending_row_classification_counts",
            "audit_error",
            "clean",
        }
        assert all(
            {"club_id", "clean"} <= set(club_report) <= allowed_club_report_keys
            for club_report in report["club_reports"]
        )
        assert "student" not in stdout.getvalue()

    def test_operational_admission_audit_blocks_activation_until_person_reconciliation(
        self,
        club,
        owner_user,
    ):
        student, _schedule, _tariff, _payment = self._create_admission(
            club=club,
            owner_user=owner_user,
            target_start_date=timezone.localdate() + timedelta(days=7),
            canonical_group=True,
            student_status=Student.Status.LEAD,
        )
        Student.objects.for_club(club).filter(id=student.id).update(
            status=Student.Status.ACTIVE,
            lead_status=Student.LeadStatus.NEW,
            became_student_at=None,
        )
        stdout = io.StringIO()

        with pytest.raises(CommandError):
            call_command(
                "audit_operational_admissions",
                "--fail-on-invalid",
                club_id=club.id,
                stdout=stdout,
            )

        report = json.loads(stdout.getvalue())
        assert report["pending_row_classification_counts"]["reconcile"] == 1
        assert report["reconciliation_required_count"] == 1
        assert report["clean"] is False

    def test_operational_admission_audit_all_clubs_emits_dirty_report_before_fail_closed_exit(
        self,
        club,
        owner_user,
    ):
        target_start_date = timezone.localdate() + timedelta(days=7)
        _student, _schedule, _tariff, payment = self._create_admission(
            club=club,
            owner_user=owner_user,
            target_start_date=target_start_date,
        )
        ScheduleEnrollment.objects.for_club(club).filter(
            id=payment.conversion_enrollment_id
        ).update(starts_on=target_start_date + timedelta(days=1))

        stdout = io.StringIO()
        with pytest.raises(CommandError):
            call_command(
                "audit_operational_admissions",
                "--all-clubs",
                "--fail-on-invalid",
                stdout=stdout,
            )
        report = json.loads(stdout.getvalue())

        assert report["audited_club_count"] == report["total_club_count"]
        assert report["aggregate_invalid_state_counts"]["target_start_mismatch"] == 1
        assert report["clean"] is False

    def test_operational_admission_audit_all_clubs_fails_closed_on_audit_error(self, club):
        stdout = io.StringIO()
        with patch(
            "apps.billing.management.commands.audit_operational_admissions._audit_club",
            side_effect=RuntimeError("audit dependency unavailable"),
        ), pytest.raises(CommandError):
            call_command(
                "audit_operational_admissions",
                "--all-clubs",
                "--fail-on-invalid",
                stdout=stdout,
            )
        report = json.loads(stdout.getvalue())

        assert report["total_club_count"] == Club.objects.count()
        assert report["audited_club_count"] == 0
        assert report["aggregate_invalid_state_counts"]["audit_failure"] == report["total_club_count"]
        assert report["clean"] is False
        assert all(set(club_report) == {"club_id", "audit_error", "clean"} for club_report in report["club_reports"])

    def test_operational_admission_audit_accepts_rejected_unlinked_legacy_payment(
        self,
        club,
        owner_user,
    ):
        target_start_date = timezone.localdate() + timedelta(days=7)
        student, schedule, tariff, _payment = self._create_admission(
            club=club,
            owner_user=owner_user,
            target_start_date=target_start_date,
            canonical_group=True,
            student_status=Student.Status.LEAD,
        )
        legacy_subscription = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.PENDING,
            expires_at=None,
        )
        PaymentFactory(
            club=club,
            student=student,
            tariff=tariff,
            subscription=legacy_subscription,
            payment_method=Payment.Method.CASH,
            status=Payment.Status.REJECTED,
            recorded_by=owner_user,
            target_schedule=schedule,
            target_start_date=target_start_date,
        )

        stdout = io.StringIO()
        call_command("audit_operational_admissions", "--fail-on-invalid", club_id=club.id, stdout=stdout)
        report = json.loads(stdout.getvalue())

        assert "missing_enrollment" not in report["invalid_state_counts"]
        assert report["legacy_unlinked_pending_manual_count"] == 0
        assert report["clean"] is True

    def test_operational_admission_audit_blocks_pending_unlinked_legacy_payment(
        self,
        club,
        owner_user,
    ):
        target_start_date = timezone.localdate() + timedelta(days=7)
        student, schedule, tariff, _payment = self._create_admission(
            club=club,
            owner_user=owner_user,
            target_start_date=target_start_date,
            canonical_group=True,
            student_status=Student.Status.LEAD,
        )
        legacy_subscription = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.PENDING,
            expires_at=None,
        )
        PaymentFactory(
            club=club,
            student=student,
            tariff=tariff,
            subscription=legacy_subscription,
            payment_method=Payment.Method.TRANSFER,
            status=Payment.Status.PENDING,
            recorded_by=owner_user,
            target_schedule=schedule,
            target_start_date=target_start_date,
        )

        stdout = io.StringIO()
        with pytest.raises(CommandError):
            call_command(
                "audit_operational_admissions",
                "--fail-on-invalid",
                club_id=club.id,
                stdout=stdout,
            )
        report = json.loads(stdout.getvalue())

        assert report["invalid_state_counts"]["missing_enrollment"] == 1
        assert report["legacy_unlinked_pending_manual_count"] == 1
        assert report["clean"] is False

    def test_operational_admission_audit_blocks_terminal_unresolved_reservation(
        self,
        club,
        owner_user,
    ):
        from apps.attendance.tests.factories import CheckinFactory

        target_start_date = timezone.localdate() + timedelta(days=7)
        student, schedule, tariff, payment = self._create_admission(
            club=club,
            owner_user=owner_user,
            target_start_date=target_start_date,
            canonical_group=True,
            student_status=Student.Status.LEAD,
        )
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            trainer=schedule.trainer,
            location=schedule.location,
            training_type=schedule.training_type,
            date=target_start_date,
        )
        debt = DebtFactory(
            club=club,
            student=student,
            checkin=checkin,
            required_tariff=tariff,
            settlement_payment=payment,
            resolved_at=None,
        )
        DebtSettlementEvent.objects.create(
            club=club,
            debt=debt,
            payment=payment,
            event_type=DebtSettlementEvent.EventType.RESERVED,
        )
        Payment.objects.for_club(club).filter(id=payment.id).update(status=Payment.Status.REJECTED)

        stdout = io.StringIO()
        with pytest.raises(CommandError):
            call_command(
                "audit_operational_admissions",
                "--fail-on-invalid",
                club_id=club.id,
                stdout=stdout,
            )
        report = json.loads(stdout.getvalue())

        assert report["invalid_state_counts"]["terminal_unresolved_debt_reservation"] == 1

    def test_operational_admission_audit_reports_schedule_and_settlement_event_tenancy(
        self,
        club,
        other_club,
        owner_user,
    ):
        from apps.attendance.tests.factories import CheckinFactory, ScheduleFactory

        target_start_date = timezone.localdate() + timedelta(days=7)
        student, schedule, tariff, payment = self._create_admission(
            club=club,
            owner_user=owner_user,
            target_start_date=target_start_date,
        )
        foreign_schedule = ScheduleFactory(club=other_club)
        Payment.objects.for_club(club).filter(id=payment.id).update(target_schedule=foreign_schedule)
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            trainer=schedule.trainer,
            location=schedule.location,
            training_type=schedule.training_type,
            date=target_start_date,
        )
        debt = DebtFactory(
            club=club,
            student=student,
            checkin=checkin,
            required_tariff=tariff,
            settlement_payment=payment,
            resolved_at=None,
        )
        DebtSettlementEvent.objects.create(
            club=other_club,
            debt=debt,
            payment=payment,
            event_type=DebtSettlementEvent.EventType.RESERVED,
        )

        stdout = io.StringIO()
        call_command("audit_operational_admissions", club_id=club.id, stdout=stdout)
        invalid = json.loads(stdout.getvalue())["invalid_state_counts"]

        assert invalid["target_schedule_tenancy_mismatch"] == 1
        assert invalid["debt_settlement_event_debt_tenancy_mismatch"] == 1
        assert invalid["debt_settlement_event_payment_tenancy_mismatch"] == 1

    def test_operational_admission_audit_reports_orphan_and_reused_paid_conversion_enrollments(
        self,
        club,
        owner_user,
    ):
        target_start_date = timezone.localdate() + timedelta(days=7)
        student, schedule, tariff, payment = self._create_admission(
            club=club,
            owner_user=owner_user,
            target_start_date=target_start_date,
        )
        orphan_student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        ScheduleEnrollment.objects.create(
            club=club,
            student=orphan_student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=target_start_date,
            created_from=ScheduleEnrollment.CreatedFrom.PAID_CONVERSION,
        )
        terminal_subscription = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.PENDING,
            expires_at=None,
        )
        PaymentFactory(
            club=club,
            student=student,
            tariff=tariff,
            subscription=terminal_subscription,
            payment_method=Payment.Method.CASH,
            status=Payment.Status.REJECTED,
            recorded_by=owner_user,
            target_schedule=schedule,
            target_start_date=target_start_date,
            conversion_enrollment=payment.conversion_enrollment,
        )

        stdout = io.StringIO()
        call_command("audit_operational_admissions", club_id=club.id, stdout=stdout)
        invalid = json.loads(stdout.getvalue())["invalid_state_counts"]

        assert invalid["orphan_paid_conversion_enrollment"] == 1
        assert invalid["reused_conversion_enrollment"] == 1

    def test_audit_allows_legacy_online_pending_order_without_enrollment(
        self,
        settings,
        club,
        owner_user,
    ):
        from apps.attendance.tests.factories import ScheduleFactory

        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(club=club, training_type=training_type)
        schedule = ScheduleFactory(
            club=club,
            training_type=training_type,
            day_of_week=timezone.localdate().weekday(),
        )
        student = StudentFactory(club=club)
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.OWNER,
            created_by_id=owner_user.id,
            target_schedule_id=schedule.id,
            target_start_date=timezone.localdate(),
        )

        assert order.payment.conversion_enrollment_id is None
        stdout = io.StringIO()
        call_command(
            "audit_operational_admissions",
            "--fail-on-invalid",
            club_id=club.id,
            stdout=stdout,
        )
        report = json.loads(stdout.getvalue())
        assert report["clean"] is True
        assert report["operational_admission_payment_count"] == 0

    @patch("apps.attendance.services.async_task")
    @patch("django_q.tasks.async_task")
    def test_switch_off_keeps_existing_owned_checkin_and_confirmation_lifecycle(
        self,
        _mock_payment_async,
        _mock_checkin_async,
        club,
        owner_user,
    ):
        target_start_date = timezone.localdate() + timedelta(days=7)
        student, schedule, tariff, payment = self._create_admission(
            club=club,
            owner_user=owner_user,
            target_start_date=target_start_date,
        )

        with override_settings(MANUAL_OPERATIONAL_ADMISSION_ENABLED=False):
            checkin_result = create_checkin(
                club_id=club.id,
                student_id=student.id,
                schedule_id=schedule.id,
                training_type_id=tariff.training_type_id,
                source=Checkin.Source.KIOSK,
                checkin_date=target_start_date,
            )
            verify_payment(
                payment_id=payment.id,
                club_id=club.id,
                verified_by_id=owner_user.id,
                action="confirm",
            )

        debt = Debt.objects.for_club(club).get(checkin_id=checkin_result["checkin_id"])
        payment.refresh_from_db()
        assert payment.status == Payment.Status.CONFIRMED
        assert debt.resolved_at is not None

    @patch("django_q.tasks.async_task")
    def test_switch_off_rejects_existing_owned_payment_before_any_attended_visit(
        self,
        _mock_payment_async,
        club,
        owner_user,
    ):
        target_start_date = timezone.localdate() + timedelta(days=7)
        _student, _schedule, _tariff, payment = self._create_admission(
            club=club,
            owner_user=owner_user,
            target_start_date=target_start_date,
        )

        with override_settings(MANUAL_OPERATIONAL_ADMISSION_ENABLED=False):
            verify_payment(
                payment_id=payment.id,
                club_id=club.id,
                verified_by_id=owner_user.id,
                action="reject",
                rejection_reason="rollback before visit",
            )

        payment.refresh_from_db()
        payment.conversion_enrollment.refresh_from_db()
        assert payment.status == Payment.Status.REJECTED
        assert payment.conversion_enrollment.status == ScheduleEnrollment.Status.CANCELLED
        assert payment.conversion_enrollment.ends_on == target_start_date

    @patch("django_q.tasks.async_task")
    def test_switch_off_legacy_unlinked_pending_manual_rows_use_confirmation_and_rejection_compatibility(
        self,
        _mock_payment_async,
        club,
        owner_user,
    ):
        from apps.attendance.tests.factories import ScheduleFactory

        target_start_date = timezone.localdate() + timedelta(days=7)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            training_type=training_type,
            day_of_week=target_start_date.weekday(),
            one_time_date=None,
        )
        tariff = TariffFactory(club=club, training_type=training_type)
        confirmed_student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        rejected_student = StudentFactory(club=club, status=Student.Status.ACTIVE)

        with override_settings(MANUAL_OPERATIONAL_ADMISSION_ENABLED=False):
            legacy_confirm = create_payment(
                club_id=club.id,
                student_id=confirmed_student.id,
                tariff_id=tariff.id,
                payment_method=Payment.Method.CASH,
                recorded_by_id=owner_user.id,
                target_schedule_id=schedule.id,
                target_start_date=target_start_date,
            )
            legacy_reject = create_payment(
                club_id=club.id,
                student_id=rejected_student.id,
                tariff_id=tariff.id,
                payment_method=Payment.Method.TRANSFER,
                recorded_by_id=owner_user.id,
                target_schedule_id=schedule.id,
                target_start_date=target_start_date,
            )
            verify_payment(
                payment_id=legacy_confirm.id,
                club_id=club.id,
                verified_by_id=owner_user.id,
                action="confirm",
            )
            verify_payment(
                payment_id=legacy_reject.id,
                club_id=club.id,
                verified_by_id=owner_user.id,
                action="reject",
                rejection_reason="legacy rejected",
            )

        legacy_confirm.refresh_from_db()
        legacy_reject.refresh_from_db()
        assert legacy_confirm.status == Payment.Status.CONFIRMED
        assert legacy_confirm.conversion_enrollment_id is not None
        assert legacy_reject.status == Payment.Status.REJECTED
        assert legacy_reject.conversion_enrollment_id is None


@pytest.mark.django_db
class TestCreatePayment:
    @patch("django_q.tasks.async_task")
    @patch("apps.billing.service_modules.payment_creation.logger.info")
    def test_payment_created_log_excludes_amount(self, mock_log, _mock_async, club, owner_user):
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(club=club, training_type=training_type, price=Decimal("5000.00"))
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)

        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
        )

        log_call = next(
            call for call in mock_log.call_args_list if call.args == ("payment_created",)
        )
        assert log_call.kwargs["extra"] == {
            "id": payment.id,
            "student_id": student.id,
            "club_id": club.id,
        }

    @patch("django_q.tasks.async_task")
    def test_create_payment(self, mock_async, club, owner_user):
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=tt, price=Decimal("5000"))
        student = StudentFactory(club=club)

        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method="cash",
            recorded_by_id=owner_user.id,
        )

        assert payment.status == Payment.Status.PENDING
        assert payment.amount == Decimal("5000")
        assert payment.original_amount == Decimal("5000")
        assert payment.payment_method == "cash"
        assert payment.recorded_by_id == owner_user.id
        assert payment.subscription is not None
        assert payment.subscription.status == Subscription.Status.PENDING
        mock_async.assert_called_once()

    @patch("django_q.tasks.async_task")
    def test_create_group_payment_with_target_snapshots_conversion_context(
        self,
        mock_async,
        club,
        owner_user,
        settings,
    ):
        from apps.attendance.models import ScheduleEnrollment
        from apps.attendance.tests.factories import ScheduleFactory
        from apps.leads.models import LeadLifecycleEvent
        from apps.trainers.tests.factories import TrainerFactory

        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        target_trainer = TrainerFactory(club=club)
        target_schedule = ScheduleFactory(
            club=club,
            trainer=target_trainer,
            training_type=training_type,
            day_of_week=0,
        )
        tariff = TariffFactory(club=club, training_type=training_type, price=Decimal("4000"))
        lead = LeadFactory(club=club, lead_status=Student.LeadStatus.TRIAL_DONE)
        target_start_date = date(2030, 1, 7)
        _enable_legacy_manual_group_admission(
            club=club,
            settings=settings,
            manual_admission_enabled=True,
        )

        payment = create_payment(
            club_id=club.id,
            student_id=lead.id,
            tariff_id=tariff.id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=target_schedule.id,
            target_start_date=target_start_date,
            create_manual_operational_admission=True,
        )

        assert payment.status == Payment.Status.PENDING
        assert payment.recorded_by_id == owner_user.id
        assert payment.seller_trainer_id == target_trainer.id
        assert payment.sale_trainer_id_snapshot == target_trainer.id
        assert payment.sale_earning_snapshot_recorded is False
        assert payment.target_schedule_id == target_schedule.id
        assert payment.target_start_date == target_start_date
        assert payment.target_group_name_snapshot == target_schedule.group_name
        assert payment.target_location_id_snapshot == target_schedule.location_id
        assert payment.target_trainer_id_snapshot == target_trainer.id
        assert payment.target_training_type_id_snapshot == training_type.id
        assert payment.sale_attribution_source == "target_group_regular_trainer"
        lead.refresh_from_db()
        assert lead.status == Student.Status.ACTIVE
        assert lead.lead_status is None
        enrollment = ScheduleEnrollment.objects.for_club(club).get(
            student=lead,
            schedule=target_schedule,
            ends_on__isnull=True,
        )
        assert enrollment.created_from == ScheduleEnrollment.CreatedFrom.PAID_CONVERSION
        assert enrollment.starts_on == target_start_date
        assert payment.conversion_enrollment_id == enrollment.id
        assert LeadLifecycleEvent.objects.for_club(club).filter(
            student=lead,
            event_type=LeadLifecycleEvent.EventType.LEAD_CONVERTED,
        ).count() == 1
        retried_payment = create_payment(
            club_id=club.id,
            student_id=lead.id,
            tariff_id=tariff.id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=target_schedule.id,
            target_start_date=target_start_date,
            create_manual_operational_admission=True,
        )
        assert retried_payment.id == payment.id
        assert ScheduleEnrollment.objects.for_club(club).filter(
            student=lead,
            schedule=target_schedule,
            ends_on__isnull=True,
        ).count() == 1
        assert LeadLifecycleEvent.objects.for_club(club).filter(
            student=lead,
            event_type=LeadLifecycleEvent.EventType.LEAD_CONVERTED,
        ).count() == 1
        mock_async.assert_called_once()

    @pytest.mark.django_db(transaction=True)
    @pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="requires PostgreSQL row-lock semantics; run in the PostgreSQL S5 suite",
    )
    @patch("django_q.tasks.async_task")
    def test_postgresql_group_payment_retry_keeps_one_owned_membership_family(
        self,
        _mock_async,
        club,
        owner_user,
        settings,
    ):
        from apps.attendance.models import TrainingGroupMembership, TrainingGroupRolloutState
        from apps.attendance.services.schedule import create_schedule
        from apps.clubs.tests.factories import LocationFactory
        from apps.trainers.tests.factories import TrainerFactory

        _enable_legacy_manual_group_admission(
            club=club,
            settings=settings,
            manual_admission_enabled=True,
        )
        settings.TRAINING_GROUP_NEW_WRITES_ENABLED = True
        state, _ = TrainingGroupRolloutState.objects.for_club(club).get_or_create(
            club=club,
            defaults={"mode": TrainingGroupRolloutState.Mode.OFF},
        )
        update_training_group_rollout_state_for_test(
            TrainingGroupRolloutState.objects.for_club(club).filter(id=state.id),
            mode=TrainingGroupRolloutState.Mode.ACTIVE,
        )
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        location = LocationFactory(club=club)
        trainer = TrainerFactory(club=club)
        schedule = create_schedule(
            club_id=club.id,
            day_of_week=0,
            start_time=time(18, 0),
            end_time=time(19, 0),
            group_name="S5 PostgreSQL group",
            trainer_id=trainer.id,
            location_id=location.id,
            training_type_id=training_type.id,
        )
        tariff = TariffFactory(club=club, training_type=training_type)
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        target_start_date = date(2030, 1, 7)

        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=schedule.id,
            target_training_group_id=schedule.training_group_id,
            target_start_date=target_start_date,
            create_manual_operational_admission=True,
        )
        retry = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=schedule.id,
            target_training_group_id=schedule.training_group_id,
            target_start_date=target_start_date,
            create_manual_operational_admission=True,
        )

        assert retry.id == payment.id
        assert payment.conversion_group_membership_id is not None
        assert retry.target_group_membership_id == payment.conversion_group_membership_id
        assert (
            retry.group_membership_action_snapshot
            == Payment.GroupMembershipActionSnapshot.NEW_ADMISSION
        )
        assert retry.conversion_group_membership_id == payment.conversion_group_membership_id
        assert retry.conversion_enrollment_id == payment.conversion_enrollment_id
        assert TrainingGroupMembership.objects.for_club(club).filter(
            student_id=student.id,
            training_group_id=schedule.training_group_id,
            authority=TrainingGroupMembership.Authority.PAYMENT_OWNED,
        ).count() == 1
        assert ScheduleEnrollment.objects.for_club(club).filter(
            training_group_membership_id=payment.conversion_group_membership_id
        ).count() == 1

    @patch("django_q.tasks.async_task")
    def test_manual_operational_admission_flag_rejects_before_financial_mutations(
        self,
        mock_async,
        club,
        owner_user,
        settings,
    ):
        from apps.attendance.tests.factories import ScheduleFactory
        from apps.trainers.tests.factories import TrainerFactory

        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        target_schedule = ScheduleFactory(
            club=club,
            trainer=TrainerFactory(club=club),
            training_type=training_type,
            day_of_week=0,
        )
        tariff = TariffFactory(club=club, training_type=training_type)
        lead = LeadFactory(club=club, lead_status=Student.LeadStatus.NEW)
        _enable_legacy_manual_group_admission(
            club=club,
            settings=settings,
            manual_admission_enabled=False,
        )
        component_count_before = TariffComponent.objects.for_club(club).filter(tariff=tariff).count()

        with pytest.raises(BusinessLogicError) as exc_info:
            create_payment(
                club_id=club.id,
                student_id=lead.id,
                tariff_id=tariff.id,
                payment_method=Payment.Method.TRANSFER,
                recorded_by_id=owner_user.id,
                target_schedule_id=target_schedule.id,
                target_start_date=date(2030, 1, 7),
                create_manual_operational_admission=True,
            )

        assert exc_info.value.code == "manual_operational_admission_disabled"
        assert TariffComponent.objects.for_club(club).filter(tariff=tariff).count() == component_count_before
        assert not Payment.objects.for_club(club).filter(student=lead).exists()
        assert not Subscription.objects.for_club(club).filter(student=lead).exists()
        assert not lead.schedule_enrollments.exists()
        lead.refresh_from_db()
        assert lead.status == Student.Status.LEAD
        assert lead.lead_status == Student.LeadStatus.NEW
        mock_async.assert_not_called()

    @patch("django_q.tasks.async_task")
    def test_manual_operational_admission_retry_is_idempotent_for_active_student(
        self,
        mock_async,
        club,
        owner_user,
        settings,
    ):
        from apps.attendance.models import ScheduleEnrollment
        from apps.attendance.tests.factories import ScheduleFactory
        from apps.leads.models import LeadLifecycleEvent
        from apps.trainers.tests.factories import TrainerFactory

        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        target_schedule = ScheduleFactory(
            club=club,
            trainer=TrainerFactory(club=club),
            training_type=training_type,
            day_of_week=0,
        )
        tariff = TariffFactory(club=club, training_type=training_type)
        student = StudentFactory(club=club, status=Student.Status.ACTIVE, lead_status=None)
        _enable_legacy_manual_group_admission(
            club=club,
            settings=settings,
            manual_admission_enabled=True,
        )
        payment_kwargs = {
            "club_id": club.id,
            "student_id": student.id,
            "tariff_id": tariff.id,
            "payment_method": Payment.Method.TRANSFER,
            "recorded_by_id": owner_user.id,
            "target_schedule_id": target_schedule.id,
            "target_start_date": date(2030, 1, 7),
            "create_manual_operational_admission": True,
        }

        payment = create_payment(**payment_kwargs)
        retried_payment = create_payment(**payment_kwargs)

        assert retried_payment.id == payment.id
        assert ScheduleEnrollment.objects.for_club(club).filter(
            student=student,
            schedule=target_schedule,
            ends_on__isnull=True,
        ).count() == 1
        assert not LeadLifecycleEvent.objects.for_club(club).filter(student=student).exists()
        mock_async.assert_called_once()

    @patch("django_q.tasks.async_task")
    def test_manual_operational_admission_rolls_back_payment_and_lead_transition(
        self,
        mock_async,
        club,
        owner_user,
        settings,
    ):
        from apps.attendance.models import ScheduleEnrollment
        from apps.attendance.tests.factories import ScheduleFactory
        from apps.leads.models import LeadLifecycleEvent
        from apps.trainers.tests.factories import TrainerFactory

        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        target_schedule = ScheduleFactory(
            club=club,
            trainer=TrainerFactory(club=club),
            training_type=training_type,
            day_of_week=0,
        )
        tariff = TariffFactory(club=club, training_type=training_type)
        lead = LeadFactory(club=club, lead_status=Student.LeadStatus.NEW)
        _enable_legacy_manual_group_admission(
            club=club,
            settings=settings,
            manual_admission_enabled=True,
        )

        with (
            patch(
                "apps.leads.services._finalize_lead_conversion_side_effects",
                side_effect=RuntimeError("rollback admission"),
            ),
            pytest.raises(RuntimeError, match="rollback admission"),
        ):
            create_payment(
                club_id=club.id,
                student_id=lead.id,
                tariff_id=tariff.id,
                payment_method=Payment.Method.CASH,
                recorded_by_id=owner_user.id,
                target_schedule_id=target_schedule.id,
                target_start_date=date(2030, 1, 7),
                create_manual_operational_admission=True,
            )

        lead.refresh_from_db()
        assert lead.status == Student.Status.LEAD
        assert lead.lead_status == Student.LeadStatus.NEW
        assert not Payment.objects.for_club(club).filter(student=lead).exists()
        assert not Subscription.objects.for_club(club).filter(student=lead).exists()
        assert not ScheduleEnrollment.objects.for_club(club).filter(student=lead).exists()
        assert not LeadLifecycleEvent.objects.for_club(club).filter(student=lead).exists()
        mock_async.assert_not_called()

    @patch("django_q.tasks.async_task")
    def test_manual_operational_admission_rejects_open_permanent_enrollment_without_adopting_it(
        self,
        mock_async,
        club,
        owner_user,
        settings,
    ):
        from apps.attendance.models import ScheduleEnrollment
        from apps.attendance.tests.factories import ScheduleFactory
        from apps.trainers.tests.factories import TrainerFactory

        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        target_schedule = ScheduleFactory(
            club=club,
            trainer=TrainerFactory(club=club),
            training_type=training_type,
            day_of_week=0,
        )
        tariff = TariffFactory(club=club, training_type=training_type)
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        existing = ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=target_schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=date(2029, 12, 3),
            ends_on=None,
            created_from=ScheduleEnrollment.CreatedFrom.MANUAL,
        )
        _enable_legacy_manual_group_admission(
            club=club,
            settings=settings,
            manual_admission_enabled=True,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            create_payment(
                club_id=club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                payment_method=Payment.Method.CASH,
                recorded_by_id=owner_user.id,
                target_schedule_id=target_schedule.id,
                target_start_date=date(2030, 1, 7),
                create_manual_operational_admission=True,
            )

        assert exc_info.value.code == "manual_operational_admission_enrollment_conflict"
        existing.refresh_from_db()
        assert existing.created_from == ScheduleEnrollment.CreatedFrom.MANUAL
        assert existing.starts_on == date(2029, 12, 3)
        assert not Payment.objects.for_club(club).filter(student=student).exists()
        mock_async.assert_not_called()

    @patch("django_q.tasks.async_task")
    def test_trainer_group_contract_runs_under_student_and_enrollment_lock(
        self,
        mock_async,
        club,
        owner_user,
    ):
        from apps.attendance.tests.factories import ScheduleFactory
        from apps.billing.service_modules import group_payments
        from apps.trainers.tests.factories import TrainerFactory

        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        target_schedule = ScheduleFactory(
            club=club,
            trainer=TrainerFactory(club=club),
            training_type=training_type,
            day_of_week=0,
        )
        tariff = TariffFactory(club=club, training_type=training_type)
        student = LeadFactory(club=club, lead_status=Student.LeadStatus.TRIAL_DONE)
        original_validator = group_payments._validate_trainer_group_payment_contract

        def assert_locked_contract(**kwargs):
            assert transaction.get_connection().in_atomic_block is True
            assert kwargs["lock_enrollments"] is True
            return original_validator(**kwargs)

        with patch(
            "apps.billing.service_modules.group_payments._validate_trainer_group_payment_contract",
            side_effect=assert_locked_contract,
        ):
            payment = create_payment(
                club_id=club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                payment_method=Payment.Method.CASH,
                recorded_by_id=owner_user.id,
                target_schedule_id=target_schedule.id,
                target_start_date=date(2030, 1, 7),
                enforce_trainer_group_contract=True,
            )

        assert payment.status == Payment.Status.PENDING
        mock_async.assert_called_once()

    @patch("django_q.tasks.async_task")
    def test_group_target_validation_uses_club_local_date(
        self,
        mock_async,
        club,
        owner_user,
    ):
        from apps.attendance.tests.factories import ScheduleFactory
        from apps.trainers.tests.factories import TrainerFactory

        club.timezone = "Pacific/Kiritimati"
        club.save(update_fields=["timezone"])
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        target_schedule = ScheduleFactory(
            club=club,
            trainer=TrainerFactory(club=club),
            training_type=training_type,
            day_of_week=0,
        )
        tariff = TariffFactory(club=club, training_type=training_type)
        student = LeadFactory(club=club, lead_status=Student.LeadStatus.TRIAL_DONE)

        with (
            patch(
                "apps.clubs.timezones.timezone.now",
                return_value=datetime(2030, 1, 7, 12, 30, tzinfo=UTC),
            ),
            pytest.raises(BusinessLogicError) as exc_info,
        ):
            create_payment(
                club_id=club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                payment_method=Payment.Method.CASH,
                recorded_by_id=owner_user.id,
                target_schedule_id=target_schedule.id,
                target_start_date=date(2030, 1, 7),
            )

        assert exc_info.value.code == "target_start_date_in_past"
        assert not Payment.objects.for_club(club).filter(student=student).exists()
        assert not Subscription.objects.for_club(club).filter(student=student).exists()
        mock_async.assert_not_called()

    @patch("django_q.tasks.async_task")
    def test_create_payment_rejects_personal_package_without_owner_trainer(
        self,
        mock_async,
        club,
        owner_user,
    ):
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        tariff = TariffFactory(training_type=tt, price=Decimal("5000"))
        student = StudentFactory(club=club)

        with pytest.raises(BusinessLogicError) as exc_info:
            create_payment(
                club_id=club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                payment_method="cash",
                recorded_by_id=owner_user.id,
            )

        assert exc_info.value.code == "package_owner_trainer_required"
        assert Payment.objects.for_club(club).filter(student=student).count() == 0
        assert Subscription.objects.for_club(club).filter(student=student).count() == 0
        mock_async.assert_not_called()

    @patch("django_q.tasks.async_task")
    def test_create_payment_stores_personal_package_owner_separately_from_seller(
        self,
        mock_async,
        club,
        owner_user,
    ):
        from apps.trainers.models import TrainerPackageAllocation
        from apps.trainers.tests.factories import TrainerFactory

        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        tariff = TariffFactory(training_type=tt, price=Decimal("5000"))
        student = StudentFactory(club=club)
        seller = TrainerFactory(club=club)
        package_owner = TrainerFactory(club=club)

        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method="cash",
            recorded_by_id=owner_user.id,
            seller_trainer_id=seller.id,
            package_owner_trainer_id=package_owner.id,
        )

        assert payment.status == Payment.Status.PENDING
        assert payment.seller_trainer_id == seller.id
        assert payment.package_owner_trainer_id == package_owner.id
        assert not TrainerPackageAllocation.objects.filter(payment=payment).exists()
        mock_async.assert_called_once()

    @patch("django_q.tasks.async_task")
    def test_create_payment_rejects_foreign_package_owner_without_financial_records(
        self,
        mock_async,
        club,
        other_club,
        owner_user,
    ):
        from apps.trainers.tests.factories import TrainerFactory

        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        tariff = TariffFactory(training_type=tt, price=Decimal("5000"))
        student = StudentFactory(club=club)
        package_owner = TrainerFactory(club=other_club)

        with pytest.raises(BusinessLogicError) as exc_info:
            create_payment(
                club_id=club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                payment_method="cash",
                recorded_by_id=owner_user.id,
                package_owner_trainer_id=package_owner.id,
            )

        assert exc_info.value.code == "package_owner_trainer_not_found"
        assert Payment.objects.for_club(club).filter(student=student).count() == 0
        assert Subscription.objects.for_club(club).filter(student=student).count() == 0
        mock_async.assert_not_called()

    @patch("django_q.tasks.async_task")
    def test_create_payment_rejects_invalid_payment_method_without_financial_records(
        self,
        mock_async,
        club,
        owner_user,
    ):
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt, price=Decimal("5000"))
        student = StudentFactory(club=club)

        with pytest.raises(BusinessLogicError) as exc_info:
            create_payment(
                club_id=club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                payment_method="crypto",
                recorded_by_id=owner_user.id,
            )

        assert exc_info.value.code == "invalid_payment_method"
        assert Payment.objects.for_club(club).filter(student=student).count() == 0
        assert Subscription.objects.for_club(club).filter(student=student).count() == 0
        mock_async.assert_not_called()

    @pytest.mark.parametrize("status", [Subscription.Status.ACTIVE, Subscription.Status.PENDING])
    @patch("django_q.tasks.async_task")
    def test_create_payment_rejects_existing_current_subscription(
        self,
        mock_async,
        club,
        owner_user,
        status,
    ):
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        SubscriptionFactory(tariff=tariff, student=student, status=status)

        with pytest.raises(BusinessLogicError) as exc_info:
            create_payment(
                club_id=club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                payment_method="cash",
                recorded_by_id=owner_user.id,
            )

        assert exc_info.value.code == "active_subscription_exists"
        assert Payment.objects.for_club(club).filter(student=student).count() == 0
        mock_async.assert_not_called()

    @patch("django_q.tasks.async_task")
    def test_create_payment_with_discounts(self, mock_async, club, owner_user):
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=tt, price=Decimal("5000"))
        student = StudentFactory(club=club)
        d1 = DiscountFactory(club=club, discount_type="percent", value=Decimal("15"))
        d2 = DiscountFactory(club=club, discount_type="fixed", value=Decimal("500"))

        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method="transfer",
            discount_ids=[d1.id, d2.id],
            recorded_by_id=owner_user.id,
        )

        # 5000 - 750 - 500 = 3750
        assert payment.amount == Decimal("3750")
        assert payment.original_amount == Decimal("5000")
        assert set(payment.applied_discounts.values_list("id", flat=True)) == {d1.id, d2.id}
        payment.subscription.refresh_from_db()
        assert payment.subscription.paid_amount == Decimal("3750")

    @patch("django_q.tasks.async_task")
    def test_create_payment_reserves_selected_existing_debt_until_confirmation(
        self,
        mock_async,
        club,
        owner_user,
    ):
        from apps.attendance.tests.factories import CheckinFactory

        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(
            training_type=tt,
            price=Decimal("5000"),
            trainings_limit=8,
            trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
        )
        student = StudentFactory(club=club)
        checkin = CheckinFactory(
            club=club,
            student=student,
            training_type=tt,
            subscription=None,
            is_debt=True,
        )
        debt = DebtFactory(club=club, student=student, checkin=checkin)

        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method=Payment.Method.CASH,
            debt_ids=[debt.id],
            recorded_by_id=owner_user.id,
        )

        debt.refresh_from_db()
        checkin.refresh_from_db()
        assert payment.status == Payment.Status.PENDING
        assert debt.settlement_payment_id == payment.id
        assert debt.resolved_at is None
        assert debt.resolution_type == ""
        assert checkin.subscription_id is None
        assert checkin.is_debt is True
        event = DebtSettlementEvent.objects.for_club(club.id).get(payment=payment, debt=debt)
        assert event.event_type == DebtSettlementEvent.EventType.RESERVED
        mock_async.assert_called_once_with(
            "apps.billing.tasks.notify_payment_verification",
            payment.id,
            club_id=club.id,
        )

    @patch("django_q.tasks.async_task")
    def test_create_payment_rejects_foreign_selected_debt_without_financial_records(
        self,
        mock_async,
        club,
        other_club,
        owner_user,
    ):
        from apps.attendance.tests.factories import CheckinFactory

        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(
            training_type=tt,
            price=Decimal("5000"),
            trainings_limit=8,
            trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
        )
        student = StudentFactory(club=club)
        other_student = StudentFactory(club=other_club)
        other_checkin = CheckinFactory(club=other_club, student=other_student, subscription=None, is_debt=True)
        other_debt = DebtFactory(club=other_club, student=other_student, checkin=other_checkin)

        with pytest.raises(BusinessLogicError) as exc_info:
            create_payment(
                club_id=club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                payment_method=Payment.Method.CASH,
                debt_ids=[other_debt.id],
                recorded_by_id=owner_user.id,
            )

        assert exc_info.value.code == "debt_not_found"
        assert Payment.objects.for_club(club).count() == 0
        assert Subscription.objects.for_club(club).count() == 0
        assert DebtSettlementEvent.objects.unscoped().count() == 0
        mock_async.assert_not_called()

    @pytest.mark.parametrize(
        "debt_case",
        ["wrong_student", "wrong_training_type", "cancelled"],
    )
    @patch("django_q.tasks.async_task")
    def test_create_payment_rejects_invalid_selected_debt_without_financial_records(
        self,
        mock_async,
        club,
        owner_user,
        debt_case,
    ):
        from apps.attendance.tests.factories import CheckinFactory

        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        other_tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(
            training_type=tt,
            price=Decimal("5000"),
            trainings_limit=8,
            trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
        )
        student = StudentFactory(club=club)
        debt_student = StudentFactory(club=club) if debt_case == "wrong_student" else student
        checkin = CheckinFactory(
            club=club,
            student=debt_student,
            training_type=other_tt if debt_case == "wrong_training_type" else tt,
            subscription=None,
            is_debt=True,
            cancelled_at=timezone.now() if debt_case == "cancelled" else None,
        )
        debt = DebtFactory(club=club, student=debt_student, checkin=checkin)

        with pytest.raises(BusinessLogicError) as exc_info:
            create_payment(
                club_id=club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                payment_method=Payment.Method.CASH,
                debt_ids=[debt.id],
                recorded_by_id=owner_user.id,
            )

        assert exc_info.value.code == "debt_not_found"
        assert Payment.objects.for_club(club).filter(student=student).count() == 0
        assert Subscription.objects.for_club(club).filter(student=student).count() == 0
        assert DebtSettlementEvent.objects.for_club(club.id).count() == 0
        mock_async.assert_not_called()

    @patch("django_q.tasks.async_task")
    def test_create_payment_rejects_selected_debts_over_training_limit_without_financial_records(
        self,
        mock_async,
        club,
        owner_user,
    ):
        from apps.attendance.tests.factories import CheckinFactory

        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=tt, price=Decimal("5000"), trainings_limit=1)
        student = StudentFactory(club=club)
        first_checkin = CheckinFactory(
            club=club,
            student=student,
            training_type=tt,
            subscription=None,
            is_debt=True,
        )
        second_checkin = CheckinFactory(
            club=club,
            student=student,
            training_type=tt,
            subscription=None,
            is_debt=True,
        )
        first_debt = DebtFactory(club=club, student=student, checkin=first_checkin)
        second_debt = DebtFactory(club=club, student=student, checkin=second_checkin)

        with pytest.raises(BusinessLogicError) as exc_info:
            create_payment(
                club_id=club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                payment_method=Payment.Method.CASH,
                debt_ids=[first_debt.id, second_debt.id],
                recorded_by_id=owner_user.id,
            )

        assert exc_info.value.code == "debt_exceeds_subscription_limit"
        assert Payment.objects.for_club(club).filter(student=student).count() == 0
        assert Subscription.objects.for_club(club).filter(student=student).count() == 0
        assert DebtSettlementEvent.objects.for_club(club.id).count() == 0
        mock_async.assert_not_called()

    @patch("django_q.tasks.async_task")
    def test_debt_settlement_events_are_tenant_scoped(
        self,
        mock_async,
        club,
        other_club,
        owner_user,
    ):
        from apps.attendance.tests.factories import CheckinFactory

        tt = TrainingTypeFactory(club=other_club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=tt, price=Decimal("5000"), trainings_limit=8)
        student = StudentFactory(club=other_club)
        checkin = CheckinFactory(
            club=other_club,
            student=student,
            training_type=tt,
            subscription=None,
            is_debt=True,
        )
        debt = DebtFactory(club=other_club, student=student, checkin=checkin)

        payment = create_payment(
            club_id=other_club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method=Payment.Method.CASH,
            debt_ids=[debt.id],
            recorded_by_id=owner_user.id,
        )

        assert DebtSettlementEvent.objects.for_club(club.id).count() == 0
        assert DebtSettlementEvent.objects.for_club(other_club.id).get(
            payment=payment,
            debt=debt,
        ).event_type == DebtSettlementEvent.EventType.RESERVED
        mock_async.assert_called_once_with(
            "apps.billing.tasks.notify_payment_verification",
            payment.id,
            club_id=other_club.id,
        )

    @pytest.mark.parametrize(
        "discount_case",
        ["nonexistent", "foreign_club", "inactive"],
    )
    @patch("django_q.tasks.async_task")
    def test_create_payment_rejects_invalid_discount_ids_without_side_effects(
        self,
        mock_async,
        club,
        other_club,
        owner_user,
        discount_case,
    ):
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=tt, price=Decimal("5000"))
        student = StudentFactory(club=club)

        if discount_case == "nonexistent":
            discount_id = 999999
        elif discount_case == "foreign_club":
            discount_id = DiscountFactory(club=other_club).id
        else:
            discount_id = DiscountFactory(club=club, is_active=False).id

        with pytest.raises(BusinessLogicError) as exc_info:
            create_payment(
                club_id=club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                payment_method="transfer",
                discount_ids=[discount_id],
                recorded_by_id=owner_user.id,
            )

        assert exc_info.value.code == "invalid_discount_ids"
        assert Payment.objects.for_club(club).filter(student=student).count() == 0
        assert Subscription.objects.for_club(club).filter(student=student).count() == 0
        mock_async.assert_not_called()


@pytest.mark.django_db
class TestOnlineTargetBankOrderLifecycle:
    def _create_targeted_order(self, *, club, owner_user, student):
        from apps.attendance.tests.factories import ScheduleFactory

        target_start_date = date(2030, 1, 7)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            training_type=training_type,
            day_of_week=target_start_date.weekday(),
            one_time_date=None,
        )
        tariff = TariffFactory(club=club, training_type=training_type)
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
            target_schedule_id=schedule.id,
            target_start_date=target_start_date,
        )
        return order, schedule, target_start_date

    @patch("django_q.tasks.async_task")
    def test_online_target_order_creates_enrollment_only_on_provider_approval(
        self,
        _mock_async,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        order, schedule, target_start_date = self._create_targeted_order(
            club=club,
            owner_user=owner_user,
            student=student,
        )

        assert order.status == BankPaymentOrder.Status.PENDING
        assert order.payment.status == Payment.Status.PENDING
        assert order.payment.conversion_enrollment_id is None
        assert not ScheduleEnrollment.objects.for_club(club).filter(student=student).exists()

        event = process_bank_payment_webhook(
            provider=BankPaymentOrder.Provider.MOCK,
            request_body=json.dumps(
                {
                    "webhookType": "acquiringInternetPayment",
                    "event_id": f"online-target-approved-{order.id}",
                    "status": "APPROVED",
                    "paymentLinkId": order.provider_payment_link_id,
                    "operationId": f"online-target-operation-{order.id}",
                    "amount": str(order.amount_snapshot),
                    "paid_at": timezone.now().isoformat(),
                }
            ).encode(),
            headers={},
            request_id="online-target-approved",
        )

        event.refresh_from_db()
        order.refresh_from_db()
        order.payment.refresh_from_db()
        assert event.processing_status == "processed"
        assert order.status == BankPaymentOrder.Status.APPROVED
        assert order.payment.status == Payment.Status.CONFIRMED
        enrollment = order.payment.conversion_enrollment
        assert enrollment is not None
        assert enrollment.student_id == student.id
        assert enrollment.schedule_id == schedule.id
        assert enrollment.starts_on == target_start_date
        assert enrollment.created_from == ScheduleEnrollment.CreatedFrom.PAID_CONVERSION

    @patch("django_q.tasks.async_task")
    def test_switch_enabled_rejects_unlinked_manual_target_payment(
        self,
        _mock_async,
        club,
        owner_user,
    ):
        from apps.attendance.tests.factories import ScheduleFactory

        target_start_date = date(2030, 1, 7)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        schedule = ScheduleFactory(
            club=club,
            training_type=training_type,
            day_of_week=target_start_date.weekday(),
            one_time_date=None,
        )
        tariff = TariffFactory(club=club, training_type=training_type)
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        with override_settings(MANUAL_OPERATIONAL_ADMISSION_ENABLED=False):
            payment = create_payment(
                club_id=club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                payment_method=Payment.Method.CASH,
                recorded_by_id=owner_user.id,
                target_schedule_id=schedule.id,
                target_start_date=target_start_date,
            )

        with override_settings(MANUAL_OPERATIONAL_ADMISSION_ENABLED=True), pytest.raises(
            BusinessLogicError
        ) as exc_info:
            verify_payment(
                payment_id=payment.id,
                club_id=club.id,
                verified_by_id=owner_user.id,
                action="confirm",
            )

        payment.refresh_from_db()
        assert exc_info.value.code == "payment_conversion_enrollment_required"
        assert payment.status == Payment.Status.PENDING
        assert payment.conversion_enrollment_id is None

    @pytest.mark.parametrize("resolution", ["cancel", "failed"])
    @patch("django_q.tasks.async_task")
    def test_online_target_order_does_not_create_enrollment_before_provider_approval(
        self,
        _mock_async,
        resolution,
        settings,
        club,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club, status=Student.Status.ACTIVE)
        order, _schedule, _target_start_date = self._create_targeted_order(
            club=club,
            owner_user=owner_user,
            student=student,
        )

        if resolution == "cancel":
            cancel_bank_payment_order(
                club_id=club.id,
                order_id=order.id,
                actor_user_id=owner_user.id,
            )
        else:
            process_bank_payment_webhook(
                provider=BankPaymentOrder.Provider.MOCK,
                request_body=json.dumps(
                    {
                        "webhookType": "acquiringInternetPayment",
                        "event_id": f"online-target-failed-{order.id}",
                        "status": "FAILED",
                        "paymentLinkId": order.provider_payment_link_id,
                        "operationId": f"online-target-operation-{order.id}",
                        "amount": str(order.amount_snapshot),
                    }
                ).encode(),
                headers={},
                request_id="online-target-failed",
            )

        order.refresh_from_db()
        order.payment.refresh_from_db()
        expected_order_status = (
            BankPaymentOrder.Status.CANCELLED
            if resolution == "cancel"
            else BankPaymentOrder.Status.FAILED
        )
        assert order.status == expected_order_status
        assert order.payment.status == Payment.Status.REJECTED
        assert order.payment.conversion_enrollment_id is None
        assert not ScheduleEnrollment.objects.for_club(club).filter(student=student).exists()


@pytest.mark.django_db
class TestVerifyPayment:
    def test_verify_payment_confirm(self, club, owner_user):
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.PENDING)
        payment = PaymentFactory(
            tariff=tariff,
            student=student,
            subscription=subscription,
            recorded_by=owner_user,
        )

        result = verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )

        assert result.status == Payment.Status.CONFIRMED
        assert result.verified_by_id == owner_user.id
        assert result.verified_at is not None
        subscription.refresh_from_db()
        assert subscription.status == Subscription.Status.ACTIVE

    @pytest.mark.parametrize("action", ["confirm", "reject"])
    def test_reconciling_quiesces_legacy_payment_verify_then_allows_retry(self, club, owner_user, action):
        from apps.attendance.models import TrainingGroupRolloutState

        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(club=club, training_type=training_type)
        student = StudentFactory(club=club)
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
            recorded_by=owner_user,
            status=Payment.Status.PENDING,
        )
        update_training_group_rollout_state_for_test(
            TrainingGroupRolloutState.objects.for_club(club),
            mode=TrainingGroupRolloutState.Mode.RECONCILING
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            verify_payment(
                payment_id=payment.id,
                club_id=club.id,
                verified_by_id=owner_user.id,
                action=action,
                rejection_reason="Owner rejected the pending legacy payment." if action == "reject" else "",
            )

        assert exc_info.value.code == "training_group_reconciling"
        payment.refresh_from_db()
        assert payment.status == Payment.Status.PENDING

        update_training_group_rollout_state_for_test(

            TrainingGroupRolloutState.objects.for_club(club),
            mode=TrainingGroupRolloutState.Mode.OFF
        )
        result = verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action=action,
            rejection_reason="Owner rejected the pending legacy payment." if action == "reject" else "",
        )
        assert result.status == (
            Payment.Status.CONFIRMED if action == "confirm" else Payment.Status.REJECTED
        )

    def test_verify_payment_confirm_for_adult_lead_converts_without_opening_access(self, club, owner_user):
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=tt)
        lead = LeadFactory(
            club=club,
            is_child=False,
            phone="8 900 123 45 67",
            status=Student.Status.TRIAL,
            lead_status=Student.LeadStatus.TRIAL_DONE,
        )
        subscription = SubscriptionFactory(tariff=tariff, student=lead, status=Subscription.Status.PENDING)
        payment = PaymentFactory(
            tariff=tariff,
            student=lead,
            subscription=subscription,
            recorded_by=owner_user,
        )

        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )

        lead.refresh_from_db()
        assert lead.status == Student.Status.ACTIVE
        assert lead.lead_status is None
        assert lead.user_id is None
        assert not AccountAccess.objects.for_club(club).filter(student=lead).exists()

    def test_verify_payment_confirm_for_child_lead_converts_without_opening_parent_access(self, club, owner_user):
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=tt)
        child = LeadFactory(
            club=club,
            is_child=True,
            phone="8 901 222 33 44",
            status=Student.Status.TRIAL,
            lead_status=Student.LeadStatus.TRIAL_DONE,
        )
        subscription = SubscriptionFactory(tariff=tariff, student=child, status=Subscription.Status.PENDING)
        payment = PaymentFactory(
            tariff=tariff,
            student=child,
            subscription=subscription,
            recorded_by=owner_user,
        )

        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )

        child.refresh_from_db()
        assert child.status == Student.Status.ACTIVE
        assert child.lead_status is None
        assert child.user_id is None
        assert child.parent_user_id is None
        assert not AccountAccess.objects.for_club(club).filter(student=child).exists()

    def test_verify_payment_confirm_rejects_personal_payment_without_package_owner(
        self,
        club,
        owner_user,
    ):
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.PENDING)
        payment = PaymentFactory(
            tariff=tariff,
            student=student,
            subscription=subscription,
            recorded_by=owner_user,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            verify_payment(
                payment_id=payment.id,
                club_id=club.id,
                verified_by_id=owner_user.id,
                action="confirm",
            )

        assert exc_info.value.code == "package_owner_trainer_required"
        payment.refresh_from_db()
        subscription.refresh_from_db()
        assert payment.status == Payment.Status.PENDING
        assert subscription.status == Subscription.Status.PENDING

    def test_verify_payment_confirm_creates_personal_package_allocation(self, club, owner_user):
        from apps.billing.models import TrainingType
        from apps.trainers.models import TrainerPackageAllocation
        from apps.trainers.tests.factories import TrainerFactory

        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        tariff = TariffFactory(training_type=tt, trainings_limit=8, price=Decimal("8000"))
        student = StudentFactory(club=club)
        seller = TrainerFactory(club=club)
        package_owner = TrainerFactory(club=club)
        subscription = SubscriptionFactory(
            tariff=tariff,
            student=student,
            status=Subscription.Status.PENDING,
            trainings_left=8,
        )
        payment = PaymentFactory(
            tariff=tariff,
            student=student,
            subscription=subscription,
            recorded_by=owner_user,
            seller_trainer=seller,
            package_owner_trainer=package_owner,
        )

        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )

        allocation = TrainerPackageAllocation.objects.get(subscription=subscription)
        assert payment.seller_trainer_id == seller.id
        assert allocation.owner_trainer_id == package_owner.id
        assert allocation.payment_id == payment.id
        assert allocation.sessions_total_snapshot == 8
        assert allocation.sessions_remaining_snapshot == 8
        assert allocation.amount_snapshot == Decimal("8000.00")

    @patch("django_q.tasks.async_task")
    def test_verify_group_target_payment_converts_lead_enrolls_target_and_pays_target_trainer(
        self,
        mock_async,
        club,
        owner_user,
    ):
        from apps.attendance.models import ScheduleEnrollment
        from apps.attendance.tests.factories import ScheduleFactory
        from apps.billing.tasks import create_sale_earning
        from apps.trainers.models import TrainerEarning
        from apps.trainers.tests.factories import TrainerFactory, TrainerRateFactory

        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        target_trainer = TrainerFactory(club=club)
        recorder_trainer = TrainerFactory(club=club)
        target_schedule = ScheduleFactory(
            club=club,
            trainer=target_trainer,
            training_type=training_type,
            day_of_week=0,
        )
        trial_schedule = ScheduleFactory(
            club=club,
            trainer=recorder_trainer,
            training_type=training_type,
            day_of_week=0,
        )
        TrainerRateFactory(
            club=club,
            trainer=target_trainer,
            location=target_schedule.location,
            training_type=training_type,
            percent=Decimal("25.00"),
        )
        tariff = TariffFactory(
            club=club,
            training_type=training_type,
            price=Decimal("4000.00"),
            scope=Tariff.Scope.LOCATION,
            location=target_schedule.location,
        )
        lead = LeadFactory(
            club=club,
            assigned_trainer=recorder_trainer,
            lead_status=Student.LeadStatus.TRIAL_DONE,
        )
        trial_date = date(2030, 1, 7)
        trial_enrollment = ScheduleEnrollment.objects.create(
            club=club,
            student=lead,
            schedule=trial_schedule,
            status=ScheduleEnrollment.Status.TRIAL,
            starts_on=trial_date,
            ends_on=trial_date,
            created_from=ScheduleEnrollment.CreatedFrom.GUEST_VISIT,
        )

        payment = create_payment(
            club_id=club.id,
            student_id=lead.id,
            tariff_id=tariff.id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=target_schedule.id,
            target_start_date=trial_date,
        )

        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )
        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )

        lead.refresh_from_db()
        payment.refresh_from_db()
        trial_enrollment.refresh_from_db()
        assert lead.status == Student.Status.ACTIVE
        assert lead.lead_status is None
        assert payment.status == Payment.Status.CONFIRMED
        assert payment.sale_earning_snapshot_recorded is True
        assert payment.sale_trainer_id_snapshot == target_trainer.id
        assert payment.sale_rate_percent_snapshot == Decimal("25.00")
        assert trial_enrollment.starts_on == trial_date
        assert trial_enrollment.ends_on == trial_date
        permanent = ScheduleEnrollment.objects.for_club(club).get(
            student=lead,
            schedule=target_schedule,
            ends_on__isnull=True,
        )
        assert permanent.status == ScheduleEnrollment.Status.ACTIVE
        assert permanent.starts_on == trial_date
        assert permanent.created_from == ScheduleEnrollment.CreatedFrom.PAID_CONVERSION
        assert payment.conversion_enrollment_id == permanent.id
        assert ScheduleEnrollment.objects.for_club(club).filter(
            student=lead,
            schedule=target_schedule,
            ends_on__isnull=True,
        ).count() == 1

        create_sale_earning(payment.id, club.id)
        create_sale_earning(payment.id, club.id)

        earning = TrainerEarning.objects.get(payment=payment)
        assert earning.trainer_id == target_trainer.id
        assert earning.amount == Decimal("1000.00")
        assert not TrainerEarning.objects.filter(payment=payment, trainer=recorder_trainer).exists()
        mock_async.assert_called()

    @patch("django_q.tasks.async_task")
    def test_verify_group_target_payment_converts_new_lead_without_fake_trial(
        self,
        mock_async,
        club,
        owner_user,
    ):
        from apps.attendance.models import ScheduleEnrollment
        from apps.attendance.tests.factories import ScheduleFactory
        from apps.leads.models import LeadLifecycleEvent
        from apps.trainers.tests.factories import TrainerFactory

        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        target_trainer = TrainerFactory(club=club)
        target_schedule = ScheduleFactory(
            club=club,
            trainer=target_trainer,
            training_type=training_type,
            day_of_week=0,
        )
        tariff = TariffFactory(club=club, training_type=training_type)
        lead = LeadFactory(
            club=club,
            status=Student.Status.LEAD,
            lead_status=Student.LeadStatus.NEW,
            assigned_trainer=target_trainer,
        )
        payment = create_payment(
            club_id=club.id,
            student_id=lead.id,
            tariff_id=tariff.id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=target_schedule.id,
            target_start_date=date(2030, 1, 7),
            enforce_trainer_group_contract=True,
        )

        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )

        lead.refresh_from_db()
        assert lead.status == Student.Status.ACTIVE
        assert lead.lead_status is None
        enrollments = ScheduleEnrollment.objects.for_club(club).filter(student=lead)
        assert enrollments.count() == 1
        assert enrollments.get().created_from == ScheduleEnrollment.CreatedFrom.PAID_CONVERSION
        assert not enrollments.filter(
            created_from=ScheduleEnrollment.CreatedFrom.LEAD_BOOKING,
        ).exists()
        lifecycle = LeadLifecycleEvent.objects.for_club(club).get(
            student=lead,
            event_type=LeadLifecycleEvent.EventType.LEAD_CONVERTED,
        )
        assert lifecycle.old_lead_status == Student.LeadStatus.NEW
        assert lifecycle.metadata["source"] == "subscription_payment"
        mock_async.assert_called()

    @patch("django_q.tasks.async_task")
    def test_verify_group_target_payment_uses_same_type_rate_from_other_location_for_sale_earning(
        self,
        mock_async,
        club,
        owner_user,
    ):
        from apps.attendance.models import ScheduleEnrollment
        from apps.attendance.tests.factories import ScheduleFactory
        from apps.billing.tasks import create_sale_earning
        from apps.trainers.models import TrainerEarning
        from apps.trainers.tests.factories import TrainerFactory, TrainerRateFactory

        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        target_trainer = TrainerFactory(club=club)
        fallback_schedule = ScheduleFactory(
            club=club,
            trainer=target_trainer,
            training_type=training_type,
            day_of_week=0,
        )
        target_schedule = ScheduleFactory(
            club=club,
            trainer=target_trainer,
            training_type=training_type,
            day_of_week=1,
        )
        TrainerRateFactory(
            club=club,
            trainer=target_trainer,
            location=fallback_schedule.location,
            training_type=training_type,
            percent=Decimal("25.00"),
        )
        tariff = TariffFactory(
            club=club,
            training_type=training_type,
            price=Decimal("4000.00"),
            scope=Tariff.Scope.LOCATION,
            location=target_schedule.location,
        )
        student = StudentFactory(club=club, status=Student.Status.ACTIVE, lead_status=None)
        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=target_schedule.id,
            target_start_date=date(2030, 1, 8),
        )

        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )

        payment.refresh_from_db()
        assert payment.sale_earning_snapshot_recorded is True
        assert payment.sale_trainer_id_snapshot == target_trainer.id
        assert payment.sale_rate_percent_snapshot == Decimal("25.00")
        assert ScheduleEnrollment.objects.for_club(club).filter(
            student=student,
            schedule=target_schedule,
            ends_on__isnull=True,
        ).exists()

        create_sale_earning(payment.id, club.id)

        earning = TrainerEarning.objects.get(payment=payment)
        assert earning.trainer_id == target_trainer.id
        assert earning.rate_percent == Decimal("25.00")
        assert earning.amount == Decimal("1000.00")
        mock_async.assert_called()

    @patch("django_q.tasks.async_task")
    def test_verify_group_target_payment_reject_creates_no_enrollment_or_sale_earning(
        self,
        mock_async,
        club,
        owner_user,
    ):
        from apps.attendance.models import ScheduleEnrollment
        from apps.attendance.tests.factories import ScheduleFactory
        from apps.trainers.models import TrainerEarning
        from apps.trainers.tests.factories import TrainerFactory

        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        target_trainer = TrainerFactory(club=club)
        target_schedule = ScheduleFactory(
            club=club,
            trainer=target_trainer,
            training_type=training_type,
            day_of_week=0,
        )
        tariff = TariffFactory(club=club, training_type=training_type, price=Decimal("4000.00"))
        lead = LeadFactory(club=club, lead_status=Student.LeadStatus.TRIAL_DONE)
        payment = create_payment(
            club_id=club.id,
            student_id=lead.id,
            tariff_id=tariff.id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=target_schedule.id,
            target_start_date=date(2030, 1, 7),
        )

        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="reject",
            rejection_reason="operator rejected",
        )

        payment.refresh_from_db()
        lead.refresh_from_db()
        assert payment.status == Payment.Status.REJECTED
        assert payment.sale_earning_snapshot_recorded is False
        assert lead.status == Student.Status.LEAD
        assert not ScheduleEnrollment.objects.for_club(club).filter(
            student=lead,
            schedule=target_schedule,
            ends_on__isnull=True,
        ).exists()
        assert not TrainerEarning.objects.filter(payment=payment).exists()
        assert mock_async.call_count == 1

    @patch("django_q.tasks.async_task")
    def test_verify_group_target_payment_permits_trainer_only_schedule_drift(
        self,
        mock_async,
        club,
        owner_user,
    ):
        from apps.attendance.models import ScheduleEnrollment
        from apps.attendance.tests.factories import ScheduleFactory
        from apps.trainers.tests.factories import TrainerFactory

        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        target_trainer = TrainerFactory(club=club)
        replacement_trainer = TrainerFactory(club=club)
        target_schedule = ScheduleFactory(
            club=club,
            trainer=target_trainer,
            training_type=training_type,
            day_of_week=0,
        )
        tariff = TariffFactory(club=club, training_type=training_type, price=Decimal("4000.00"))
        lead = LeadFactory(club=club, lead_status=Student.LeadStatus.TRIAL_DONE)
        payment = create_payment(
            club_id=club.id,
            student_id=lead.id,
            tariff_id=tariff.id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=target_schedule.id,
            target_start_date=date(2030, 1, 7),
        )
        target_schedule.trainer = replacement_trainer
        target_schedule.save(update_fields=["trainer", "updated_at"])

        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )

        payment.refresh_from_db()
        payment.subscription.refresh_from_db()
        assert payment.status == Payment.Status.CONFIRMED
        assert payment.subscription.status == Subscription.Status.ACTIVE
        assert ScheduleEnrollment.objects.for_club(club).filter(
            student=lead,
            schedule=target_schedule,
            ends_on__isnull=True,
        ).exists()
        assert mock_async.call_count >= 1

    @patch("django_q.tasks.async_task")
    def test_verify_group_target_payment_reuses_existing_open_enrollment_without_rewriting_it(
        self,
        mock_async,
        club,
        owner_user,
    ):
        from apps.attendance.models import ScheduleEnrollment
        from apps.attendance.tests.factories import ScheduleFactory
        from apps.trainers.tests.factories import TrainerFactory, TrainerRateFactory

        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        target_trainer = TrainerFactory(club=club)
        target_schedule = ScheduleFactory(
            club=club,
            trainer=target_trainer,
            training_type=training_type,
            day_of_week=0,
        )
        TrainerRateFactory(
            club=club,
            trainer=target_trainer,
            location=target_schedule.location,
            training_type=training_type,
            percent=Decimal("25.00"),
        )
        tariff = TariffFactory(club=club, training_type=training_type, price=Decimal("4000.00"))
        student = StudentFactory(club=club, status=Student.Status.ACTIVE, lead_status=None)
        existing = ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=target_schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=date(2029, 12, 3),
            ends_on=None,
            created_from=ScheduleEnrollment.CreatedFrom.MANUAL,
        )
        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=target_schedule.id,
            target_start_date=date(2030, 1, 7),
        )

        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )

        existing.refresh_from_db()
        payment.refresh_from_db()
        assert existing.starts_on == date(2029, 12, 3)
        assert existing.created_from == ScheduleEnrollment.CreatedFrom.MANUAL
        assert payment.conversion_enrollment_id is None
        assert ScheduleEnrollment.objects.for_club(club).filter(
            student=student,
            schedule=target_schedule,
            ends_on__isnull=True,
        ).count() == 1
        mock_async.assert_called()

    @patch("django_q.tasks.async_task")
    def test_verify_payment_confirm_does_not_settle_unselected_late_debt(
        self,
        mock_async,
        club,
        owner_user,
    ):
        from apps.attendance.tests.factories import CheckinFactory

        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(
            training_type=tt,
            trainings_limit=8,
            trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
        )
        student = StudentFactory(club=club)
        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
        )
        checkin = CheckinFactory(
            club=club,
            student=student,
            training_type=tt,
            subscription=None,
            is_debt=True,
        )
        debt = DebtFactory(club=club, student=student, checkin=checkin)

        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )

        debt.refresh_from_db()
        checkin.refresh_from_db()
        payment.subscription.refresh_from_db()
        assert payment.subscription.status == Subscription.Status.ACTIVE
        assert payment.subscription.trainings_used == 0
        assert debt.settlement_payment_id is None
        assert debt.resolved_at is None
        assert debt.resolution_type == ""
        assert checkin.subscription_id is None
        assert checkin.is_debt is True

    @patch("django_q.tasks.async_task")
    def test_verify_payment_package_allocation_snapshots_remaining_after_selected_debt_settlement(
        self,
        mock_async,
        club,
        owner_user,
    ):
        from apps.attendance.tests.factories import CheckinFactory
        from apps.trainers.models import TrainerPackageAllocation
        from apps.trainers.tests.factories import TrainerFactory

        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        tariff = TariffFactory(training_type=tt, trainings_limit=8, price=Decimal("8000"))
        student = StudentFactory(club=club)
        package_owner = TrainerFactory(club=club)
        subscription = SubscriptionFactory(
            tariff=tariff,
            student=student,
            status=Subscription.Status.PENDING,
            trainings_left=8,
        )
        payment = PaymentFactory(
            tariff=tariff,
            student=student,
            subscription=subscription,
            recorded_by=owner_user,
            package_owner_trainer=package_owner,
        )
        checkin = CheckinFactory(
            club=club,
            student=student,
            training_type=tt,
            subscription=None,
            is_debt=True,
        )
        DebtFactory(club=club, student=student, checkin=checkin, settlement_payment=payment)

        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )

        allocation = TrainerPackageAllocation.objects.get(subscription=subscription)
        assert allocation.sessions_total_snapshot == 8
        assert allocation.sessions_remaining_snapshot == 7
        mock_async.assert_called_once_with(
            "apps.attendance.tasks.calculate_salary",
            checkin.id,
            club_id=club.id,
        )

    def test_verify_payment_reject_does_not_create_package_allocation(self, club, owner_user):
        from apps.billing.models import TrainingType
        from apps.trainers.models import TrainerPackageAllocation
        from apps.trainers.tests.factories import TrainerFactory

        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        trainer = TrainerFactory(club=club)
        subscription = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.PENDING)
        payment = PaymentFactory(
            tariff=tariff,
            student=student,
            subscription=subscription,
            recorded_by=owner_user,
            seller_trainer=trainer,
        )

        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="reject",
            rejection_reason="receipt mismatch",
        )

        assert not TrainerPackageAllocation.objects.filter(subscription=subscription).exists()

    def test_confirm_payment_converts_paid_trial_lead_end_to_end(self, club, owner_user):
        from apps.leads.tests.factories import LeadFactory
        from apps.pipelines.models import Pipeline
        from apps.pipelines.tests.factories import PipelineExecutionFactory, PipelineFactory
        from apps.retention.models import RetentionTask
        from apps.retention.tests.factories import RetentionTaskFactory
        from apps.students.models import Student
        from apps.trainers.tests.factories import TrainerFactory

        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=tt)
        trainer = TrainerFactory(club=club)
        lead = LeadFactory(
            club=club,
            assigned_trainer=trainer,
            status=Student.Status.TRIAL,
            lead_status=Student.LeadStatus.TRIAL_DONE,
        )
        lead.refresh_from_db()
        assert lead.status == Student.Status.TRIAL
        assert lead.lead_status == Student.LeadStatus.TRIAL_DONE
        subscription = SubscriptionFactory(
            tariff=tariff,
            student=lead,
            status=Subscription.Status.PENDING,
        )
        payment = PaymentFactory(
            tariff=tariff,
            student=lead,
            subscription=subscription,
            recorded_by=owner_user,
        )
        follow_up = PipelineFactory(
            club=club,
            pipeline_type=Pipeline.PipelineType.FOLLOW_UP,
        )
        win_back = PipelineFactory(
            club=club,
            pipeline_type=Pipeline.PipelineType.WIN_BACK,
        )
        follow_up_execution = PipelineExecutionFactory(
            club=club,
            pipeline=follow_up,
            student=lead,
        )
        win_back_execution = PipelineExecutionFactory(
            club=club,
            pipeline=win_back,
            student=lead,
        )
        new_lead_task = RetentionTaskFactory(
            club=club,
            student=lead,
            trainer=trainer,
            task_type=RetentionTask.TaskType.NEW_LEAD,
        )
        post_trial_task = RetentionTaskFactory(
            club=club,
            student=lead,
            trainer=trainer,
            task_type=RetentionTask.TaskType.POST_TRIAL,
        )
        renewal_task = RetentionTaskFactory(
            club=club,
            student=lead,
            trainer=trainer,
            task_type=RetentionTask.TaskType.RENEWAL,
        )

        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )

        lead.refresh_from_db()
        follow_up_execution.refresh_from_db()
        win_back_execution.refresh_from_db()
        new_lead_task.refresh_from_db()
        post_trial_task.refresh_from_db()
        renewal_task.refresh_from_db()
        assert lead.lead_status is None
        assert lead.status == Student.Status.ACTIVE
        assert follow_up_execution.cancelled_at is not None
        assert win_back_execution.cancelled_at is not None
        assert new_lead_task.status == RetentionTask.TaskStatus.CLOSED
        assert new_lead_task.resolution == RetentionTask.Resolution.AUTO_SUBSCRIPTION
        assert new_lead_task.resolved_at is not None
        assert post_trial_task.status == RetentionTask.TaskStatus.CLOSED
        assert post_trial_task.resolution == RetentionTask.Resolution.AUTO_SUBSCRIPTION
        assert post_trial_task.resolved_at is not None
        assert renewal_task.status == RetentionTask.TaskStatus.CLOSED
        assert renewal_task.resolution == RetentionTask.Resolution.AUTO_SUBSCRIPTION
        assert renewal_task.resolved_at is not None

        first_follow_up_cancelled_at = follow_up_execution.cancelled_at
        first_win_back_cancelled_at = win_back_execution.cancelled_at
        first_new_lead_resolved_at = new_lead_task.resolved_at
        first_post_trial_resolved_at = post_trial_task.resolved_at
        first_renewal_resolved_at = renewal_task.resolved_at

        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )

        follow_up_execution.refresh_from_db()
        win_back_execution.refresh_from_db()
        new_lead_task.refresh_from_db()
        post_trial_task.refresh_from_db()
        renewal_task.refresh_from_db()
        assert follow_up_execution.cancelled_at == first_follow_up_cancelled_at
        assert win_back_execution.cancelled_at == first_win_back_cancelled_at
        assert new_lead_task.resolved_at == first_new_lead_resolved_at
        assert post_trial_task.resolved_at == first_post_trial_resolved_at
        assert renewal_task.resolved_at == first_renewal_resolved_at

    def test_confirm_payment_for_active_non_lead_keeps_existing_pipeline_state(self, club, owner_user):
        from apps.pipelines.models import Pipeline
        from apps.pipelines.tests.factories import PipelineExecutionFactory, PipelineFactory
        from apps.retention.models import RetentionTask
        from apps.retention.tests.factories import RetentionTaskFactory
        from apps.students.models import Student
        from apps.trainers.tests.factories import TrainerFactory

        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=tt)
        trainer = TrainerFactory(club=club)
        student = StudentFactory(
            club=club,
            assigned_trainer=trainer,
            status=Student.Status.ACTIVE,
            lead_status=None,
        )
        subscription = SubscriptionFactory(
            tariff=tariff,
            student=student,
            status=Subscription.Status.PENDING,
        )
        payment = PaymentFactory(
            tariff=tariff,
            student=student,
            subscription=subscription,
            recorded_by=owner_user,
        )
        pipeline = PipelineFactory(
            club=club,
            pipeline_type=Pipeline.PipelineType.FOLLOW_UP,
        )
        execution = PipelineExecutionFactory(
            club=club,
            pipeline=pipeline,
            student=student,
        )
        task = RetentionTaskFactory(
            club=club,
            student=student,
            trainer=trainer,
            task_type=RetentionTask.TaskType.POST_TRIAL,
        )

        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )

        student.refresh_from_db()
        execution.refresh_from_db()
        task.refresh_from_db()
        assert student.status == Student.Status.ACTIVE
        assert student.lead_status is None
        assert execution.cancelled_at is None
        assert task.status == RetentionTask.TaskStatus.OPEN
        assert task.resolved_at is None

    @patch("django_q.tasks.async_task")
    def test_confirm_discounted_pending_payment_keeps_subscription_paid_amount(
        self,
        mock_async,
        club,
        owner_user,
    ):
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=tt, price=Decimal("5000"))
        discount = DiscountFactory(club=club, discount_type=Discount.Type.FIXED, value=Decimal("1250"))
        student = StudentFactory(club=club)
        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method=Payment.Method.CASH,
            discount_ids=[discount.id],
            recorded_by_id=owner_user.id,
        )
        tariff.price = Decimal("9000")
        tariff.save(update_fields=["price"])

        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )

        payment.subscription.refresh_from_db()
        assert payment.amount == Decimal("3750")
        assert payment.subscription.status == Subscription.Status.ACTIVE
        assert payment.subscription.paid_amount == payment.amount
        assert mock_async.call_count >= 1

    @patch("django_q.tasks.async_task")
    def test_confirm_preserves_existing_subscription_paid_amount_snapshot(
        self,
        mock_async,
        club,
        owner_user,
    ):
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=tt, price=Decimal("5000"))
        discount = DiscountFactory(club=club, discount_type=Discount.Type.FIXED, value=Decimal("1250"))
        student = StudentFactory(club=club)
        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method=Payment.Method.CASH,
            discount_ids=[discount.id],
            recorded_by_id=owner_user.id,
        )
        preserved_snapshot = Decimal("4200")
        payment.subscription.paid_amount = preserved_snapshot
        payment.subscription.save(update_fields=["paid_amount"])

        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )

        payment.subscription.refresh_from_db()
        assert payment.amount == Decimal("3750")
        assert payment.subscription.status == Subscription.Status.ACTIVE
        assert payment.subscription.paid_amount == preserved_snapshot
        assert mock_async.call_count >= 1

    def test_verify_payment_reject(self, club, owner_user):
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.PENDING)
        payment = PaymentFactory(
            tariff=tariff,
            student=student,
            subscription=subscription,
            recorded_by=owner_user,
        )

        result = verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="reject",
            rejection_reason="duplicate receipt",
        )

        assert result.status == Payment.Status.REJECTED
        subscription.refresh_from_db()
        assert subscription.deleted_at is not None

    def test_verify_payment_reject_requires_reason_without_mutation(self, club, owner_user):
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.PENDING)
        payment = PaymentFactory(
            tariff=tariff,
            student=student,
            subscription=subscription,
            recorded_by=owner_user,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            verify_payment(
                payment_id=payment.id,
                club_id=club.id,
                verified_by_id=owner_user.id,
                action="reject",
                rejection_reason="   ",
            )

        assert exc_info.value.code == "payment_rejection_reason_required"
        payment.refresh_from_db()
        subscription.refresh_from_db()
        assert payment.status == Payment.Status.PENDING
        assert payment.rejection_reason == ""
        assert subscription.deleted_at is None

    def test_verify_payment_blocks_manual_confirm_for_online_payment(self, club, owner_user):
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.PENDING)
        payment = PaymentFactory(
            tariff=tariff,
            student=student,
            subscription=subscription,
            payment_method=Payment.Method.ONLINE,
            recorded_by=owner_user,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            verify_payment(
                payment_id=payment.id,
                club_id=club.id,
                verified_by_id=owner_user.id,
                action="confirm",
            )

        assert exc_info.value.code == "online_payment_manual_verification_forbidden"
        payment.refresh_from_db()
        subscription.refresh_from_db()
        assert payment.status == Payment.Status.PENDING
        assert subscription.status == Subscription.Status.PENDING

    def test_verify_payment_blocks_manual_reject_for_online_payment(self, club, owner_user):
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.PENDING)
        payment = PaymentFactory(
            tariff=tariff,
            student=student,
            subscription=subscription,
            payment_method=Payment.Method.ONLINE,
            recorded_by=owner_user,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            verify_payment(
                payment_id=payment.id,
                club_id=club.id,
                verified_by_id=owner_user.id,
                action="reject",
                rejection_reason="provider dispute",
            )

        assert exc_info.value.code == "online_payment_manual_verification_forbidden"
        payment.refresh_from_db()
        subscription.refresh_from_db()
        assert payment.status == Payment.Status.PENDING
        assert payment.rejection_reason == ""
        assert subscription.deleted_at is None

    @patch("django_q.tasks.async_task")
    def test_verify_payment_reject_releases_reserved_debt(self, mock_async, club, owner_user):
        from apps.attendance.tests.factories import CheckinFactory

        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(
            training_type=tt,
            trainings_limit=8,
            trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
        )
        student = StudentFactory(club=club)
        checkin = CheckinFactory(
            club=club,
            student=student,
            training_type=tt,
            subscription=None,
            is_debt=True,
        )
        debt = DebtFactory(club=club, student=student, checkin=checkin)
        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method=Payment.Method.CASH,
            debt_ids=[debt.id],
            recorded_by_id=owner_user.id,
        )

        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="reject",
            rejection_reason="package owner changed",
        )

        debt.refresh_from_db()
        checkin.refresh_from_db()
        assert debt.settlement_payment_id is None
        assert debt.resolved_at is None
        assert checkin.subscription_id is None
        assert checkin.is_debt is True
        events = list(
            DebtSettlementEvent.objects.for_club(club.id)
            .filter(payment=payment, debt=debt)
            .order_by("created_at", "id")
            .values_list("event_type", flat=True)
        )
        assert events == [
            DebtSettlementEvent.EventType.RESERVED,
            DebtSettlementEvent.EventType.REJECTED,
        ]
        mock_async.assert_called_once_with(
            "apps.billing.tasks.notify_payment_verification",
            payment.id,
            club_id=club.id,
        )

    def test_verify_payment_reject_persists_reason(self, club, owner_user):
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.PENDING)
        payment = PaymentFactory(
            tariff=tariff,
            student=student,
            subscription=subscription,
            recorded_by=owner_user,
        )

        result = verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="reject",
            rejection_reason="suspicious transfer",
        )

        assert result.rejection_reason == "suspicious transfer"

    def test_verify_payment_rejects_invalid_action(self, club, owner_user):
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.PENDING)
        payment = PaymentFactory(
            tariff=tariff,
            student=student,
            subscription=subscription,
            recorded_by=owner_user,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            verify_payment(
                payment_id=payment.id,
                club_id=club.id,
                verified_by_id=owner_user.id,
                action="maybe",
            )

        assert exc_info.value.code == "invalid_payment_verify_action"

    def test_verify_payment_idempotent(self, club, owner_user):
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.PENDING)
        payment = PaymentFactory(
            tariff=tariff,
            student=student,
            subscription=subscription,
            recorded_by=owner_user,
        )

        # First call
        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )
        # Second call -- no error
        result = verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )
        assert result.status == Payment.Status.CONFIRMED

    def test_confirm_existing_student_access_does_not_rotate_or_duplicate_on_reconfirm(
        self,
        club,
        owner_user,
    ):
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=tt)
        user = UserFactory(username="+79001234567")
        user.set_password("ExistingPass123!")
        user.save(update_fields=["password"])
        ClubMembership.objects.create(
            user=user,
            club=club,
            role=ClubMembership.Role.STUDENT,
        )
        lead = LeadFactory(
            club=club,
            is_child=False,
            phone="8 900 123 45 67",
            user=user,
            status=Student.Status.TRIAL,
            lead_status=Student.LeadStatus.TRIAL_DONE,
        )
        access = AccountAccess.objects.create(
            club=club,
            student=lead,
            user=user,
            role=ClubMembership.Role.STUDENT,
            issued_by=owner_user,
            must_change_password=False,
        )
        original_password_hash = user.password
        subscription = SubscriptionFactory(tariff=tariff, student=lead, status=Subscription.Status.PENDING)
        payment = PaymentFactory(
            tariff=tariff,
            student=lead,
            subscription=subscription,
            recorded_by=owner_user,
        )

        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )
        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )

        user.refresh_from_db()
        assert user.password == original_password_hash
        assert user.check_password("ExistingPass123!")
        assert ClubMembership.objects.filter(user=user, club=club, role=ClubMembership.Role.STUDENT).count() == 1
        assert AccountAccess.objects.for_club(club).filter(
            student=lead,
            role=ClubMembership.Role.STUDENT,
        ).count() == 1
        assert AccountAccess.objects.for_club(club).get(id=access.id).user_id == user.id

    def test_verify_payment_reject_or_pending_does_not_open_access(self, club, owner_user):
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=tt)
        lead = LeadFactory(
            club=club,
            is_child=False,
            phone="8 900 123 45 67",
            status=Student.Status.TRIAL,
            lead_status=Student.LeadStatus.TRIAL_DONE,
        )
        subscription = SubscriptionFactory(tariff=tariff, student=lead, status=Subscription.Status.PENDING)
        payment = PaymentFactory(
            tariff=tariff,
            student=lead,
            subscription=subscription,
            recorded_by=owner_user,
        )

        assert not AccountAccess.objects.for_club(club).filter(student=lead).exists()

        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="reject",
            rejection_reason="trial not paid",
        )

        lead.refresh_from_db()
        assert lead.lead_status == Student.LeadStatus.TRIAL_DONE
        assert lead.user_id is None
        assert not AccountAccess.objects.for_club(club).filter(student=lead).exists()

    def test_confirm_invalid_phone_lead_converts_without_access_side_effect(
        self,
        club,
        owner_user,
    ):
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=tt)
        lead = LeadFactory(
            club=club,
            is_child=False,
            phone="123",
            status=Student.Status.TRIAL,
            lead_status=Student.LeadStatus.TRIAL_DONE,
        )
        subscription = SubscriptionFactory(tariff=tariff, student=lead, status=Subscription.Status.PENDING)
        payment = PaymentFactory(
            tariff=tariff,
            student=lead,
            subscription=subscription,
            recorded_by=owner_user,
        )

        with patch("apps.students.access_services._generate_temporary_password") as generate_password:
            verify_payment(
                payment_id=payment.id,
                club_id=club.id,
                verified_by_id=owner_user.id,
                action="confirm",
            )

        payment.refresh_from_db()
        subscription.refresh_from_db()
        lead.refresh_from_db()
        assert payment.status == Payment.Status.CONFIRMED
        assert subscription.status == Subscription.Status.ACTIVE
        assert lead.status == Student.Status.ACTIVE
        assert lead.lead_status is None
        assert lead.user_id is None
        assert not AccountAccess.objects.for_club(club).filter(student=lead).exists()
        generate_password.assert_not_called()

    def test_confirm_payment_does_not_close_pre_payment_debt_without_attachment(self, club, owner_user):
        """verify_payment must not resolve debts it did not attach to the paid subscription."""
        from apps.attendance.tests.factories import CheckinFactory

        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        other_tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)

        # Checkin with matching training type -> debt should be resolved
        checkin1 = CheckinFactory(
            club=club,
            student=student,
            training_type=tt,
            subscription=None,
            is_debt=True,
        )
        debt1 = DebtFactory(club=club, student=student, checkin=checkin1)

        # Checkin with different training type -> debt should NOT be resolved
        checkin2 = CheckinFactory(
            club=club,
            student=student,
            training_type=other_tt,
            subscription=None,
            is_debt=True,
        )
        debt2 = DebtFactory(club=club, student=student, checkin=checkin2)

        subscription = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.PENDING)
        payment = PaymentFactory(
            tariff=tariff,
            student=student,
            subscription=subscription,
            recorded_by=owner_user,
        )
        checkin1.created_at = payment.created_at - timedelta(hours=1)
        checkin1.save(update_fields=["created_at"])

        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )

        debt1.refresh_from_db()
        debt2.refresh_from_db()
        assert debt1.resolved_at is None
        checkin1.refresh_from_db()
        assert checkin1.subscription_id is None
        assert checkin1.is_debt is True
        # Different training type debt should remain unresolved
        assert debt2.resolved_at is None

    @patch("django_q.tasks.async_task")
    def test_confirm_pending_payment_closes_explicit_existing_debt(
        self,
        mock_async,
        club,
        owner_user,
    ):
        from apps.attendance.tests.factories import CheckinFactory

        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(
            training_type=tt,
            price=Decimal("5000"),
            trainings_limit=8,
            trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
        )
        student = StudentFactory(club=club)
        checkin = CheckinFactory(
            club=club,
            student=student,
            training_type=tt,
            subscription=None,
            is_debt=True,
        )
        debt = DebtFactory(club=club, student=student, checkin=checkin)
        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method=Payment.Method.CASH,
            debt_ids=[debt.id],
            recorded_by_id=owner_user.id,
        )

        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )

        payment.refresh_from_db()
        payment.subscription.refresh_from_db()
        debt.refresh_from_db()
        checkin.refresh_from_db()
        assert payment.status == Payment.Status.CONFIRMED
        assert payment.subscription.status == Subscription.Status.ACTIVE
        assert payment.subscription.trainings_left == 7
        assert payment.subscription.trainings_used == 1
        assert checkin.subscription_id == payment.subscription_id
        assert checkin.is_debt is False
        assert debt.resolved_at is not None
        assert debt.resolution_type == "payment"
        assert debt.settlement_payment_id == payment.id
        events = list(
            DebtSettlementEvent.objects.for_club(club.id)
            .filter(payment=payment, debt=debt)
            .order_by("created_at", "id")
            .values_list("event_type", flat=True)
        )
        assert events == [
            DebtSettlementEvent.EventType.RESERVED,
            DebtSettlementEvent.EventType.CONFIRMED,
        ]
        mock_async.assert_any_call(
            "apps.attendance.tasks.calculate_salary",
            checkin.id,
            club_id=club.id,
        )

    @patch("django_q.tasks.async_task")
    def test_confirm_pending_payment_rejects_debt_settlement_for_closed_payroll_period(
        self,
        mock_async,
        club,
        owner_user,
    ):
        from apps.attendance.tests.factories import CheckinFactory
        from apps.trainers.services import close_trainer_payroll_period
        from apps.trainers.tests.factories import TrainerFactory

        target_date = date(2026, 6, 10)
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        tariff = TariffFactory(training_type=tt, price=Decimal("5000"), trainings_limit=8)
        student = StudentFactory(club=club)
        package_owner = TrainerFactory(club=club)
        checkin = CheckinFactory(
            club=club,
            student=student,
            training_type=tt,
            subscription=None,
            is_debt=True,
            date=target_date,
        )
        debt = DebtFactory(club=club, student=student, checkin=checkin)
        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method=Payment.Method.CASH,
            debt_ids=[debt.id],
            recorded_by_id=owner_user.id,
            package_owner_trainer_id=package_owner.id,
        )
        close_trainer_payroll_period(
            club_id=club.id,
            period_start=target_date.replace(day=1),
            period_end=target_date,
            reason="Closed",
            actor_user_id=owner_user.id,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            verify_payment(
                payment_id=payment.id,
                club_id=club.id,
                verified_by_id=owner_user.id,
                action="confirm",
            )

        payment.refresh_from_db()
        payment.subscription.refresh_from_db()
        checkin.refresh_from_db()
        debt.refresh_from_db()
        assert exc_info.value.code == "payroll_period_closed"
        assert payment.status == Payment.Status.PENDING
        assert payment.subscription.status == Subscription.Status.PENDING
        assert checkin.subscription_id is None
        assert checkin.is_debt is True
        assert debt.resolved_at is None
        assert debt.resolution_type == ""
        assert debt.settlement_payment_id == payment.id

    @patch("django_q.tasks.async_task")
    def test_confirm_pending_payment_keeps_unselected_newer_debt_open(self, mock_async, club, owner_user):
        """A check-in created while payment is pending is not settled unless explicitly selected."""
        from apps.attendance.tests.factories import CheckinFactory

        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(
            training_type=tt,
            trainings_limit=8,
            trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
        )
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(
            tariff=tariff,
            student=student,
            status=Subscription.Status.PENDING,
            trainings_left=8,
            trainings_used=0,
            expires_at=None,
        )
        payment = PaymentFactory(
            tariff=tariff,
            student=student,
            subscription=subscription,
            recorded_by=owner_user,
        )
        checkin = CheckinFactory(
            club=club,
            student=student,
            training_type=tt,
            subscription=None,
            is_debt=True,
        )
        debt = DebtFactory(club=club, student=student, checkin=checkin)

        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )

        subscription.refresh_from_db()
        checkin.refresh_from_db()
        debt.refresh_from_db()
        assert subscription.trainings_left == 8
        assert subscription.trainings_used == 0
        assert checkin.subscription_id is None
        assert checkin.is_debt is True
        assert debt.resolved_at is None
        assert debt.resolution_type == ""
        assert debt.settlement_payment_id is None
        mock_async.assert_not_called()

    @patch("django_q.tasks.async_task")
    def test_confirm_pending_location_payment_does_not_attach_other_location_debt(
        self,
        mock_async,
        club,
        owner_user,
    ):
        from apps.attendance.tests.factories import CheckinFactory, ScheduleFactory

        location_a = LocationFactory(club=club, name="A")
        location_b = LocationFactory(club=club, name="B")
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(
            training_type=tt,
            trainings_limit=8,
            scope=Tariff.Scope.LOCATION,
            location=location_a,
        )
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(
            tariff=tariff,
            student=student,
            status=Subscription.Status.PENDING,
            trainings_left=8,
            trainings_used=0,
            expires_at=None,
            scope=Tariff.Scope.LOCATION,
            location=location_a,
        )
        payment = PaymentFactory(
            tariff=tariff,
            student=student,
            subscription=subscription,
            recorded_by=owner_user,
        )
        schedule = ScheduleFactory(club=club, training_type=tt, location=location_b)
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=tt,
            location=location_b,
            subscription=None,
            is_debt=True,
        )
        debt = DebtFactory(club=club, student=student, checkin=checkin)

        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )

        subscription.refresh_from_db()
        checkin.refresh_from_db()
        debt.refresh_from_db()
        assert subscription.trainings_left == 8
        assert subscription.trainings_used == 0
        assert checkin.subscription_id is None
        assert checkin.is_debt is True
        assert debt.resolved_at is None
        mock_async.assert_not_called()

    @patch("apps.attendance.services.async_task")
    @patch("django_q.tasks.async_task")
    def test_cancel_checkin_cancels_unselected_debt_after_pending_payment_confirm(
        self,
        mock_salary_async,
        mock_reverse_async,
        club,
        owner_user,
    ):
        from apps.attendance.tests.factories import CheckinFactory

        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(
            training_type=tt,
            trainings_limit=8,
            trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
        )
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(
            tariff=tariff,
            student=student,
            status=Subscription.Status.PENDING,
            trainings_left=8,
            trainings_used=0,
            expires_at=None,
        )
        payment = PaymentFactory(
            tariff=tariff,
            student=student,
            subscription=subscription,
            recorded_by=owner_user,
        )
        checkin = CheckinFactory(
            club=club,
            student=student,
            training_type=tt,
            subscription=None,
            is_debt=True,
        )
        debt = DebtFactory(club=club, student=student, checkin=checkin)

        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )
        cancel_checkin(
            checkin_id=checkin.id,
            club_id=club.id,
            cancelled_by_user_id=owner_user.id,
            user_role="owner",
        )

        subscription.refresh_from_db()
        checkin.refresh_from_db()
        debt.refresh_from_db()
        assert subscription.trainings_left == 8
        assert subscription.trainings_used == 0
        assert checkin.deleted_at is not None
        assert debt.resolved_at is not None
        assert debt.resolution_type == "cancelled"
        mock_salary_async.assert_not_called()
        assert mock_reverse_async.call_count == 5

    @patch("django_q.tasks.async_task")
    def test_confirm_pending_payment_keeps_unselected_debts_open_even_with_limited_capacity(
        self,
        mock_async,
        club,
        owner_user,
    ):
        from apps.attendance.tests.factories import CheckinFactory

        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(
            training_type=tt,
            trainings_limit=1,
            trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
        )
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(
            tariff=tariff,
            student=student,
            status=Subscription.Status.PENDING,
            trainings_left=1,
            trainings_used=0,
            expires_at=None,
        )
        payment = PaymentFactory(
            tariff=tariff,
            student=student,
            subscription=subscription,
            recorded_by=owner_user,
        )
        checkins = [
            CheckinFactory(
                club=club,
                student=student,
                training_type=tt,
                subscription=None,
                is_debt=True,
            )
            for _ in range(3)
        ]
        debts = [
            DebtFactory(club=club, student=student, checkin=checkin)
            for checkin in checkins
        ]

        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )

        subscription.refresh_from_db()
        assert subscription.trainings_left == 1
        assert subscription.trainings_used == 0
        assert subscription.status == Subscription.Status.ACTIVE

        for debt in debts:
            debt.refresh_from_db()
        for checkin in checkins:
            checkin.refresh_from_db()

        assert all(debt.resolved_at is None for debt in debts)
        assert all(debt.resolution_type == "" for debt in debts)
        assert all(debt.settlement_payment_id is None for debt in debts)
        assert all(checkin.subscription_id is None for checkin in checkins)
        assert all(checkin.is_debt is True for checkin in checkins)
        mock_async.assert_not_called()


@pytest.mark.django_db
class TestWriteOffDebt:
    def test_write_off_debt_records_audit_snapshot(self, club, owner_user):
        from apps.attendance.tests.factories import CheckinFactory

        student = StudentFactory(club=club)
        checkin = CheckinFactory(club=club, student=student)
        debt = DebtFactory(
            club=club,
            student=student,
            checkin=checkin,
            tariff_price=Decimal("1500.00"),
            reason="no_subscription",
        )

        result = write_off_debt(
            debt_id=debt.id,
            club_id=club.id,
            written_off_by_id=owner_user.id,
            reason="Goodwill correction",
        )

        assert result.resolved_at is not None
        assert result.resolution_type == "writeoff"

        event = DebtWriteOffEvent.objects.for_club(club.id).get(debt=debt)
        assert event.written_off_by_id == owner_user.id
        assert event.reason == "Goodwill correction"
        assert event.decided_at == result.resolved_at
        assert event.amount_snapshot == Decimal("1500.00")
        assert event.debt_id_snapshot == debt.id
        assert event.student_id_snapshot == student.id
        assert event.student_name_snapshot == str(student)
        assert event.checkin_id_snapshot == checkin.id
        assert event.debt_reason_snapshot == "no_subscription"

    def test_write_off_debt_rejects_already_resolved_debt(self, club, owner_user):
        from apps.attendance.tests.factories import CheckinFactory

        student = StudentFactory(club=club)
        checkin = CheckinFactory(club=club, student=student)
        debt = DebtFactory(
            club=club,
            student=student,
            checkin=checkin,
            resolved_at=timezone.now(),
            resolution_type="payment",
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            write_off_debt(
                debt_id=debt.id,
                club_id=club.id,
                written_off_by_id=owner_user.id,
                reason="Duplicate write-off",
            )

        debt.refresh_from_db()
        assert exc_info.value.code == "debt_already_resolved"
        assert debt.resolution_type == "payment"
        assert DebtWriteOffEvent.objects.for_club(club.id).filter(debt=debt).count() == 0

    def test_write_off_debt_requires_reason(self, club, owner_user):
        from apps.attendance.tests.factories import CheckinFactory

        student = StudentFactory(club=club)
        checkin = CheckinFactory(club=club, student=student)
        debt = DebtFactory(club=club, student=student, checkin=checkin)

        with pytest.raises(BusinessLogicError) as exc_info:
            write_off_debt(
                debt_id=debt.id,
                club_id=club.id,
                written_off_by_id=owner_user.id,
                reason="   ",
            )

        assert exc_info.value.code == "writeoff_reason_required"
        debt.refresh_from_db()
        assert debt.resolved_at is None
        assert DebtWriteOffEvent.objects.for_club(club.id).filter(debt=debt).count() == 0

    def test_write_off_debt_rejects_foreign_club_debt(self, club, other_club, owner_user):
        from apps.attendance.tests.factories import CheckinFactory

        other_student = StudentFactory(club=other_club)
        other_checkin = CheckinFactory(club=other_club, student=other_student)
        other_debt = DebtFactory(club=other_club, student=other_student, checkin=other_checkin)

        with pytest.raises(Debt.DoesNotExist):
            write_off_debt(
                debt_id=other_debt.id,
                club_id=club.id,
                written_off_by_id=owner_user.id,
                reason="Wrong tenant attempt",
            )

        other_debt.refresh_from_db()
        assert other_debt.resolved_at is None
        assert DebtWriteOffEvent.objects.for_club(other_club.id).filter(debt=other_debt).count() == 0

    @patch("django_q.tasks.async_task")
    def test_write_off_debt_rejects_debt_reserved_by_pending_payment(
        self,
        mock_async,
        club,
        owner_user,
    ):
        from apps.attendance.tests.factories import CheckinFactory

        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=tt, trainings_limit=8)
        student = StudentFactory(club=club)
        checkin = CheckinFactory(
            club=club,
            student=student,
            training_type=tt,
            subscription=None,
            is_debt=True,
        )
        debt = DebtFactory(club=club, student=student, checkin=checkin)
        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method=Payment.Method.CASH,
            debt_ids=[debt.id],
            recorded_by_id=owner_user.id,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            write_off_debt(
                debt_id=debt.id,
                club_id=club.id,
                written_off_by_id=owner_user.id,
                reason="Bypass attempt",
            )

        debt.refresh_from_db()
        assert exc_info.value.code == "debt_payment_pending"
        assert debt.resolved_at is None
        assert debt.resolution_type == ""
        assert debt.settlement_payment_id == payment.id
        assert DebtWriteOffEvent.objects.for_club(club.id).filter(debt=debt).count() == 0
        mock_async.assert_called_once_with(
            "apps.billing.tasks.notify_payment_verification",
            payment.id,
            club_id=club.id,
        )

    def test_write_off_debt_rejects_closed_payroll_period(self, club, owner_user):
        from apps.attendance.tests.factories import CheckinFactory
        from apps.trainers.services import close_trainer_payroll_period

        target_date = date(2026, 6, 10)
        student = StudentFactory(club=club)
        checkin = CheckinFactory(
            club=club,
            student=student,
            subscription=None,
            is_debt=True,
            date=target_date,
        )
        debt = DebtFactory(club=club, student=student, checkin=checkin)
        close_trainer_payroll_period(
            club_id=club.id,
            period_start=target_date.replace(day=1),
            period_end=target_date,
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

        debt.refresh_from_db()
        assert exc_info.value.code == "payroll_period_closed"
        assert debt.resolved_at is None
        assert debt.resolution_type == ""
        assert DebtWriteOffEvent.objects.for_club(club.id).filter(debt=debt).count() == 0

    @patch("apps.attendance.services.async_task")
    @patch("django_q.tasks.async_task")
    def test_cancel_checkin_rejects_debt_reserved_by_pending_payment(
        self,
        mock_payment_async,
        mock_cancel_async,
        club,
        owner_user,
    ):
        from apps.attendance.tests.factories import CheckinFactory

        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=tt, trainings_limit=8)
        student = StudentFactory(club=club)
        checkin = CheckinFactory(
            club=club,
            student=student,
            training_type=tt,
            subscription=None,
            is_debt=True,
        )
        debt = DebtFactory(club=club, student=student, checkin=checkin)
        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method=Payment.Method.CASH,
            debt_ids=[debt.id],
            recorded_by_id=owner_user.id,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            cancel_checkin(
                checkin_id=checkin.id,
                club_id=club.id,
                cancelled_by_user_id=owner_user.id,
                user_role="owner",
            )

        debt.refresh_from_db()
        checkin.refresh_from_db()
        assert exc_info.value.code == "debt_payment_pending"
        assert debt.resolved_at is None
        assert debt.resolution_type == ""
        assert debt.settlement_payment_id == payment.id
        assert checkin.deleted_at is None
        assert checkin.cancelled_at is None
        assert checkin.is_debt is True
        assert checkin.subscription_id is None
        mock_payment_async.assert_called_once_with(
            "apps.billing.tasks.notify_payment_verification",
            payment.id,
            club_id=club.id,
        )
        mock_cancel_async.assert_not_called()


@pytest.mark.django_db
class TestCreateDiscount:
    def test_create_discount(self, club):
        discount = create_discount(
            club_id=club.id,
            name="Family",
            discount_type="percent",
            value=Decimal("15"),
        )
        assert discount.club_id == club.id
        assert discount.name == "Family"
        assert discount.discount_type == "percent"
        assert discount.value == Decimal("15")
        assert discount.is_active is True

    @pytest.mark.parametrize(
        ("discount_type", "value"),
        [
            ("percent", Decimal("-1")),
            ("percent", Decimal("101")),
            ("fixed", Decimal("-1")),
        ],
    )
    def test_create_discount_rejects_invalid_value(self, club, discount_type, value):
        with pytest.raises(BusinessLogicError) as exc_info:
            create_discount(
                club_id=club.id,
                name="Invalid",
                discount_type=discount_type,
                value=value,
            )

        assert exc_info.value.code == "invalid_discount_value"

    def test_update_discount_rejects_invalid_value(self, club):
        discount = DiscountFactory(club=club, discount_type="percent", value=Decimal("10"))

        with pytest.raises(BusinessLogicError) as exc_info:
            update_discount(
                discount_id=discount.id,
                club_id=club.id,
                value=Decimal("101"),
            )

        assert exc_info.value.code == "invalid_discount_value"

    def test_create_payment_rejects_zero_amount_after_discounts(self, club, owner_user):
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(club=club, training_type=tt, price=Decimal("5000"))
        student = StudentFactory(club=club)
        discount = DiscountFactory(club=club, discount_type="fixed", value=Decimal("5000"))

        with pytest.raises(BusinessLogicError) as exc_info:
            create_payment(
                club_id=club.id,
                student_id=student.id,
                tariff_id=tariff.id,
                payment_method="cash",
                discount_ids=[discount.id],
                recorded_by_id=owner_user.id,
            )

        assert exc_info.value.code == "invalid_money_amount"


@pytest.mark.django_db
class TestCreateExpense:
    def test_create_expense_persists_expected_fields(self, club):
        expense_date = timezone.localdate()

        expense = create_expense(
            club_id=club.id,
            name="Rent",
            amount=Decimal("10000.00"),
            date=expense_date,
            category="facility",
            is_recurring=True,
        )

        assert expense.club_id == club.id
        assert expense.name == "Rent"
        assert expense.amount == Decimal("10000.00")
        assert expense.date == expense_date
        assert expense.category == "facility"
        assert expense.is_recurring is True

    @pytest.mark.parametrize("amount", [Decimal("0"), Decimal("-1")])
    def test_create_expense_rejects_non_positive_amount(self, club, amount):
        with pytest.raises(BusinessLogicError) as exc_info:
            create_expense(
                club_id=club.id,
                name="Bad expense",
                amount=amount,
                date=timezone.localdate(),
            )

        assert exc_info.value.code == "invalid_money_amount"

    def test_update_expense_updates_expected_fields(self, club):
        expense = ExpenseFactory(
            club=club,
            name="Rent",
            amount=Decimal("10000"),
            category="facility",
            is_recurring=True,
        )
        new_date = timezone.localdate() + timedelta(days=1)

        updated = update_expense(
            expense_id=expense.id,
            club_id=club.id,
            name="Mat repair",
            amount=Decimal("2500.00"),
            date=new_date,
            category="equipment",
            is_recurring=False,
        )

        assert updated.name == "Mat repair"
        assert updated.amount == Decimal("2500.00")
        assert updated.date == new_date
        assert updated.category == "equipment"
        assert updated.is_recurring is False

    def test_update_expense_rejects_non_positive_amount(self, club):
        expense = ExpenseFactory(club=club, amount=Decimal("1000"))

        with pytest.raises(BusinessLogicError) as exc_info:
            update_expense(
                expense_id=expense.id,
                club_id=club.id,
                amount=Decimal("0"),
            )

        assert exc_info.value.code == "invalid_money_amount"

    def test_delete_expense_soft_deletes_record(self, club):
        expense = ExpenseFactory(club=club)

        delete_expense(expense_id=expense.id, club_id=club.id)

        expense.refresh_from_db()
        assert expense.deleted_at is not None


@pytest.mark.django_db
class TestMoneyDatabaseConstraints:
    def test_drop_in_price_must_be_positive_when_set_at_db_layer(self, club):
        with pytest.raises(IntegrityError), transaction.atomic():
            TrainingTypeFactory(club=club, drop_in_price=Decimal("-1"))

    def test_tariff_price_must_be_positive_at_db_layer(self, club):
        tt = TrainingTypeFactory(club=club)

        with pytest.raises(IntegrityError), transaction.atomic():
            Tariff.objects.create(
                club=club,
                name="Zero price",
                training_type=tt,
                price=Decimal("0"),
                trainings_limit=8,
                duration_days=30,
                scope=Tariff.Scope.CLUB,
            )

    @pytest.mark.parametrize(
        ("discount_type", "value"),
        [
            (Discount.Type.PERCENT, Decimal("101")),
            (Discount.Type.FIXED, Decimal("-1")),
        ],
    )
    def test_discount_value_must_be_valid_at_db_layer(self, club, discount_type, value):
        with pytest.raises(IntegrityError), transaction.atomic():
            Discount.objects.create(
                club=club,
                name="Invalid",
                discount_type=discount_type,
                value=value,
            )

    def test_payment_amounts_must_be_positive_at_db_layer(self, club, owner_user):
        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(club=club, training_type=tt)
        student = StudentFactory(club=club)

        with pytest.raises(IntegrityError), transaction.atomic():
            Payment.objects.create(
                club=club,
                student=student,
                tariff=tariff,
                amount=Decimal("0"),
                original_amount=Decimal("5000"),
                payment_method=Payment.Method.CASH,
                recorded_by=owner_user,
            )

    def test_subscription_paid_amount_must_be_positive_when_set_at_db_layer(self, club):
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(club=club, training_type=tt)
        student = StudentFactory(club=club)

        with pytest.raises(IntegrityError), transaction.atomic():
            Subscription.objects.create(
                club=club,
                student=student,
                tariff=tariff,
                paid_amount=Decimal("0"),
                trainings_left=8,
                expires_at=timezone.now() + timedelta(days=30),
                scope=Tariff.Scope.CLUB,
            )

    def test_expense_amount_must_be_positive_at_db_layer(self, club):
        with pytest.raises(IntegrityError), transaction.atomic():
            Expense.objects.create(
                club=club,
                name="Zero expense",
                amount=Decimal("0"),
                date=timezone.localdate(),
            )

    def test_debt_tariff_price_must_be_non_negative_at_db_layer(self, club):
        from apps.attendance.tests.factories import CheckinFactory

        student = StudentFactory(club=club)
        checkin = CheckinFactory(club=club, student=student)

        with pytest.raises(IntegrityError), transaction.atomic():
            DebtFactory(
                club=club,
                student=student,
                checkin=checkin,
                tariff_price=Decimal("-1"),
            )


def test_migration_0013_normalizes_freeze_days_before_constraint():
    migration = importlib.import_module(
        "apps.billing.migrations.0013_subscription_paid_amount_freeze_constraints"
    )
    operations = migration.Migration.operations

    normalize_index = next(
        index
        for index, operation in enumerate(operations)
        if isinstance(operation, migrations.RunPython)
        and operation.code is migration.normalize_subscription_freeze_days
    )
    constraint_index = next(
        index
        for index, operation in enumerate(operations)
        if getattr(getattr(operation, "constraint", None), "name", "")
        == "billing_subscriptionfreeze_days_positive"
    )

    assert normalize_index < constraint_index

    backfill_index = next(
        index
        for index, operation in enumerate(operations)
        if isinstance(operation, migrations.RunPython)
        and operation.code is migration.backfill_subscription_paid_amount
    )
    paid_constraint_index = next(
        index
        for index, operation in enumerate(operations)
        if getattr(getattr(operation, "constraint", None), "name", "")
        == "billing_subscription_paid_positive"
    )

    assert backfill_index < paid_constraint_index


@pytest.mark.django_db
def test_migration_0021_backfills_package_owner_from_allocation_then_seller(club, owner_user):
    from apps.trainers.models import TrainerPackageAllocation
    from apps.trainers.tests.factories import TrainerFactory

    migration = importlib.import_module("apps.billing.migrations.0021_payment_package_owner_trainer")
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=8)
    student = StudentFactory(club=club)
    seller = TrainerFactory(club=club)
    allocation_owner = TrainerFactory(club=club)
    allocated_subscription = SubscriptionFactory(
        club=club,
        student=student,
        tariff=tariff,
        status=Subscription.Status.ACTIVE,
        paid_amount=Decimal("8000"),
    )
    allocated_payment = PaymentFactory(
        club=club,
        student=student,
        tariff=tariff,
        subscription=allocated_subscription,
        seller_trainer=seller,
        recorded_by=owner_user,
    )
    TrainerPackageAllocation.objects.create(
        club=club,
        subscription=allocated_subscription,
        payment=allocated_payment,
        student=student,
        tariff=tariff,
        training_type=training_type,
        owner_trainer=allocation_owner,
        source=TrainerPackageAllocation.Source.PAYMENT,
        sessions_total_snapshot=8,
        sessions_remaining_snapshot=8,
        amount_snapshot=Decimal("8000"),
        is_active=True,
    )

    fallback_student = StudentFactory(club=club)
    fallback_seller = TrainerFactory(club=club)
    fallback_subscription = SubscriptionFactory(
        club=club,
        student=fallback_student,
        tariff=tariff,
        status=Subscription.Status.PENDING,
    )
    fallback_payment = PaymentFactory(
        club=club,
        student=fallback_student,
        tariff=tariff,
        subscription=fallback_subscription,
        seller_trainer=fallback_seller,
        recorded_by=owner_user,
    )

    migration.backfill_package_owner_trainer(django_apps, None)

    allocated_payment.refresh_from_db()
    fallback_payment.refresh_from_db()
    assert allocated_payment.package_owner_trainer_id == allocation_owner.id
    assert fallback_payment.package_owner_trainer_id == fallback_seller.id


@pytest.mark.django_db
class TestGetActiveSubscriptionIncludesPending:
    def test_get_active_subscription_includes_pending(self, club):
        from apps.billing.selectors import get_active_subscription

        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        SubscriptionFactory(
            tariff=tariff,
            student=student,
            status=Subscription.Status.PENDING,
        )

        result = get_active_subscription(
            club_id=club.id,
            student_id=student.id,
            training_type_id=tt.id,
            location=None,
        )
        assert result is not None
        assert result.status == Subscription.Status.PENDING

    @patch("django_q.tasks.async_task")
    def test_get_active_subscription_includes_created_pending_payment_without_expiry(
        self,
        _mock_async,
        club,
        owner_user,
    ):
        from apps.billing.selectors import get_active_subscription

        tt = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(club=club, training_type=tt)
        student = StudentFactory(club=club)
        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
        )

        result = get_active_subscription(
            club_id=club.id,
            student_id=student.id,
            training_type_id=tt.id,
            location=None,
        )

        assert result == payment.subscription
        assert result.expires_at is None

    def test_get_active_subscription_excludes_depleted_active_subscription(self, club):
        from apps.billing.selectors import get_active_subscription

        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(club=club, training_type=tt)
        student = StudentFactory(club=club)
        SubscriptionFactory(
            club=club,
            tariff=tariff,
            student=student,
            status=Subscription.Status.ACTIVE,
            trainings_left=0,
        )

        result = get_active_subscription(
            club_id=club.id,
            student_id=student.id,
            training_type_id=tt.id,
            location=None,
        )

        assert result is None


@pytest.mark.django_db
class TestTenantIsolation:
    def test_tariff_tenant_isolation(self):
        club_a = ClubFactory()
        club_b = ClubFactory()
        tt_a = TrainingTypeFactory(club=club_a)
        TariffFactory(training_type=tt_a)

        assert Tariff.objects.for_club(club_a).count() == 1
        assert Tariff.objects.for_club(club_b).count() == 0

    def test_subscription_tenant_isolation(self):
        club_a = ClubFactory()
        club_b = ClubFactory()
        tt = TrainingTypeFactory(club=club_a)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club_a)
        create_subscription(club_id=club_a.id, student_id=student.id, tariff_id=tariff.id)

        assert Subscription.objects.for_club(club_a).count() == 1
        assert Subscription.objects.for_club(club_b).count() == 0

    def test_payment_tenant_isolation(self):
        club_a = ClubFactory()
        club_b = ClubFactory()
        tt = TrainingTypeFactory(club=club_a)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club_a)
        user = UserFactory()
        PaymentFactory(tariff=tariff, student=student, recorded_by=user)

        assert Payment.objects.for_club(club_a).count() == 1
        assert Payment.objects.for_club(club_b).count() == 0

    def test_discount_tenant_isolation(self):
        club_a = ClubFactory()
        club_b = ClubFactory()
        DiscountFactory(club=club_a)

        assert Discount.objects.for_club(club_a).count() == 1
        assert Discount.objects.for_club(club_b).count() == 0


@pytest.mark.django_db
class TestFreezeSubscription:
    def test_freeze_subscription(self, club, owner_user):
        ClubSettingsFactory(club=club, freeze_max_days=30)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        sub = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.ACTIVE)
        original_expires = sub.expires_at

        freeze = freeze_subscription(
            club_id=club.id,
            subscription_id=sub.id,
            days=10,
            reason="vacation",
            frozen_by_id=owner_user.id,
        )

        assert freeze.days == 10
        assert freeze.reason == "vacation"
        assert freeze.approved_by_id == owner_user.id
        assert freeze.rejected_by_id is None
        assert freeze.decision_at is not None
        assert freeze.decision_reason == ""
        assert freeze.ends_at is None
        sub.refresh_from_db()
        assert sub.status == Subscription.Status.FROZEN
        assert sub.expires_at == original_expires + timedelta(days=10)

    def test_freeze_unlimited_subscription_keeps_null_expiry(self, club, owner_user):
        ClubSettingsFactory(club=club, freeze_max_days=30)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt, duration_days=30, trainings_limit=None)
        student = StudentFactory(club=club)
        sub = SubscriptionFactory(
            tariff=tariff,
            student=student,
            status=Subscription.Status.ACTIVE,
            expires_at=None,
            trainings_left=None,
        )

        freeze = freeze_subscription(
            club_id=club.id,
            subscription_id=sub.id,
            days=10,
            reason="vacation",
            frozen_by_id=owner_user.id,
        )

        assert freeze.status == SubscriptionFreeze.FreezeStatus.APPROVED
        sub.refresh_from_db()
        assert sub.status == Subscription.Status.FROZEN
        assert sub.expires_at is None

    @pytest.mark.parametrize("days", [0, -1])
    def test_freeze_rejects_non_positive_days(self, club, owner_user, days):
        ClubSettingsFactory(club=club, freeze_max_days=30)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        sub = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.ACTIVE)

        with pytest.raises(BusinessLogicError) as exc_info:
            freeze_subscription(
                club_id=club.id,
                subscription_id=sub.id,
                days=days,
                reason="vacation",
                frozen_by_id=owner_user.id,
            )

        assert exc_info.value.code == "invalid_freeze_days"

    def test_freeze_cumulative_limit(self, club, owner_user):
        ClubSettingsFactory(club=club, freeze_max_days=30)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        sub = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.ACTIVE)

        # Create existing freeze with 25 days
        SubscriptionFreezeFactory(
            subscription=sub,
            days=25,
            frozen_by=owner_user,
            ends_at=timezone.now(),
        )

        with pytest.raises(BusinessLogicError, match="Freeze limit exceeded"):
            freeze_subscription(
                club_id=club.id,
                subscription_id=sub.id,
                days=10,
                reason="vacation",
                frozen_by_id=owner_user.id,
            )

    def test_freeze_count_limit(self, club, owner_user):
        ClubSettingsFactory(club=club, freeze_max_count=2)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        sub = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.ACTIVE)

        SubscriptionFreezeFactory(subscription=sub, days=3, frozen_by=owner_user, ends_at=timezone.now())
        SubscriptionFreezeFactory(subscription=sub, days=3, frozen_by=owner_user, ends_at=timezone.now())

        with pytest.raises(BusinessLogicError, match="Maximum freeze count"):
            freeze_subscription(
                club_id=club.id,
                subscription_id=sub.id,
                days=5,
                reason="injury",
                frozen_by_id=owner_user.id,
            )

    def test_rejected_freezes_do_not_count_against_limits(self, club, owner_user):
        ClubSettingsFactory(club=club, freeze_max_days=10, freeze_max_count=1)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        sub = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.ACTIVE)
        SubscriptionFreezeFactory(
            subscription=sub,
            days=10,
            frozen_by=owner_user,
            status=SubscriptionFreeze.FreezeStatus.REJECTED,
        )

        freeze = freeze_subscription(
            club_id=club.id,
            subscription_id=sub.id,
            days=10,
            reason="vacation",
            frozen_by_id=owner_user.id,
        )

        assert freeze.status == SubscriptionFreeze.FreezeStatus.APPROVED

    def test_trainer_freeze_rejects_duplicate_pending_request(self, club, owner_user, trainer_user):
        ClubSettingsFactory(club=club)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        sub = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.ACTIVE)

        first = freeze_subscription(
            club_id=club.id,
            subscription_id=sub.id,
            days=5,
            reason="vacation",
            frozen_by_id=trainer_user.id,
            initiator_role="trainer",
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            freeze_subscription(
                club_id=club.id,
                subscription_id=sub.id,
                days=7,
                reason="injury",
                frozen_by_id=trainer_user.id,
                initiator_role="trainer",
            )

        assert exc_info.value.code == "freeze_pending_exists"
        assert first.status == SubscriptionFreeze.FreezeStatus.PENDING
        assert SubscriptionFreeze.objects.for_club(club).filter(
            subscription=sub,
            status=SubscriptionFreeze.FreezeStatus.PENDING,
        ).count() == 1

    def test_owner_direct_freeze_auto_rejects_existing_pending_request(self, club, owner_user, trainer_user):
        ClubSettingsFactory(club=club)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        sub = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.ACTIVE)
        pending = freeze_subscription(
            club_id=club.id,
            subscription_id=sub.id,
            days=5,
            reason="vacation",
            frozen_by_id=trainer_user.id,
            initiator_role="trainer",
        )

        approved = freeze_subscription(
            club_id=club.id,
            subscription_id=sub.id,
            days=7,
            reason="injury",
            frozen_by_id=owner_user.id,
            initiator_role="owner",
        )

        pending.refresh_from_db()
        sub.refresh_from_db()
        assert approved.status == SubscriptionFreeze.FreezeStatus.APPROVED
        assert sub.status == Subscription.Status.FROZEN
        assert pending.status == SubscriptionFreeze.FreezeStatus.REJECTED
        assert pending.rejected_by_id == owner_user.id
        assert pending.decision_reason == "Закрыта автоматически: абонемент заморожен напрямую"
        assert SubscriptionFreeze.objects.for_club(club).filter(
            subscription=sub,
            status=SubscriptionFreeze.FreezeStatus.PENDING,
        ).count() == 0

    def test_freeze_disabled(self, club, owner_user):
        ClubSettingsFactory(club=club, freeze_enabled=False)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        sub = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.ACTIVE)

        with pytest.raises(BusinessLogicError, match="Freeze is disabled"):
            freeze_subscription(
                club_id=club.id,
                subscription_id=sub.id,
                days=5,
                reason="vacation",
                frozen_by_id=owner_user.id,
            )

    def test_freeze_only_active_subscription(self, club, owner_user):
        ClubSettingsFactory(club=club)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)

        for status in [Subscription.Status.FROZEN, Subscription.Status.EXPIRED, Subscription.Status.PENDING]:
            sub = SubscriptionFactory(tariff=tariff, student=student, status=status)
            with pytest.raises(BusinessLogicError, match="Only active subscriptions"):
                freeze_subscription(
                    club_id=club.id,
                    subscription_id=sub.id,
                    days=5,
                    reason="vacation",
                    frozen_by_id=owner_user.id,
                )

    def test_freeze_rejects_date_expired_active_subscription(self, club, owner_user):
        ClubSettingsFactory(club=club)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        sub = SubscriptionFactory(
            tariff=tariff,
            student=student,
            status=Subscription.Status.ACTIVE,
            expires_at=timezone.now() - timedelta(days=1),
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            freeze_subscription(
                club_id=club.id,
                subscription_id=sub.id,
                days=5,
                reason="vacation",
                frozen_by_id=owner_user.id,
            )

        assert exc_info.value.code == "invalid_subscription_status"

    def test_freeze_tenant_isolation(self, club, owner_user):
        club_b = ClubFactory()
        ClubSettingsFactory(club=club)
        tt = TrainingTypeFactory(club=club_b)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club_b)
        sub = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.ACTIVE)

        with pytest.raises(Exception):
            freeze_subscription(
                club_id=club.id,
                subscription_id=sub.id,
                days=5,
                reason="vacation",
                frozen_by_id=owner_user.id,
            )


@pytest.mark.django_db
class TestFreezeApprovalDecisionAudit:
    def test_approve_sets_actor_and_decision_timestamp(self, club, owner_user, trainer_user):
        sub = SubscriptionFactory(club=club, status=Subscription.Status.ACTIVE)
        original_expires = sub.expires_at
        freeze = SubscriptionFreezeFactory(
            subscription=sub,
            frozen_by=trainer_user,
            status=SubscriptionFreeze.FreezeStatus.PENDING,
        )

        result = approve_freeze(
            freeze_id=freeze.id,
            club_id=club.id,
            approved_by_id=owner_user.id,
        )

        result.refresh_from_db()
        sub.refresh_from_db()
        assert result.status == SubscriptionFreeze.FreezeStatus.APPROVED
        assert result.approved_by_id == owner_user.id
        assert result.rejected_by_id is None
        assert result.decision_at is not None
        assert sub.status == Subscription.Status.FROZEN
        assert sub.expires_at == original_expires + timedelta(days=freeze.days)

    def test_approve_unlimited_subscription_keeps_null_expiry(self, club, owner_user, trainer_user):
        sub = SubscriptionFactory(
            club=club,
            status=Subscription.Status.ACTIVE,
            expires_at=None,
            trainings_left=None,
        )
        freeze = SubscriptionFreezeFactory(
            subscription=sub,
            frozen_by=trainer_user,
            status=SubscriptionFreeze.FreezeStatus.PENDING,
        )

        result = approve_freeze(
            freeze_id=freeze.id,
            club_id=club.id,
            approved_by_id=owner_user.id,
        )

        result.refresh_from_db()
        sub.refresh_from_db()
        assert result.status == SubscriptionFreeze.FreezeStatus.APPROVED
        assert sub.status == Subscription.Status.FROZEN
        assert sub.expires_at is None

    def test_approve_rechecks_subscription_status(self, club, owner_user, trainer_user):
        ClubSettingsFactory(club=club)
        sub = SubscriptionFactory(
            club=club,
            status=Subscription.Status.ACTIVE,
            expires_at=timezone.now() - timedelta(days=1),
        )
        freeze = SubscriptionFreezeFactory(
            subscription=sub,
            frozen_by=trainer_user,
            status=SubscriptionFreeze.FreezeStatus.PENDING,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            approve_freeze(
                freeze_id=freeze.id,
                club_id=club.id,
                approved_by_id=owner_user.id,
            )

        assert exc_info.value.code == "invalid_subscription_status"
        freeze.refresh_from_db()
        sub.refresh_from_db()
        assert freeze.status == SubscriptionFreeze.FreezeStatus.PENDING
        assert sub.status != Subscription.Status.FROZEN

    def test_approve_rechecks_freeze_count_limit(self, club, owner_user, trainer_user):
        ClubSettingsFactory(club=club, freeze_max_count=1)
        sub = SubscriptionFactory(club=club, status=Subscription.Status.ACTIVE)
        SubscriptionFreezeFactory(
            subscription=sub,
            frozen_by=owner_user,
            status=SubscriptionFreeze.FreezeStatus.APPROVED,
            ends_at=timezone.now(),
        )
        freeze = SubscriptionFreezeFactory(
            subscription=sub,
            frozen_by=trainer_user,
            status=SubscriptionFreeze.FreezeStatus.PENDING,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            approve_freeze(
                freeze_id=freeze.id,
                club_id=club.id,
                approved_by_id=owner_user.id,
            )

        assert exc_info.value.code == "freeze_count_exceeded"
        freeze.refresh_from_db()
        sub.refresh_from_db()
        assert freeze.status == SubscriptionFreeze.FreezeStatus.PENDING
        assert sub.status == Subscription.Status.ACTIVE

    def test_reject_sets_actor_timestamp_and_reason(self, club, owner_user, trainer_user):
        sub = SubscriptionFactory(club=club, status=Subscription.Status.ACTIVE)
        original_expires = sub.expires_at
        freeze = SubscriptionFreezeFactory(
            subscription=sub,
            frozen_by=trainer_user,
            status=SubscriptionFreeze.FreezeStatus.PENDING,
        )

        result = reject_freeze(
            freeze_id=freeze.id,
            club_id=club.id,
            rejected_by_id=owner_user.id,
            decision_reason="Недостаточно данных",
        )

        result.refresh_from_db()
        sub.refresh_from_db()
        assert result.status == SubscriptionFreeze.FreezeStatus.REJECTED
        assert result.rejected_by_id == owner_user.id
        assert result.approved_by_id is None
        assert result.decision_at is not None
        assert result.decision_reason == "Недостаточно данных"
        assert sub.status == Subscription.Status.ACTIVE
        assert sub.expires_at == original_expires

    def test_double_approve_or_reject_is_rejected(self, club, owner_user, admin_user, trainer_user):
        approved_freeze = SubscriptionFreezeFactory(
            subscription=SubscriptionFactory(club=club, status=Subscription.Status.ACTIVE),
            frozen_by=trainer_user,
            status=SubscriptionFreeze.FreezeStatus.PENDING,
        )
        approve_freeze(
            freeze_id=approved_freeze.id,
            club_id=club.id,
            approved_by_id=owner_user.id,
        )

        with pytest.raises(BusinessLogicError) as approve_exc:
            approve_freeze(
                freeze_id=approved_freeze.id,
                club_id=club.id,
                approved_by_id=admin_user.id,
            )

        assert approve_exc.value.code == "freeze_already_decided"

        rejected_freeze = SubscriptionFreezeFactory(
            subscription=SubscriptionFactory(club=club, status=Subscription.Status.ACTIVE),
            frozen_by=trainer_user,
            status=SubscriptionFreeze.FreezeStatus.PENDING,
        )
        reject_freeze(
            freeze_id=rejected_freeze.id,
            club_id=club.id,
            rejected_by_id=owner_user.id,
            decision_reason="duplicate",
        )

        with pytest.raises(BusinessLogicError) as reject_exc:
            reject_freeze(
                freeze_id=rejected_freeze.id,
                club_id=club.id,
                rejected_by_id=admin_user.id,
            )

        assert reject_exc.value.code == "freeze_already_decided"

    def test_foreign_club_cannot_decide_freeze(self, club, other_club, owner_user, trainer_user):
        freeze = SubscriptionFreezeFactory(
            subscription=SubscriptionFactory(club=other_club, status=Subscription.Status.ACTIVE),
            frozen_by=trainer_user,
            status=SubscriptionFreeze.FreezeStatus.PENDING,
        )

        with pytest.raises(SubscriptionFreeze.DoesNotExist):
            approve_freeze(
                freeze_id=freeze.id,
                club_id=club.id,
                approved_by_id=owner_user.id,
            )


@pytest.mark.django_db
class TestUnfreezeSubscription:
    def test_unfreeze_subscription(self, club, owner_user):
        ClubSettingsFactory(club=club)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        sub = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.FROZEN)
        original_expires = sub.expires_at

        # Create freeze that started 5 days ago, requested 10 days
        freeze = SubscriptionFreezeFactory(
            subscription=sub,
            days=10,
            frozen_by=owner_user,
            starts_at=timezone.now() - timedelta(days=5),
        )

        result = unfreeze_subscription(freeze_id=freeze.id, club_id=club.id)

        assert result.ends_at is not None
        assert result.days == 5  # actual days, not requested
        sub.refresh_from_db()
        assert sub.status == Subscription.Status.ACTIVE
        # expires_at should be reduced by (10-5)=5 days
        assert sub.expires_at == original_expires - timedelta(days=5)

    def test_unfreeze_unlimited_subscription_keeps_null_expiry(self, club, owner_user):
        ClubSettingsFactory(club=club)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        sub = SubscriptionFactory(
            tariff=tariff,
            student=student,
            status=Subscription.Status.FROZEN,
            expires_at=None,
        )
        freeze = SubscriptionFreezeFactory(
            subscription=sub,
            days=10,
            frozen_by=owner_user,
            starts_at=timezone.now() - timedelta(days=5),
        )

        result = unfreeze_subscription(freeze_id=freeze.id, club_id=club.id)

        assert result.ends_at is not None
        assert result.days == 5
        sub.refresh_from_db()
        assert sub.status == Subscription.Status.ACTIVE
        assert sub.expires_at is None

    def test_unfreeze_date_expired_subscription_stays_expired(self, club, owner_user):
        ClubSettingsFactory(club=club)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        sub = SubscriptionFactory(
            tariff=tariff,
            student=student,
            status=Subscription.Status.FROZEN,
            expires_at=timezone.now() - timedelta(days=1),
        )
        freeze = SubscriptionFreezeFactory(
            subscription=sub,
            days=10,
            frozen_by=owner_user,
            starts_at=timezone.now() - timedelta(days=5),
        )

        unfreeze_subscription(freeze_id=freeze.id, club_id=club.id)

        sub.refresh_from_db()
        assert sub.status == Subscription.Status.EXPIRED

    def test_unfreeze_depleted_subscription_stays_expired(self, club, owner_user):
        ClubSettingsFactory(club=club)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        sub = SubscriptionFactory(
            tariff=tariff,
            student=student,
            status=Subscription.Status.FROZEN,
            trainings_left=0,
            expires_at=timezone.now() + timedelta(days=10),
        )
        freeze = SubscriptionFreezeFactory(
            subscription=sub,
            days=10,
            frozen_by=owner_user,
            starts_at=timezone.now() - timedelta(days=5),
        )

        unfreeze_subscription(freeze_id=freeze.id, club_id=club.id)

        sub.refresh_from_db()
        assert sub.status == Subscription.Status.EXPIRED

    def test_unfreeze_already_ended(self, club, owner_user):
        ClubSettingsFactory(club=club)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        sub = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.ACTIVE)

        freeze = SubscriptionFreezeFactory(
            subscription=sub,
            days=5,
            frozen_by=owner_user,
            ends_at=timezone.now(),
        )

        with pytest.raises(BusinessLogicError, match="already ended"):
            unfreeze_subscription(freeze_id=freeze.id, club_id=club.id)

    def test_unfreeze_pending_freeze_is_rejected(self, club, owner_user):
        ClubSettingsFactory(club=club)
        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=tt)
        student = StudentFactory(club=club)
        sub = SubscriptionFactory(tariff=tariff, student=student, status=Subscription.Status.ACTIVE)
        original_expires = sub.expires_at
        freeze = SubscriptionFreezeFactory(
            subscription=sub,
            days=10,
            frozen_by=owner_user,
            status=SubscriptionFreeze.FreezeStatus.PENDING,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            unfreeze_subscription(freeze_id=freeze.id, club_id=club.id)

        sub.refresh_from_db()
        freeze.refresh_from_db()
        assert exc_info.value.code == "freeze_not_approved"
        assert sub.status == Subscription.Status.ACTIVE
        assert sub.expires_at == original_expires
        assert freeze.ends_at is None


@pytest.mark.django_db
class TestCrossTenantBillingRejection:
    def test_create_subscription_rejects_other_club_student(self):
        from apps.students.models import Student

        club_a = ClubFactory()
        club_b = ClubFactory()
        tt = TrainingTypeFactory(club=club_a)
        tariff = TariffFactory(training_type=tt)
        student_b = StudentFactory(club=club_b)

        with pytest.raises(Student.DoesNotExist):
            create_subscription(
                club_id=club_a.id,
                student_id=student_b.id,
                tariff_id=tariff.id,
            )

    @patch("django_q.tasks.schedule")
    @patch("django_q.tasks.async_task")
    def test_create_payment_rejects_other_club_student(self, mock_async, mock_schedule):
        from apps.students.models import Student

        club_a = ClubFactory()
        club_b = ClubFactory()
        tt = TrainingTypeFactory(club=club_a)
        tariff = TariffFactory(training_type=tt)
        student_b = StudentFactory(club=club_b)
        user = UserFactory()

        with pytest.raises(Student.DoesNotExist):
            create_payment(
                club_id=club_a.id,
                student_id=student_b.id,
                tariff_id=tariff.id,
                payment_method="cash",
                recorded_by_id=user.id,
            )


@pytest.mark.django_db
class TestClubSettingsDefaults:
    def test_club_settings_defaults(self, club):
        from apps.billing.services import get_or_create_club_settings

        settings = get_or_create_club_settings(club.id)
        assert settings.freeze_enabled is True
        assert settings.freeze_max_days == 30
        assert settings.freeze_max_count is None


@pytest.mark.django_db
def test_canonical_group_snapshot_uses_responsible_trainer_and_stays_immutable_for_pending_order(
    club,
    owner_user,
    settings,
):
    from apps.attendance.models import TrainingGroupRolloutState
    from apps.attendance.services.schedule import create_schedule
    from apps.attendance.services.training_groups import reassign_training_group_responsibility
    from apps.attendance.tests.factories import ScheduleFactory
    from apps.trainers.tests.factories import TrainerFactory

    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    settings.TRAINING_GROUP_NEW_WRITES_ENABLED = True
    target_start_date = date(2030, 1, 7)
    state, _ = TrainingGroupRolloutState.objects.for_club(club).get_or_create(
        club=club,
        defaults={"mode": TrainingGroupRolloutState.Mode.OFF},
    )
    update_training_group_rollout_state_for_test(
        TrainingGroupRolloutState.objects.for_club(club).filter(id=state.id),
        mode=TrainingGroupRolloutState.Mode.ACTIVE,
    )
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    location = LocationFactory(club=club)
    responsible_trainer = TrainerFactory(club=club)
    occurrence_trainer = TrainerFactory(club=club)
    later_responsible_trainer = TrainerFactory(club=club)
    anchor = create_schedule(
        club_id=club.id,
        day_of_week=target_start_date.weekday(),
        start_time=time(18, 0),
        end_time=time(19, 0),
        group_name="Canonical sale ownership",
        trainer_id=responsible_trainer.id,
        location_id=location.id,
        training_type_id=training_type.id,
    )
    group = anchor.training_group
    selected_slot = ScheduleFactory(
        club=club,
        training_group=group,
        trainer=occurrence_trainer,
        location=location,
        training_type=training_type,
        day_of_week=target_start_date.weekday(),
        is_active=True,
    )
    tariff = TariffFactory(club=club, training_type=training_type)
    student = StudentFactory(club=club, status=Student.Status.ACTIVE)

    order = create_bank_payment_order(
        club_id=club.id,
        student_id=student.id,
        tariff_id=tariff.id,
        source=BankPaymentOrder.Source.OWNER,
        created_by_id=owner_user.id,
        target_schedule_id=selected_slot.id,
        target_training_group_id=group.id,
        target_start_date=target_start_date,
    )
    pending = order.payment
    assert pending.target_group_name_snapshot == group.name
    assert pending.target_trainer_id_snapshot == occurrence_trainer.id
    assert pending.target_trainer_name_snapshot == (
        f"{occurrence_trainer.first_name} {occurrence_trainer.last_name}"
    )
    assert pending.seller_trainer_id == responsible_trainer.id
    assert pending.sale_trainer_id_snapshot == responsible_trainer.id
    assert pending.sale_attribution_source == "training_group_responsible_trainer"
    from apps.billing.schemas import PaymentOut
    from apps.billing.selectors import get_payment_by_id

    serialized_payment = get_payment_by_id(club=club, payment_id=pending.id)
    assert PaymentOut.resolve_sale_trainer_name_snapshot(serialized_payment) == str(
        responsible_trainer
    )

    reassign_training_group_responsibility(
        club_id=club.id,
        training_group_id=group.id,
        responsible_trainer_id=later_responsible_trainer.id,
    )
    verify_payment(
        payment_id=pending.id,
        club_id=club.id,
        verified_by_id=owner_user.id,
        action="confirm",
        allow_online=True,
    )
    pending.refresh_from_db()
    assert pending.seller_trainer_id == responsible_trainer.id
    assert pending.sale_trainer_id_snapshot == responsible_trainer.id

    renewal = create_bank_payment_order(
        club_id=club.id,
        student_id=student.id,
        tariff_id=tariff.id,
        source=BankPaymentOrder.Source.OWNER,
        created_by_id=owner_user.id,
        target_schedule_id=selected_slot.id,
        target_training_group_id=group.id,
        target_start_date=target_start_date,
    ).payment
    assert renewal.group_membership_action_snapshot == Payment.GroupMembershipActionSnapshot.RENEWAL
    assert renewal.seller_trainer_id == later_responsible_trainer.id
    assert renewal.sale_trainer_id_snapshot == later_responsible_trainer.id


@pytest.mark.django_db(transaction=True)
def test_postgresql_provider_approval_confirms_canonical_group_payment_after_reassignment_and_deactivation(
    club,
    owner_user,
    settings,
):
    from django.db import connection

    if connection.vendor != "postgresql":
        pytest.skip("requires PostgreSQL provider-payment lifecycle coverage")

    from apps.attendance.models import TrainingGroupRolloutState
    from apps.attendance.services.schedule import create_schedule
    from apps.attendance.services.training_groups import reassign_training_group_responsibility
    from apps.attendance.tests.factories import ScheduleFactory
    from apps.trainers.services import update_trainer
    from apps.trainers.tests.factories import TrainerFactory

    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    settings.TRAINING_GROUP_NEW_WRITES_ENABLED = True
    target_start_date = date(2030, 1, 7)
    state, _ = TrainingGroupRolloutState.objects.for_club(club).get_or_create(
        club=club,
        defaults={"mode": TrainingGroupRolloutState.Mode.OFF},
    )
    update_training_group_rollout_state_for_test(
        TrainingGroupRolloutState.objects.for_club(club).filter(id=state.id),
        mode=TrainingGroupRolloutState.Mode.ACTIVE,
    )
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    location = LocationFactory(club=club)
    original_trainer = TrainerFactory(club=club)
    replacement_trainer = TrainerFactory(club=club)
    anchor = create_schedule(
        club_id=club.id,
        day_of_week=target_start_date.weekday(),
        start_time=time(18, 0),
        end_time=time(19, 0),
        group_name="Immutable pending provider payment",
        trainer_id=original_trainer.id,
        location_id=location.id,
        training_type_id=training_type.id,
    )
    group = anchor.training_group
    selected_slot = ScheduleFactory(
        club=club,
        training_group=group,
        trainer=original_trainer,
        location=location,
        training_type=training_type,
        day_of_week=target_start_date.weekday(),
        is_active=True,
    )
    tariff = TariffFactory(club=club, training_type=training_type)
    student = StudentFactory(club=club, status=Student.Status.ACTIVE)
    order = create_bank_payment_order(
        club_id=club.id,
        student_id=student.id,
        tariff_id=tariff.id,
        source=BankPaymentOrder.Source.OWNER,
        created_by_id=owner_user.id,
        target_schedule_id=selected_slot.id,
        target_training_group_id=group.id,
        target_start_date=target_start_date,
    )
    pending = order.payment
    assert pending.target_trainer_id_snapshot == original_trainer.id
    assert pending.sale_trainer_id_snapshot == original_trainer.id

    reassign_training_group_responsibility(
        club_id=club.id,
        training_group_id=group.id,
        responsible_trainer_id=replacement_trainer.id,
    )
    update_trainer(
        trainer_id=original_trainer.id,
        club_id=club.id,
        is_active=False,
    )

    event = process_bank_payment_webhook(
        provider=BankPaymentOrder.Provider.MOCK,
        request_body=json.dumps(
            {
                "webhookType": "acquiringInternetPayment",
                "event_id": f"immutable-pending-provider-approval-{order.id}",
                "status": "APPROVED",
                "paymentLinkId": order.provider_payment_link_id,
                "operationId": f"immutable-pending-provider-operation-{order.id}",
                "amount": str(order.amount_snapshot),
                "paid_at": timezone.now().isoformat(),
            }
        ).encode(),
        headers={},
        request_id="immutable-pending-provider-approval",
    )

    event.refresh_from_db()
    order.refresh_from_db()
    pending.refresh_from_db()
    assert event.processing_status == "processed"
    assert order.status == BankPaymentOrder.Status.APPROVED
    assert pending.status == Payment.Status.CONFIRMED
    assert pending.target_trainer_id_snapshot == original_trainer.id
    assert pending.sale_trainer_id_snapshot == original_trainer.id
    assert pending.conversion_group_membership_id is not None


@pytest.mark.django_db
def test_canonical_group_payment_rejects_target_before_future_open_membership(
    club,
    owner_user,
    settings,
):
    from apps.attendance.models import TrainingGroupRolloutState
    from apps.attendance.services.schedule import create_schedule
    from apps.attendance.tests.factories import TrainingGroupMembershipFactory
    from apps.trainers.tests.factories import TrainerFactory

    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    settings.TRAINING_GROUP_NEW_WRITES_ENABLED = True
    target_start_date = date(2030, 1, 7)
    state, _ = TrainingGroupRolloutState.objects.for_club(club).get_or_create(
        club=club,
        defaults={"mode": TrainingGroupRolloutState.Mode.OFF},
    )
    update_training_group_rollout_state_for_test(
        TrainingGroupRolloutState.objects.for_club(club).filter(id=state.id),
        mode=TrainingGroupRolloutState.Mode.ACTIVE,
    )
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club)
    schedule = create_schedule(
        club_id=club.id,
        day_of_week=target_start_date.weekday(),
        start_time=time(18, 0),
        end_time=time(19, 0),
        group_name="Future membership boundary",
        trainer_id=trainer.id,
        location_id=location.id,
        training_type_id=training_type.id,
    )
    tariff = TariffFactory(club=club, training_type=training_type)
    student = StudentFactory(club=club, status=Student.Status.ACTIVE)
    TrainingGroupMembershipFactory(
        club=club,
        student=student,
        training_group=schedule.training_group,
        starts_on=target_start_date + timedelta(days=7),
    )

    payment_count = Payment.objects.for_club(club).count()
    order_count = BankPaymentOrder.objects.for_club(club).count()

    with pytest.raises(BusinessLogicError) as exc_info:
        create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.OWNER,
            created_by_id=owner_user.id,
            target_schedule_id=schedule.id,
            target_training_group_id=schedule.training_group_id,
            target_start_date=target_start_date,
        )

    assert exc_info.value.code == "training_group_membership_exists"
    assert Payment.objects.for_club(club).count() == payment_count
    assert BankPaymentOrder.objects.for_club(club).count() == order_count


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="requires PostgreSQL row-lock semantics; run in the PostgreSQL S5 suite",
)
def test_group_payment_lock_order_contract(club, owner_user, settings, monkeypatch):
    """Freeze the canonical group-payment lock order before extracting its owner."""
    from django.db.models import QuerySet
    from django.test.utils import CaptureQueriesContext

    from apps.attendance.models import (
        Schedule,
        TrainingGroup,
        TrainingGroupMembership,
        TrainingGroupRolloutState,
    )
    from apps.attendance.services.schedule import create_schedule
    from apps.trainers.tests.factories import TrainerFactory

    _enable_legacy_manual_group_admission(
        club=club,
        settings=settings,
        manual_admission_enabled=True,
    )
    settings.TRAINING_GROUP_NEW_WRITES_ENABLED = True
    state, _ = TrainingGroupRolloutState.objects.for_club(club).get_or_create(
        club=club,
        defaults={"mode": TrainingGroupRolloutState.Mode.OFF},
    )
    update_training_group_rollout_state_for_test(
        TrainingGroupRolloutState.objects.for_club(club).filter(id=state.id),
        mode=TrainingGroupRolloutState.Mode.ACTIVE,
    )
    target_start_date = date(2030, 1, 7)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club)
    schedule = create_schedule(
        club_id=club.id,
        day_of_week=target_start_date.weekday(),
        start_time=time(18, 0),
        end_time=time(19, 0),
        group_name="S5 lock-order group",
        trainer_id=trainer.id,
        location_id=location.id,
        training_type_id=training_type.id,
    )
    tariff = TariffFactory(club=club, training_type=training_type)
    student = StudentFactory(club=club, status=Student.Status.ACTIVE)

    lock_calls: list[str] = []
    original_select_for_update = QuerySet.select_for_update
    tracked_models = {
        Club,
        TrainingGroupRolloutState,
        Student,
        Schedule,
        TrainingGroup,
        TrainingGroupMembership,
        Payment,
        ScheduleEnrollment,
    }

    def recording_select_for_update(queryset, *args, **kwargs):
        if queryset.model in tracked_models:
            lock_calls.append(queryset.model.__name__)
        return original_select_for_update(queryset, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", recording_select_for_update)

    with patch("django_q.tasks.async_task"), CaptureQueriesContext(connection) as queries:
        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=schedule.id,
            target_training_group_id=schedule.training_group_id,
            target_start_date=target_start_date,
            create_manual_operational_admission=True,
        )

    expected_lock_order = [
        Club,
        TrainingGroupRolloutState,
        Student,
        Schedule,
        TrainingGroup,
        TrainingGroupMembership,
        Payment,
        ScheduleEnrollment,
    ]
    expected_lock_names = [model.__name__ for model in expected_lock_order]
    assert [lock_calls.index(name) for name in expected_lock_names] == sorted(
        lock_calls.index(name) for name in expected_lock_names
    )

    sql = [query["sql"].upper() for query in queries.captured_queries]
    transaction_begin = sql.index("BEGIN")
    transaction_commit = next(
        index
        for index, statement in enumerate(sql[transaction_begin + 1 :], start=transaction_begin + 1)
        if statement == "COMMIT"
    )
    transaction_sql = sql[transaction_begin : transaction_commit + 1]
    locked_sql = [statement for statement in transaction_sql if "FOR UPDATE" in statement]
    table_order = [f'"{model._meta.db_table.upper()}"' for model in expected_lock_order]
    first_lock_indexes = [
        next(index for index, statement in enumerate(locked_sql) if table_name in statement)
        for table_name in table_order
    ]
    assert first_lock_indexes == sorted(first_lock_indexes)

    payment_table = f'"{Payment._meta.db_table.upper()}"'
    membership_table = f'"{TrainingGroupMembership._meta.db_table.upper()}"'
    projection_table = f'"{ScheduleEnrollment._meta.db_table.upper()}"'
    first_payment_insert = next(
        index
        for index, statement in enumerate(transaction_sql)
        if statement.startswith(f"INSERT INTO {payment_table}")
    )
    assert all(
        next(
            index
            for index, statement in enumerate(transaction_sql)
            if table_name in statement and "FOR UPDATE" in statement
        )
        < first_payment_insert
        for table_name in table_order
    )
    membership_insert = next(
        index
        for index, statement in enumerate(transaction_sql)
        if statement.startswith(f"INSERT INTO {membership_table}")
    )
    projection_insert = next(
        index
        for index, statement in enumerate(transaction_sql)
        if statement.startswith(f"INSERT INTO {projection_table}")
    )
    assert first_payment_insert < membership_insert < projection_insert

    payment.refresh_from_db()
    assert payment.conversion_group_membership_id is not None
    assert payment.conversion_enrollment_id is not None


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="requires PostgreSQL row-lock semantics; run in the PostgreSQL S8 suite",
)
def test_verify_payment_lock_order_contract(club, owner_user, settings, monkeypatch):
    """Freeze confirmation lock order before extracting payment review."""
    from django.db.models import QuerySet
    from django.test.utils import CaptureQueriesContext

    from apps.attendance.models import (
        TrainingGroupMembership,
        TrainingGroupRolloutState,
    )
    from apps.attendance.services.schedule import create_schedule
    from apps.trainers.tests.factories import TrainerFactory

    _enable_legacy_manual_group_admission(
        club=club,
        settings=settings,
        manual_admission_enabled=True,
    )
    settings.TRAINING_GROUP_NEW_WRITES_ENABLED = True
    state, _ = TrainingGroupRolloutState.objects.for_club(club).get_or_create(
        club=club,
        defaults={"mode": TrainingGroupRolloutState.Mode.OFF},
    )
    update_training_group_rollout_state_for_test(
        TrainingGroupRolloutState.objects.for_club(club).filter(id=state.id),
        mode=TrainingGroupRolloutState.Mode.ACTIVE,
    )
    target_start_date = date(2030, 1, 7)
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club)
    schedule = create_schedule(
        club_id=club.id,
        day_of_week=target_start_date.weekday(),
        start_time=time(18, 0),
        end_time=time(19, 0),
        group_name="S8 lock-order group",
        trainer_id=trainer.id,
        location_id=location.id,
        training_type_id=training_type.id,
    )
    tariff = TariffFactory(club=club, training_type=training_type)
    student = StudentFactory(club=club, status=Student.Status.ACTIVE)
    with patch("django_q.tasks.async_task"):
        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method=Payment.Method.CASH,
            recorded_by_id=owner_user.id,
            target_schedule_id=schedule.id,
            target_training_group_id=schedule.training_group_id,
            target_start_date=target_start_date,
            create_manual_operational_admission=True,
        )

    lock_calls: list[str] = []
    original_select_for_update = QuerySet.select_for_update
    tracked_models = {
        Club,
        TrainingGroupRolloutState,
        Payment,
        TrainingGroupMembership,
        ScheduleEnrollment,
        Debt,
        SubscriptionComponent,
    }

    def recording_select_for_update(queryset, *args, **kwargs):
        if queryset.model in tracked_models:
            lock_calls.append(queryset.model.__name__)
        return original_select_for_update(queryset, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", recording_select_for_update)

    with patch("django_q.tasks.async_task"), CaptureQueriesContext(connection) as queries:
        confirmed = verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )

    expected_lock_order = [
        Club,
        TrainingGroupRolloutState,
        Payment,
        TrainingGroupMembership,
        ScheduleEnrollment,
        Debt,
        SubscriptionComponent,
    ]
    expected_lock_names = [model.__name__ for model in expected_lock_order]
    assert [lock_calls.index(name) for name in expected_lock_names] == sorted(
        lock_calls.index(name) for name in expected_lock_names
    )

    sql = [query["sql"].upper() for query in queries.captured_queries]
    transaction_begin = sql.index("BEGIN")
    transaction_commit = next(
        index
        for index, statement in enumerate(sql[transaction_begin + 1 :], start=transaction_begin + 1)
        if statement == "COMMIT"
    )
    transaction_sql = sql[transaction_begin : transaction_commit + 1]
    locked_sql = [statement for statement in transaction_sql if "FOR UPDATE" in statement]
    table_order = [f'"{model._meta.db_table.upper()}"' for model in expected_lock_order]
    first_lock_indexes = [
        next(index for index, statement in enumerate(locked_sql) if table_name in statement)
        for table_name in table_order
    ]
    assert first_lock_indexes == sorted(first_lock_indexes)
    assert confirmed.status == Payment.Status.CONFIRMED


@pytest.mark.django_db(transaction=True)
def test_sale_earning_enqueue_runs_only_after_outer_commit(club):
    from apps.billing.service_modules import sale_earnings

    with patch("django_q.tasks.async_task") as mock_async:
        with transaction.atomic():
            sale_earnings._enqueue_sale_earning_after_commit(
                payment_id=987654,
                club_id=club.id,
            )
            mock_async.assert_not_called()

        mock_async.assert_called_once_with(
            "apps.billing.tasks.create_sale_earning",
            987654,
            club_id=club.id,
        )
