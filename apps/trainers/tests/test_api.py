from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from ninja.testing import TestClient

from apps.attendance.tests.factories import CheckinFactory, ScheduleFactory
from apps.billing.models import TrainingType
from apps.billing.tests.factories import SubscriptionFactory, TariffFactory, TrainingTypeFactory
from apps.clubs.tests.factories import LocationFactory
from apps.common.tests.helpers import make_auth_params as _auth_params
from apps.students.tests.factories import StudentFactory
from apps.trainers.models import TrainerEarningAdjustment, TrainerPackageAllocation
from apps.trainers.tests.factories import TrainerEarningFactory, TrainerFactory, TrainerLocationFactory
from config.api import api

client = TestClient(api)


@pytest.fixture(autouse=True)
def _bypass_jwt_auth(bypass_jwt_auth):
    pass


@pytest.mark.django_db
class TestTrainerMe:
    def test_active_trainer_can_read_own_profile(self, club, trainer_user):
        trainer = TrainerFactory(club=club, user=trainer_user)

        response = client.get("/trainers/me/", **_auth_params(trainer_user, club, role="trainer"))

        assert response.status_code == 200
        assert response.json()["id"] == trainer.id

    def test_inactive_trainer_profile_is_not_current_profile(self, club, trainer_user):
        TrainerFactory(club=club, user=trainer_user, is_active=False)

        response = client.get("/trainers/me/", **_auth_params(trainer_user, club, role="trainer"))

        assert response.status_code == 404


@pytest.mark.django_db
class TestListTrainers:
    def test_list_trainers_owner(self, club, owner_user):
        loc = LocationFactory(club=club)
        trainer = TrainerFactory(club=club)
        TrainerLocationFactory(trainer=trainer, location=loc)
        response = client.get("/trainers/", **_auth_params(owner_user, club))
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        item = data["items"][0]
        assert item["first_name"] == trainer.first_name
        assert len(item["locations"]) == 1

    def test_list_trainers_tenant_isolation(self, club, other_club, owner_user):
        TrainerFactory(club=club)
        TrainerFactory(club=other_club)
        response = client.get("/trainers/", **_auth_params(owner_user, club))
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1


