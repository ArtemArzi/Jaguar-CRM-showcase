from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.attendance.tests.factories import CheckinFactory, ScheduleFactory
from apps.billing.models import BankPaymentOrder, Payment
from apps.billing.schemas import SubscriptionOut
from apps.billing.selectors import (
    export_debtors_excel,
    get_club_subscriptions,
    get_debtors,
    get_manual_payment_review_queue,
    get_online_payment_review_queue,
    get_payment_history,
)
from apps.billing.tests.factories import (
    DebtFactory,
    PaymentFactory,
    SubscriptionFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.clubs.tests.factories import ClubFactory, LocationFactory
from apps.clubs.timezones import club_local_day_start, club_localdate
from apps.students.tests.factories import StudentFactory
from apps.trainers.tests.factories import TrainerFactory


@pytest.mark.django_db
class TestGetClubSubscriptions:
    def test_returns_subscription_paid_amount_snapshot(self, club):
        training_type = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=training_type, price=Decimal("5000.00"))
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            paid_amount=Decimal("5000.00"),
        )
        PaymentFactory(
            club=club,
            student=student,
            tariff=tariff,
            subscription=subscription,
            amount=Decimal("4500.00"),
            original_amount=Decimal("5000.00"),
            status=Payment.Status.CONFIRMED,
        )
        tariff.price = Decimal("7000.00")
        tariff.save(update_fields=["price"])

        result = get_club_subscriptions(club=club).get(id=subscription.id)

        assert result.paid_amount == Decimal("5000.00")

    def test_schema_falls_back_to_tariff_for_legacy_rows_without_snapshot(self, club):
        training_type = TrainingTypeFactory(club=club)
        tariff = TariffFactory(training_type=training_type, price=Decimal("7000.00"))
        student = StudentFactory(club=club)
        subscription = SubscriptionFactory(club=club, student=student, tariff=tariff)

        result = get_club_subscriptions(club=club).get(id=subscription.id)

        assert result.paid_amount is None
        assert SubscriptionOut.resolve_paid_amount(result) == Decimal("7000.00")


