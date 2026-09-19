from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.billing.models import Subscription, TrainingType
from apps.billing.services import create_payment, verify_payment
from apps.billing.tests.factories import TariffFactory, TrainingTypeFactory
from apps.clubs.tests.factories import ClubFactory, UserFactory
from apps.students.tests.factories import StudentFactory


@pytest.fixture
def club(db):
    return ClubFactory()


@pytest.fixture
def tariff(club):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    return TariffFactory(
        club=club,
        training_type=training_type,
        duration_days=30,
        trainings_limit=8,
    )


@pytest.fixture
def student(club):
    return StudentFactory(club=club)


@pytest.fixture
def owner(club):
    return UserFactory()


@pytest.mark.django_db
class TestPaymentSubscriptionTimer:
    @patch("django_q.tasks.schedule")
    @patch("django_q.tasks.async_task")
    def test_payment_subscription_expires_at_none_before_confirm(
        self, mock_async, mock_schedule, club, tariff, student, owner
    ):
        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method="cash",
            recorded_by_id=owner.id,
        )
        payment.subscription.refresh_from_db()
        assert payment.subscription.expires_at is None
        assert payment.subscription.status == Subscription.Status.PENDING

    @patch("django_q.tasks.schedule")
    @patch("django_q.tasks.async_task")
    def test_payment_subscription_expires_at_set_on_confirm(
        self, mock_async, mock_schedule, club, tariff, student, owner
    ):
        payment = create_payment(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            payment_method="cash",
            recorded_by_id=owner.id,
        )

        before = timezone.now()
        verified = verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner.id,
            action="confirm",
        )
        after = timezone.now()

        verified.subscription.refresh_from_db()
        assert verified.subscription.status == Subscription.Status.ACTIVE
        assert verified.subscription.expires_at is not None

        expected_min = before + timedelta(days=tariff.duration_days)
        expected_max = after + timedelta(days=tariff.duration_days)
        assert expected_min <= verified.subscription.expires_at <= expected_max