@pytest.mark.django_db
class TestCreateTrainer:
    def test_create_trainer_endpoint(self, club, owner_user):
        loc = LocationFactory(club=club)
        response = client.post(
            "/trainers/",
            json={
                "first_name": "Sergey",
                "last_name": "Ivanov",
                "phone": "+79001111111",
                "locations": [
                    {
                        "location_id": loc.id,
                        "rate_group": "25.00",
                        "rate_personal": "50.00",
                        "rate_mini_group": "40.00",
                    },
                ],
            },
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 201
        data = response.json()
        assert data["first_name"] == "Sergey"
        assert len(data["locations"]) == 1

    def test_create_trainer_permission(self, club, trainer_user):
        response = client.post(
            "/trainers/",
            json={"first_name": "Test", "last_name": "Trainer"},
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert response.status_code == 403


@pytest.mark.django_db
class TestTrainerDetail:
    def test_get_trainer_detail(self, club, owner_user):
        loc = LocationFactory(club=club)
        trainer = TrainerFactory(club=club)
        TrainerLocationFactory(trainer=trainer, location=loc)
        response = client.get(f"/trainers/{trainer.id}/", **_auth_params(owner_user, club))
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == trainer.id
        assert len(data["locations"]) == 1
        assert data["locations"][0]["location_name"] == loc.name


@pytest.mark.django_db
class TestUpdateTrainer:
    def test_update_trainer_endpoint(self, club, owner_user):
        trainer = TrainerFactory(club=club, first_name="Old", last_name="Coach", is_active=True)

        response = client.put(
            f"/trainers/{trainer.id}/",
            json={
                "first_name": "New",
                "last_name": "Name",
                "phone": "+79005550000",
                "is_active": False,
            },
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["first_name"] == "New"
        assert data["last_name"] == "Name"
        assert data["phone"] == "+79005550000"
        assert data["is_active"] is False
        trainer.refresh_from_db()
        assert trainer.first_name == "New"
        assert trainer.is_active is False

    def test_trainer_cannot_update_trainer_profile(self, club, trainer_user):
        trainer = TrainerFactory(club=club)

        response = client.put(
            f"/trainers/{trainer.id}/",
            json={"first_name": "Leaked"},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403
        trainer.refresh_from_db()
        assert trainer.first_name != "Leaked"


@pytest.mark.django_db
class TestUpdateTrainerLocations:
    def test_update_trainer_locations_endpoint(self, club, owner_user):
        loc1 = LocationFactory(club=club)
        loc2 = LocationFactory(club=club)
        trainer = TrainerFactory(club=club)
        TrainerLocationFactory(trainer=trainer, location=loc1)

        response = client.put(
            f"/trainers/{trainer.id}/locations/",
            json=[
                {"location_id": loc2.id, "rate_group": "30.00", "rate_personal": "60.00", "rate_mini_group": "45.00"},
            ],
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["location_id"] == loc2.id

    def test_update_trainer_locations_rejects_foreign_location(self, club, other_club, owner_user):
        trainer = TrainerFactory(club=club)
        foreign_location = LocationFactory(club=other_club)

        response = client.put(
            f"/trainers/{trainer.id}/locations/",
            json=[{"location_id": foreign_location.id}],
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "location_club_mismatch"
        assert trainer.trainer_locations.count() == 0


# ──────────────────────────────────────────────
# Salary / Earnings API tests
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestTrainerEarningsAPI:
    def test_trainer_earnings_api(self, club, owner_user):
        trainer = TrainerFactory(club=club)
        schedule = ScheduleFactory(club=club, trainer=trainer)
        training_type = TrainingTypeFactory(club=club)
        student = StudentFactory(club=club)
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            date=date.today(),
        )
        TrainerEarningFactory(
            club=club,
            trainer=trainer,
            checkin=checkin,
            amount=Decimal("1000.00"),
        )
        date_from = (date.today() - timedelta(days=1)).isoformat()
        date_to = (date.today() + timedelta(days=1)).isoformat()
        response = client.get(
            f"/trainers/{trainer.id}/earnings/?date_from={date_from}&date_to={date_to}",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert Decimal(data[0]["amount"]) == Decimal("1000.00")

    def test_trainer_earnings_api_exposes_manual_adjustment_row(self, club, owner_user):
        trainer = TrainerFactory(club=club)
        target_date = date.today()
        adjustment = TrainerEarningAdjustment.objects.create(
            club=club,
            trainer=trainer,
            amount_basis_snapshot=Decimal("0.00"),
            payable_amount_delta=Decimal("-500.00"),
            affects_payroll=True,
            direction=TrainerEarningAdjustment.Direction.DEBIT,
            kind=TrainerEarningAdjustment.Kind.MANUAL_ADJUSTMENT,
            effective_date=target_date,
            reason="manual penalty",
        )
        date_from = target_date.isoformat()
        date_to = target_date.isoformat()

        response = client.get(
            f"/trainers/{trainer.id}/earnings/?date_from={date_from}&date_to={date_to}",
            **_auth_params(owner_user, club),
        )
        summary = client.get(
            f"/trainers/{trainer.id}/earnings/summary/?date_from={date_from}&date_to={date_to}",
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        item = data[0]
        assert item["id"] == adjustment.id
        assert item["row_type"] == "adjustment"
        assert item["earning_type"] == "manual_adjustment"
        assert item["amount"] == "-500.00"
        assert item["checkin_date"] == target_date.isoformat()
        assert item["adjustment_direction"] == "debit"
        assert item["adjustment_reason"] == "manual penalty"
        assert summary.status_code == 200
        assert Decimal(summary.json()["total_amount"]) == Decimal("-500.00")
        assert summary.json()["total_sessions"] == 0

    def test_salary_summary_default_range_uses_club_local_today(self, club, owner_user, monkeypatch):
        club.timezone = "Asia/Yekaterinburg"
        club.save(update_fields=["timezone"])
        captured = {}

        def fake_get_salary_summary(*, club, date_from, date_to):
            captured["date_from"] = date_from
            captured["date_to"] = date_to
            return []

        monkeypatch.setattr("apps.trainers.api.get_salary_summary", fake_get_salary_summary)
        monkeypatch.setattr(
            "apps.clubs.timezones.timezone.now",
            lambda: datetime(2026, 6, 28, 20, 30, tzinfo=UTC),
        )

        response = client.get("/trainers/salary-summary/", **_auth_params(owner_user, club))

        assert response.status_code == 200
        assert captured["date_from"] == date(2026, 6, 1)
        assert captured["date_to"] == date(2026, 6, 29)

    def test_inactive_trainer_cannot_read_own_earnings(self, club, trainer_user):
        trainer = TrainerFactory(club=club, user=trainer_user, is_active=False)
        date_from = (date.today() - timedelta(days=1)).isoformat()
        date_to = (date.today() + timedelta(days=1)).isoformat()

        earnings = client.get(
            f"/trainers/{trainer.id}/earnings/?date_from={date_from}&date_to={date_to}",
            **_auth_params(trainer_user, club, role="trainer"),
        )
        summary = client.get(
            f"/trainers/{trainer.id}/earnings/summary/?date_from={date_from}&date_to={date_to}",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert earnings.status_code == 403
        assert summary.status_code == 403

    def test_trainer_earnings_api_exposes_package_transfer_metadata(self, club, owner_user):
        owner_trainer = TrainerFactory(club=club, first_name="Owner", last_name="Coach")
        actual_trainer = TrainerFactory(club=club, first_name="Actual", last_name="Coach")
        training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
        tariff = TariffFactory(training_type=training_type, price=Decimal("6000.00"), trainings_limit=6)
        subscription = SubscriptionFactory(club=club, tariff=tariff)
        schedule = ScheduleFactory(club=club, trainer=actual_trainer, training_type=training_type)
        checkin = CheckinFactory(
            club=club,
            student=subscription.student,
            schedule=schedule,
            training_type=training_type,
            trainer=actual_trainer,
            subscription=subscription,
            date=date.today(),
        )
        TrainerPackageAllocation.objects.create(
            club=club,
            subscription=subscription,
            student=subscription.student,
            tariff=tariff,
            training_type=training_type,
            owner_trainer=owner_trainer,
            sessions_total_snapshot=tariff.trainings_limit,
            sessions_remaining_snapshot=subscription.trainings_left,
            amount_snapshot=tariff.price,
        )
        earning = TrainerEarningFactory(
            club=club,
            trainer=actual_trainer,
            checkin=checkin,
            amount=Decimal("3000.00"),
            earning_type=TrainingType.Kind.PERSONAL,
        )
        TrainerEarningAdjustment.objects.create(
            club=club,
            trainer=actual_trainer,
            amount_basis_snapshot=earning.amount,
            payable_amount_delta=Decimal("0.00"),
            affects_payroll=False,
            direction=TrainerEarningAdjustment.Direction.INFO,
            kind=TrainerEarningAdjustment.Kind.PACKAGE_TRANSFER,
            effective_date=checkin.date,
            source_checkin=checkin,
            source_subscription=subscription,
            counterparty_trainer=owner_trainer,
            reason="package_owner_differs_from_actual_trainer",
        )
        date_from = (date.today() - timedelta(days=1)).isoformat()
        date_to = (date.today() + timedelta(days=1)).isoformat()

        response = client.get(
            f"/trainers/{actual_trainer.id}/earnings/?date_from={date_from}&date_to={date_to}",
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        item = data[0]
        assert item["package_owner_trainer_id"] == owner_trainer.id
        assert item["package_owner_trainer_name"] == str(owner_trainer)
        assert item["package_transfer_amount_basis"] == "3000.00"
        assert item["package_transfer_payable_delta"] == "0.00"
        assert item["package_transfer_affects_payroll"] is False
        assert item["package_transfer_reason"] == "package_owner_differs_from_actual_trainer"

    def test_trainer_earnings_summary_api(self, club, owner_user):
        trainer = TrainerFactory(club=club)
        schedule = ScheduleFactory(club=club, trainer=trainer)
        training_type = TrainingTypeFactory(club=club)
        student = StudentFactory(club=club)
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            date=date.today(),
        )
        TrainerEarningFactory(
            club=club,
            trainer=trainer,
            checkin=checkin,
            amount=Decimal("1500.00"),
        )
        date_from = (date.today() - timedelta(days=1)).isoformat()
        date_to = (date.today() + timedelta(days=1)).isoformat()
        response = client.get(
            f"/trainers/{trainer.id}/earnings/summary/?date_from={date_from}&date_to={date_to}",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert Decimal(data["total_amount"]) == Decimal("1500.00")
        assert data["total_sessions"] == 1


@pytest.mark.django_db
class TestSalarySummaryAPI:
    def test_salary_summary_api(self, club, owner_user):
        trainer1 = TrainerFactory(club=club)
        trainer2 = TrainerFactory(club=club)
        schedule = ScheduleFactory(club=club, trainer=trainer1)
        training_type = TrainingTypeFactory(club=club)

        for trainer in [trainer1, trainer2]:
            student = StudentFactory(club=club)
            checkin = CheckinFactory(
                club=club,
                student=student,
                schedule=schedule,
                training_type=training_type,
                date=date.today(),
            )
            TrainerEarningFactory(
                club=club,
                trainer=trainer,
                checkin=checkin,
                amount=Decimal("1000.00"),
            )

        date_from = (date.today() - timedelta(days=1)).isoformat()
        date_to = (date.today() + timedelta(days=1)).isoformat()
        response = client.get(
            f"/trainers/salary-summary/?date_from={date_from}&date_to={date_to}",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2

    def test_salary_tenant_isolation_api(self, club, other_club, owner_user):
        trainer = TrainerFactory(club=club)
        other_trainer = TrainerFactory(club=other_club)
        schedule = ScheduleFactory(club=club, trainer=trainer)
        other_schedule = ScheduleFactory(club=other_club, trainer=other_trainer)
        training_type = TrainingTypeFactory(club=club)
        other_training_type = TrainingTypeFactory(club=other_club)

        student = StudentFactory(club=club)
        checkin = CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            training_type=training_type,
            date=date.today(),
        )
        TrainerEarningFactory(club=club, trainer=trainer, checkin=checkin)

        other_student = StudentFactory(club=other_club)
        other_checkin = CheckinFactory(
            club=other_club,
            student=other_student,
            schedule=other_schedule,
            training_type=other_training_type,
            date=date.today(),
        )
        TrainerEarningFactory(club=other_club, trainer=other_trainer, checkin=other_checkin)

        date_from = (date.today() - timedelta(days=1)).isoformat()
        date_to = (date.today() + timedelta(days=1)).isoformat()
        response = client.get(
            f"/trainers/salary-summary/?date_from={date_from}&date_to={date_to}",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