@pytest.mark.django_db
class TestFinanceWorkspaceQueues:
    @staticmethod
    def _manual_review_order(*, club, student, tariff, recorder):
        subscription = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status="pending",
        )
        payment = PaymentFactory(
            club=club,
            student=student,
            tariff=tariff,
            subscription=subscription,
            payment_method=Payment.Method.ONLINE,
            status=Payment.Status.PENDING,
            recorded_by=recorder,
        )
        return BankPaymentOrder.objects.create(
            club=club,
            payment=payment,
            subscription=subscription,
            student=student,
            provider=BankPaymentOrder.Provider.MOCK,
            source=BankPaymentOrder.Source.OWNER,
            status=BankPaymentOrder.Status.MANUAL_REVIEW,
            amount_snapshot=payment.amount,
            purpose_snapshot="Provider exception",
            expires_at=timezone.now() + timedelta(hours=1),
            created_by=recorder,
        )

    def test_manual_and_online_action_queues_are_disjoint(self, club, owner_user):
        student = StudentFactory(club=club)
        tariff = TariffFactory(club=club)
        manual = PaymentFactory(
            club=club,
            student=student,
            tariff=tariff,
            payment_method=Payment.Method.CASH,
            status=Payment.Status.PENDING,
            recorded_by=owner_user,
        )
        online_order = self._manual_review_order(
            club=club,
            student=student,
            tariff=tariff,
            recorder=owner_user,
        )
        confirmed_online = PaymentFactory(
            club=club,
            student=student,
            tariff=tariff,
            payment_method=Payment.Method.ONLINE,
            status=Payment.Status.CONFIRMED,
            recorded_by=owner_user,
        )

        manual_ids = list(get_manual_payment_review_queue(club=club).values_list("id", flat=True))
        review_ids = list(get_online_payment_review_queue(club=club).values_list("id", flat=True))
        history_ids = list(get_payment_history(club=club).values_list("id", flat=True))

        assert manual_ids == [manual.id]
        assert review_ids == [online_order.id]
        assert confirmed_online.id in history_ids
        assert confirmed_online.id not in manual_ids

    def test_filters_bind_trainer_method_context_and_local_date(self, club, owner_user):
        trainer = TrainerFactory(club=club)
        other_trainer = TrainerFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind="group")
        tariff = TariffFactory(club=club, training_type=training_type)
        student = StudentFactory(club=club)
        matching = PaymentFactory(
            club=club,
            student=student,
            tariff=tariff,
            seller_trainer=trainer,
            target_training_group_id=None,
            target_schedule=ScheduleFactory(club=club, trainer=trainer, training_type=training_type),
            payment_method=Payment.Method.TRANSFER,
            status=Payment.Status.PENDING,
            recorded_by=owner_user,
        )
        PaymentFactory(
            club=club,
            student=student,
            tariff=tariff,
            seller_trainer=other_trainer,
            payment_method=Payment.Method.CASH,
            status=Payment.Status.PENDING,
            recorded_by=owner_user,
        )
        today = timezone.localdate()

        result = get_manual_payment_review_queue(
            club=club,
            trainer_id=trainer.id,
            payment_method=Payment.Method.TRANSFER,
            context="group",
            date_from=today,
            date_to=today,
        )

        assert list(result.values_list("id", flat=True)) == [matching.id]

    def test_date_filters_support_open_bounds_and_normalize_reversed_range(self, club, owner_user):
        tariff = TariffFactory(club=club)
        today = club_localdate(club)

        def payment_on(target_date: date):
            payment = PaymentFactory(
                club=club,
                tariff=tariff,
                payment_method=Payment.Method.CASH,
                status=Payment.Status.PENDING,
                recorded_by=owner_user,
            )
            Payment.objects.filter(id=payment.id).update(
                created_at=club_local_day_start(club, target_date) + timedelta(hours=12),
            )
            return payment

        oldest = payment_on(today - timedelta(days=4))
        middle = payment_on(today - timedelta(days=2))
        newest = payment_on(today)

        from_ids = set(
            get_manual_payment_review_queue(
                club=club,
                date_from=today - timedelta(days=2),
            ).values_list("id", flat=True)
        )
        to_ids = set(
            get_manual_payment_review_queue(
                club=club,
                date_to=today - timedelta(days=2),
            ).values_list("id", flat=True)
        )
        reversed_ids = set(
            get_manual_payment_review_queue(
                club=club,
                date_from=today,
                date_to=today - timedelta(days=4),
            ).values_list("id", flat=True)
        )

        assert from_ids == {middle.id, newest.id}
        assert to_ids == {oldest.id, middle.id}
        assert reversed_ids == {oldest.id, middle.id, newest.id}

    def test_context_filters_keep_renewal_disjoint_from_group(self, club, owner_user):
        training_type = TrainingTypeFactory(club=club, kind="group")
        tariff = TariffFactory(club=club, training_type=training_type)
        schedule = ScheduleFactory(club=club, training_type=training_type)
        source = SubscriptionFactory(club=club, tariff=tariff)
        renewal_subscription = SubscriptionFactory(
            club=club,
            tariff=tariff,
            renewed_from=source,
        )
        renewal = PaymentFactory(
            club=club,
            tariff=tariff,
            subscription=renewal_subscription,
            target_schedule=schedule,
            payment_method=Payment.Method.CASH,
            status=Payment.Status.PENDING,
            recorded_by=owner_user,
        )
        group = PaymentFactory(
            club=club,
            tariff=tariff,
            target_schedule=schedule,
            payment_method=Payment.Method.CASH,
            status=Payment.Status.PENDING,
            recorded_by=owner_user,
        )

        group_ids = set(
            get_manual_payment_review_queue(club=club, context="group").values_list(
                "id", flat=True
            )
        )
        renewal_ids = set(
            get_manual_payment_review_queue(club=club, context="renewal").values_list(
                "id", flat=True
            )
        )

        assert group_ids == {group.id}
        assert renewal_ids == {renewal.id}

    def test_queues_are_tenant_scoped(self, club, other_club, owner_user):
        local_payment = PaymentFactory(
            club=club,
            tariff=TariffFactory(club=club),
            payment_method=Payment.Method.CASH,
            status=Payment.Status.PENDING,
            recorded_by=owner_user,
        )
        PaymentFactory(
            club=other_club,
            tariff=TariffFactory(club=other_club),
            payment_method=Payment.Method.CASH,
            status=Payment.Status.PENDING,
            recorded_by=owner_user,
        )

        assert list(
            get_manual_payment_review_queue(club=club).values_list("id", flat=True)
        ) == [local_payment.id]

        local_order = self._manual_review_order(
            club=club,
            student=StudentFactory(club=club),
            tariff=TariffFactory(club=club),
            recorder=owner_user,
        )
        self._manual_review_order(
            club=other_club,
            student=StudentFactory(club=other_club),
            tariff=TariffFactory(club=other_club),
            recorder=owner_user,
        )
        local_history = PaymentFactory(
            club=club,
            tariff=TariffFactory(club=club),
            status=Payment.Status.CONFIRMED,
            payment_method=Payment.Method.ONLINE,
            recorded_by=owner_user,
        )
        PaymentFactory(
            club=other_club,
            tariff=TariffFactory(club=other_club),
            status=Payment.Status.CONFIRMED,
            payment_method=Payment.Method.ONLINE,
            recorded_by=owner_user,
        )

        assert list(
            get_online_payment_review_queue(club=club).values_list("id", flat=True)
        ) == [local_order.id]
        assert set(get_payment_history(club=club).values_list("id", flat=True)) == {
            local_history.id,
        }


