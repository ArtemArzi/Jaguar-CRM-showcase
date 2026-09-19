from datetime import date, time
from decimal import Decimal
from unittest.mock import patch

import pytest
from ninja.testing import TestClient

from apps.attendance.models import ScheduleEnrollment
from apps.attendance.tests.factories import (
    CheckinFactory,
    ScheduleExceptionFactory,
    ScheduleFactory,
)
from apps.billing.models import (
    BankPaymentOrder,
    BankPaymentReconciliationAttempt,
    Payment,
    Subscription,
    SubscriptionFreeze,
    TrainingType,
)
from apps.billing.services import create_bank_payment_order
from apps.billing.tests.factories import (
    DebtFactory,
    SubscriptionFactory,
    SubscriptionFreezeFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.clubs.tests.factories import ClubFactory, ClubSettingsFactory
from apps.common.tests.helpers import make_auth_params as _auth_params
from apps.feedback.tests.factories import FeedbackFormFactory, FeedbackQuestionFactory, FeedbackResponseFactory
from apps.students.tests.factories import StudentFactory
from apps.trainers.tests.factories import TrainerFactory
from config.api import api

client = TestClient(api)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _bypass_jwt_auth(bypass_jwt_auth):
    pass


class TestStudentMeEndpoint:
    def test_student_gets_own_profile(self, club, student_user):
        student = StudentFactory(club=club, user=student_user)
        response = client.get(
            "/students/me/",
            **_auth_params(student_user, club, role="student"),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == student.id
        assert data["first_name"] == student.first_name
        assert data["phone"] == student.phone
        assert data["email"] == student.email
        assert data["status"] == student.status
        assert data["is_child"] == student.is_child
        expected_dob = student.date_of_birth.isoformat() if student.date_of_birth else None
        assert data["date_of_birth"] == expected_dob

    def test_student_cannot_see_other_club(self, club, student_user):
        other_club = ClubFactory(name="Other Club")
        StudentFactory(club=other_club, user=student_user)
        # student_user has membership in `club` but Student record is in other_club
        response = client.get(
            "/students/me/",
            **_auth_params(student_user, club, role="student"),
        )
        # get_student_by_user raises DoesNotExist -> 404 via global handler
        assert response.status_code == 404

    def test_non_student_role_denied(self, club, parent_user):
        response = client.get(
            "/students/me/",
            **_auth_params(parent_user, club, role="parent"),
        )
        assert response.status_code == 403


class TestStudentSubscriptions:
    def test_returns_own_subscriptions(self, club, student_user):
        student = StudentFactory(club=club, user=student_user)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=12)
        SubscriptionFactory(club=club, student=student, tariff=tariff, trainings_used=4)
        response = client.get(
            "/students/me/subscriptions/",
            **_auth_params(student_user, club, role="student"),
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1
        assert data[0]["trainings_used"] == 4
        assert data[0]["trainings_total"] == 12

    def test_pending_subscription_with_null_expires_at_serializes(self, club, student_user):
        student = StudentFactory(club=club, user=student_user)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=12)
        SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.PENDING,
            expires_at=None,
        )

        response = client.get(
            "/students/me/subscriptions/",
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 200
        data = response.json()
        assert data[0]["status"] == Subscription.Status.PENDING
        assert data[0]["expires_at"] is None

    def test_pending_and_frozen_subscription_statuses_are_preserved(self, club, student_user):
        student = StudentFactory(club=club, user=student_user)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        pending_tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=12)
        frozen_tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=12)
        SubscriptionFactory(
            club=club,
            student=student,
            tariff=pending_tariff,
            status=Subscription.Status.PENDING,
            expires_at=None,
        )
        SubscriptionFactory(
            club=club,
            student=student,
            tariff=frozen_tariff,
            status=Subscription.Status.FROZEN,
        )

        response = client.get(
            "/students/me/subscriptions/",
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 200
        statuses = {item["status"] for item in response.json()}
        assert Subscription.Status.PENDING in statuses
        assert Subscription.Status.FROZEN in statuses

    def test_subscription_exposes_pending_freeze_status(self, club, student_user):
        student = StudentFactory(club=club, user=student_user)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=12)
        subscription = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
        )
        SubscriptionFreezeFactory(
            subscription=subscription,
            status=SubscriptionFreeze.FreezeStatus.PENDING,
        )

        response = client.get(
            "/students/me/subscriptions/",
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 200
        data = response.json()
        assert data[0]["freeze_status"] == SubscriptionFreeze.FreezeStatus.PENDING


class TestStudentDebts:
    def test_student_sees_own_open_debts(self, club, student_user):
        student = StudentFactory(club=club, user=student_user)
        training_type = TrainingTypeFactory(club=club, name="Muay Thai")
        schedule = ScheduleFactory(club=club, training_type=training_type)
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            is_debt=True,
        )
        DebtFactory(
            club=club,
            student=student,
            checkin=checkin,
            tariff_price=Decimal("1000"),
        )
        resolved_checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            is_debt=True,
            date=date(2026, 3, 2),
        )
        DebtFactory(
            club=club,
            student=student,
            checkin=resolved_checkin,
            tariff_price=Decimal("500"),
            resolved_at=checkin.created_at,
        )

        response = client.get(
            "/students/me/debts/",
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["checkin_id"] == checkin.id
        assert Decimal(data[0]["tariff_price"]) == Decimal("1000")
        assert data[0]["reason"] == "no_subscription"
        assert data[0]["training_type_name"] == "Muay Thai"

    def test_student_debts_hide_reserved_payment_debts(self, settings, club, student_user, owner_user):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club, user=student_user)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=training_type, trainings_limit=8)
        SubscriptionFactory(club=club, student=student, tariff=tariff, status=Subscription.Status.ACTIVE)
        schedule = ScheduleFactory(club=club, training_type=training_type)
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            is_debt=True,
        )
        debt = DebtFactory(club=club, student=student, checkin=checkin)
        create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
            debt_ids=[debt.id],
        )

        response = client.get(
            "/students/me/debts/",
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 200
        assert response.json() == []


class TestStudentBankPaymentOrders:
    def test_exact_renewal_rejects_client_tariff_and_debt_terms(self, settings, club, student_user):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
        ClubSettingsFactory(club=club, unified_client_journey_enabled=True)
        student = StudentFactory(club=club, user=student_user)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=8)
        source = SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
        )
        debt = DebtFactory(
            club=club,
            student=student,
            checkin=CheckinFactory(club=club, student=student),
        )
        auth = _auth_params(student_user, club, role="student")

        tariff_only = client.post(
            "/students/me/bank-payment-orders/",
            json={"tariff_id": tariff.id},
            **auth,
        )
        assert tariff_only.status_code == 400
        assert tariff_only.json()["code"] == "renewal_source_required"

        rejected = client.post(
            "/students/me/bank-payment-orders/",
            json={
                "renewed_from_subscription_id": source.id,
                "idempotency_key": "student-exact-renewal-terms",
                "tariff_id": tariff.id,
                "debt_ids": [debt.id],
            },
            **auth,
        )
        assert rejected.status_code == 400
        assert rejected.json()["code"] == "renewal_client_terms_forbidden"

        payload = {
            "renewed_from_subscription_id": source.id,
            "idempotency_key": "student-exact-renewal-terms",
        }
        created = client.post("/students/me/bank-payment-orders/", json=payload, **auth)
        assert created.status_code == 201, created.json()
        assert created.json()["tariff_id"] == tariff.id
        assert created.json()["command_replayed"] is False
        replay = client.post("/students/me/bank-payment-orders/", json=payload, **auth)
        assert replay.status_code == 200, replay.json()
        assert replay.json()["id"] == created.json()["id"]
        assert replay.json()["command_replayed"] is True

    def test_student_can_create_own_renewal_bank_payment_order(self, settings, club, student_user):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club, user=student_user)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=training_type, trainings_limit=8)
        SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
        )

        response = client.post(
            "/students/me/bank-payment-orders/",
            json={"tariff_id": tariff.id},
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 201, response.json()
        data = response.json()
        order = BankPaymentOrder.objects.for_club(club).get(id=data["id"])
        assert data["source"] == BankPaymentOrder.Source.STUDENT
        assert order.student_id == student.id
        assert order.payment.payment_method == Payment.Method.ONLINE
        assert order.payment.status == Payment.Status.PENDING

    def test_student_can_list_own_live_bank_payment_order_from_trainer_source_without_cancel(
        self,
        settings,
        club,
        student_user,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club, user=student_user)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=training_type, trainings_limit=8)
        SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
        )
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
        )
        order.status = BankPaymentOrder.Status.AUTHORIZED
        order.save(update_fields=["status", "updated_at"])

        response = client.get(
            "/students/me/bank-payment-orders/?status=live",
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 200
        data = response.json()
        assert [item["id"] for item in data] == [order.id]
        assert data[0]["source"] == BankPaymentOrder.Source.TRAINER
        assert data[0]["status"] == BankPaymentOrder.Status.AUTHORIZED
        assert data[0]["can_cancel"] is False

    def test_student_exact_order_is_self_scoped_and_exposes_self_service_actions(
        self,
        settings,
        club,
        student_user,
        owner_user,
        parent_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        settings.MOCK_PAYMENT_BASE_URL = "https://pay.example.test"
        student = StudentFactory(club=club, user=student_user)
        other_student = StudentFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=training_type, trainings_limit=8)
        SubscriptionFactory(club=club, student=student, tariff=tariff, status=Subscription.Status.ACTIVE)
        SubscriptionFactory(club=club, student=other_student, tariff=tariff, status=Subscription.Status.ACTIVE)
        own_order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
        )
        other_order = create_bank_payment_order(
            club_id=club.id,
            student_id=other_student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.TRAINER,
            created_by_id=owner_user.id,
        )

        response = client.get(
            f"/students/me/bank-payment-orders/{own_order.id}/",
            **_auth_params(student_user, club, role="student"),
        )
        hidden_other = client.get(
            f"/students/me/bank-payment-orders/{other_order.id}/",
            **_auth_params(student_user, club, role="student"),
        )
        hidden_role = client.get(
            f"/students/me/bank-payment-orders/{own_order.id}/",
            **_auth_params(parent_user, club, role="parent"),
        )
        cross_source_refresh = client.post(
            f"/students/me/bank-payment-orders/{own_order.id}/refresh/",
            json={},
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == own_order.id
        assert data["can_pay"] is True
        assert data["can_share"] is False
        assert data["can_copy"] is False
        assert data["can_show_qr"] is False
        assert data["can_request_refresh"] is True
        assert data["can_cancel"] is False
        assert cross_source_refresh.status_code == 200
        assert BankPaymentReconciliationAttempt.objects.for_club(club).filter(order=own_order).count() == 1
        assert hidden_other.status_code == 404
        assert hidden_other.json()["detail"] == "Not found"
        assert hidden_role.status_code == 404
        assert hidden_role.json()["detail"] == "Not found"

    def test_student_refresh_coalesces_exact_source_without_provider_io(
        self,
        settings,
        club,
        student_user,
        owner_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club, user=student_user)
        other_student = StudentFactory(club=club)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(club=club, training_type=training_type, trainings_limit=8)
        SubscriptionFactory(club=club, student=student, tariff=tariff, status=Subscription.Status.ACTIVE)
        SubscriptionFactory(club=club, student=other_student, tariff=tariff, status=Subscription.Status.ACTIVE)
        own_order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.STUDENT,
            created_by_id=owner_user.id,
        )
        foreign_order = create_bank_payment_order(
            club_id=club.id,
            student_id=other_student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.STUDENT,
            created_by_id=owner_user.id,
        )

        with patch("apps.billing.payment_providers.get_payment_provider") as get_provider:
            first = client.post(
                f"/students/me/bank-payment-orders/{own_order.id}/refresh/",
                json={},
                **_auth_params(student_user, club, role="student"),
            )
            second = client.post(
                f"/students/me/bank-payment-orders/{own_order.id}/refresh/",
                json={},
                **_auth_params(student_user, club, role="student"),
            )
            foreign = client.post(
                f"/students/me/bank-payment-orders/{foreign_order.id}/refresh/",
                json={},
                **_auth_params(student_user, club, role="student"),
            )
            missing = client.post(
                "/students/me/bank-payment-orders/999999/refresh/",
                json={},
                **_auth_params(student_user, club, role="student"),
            )

        assert first.status_code == second.status_code == 200
        assert BankPaymentReconciliationAttempt.objects.for_club(club).filter(order=own_order).count() == 1
        assert foreign.status_code == missing.status_code == 404
        assert foreign.json() == missing.json()
        get_provider.assert_not_called()

    def test_student_exact_order_retains_terminal_status(self, settings, club, student_user, owner_user):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club, user=student_user)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=training_type, trainings_limit=8)
        SubscriptionFactory(club=club, student=student, tariff=tariff, status=Subscription.Status.ACTIVE)
        order = create_bank_payment_order(
            club_id=club.id,
            student_id=student.id,
            tariff_id=tariff.id,
            source=BankPaymentOrder.Source.STUDENT,
            created_by_id=owner_user.id,
        )
        order.status = BankPaymentOrder.Status.EXPIRED
        order.save(update_fields=["status", "updated_at"])

        response = client.get(
            f"/students/me/bank-payment-orders/{order.id}/",
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 200
        assert response.json()["status"] == BankPaymentOrder.Status.EXPIRED
        assert response.json()["can_pay"] is False
        assert response.json()["can_request_refresh"] is False

    def test_student_can_cancel_own_pending_bank_payment_order(self, settings, club, student_user):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club, user=student_user)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=training_type, trainings_limit=8)
        SubscriptionFactory(
            club=club,
            student=student,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
        )
        create_response = client.post(
            "/students/me/bank-payment-orders/",
            json={"tariff_id": tariff.id},
            **_auth_params(student_user, club, role="student"),
        )
        order = BankPaymentOrder.objects.for_club(club).get(id=create_response.json()["id"])

        response = client.post(
            f"/students/me/bank-payment-orders/{order.id}/cancel/",
            json={},
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 200, response.json()
        data = response.json()
        order.refresh_from_db()
        order.payment.refresh_from_db()
        order.subscription.refresh_from_db()
        assert data["status"] == BankPaymentOrder.Status.CANCELLED
        assert data["can_cancel"] is False
        assert order.status == BankPaymentOrder.Status.CANCELLED
        assert order.payment.status == Payment.Status.REJECTED
        assert order.subscription.deleted_at is not None

    def test_student_self_service_rejects_debt_ids(self, settings, club, student_user):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        student = StudentFactory(club=club, user=student_user)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=training_type, trainings_limit=8)
        SubscriptionFactory(club=club, student=student, tariff=tariff)
        checkin = CheckinFactory(
            club=club,
            student=student,
            training_type=tariff.training_type,
            subscription=None,
            is_debt=True,
        )
        debt = DebtFactory(club=club, student=student, checkin=checkin)

        response = client.post(
            "/students/me/bank-payment-orders/",
            json={"tariff_id": tariff.id, "debt_ids": [debt.id]},
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "self_service_debt_payment_not_supported"
        assert BankPaymentOrder.objects.for_club(club).count() == 0

    def test_student_self_service_rejects_tariff_without_existing_subscription(
        self,
        settings,
        club,
        student_user,
    ):
        settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
        StudentFactory(club=club, user=student_user)
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
        tariff = TariffFactory(training_type=training_type, trainings_limit=8)

        response = client.post(
            "/students/me/bank-payment-orders/",
            json={"tariff_id": tariff.id},
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "self_service_tariff_not_allowed"
        assert BankPaymentOrder.objects.for_club(club).count() == 0


class TestStudentFeedback:
    def test_student_gets_active_feedback_form(self, club, student_user):
        StudentFactory(club=club, user=student_user)
        form = FeedbackFormFactory(club=club, is_active=True)
        question = FeedbackQuestionFactory(
            club=club,
            form=form,
            question_type="rating",
            order=1,
            is_required=True,
        )

        response = client.get(
            "/students/me/feedback/form/",
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == form.id
        assert data["questions"][0]["id"] == question.id

    def test_student_gets_null_when_no_feedback_form(self, club, student_user):
        StudentFactory(club=club, user=student_user)

        response = client.get(
            "/students/me/feedback/form/",
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 200
        assert response.json() is None

    def test_student_submits_own_feedback_without_student_id(self, club, student_user):
        student = StudentFactory(club=club, user=student_user)
        form = FeedbackFormFactory(club=club, is_active=True)
        question = FeedbackQuestionFactory(
            club=club,
            form=form,
            question_type="rating",
            order=1,
            is_required=True,
        )

        response = client.post(
            "/students/me/feedback/submit/",
            json={
                "form_id": form.id,
                "answers": [{"question_id": question.id, "rating_value": 5}],
            },
            **_auth_params(student_user, club, role="student"),
        )

        data = response.json()
        assert response.status_code == 201
        assert data["student_id"] == student.id
        assert data["already_submitted"] is False

    def test_student_submit_ignores_payload_student_id(self, club, student_user):
        student = StudentFactory(club=club, user=student_user)
        other_student = StudentFactory(club=club)
        form = FeedbackFormFactory(club=club, is_active=True)
        question = FeedbackQuestionFactory(club=club, form=form, question_type="rating", order=1)

        response = client.post(
            "/students/me/feedback/submit/",
            json={
                "form_id": form.id,
                "student_id": other_student.id,
                "answers": [{"question_id": question.id, "rating_value": 4}],
            },
            **_auth_params(student_user, club, role="student"),
        )

        data = response.json()
        assert response.status_code == 201
        assert data["student_id"] == student.id
        assert data["already_submitted"] is False

    def test_student_duplicate_feedback_returns_existing_response(self, club, student_user):
        student = StudentFactory(club=club, user=student_user)
        form = FeedbackFormFactory(club=club, is_active=True)
        existing = FeedbackResponseFactory(club=club, form=form, student=student)

        response = client.post(
            "/students/me/feedback/submit/",
            json={"form_id": form.id, "answers": []},
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == existing.id
        assert data["already_submitted"] is True

    def test_student_duplicate_feedback_still_rejects_foreign_question_id(self, club, student_user):
        student = StudentFactory(club=club, user=student_user)
        form = FeedbackFormFactory(club=club, is_active=True)
        other_form = FeedbackFormFactory(club=club, is_active=False, trigger_type="churned")
        foreign_question = FeedbackQuestionFactory(club=club, form=other_form, order=1)
        FeedbackResponseFactory(club=club, form=form, student=student)

        response = client.post(
            "/students/me/feedback/submit/",
            json={
                "form_id": form.id,
                "answers": [{"question_id": foreign_question.id, "rating_value": 5}],
            },
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "invalid_question"

    def test_student_feedback_rejects_foreign_question_id(self, club, student_user):
        StudentFactory(club=club, user=student_user)
        form = FeedbackFormFactory(club=club, is_active=True)
        other_form = FeedbackFormFactory(club=club, is_active=False, trigger_type="churned")
        foreign_question = FeedbackQuestionFactory(club=club, form=other_form, order=1)

        response = client.post(
            "/students/me/feedback/submit/",
            json={
                "form_id": form.id,
                "answers": [{"question_id": foreign_question.id, "rating_value": 5}],
            },
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "invalid_question"


class TestStudentAttendance:
    def test_returns_own_attendance(self, club, student_user):
        student = StudentFactory(club=club, user=student_user)
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
        )
        response = client.get(
            "/students/me/attendance/",
            **_auth_params(student_user, club, role="student"),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["attended_count"] == 1
        assert len(data["items"]) == 1

    def test_attendance_uses_actual_checkin_trainer(self, club, student_user):
        student = StudentFactory(club=club, user=student_user)
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        substitute = TrainerFactory(club=club, first_name="Alex", last_name="Backup")
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            trainer=substitute,
        )

        response = client.get(
            "/students/me/attendance/",
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["items"][0]["trainer_name"] == "Alex Backup"

    def test_month_filter(self, club, student_user):
        StudentFactory(club=club, user=student_user)
        response = client.get(
            "/students/me/attendance/?month=2026-03",
            **_auth_params(student_user, club, role="student"),
        )
        assert response.status_code == 200

    @pytest.mark.parametrize(
        "query",
        [
            "?limit=-1",
            "?offset=-1",
            "?limit=0",
            "?limit=201",
        ],
    )
    def test_rejects_invalid_limit_and_offset(self, club, student_user, query):
        StudentFactory(club=club, user=student_user)

        response = client.get(
            f"/students/me/attendance/{query}",
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 400

    def test_month_mode_returns_full_slice_beyond_default_page_size(self, club, student_user):
        student = StudentFactory(club=club, user=student_user)
        training_type = TrainingTypeFactory(club=club)
        for index in range(60):
            CheckinFactory(
                club=club,
                student=student,
                schedule=ScheduleFactory(club=club),
                training_type=training_type,
                date=date(2026, 3, (index % 28) + 1),
            )

        response = client.get(
            "/students/me/attendance/?month=2026-03",
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["attended_count"] == 60
        assert len(data["items"]) == 60


class TestStudentSchedule:
    def test_returns_attended_schedules(self, club, student_user):
        student = StudentFactory(club=club, user=student_user)
        schedule = ScheduleFactory(club=club)
        training_type = TrainingTypeFactory(club=club)
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
        )
        response = client.get(
            "/students/me/schedule/",
            **_auth_params(student_user, club, role="student"),
        )
        assert response.status_code == 200
        assert len(response.json()) >= 1

    def test_returns_effective_occurrences_for_requested_week(self, club, student_user):
        student = StudentFactory(club=club, user=student_user)
        monday = date(2026, 4, 13)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=monday.weekday(),
            start_time=time(18, 0),
            end_time=time(19, 0),
            group_name="Evening Group",
        )
        training_type = TrainingTypeFactory(club=club)
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
        )

        response = client.get(
            f"/students/me/schedule-week/?week_start={monday.isoformat()}",
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["effective_date"] == monday.isoformat()
        assert data[0]["group_name"] == "Evening Group"

    def test_schedule_week_includes_booking_metadata_for_self_booking(self, club, student_user):
        student = StudentFactory(club=club, user=student_user)
        monday = date(2026, 4, 13)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=monday.weekday(),
            start_time=time(18, 0),
            end_time=time(19, 0),
            group_name="Self Booked Group",
        )
        enrollment = ScheduleEnrollment.objects.create(
            club=club,
            student=student,
            schedule=schedule,
            status=ScheduleEnrollment.Status.ACTIVE,
            starts_on=monday,
            ends_on=monday,
            created_from=ScheduleEnrollment.CreatedFrom.STUDENT_SELF_BOOKING,
        )

        response = client.get(
            f"/students/me/schedule-week/?week_start={monday.isoformat()}",
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["enrollment_id"] == enrollment.id
        assert data[0]["created_from"] == ScheduleEnrollment.CreatedFrom.STUDENT_SELF_BOOKING

    def test_rescheduled_occurrence_moves_inside_requested_week(self, club, student_user):
        student = StudentFactory(club=club, user=student_user)
        monday = date(2026, 4, 13)
        wednesday = date(2026, 4, 15)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=monday.weekday(),
            start_time=time(18, 0),
            end_time=time(19, 0),
            group_name="Moved Session",
        )
        training_type = TrainingTypeFactory(club=club)
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
        )
        ScheduleExceptionFactory(
            schedule=schedule,
            date=monday,
            exception_type="rescheduled",
            new_date=wednesday,
            new_start_time=time(20, 0),
            new_end_time=time(21, 0),
        )

        response = client.get(
            f"/students/me/schedule-week/?week_start={monday.isoformat()}",
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["effective_date"] == wednesday.isoformat()
        assert data[0]["effective_start_time"] == "20:00:00"
        assert data[0]["is_rescheduled"] is True

    def test_cancelled_occurrence_is_hidden_from_requested_week(self, club, student_user):
        student = StudentFactory(club=club, user=student_user)
        monday = date(2026, 4, 13)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=monday.weekday(),
            start_time=time(18, 0),
            end_time=time(19, 0),
        )
        training_type = TrainingTypeFactory(club=club)
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
        )
        ScheduleExceptionFactory(
            schedule=schedule,
            date=monday,
            exception_type="cancelled",
        )

        response = client.get(
            f"/students/me/schedule-week/?week_start={monday.isoformat()}",
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 200
        assert response.json() == []

    def test_substitute_occurrence_uses_effective_trainer(self, club, student_user):
        student = StudentFactory(club=club, user=student_user)
        monday = date(2026, 4, 13)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=monday.weekday(),
            start_time=time(18, 0),
            end_time=time(19, 0),
            group_name="Substitute Session",
        )
        training_type = TrainingTypeFactory(club=club)
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
        )
        substitute = TrainerFactory(club=club, first_name="Alex", last_name="Backup")
        ScheduleExceptionFactory(
            schedule=schedule,
            date=monday,
            exception_type="substitute",
            substitute_trainer=substitute,
        )

        response = client.get(
            f"/students/me/schedule-week/?week_start={monday.isoformat()}",
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["trainer_name"] == "Alex Backup"
        assert data[0]["is_substitute"] is True

    def test_rescheduled_occurrence_outside_week_stays_outside_response(self, club, student_user):
        student = StudentFactory(club=club, user=student_user)
        monday = date(2026, 4, 13)
        next_monday = date(2026, 4, 20)
        schedule = ScheduleFactory(
            club=club,
            day_of_week=monday.weekday(),
            start_time=time(18, 0),
            end_time=time(19, 0),
            group_name="Next Week Move",
        )
        training_type = TrainingTypeFactory(club=club)
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
        )
        ScheduleExceptionFactory(
            schedule=schedule,
            date=monday,
            exception_type="rescheduled",
            new_date=next_monday,
            new_start_time=time(20, 0),
            new_end_time=time(21, 0),
        )

        response = client.get(
            f"/students/me/schedule-week/?week_start={monday.isoformat()}",
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 200
        assert response.json() == []


class TestStudentDocumentAccess:
    def test_student_can_see_own_checklist(self, club, student_user):
        student = StudentFactory(club=club, user=student_user)
        response = client.get(
            f"/documents/students/{student.id}/checklist/",
            **_auth_params(student_user, club, role="student"),
        )
        assert response.status_code == 200

    def test_student_cannot_see_other_student_checklist(self, club, student_user):
        StudentFactory(club=club, user=student_user)
        student_other = StudentFactory(club=club)
        response = client.get(
            f"/documents/students/{student_other.id}/checklist/",
            **_auth_params(student_user, club, role="student"),
        )
        assert response.status_code == 404