@pytest.mark.django_db
class TestGetDebtors:
    def test_get_debtors_basic(self, club):
        student = StudentFactory(club=club)
        checkin = CheckinFactory(club=club, student=student)
        DebtFactory(club=club, student=student, checkin=checkin)

        # Resolved debt should not appear
        checkin2 = CheckinFactory(club=club, student=student)
        DebtFactory(club=club, student=student, checkin=checkin2, resolved_at=timezone.now())

        result = get_debtors(club=club)
        assert result.count() == 1

    def test_get_debtors_hides_debts_reserved_by_pending_payment(self, club):
        student = StudentFactory(club=club)
        checkin = CheckinFactory(club=club, student=student)
        payment = PaymentFactory(club=club, student=student, status=Payment.Status.PENDING)
        DebtFactory(club=club, student=student, checkin=checkin, settlement_payment=payment)

        result = get_debtors(club=club)

        assert result.count() == 0

    def test_get_debtors_filter_by_trainer(self, club):
        trainer_a = TrainerFactory(club=club)
        trainer_b = TrainerFactory(club=club)
        schedule_a = ScheduleFactory(club=club, trainer=trainer_a)
        schedule_b = ScheduleFactory(club=club, trainer=trainer_b)

        student = StudentFactory(club=club)
        checkin_a = CheckinFactory(club=club, student=student, schedule=schedule_a, trainer=trainer_a)
        checkin_b = CheckinFactory(club=club, student=student, schedule=schedule_b, trainer=trainer_b)
        DebtFactory(club=club, student=student, checkin=checkin_a)
        DebtFactory(club=club, student=student, checkin=checkin_b)

        result = get_debtors(club=club, trainer_id=trainer_a.id)
        assert result.count() == 1

    def test_get_debtors_filter_by_location(self, club):
        location_a = LocationFactory(club=club)
        location_b = LocationFactory(club=club)
        schedule_a = ScheduleFactory(club=club, location=location_a)
        schedule_b = ScheduleFactory(club=club, location=location_b)

        student = StudentFactory(club=club)
        checkin_a = CheckinFactory(club=club, student=student, schedule=schedule_a, location=location_a)
        checkin_b = CheckinFactory(club=club, student=student, schedule=schedule_b, location=location_b)
        DebtFactory(club=club, student=student, checkin=checkin_a)
        DebtFactory(club=club, student=student, checkin=checkin_b)

        result = get_debtors(club=club, location_id=location_a.id)
        assert result.count() == 1

    def test_get_debtors_filter_by_student_status(self, club):
        student_active = StudentFactory(club=club, status="active")
        student_lead = StudentFactory(club=club, status="lead")

        checkin_a = CheckinFactory(club=club, student=student_active)
        checkin_l = CheckinFactory(club=club, student=student_lead)
        DebtFactory(club=club, student=student_active, checkin=checkin_a)
        DebtFactory(club=club, student=student_lead, checkin=checkin_l)

        result = get_debtors(club=club, student_status="active")
        assert result.count() == 1

    def test_get_debtors_filter_by_period(self, club):
        student = StudentFactory(club=club)
        checkin = CheckinFactory(club=club, student=student)
        DebtFactory(club=club, student=student, checkin=checkin)

        today = date.today()
        result = get_debtors(club=club, date_from=today, date_to=today)
        assert result.count() == 1

        # Future date range should return nothing
        future = today + timedelta(days=30)
        result = get_debtors(club=club, date_from=future, date_to=future)
        assert result.count() == 0

    def test_get_debtors_filter_by_group(self, club):
        schedule_a = ScheduleFactory(club=club)
        schedule_b = ScheduleFactory(club=club)

        student = StudentFactory(club=club)
        checkin_a = CheckinFactory(club=club, student=student, schedule=schedule_a)
        checkin_b = CheckinFactory(club=club, student=student, schedule=schedule_b)
        DebtFactory(club=club, student=student, checkin=checkin_a)
        DebtFactory(club=club, student=student, checkin=checkin_b)

        result = get_debtors(club=club, group_id=schedule_a.id)
        assert result.count() == 1

    def test_get_debtors_tenant_isolation(self, club):
        club_b = ClubFactory()
        student_a = StudentFactory(club=club)
        student_b = StudentFactory(club=club_b)

        checkin_a = CheckinFactory(club=club, student=student_a)
        checkin_b = CheckinFactory(club=club_b, student=student_b)
        DebtFactory(club=club, student=student_a, checkin=checkin_a)
        DebtFactory(club=club_b, student=student_b, checkin=checkin_b)

        result = get_debtors(club=club)
        assert result.count() == 1


@pytest.mark.django_db
class TestExportDebtorsExcel:
    def test_export_debtors_excel(self, club):
        student = StudentFactory(club=club)
        checkin = CheckinFactory(club=club, student=student)
        DebtFactory(club=club, student=student, checkin=checkin, tariff_price=5000)

        data = export_debtors_excel(club=club)
        assert isinstance(data, bytes)
        assert len(data) > 0
        # XLSX files start with PK signature
        assert data[:2] == b"PK"
