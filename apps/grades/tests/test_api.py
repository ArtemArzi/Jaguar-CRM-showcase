from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from ninja.testing import TestClient

from apps.attendance.tests.factories import CheckinFactory, ScheduleFactory
from apps.billing.models import Subscription, TrainingType
from apps.billing.tests.factories import SubscriptionFactory, TariffFactory, TrainingTypeFactory
from apps.common.tests.helpers import make_auth_params as _auth_params
from apps.grades.models import StudentGrade
from apps.grades.services import add_grade, assign_student_grade, create_grade_system
from apps.students.tests.factories import StudentFactory
from apps.trainers.models import TrainerPackageAllocation
from apps.trainers.tests.factories import TrainerFactory
from config.api import api

client = TestClient(api)


NON_CURRENT_PACKAGE_CASES = [
    ("expired", Subscription.Status.EXPIRED, 30, 8, False),
    ("pending", Subscription.Status.PENDING, 30, 8, False),
    ("frozen", Subscription.Status.FROZEN, 30, 8, False),
    ("past_expiry", Subscription.Status.ACTIVE, -1, 8, False),
    ("depleted", Subscription.Status.ACTIVE, 30, 0, False),
    ("soft_deleted", Subscription.Status.ACTIVE, 30, 8, True),
]


def _create_active_package_allocation(
    *,
    club,
    student,
    owner_trainer,
    status=Subscription.Status.ACTIVE,
    expires_at=None,
    trainings_left=None,
    deleted=False,
):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.PERSONAL)
    tariff = TariffFactory(training_type=training_type, price=Decimal("5000"))
    subscription = SubscriptionFactory(
        tariff=tariff,
        student=student,
        status=status,
        expires_at=expires_at,
        trainings_left=tariff.trainings_limit if trainings_left is None else trainings_left,
        paid_amount=Decimal("5000"),
    )
    if deleted:
        subscription.soft_delete()
    TrainerPackageAllocation.objects.create(
        club=club,
        subscription=subscription,
        student=student,
        tariff=tariff,
        training_type=training_type,
        owner_trainer=owner_trainer,
        sessions_total_snapshot=tariff.trainings_limit,
        sessions_remaining_snapshot=subscription.trainings_left,
        amount_snapshot=tariff.price,
        is_active=True,
    )
    return subscription


@pytest.fixture(autouse=True)
def _bypass_jwt_auth(bypass_jwt_auth):
    pass


@pytest.mark.django_db
class TestCreateGradeSystemAPI:
    def test_create_grade_system_api(self, club, owner_user):
        response = client.post(
            "/grades/systems/",
            json={"discipline": "BJJ"},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 201
        data = response.json()
        assert data["discipline"] == "BJJ"
        assert data["is_active"] is True

    def test_list_grade_systems_api(self, club, owner_user):
        create_grade_system(club_id=club.id, discipline="BJJ")
        response = client.get(
            "/grades/systems/",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        assert len(response.json()) == 1


@pytest.mark.django_db
class TestAddGradeAPI:
    def test_add_grade_api(self, club, owner_user):
        gs = create_grade_system(club_id=club.id, discipline="BJJ")
        response = client.post(
            f"/grades/systems/{gs.id}/grades/",
            json={"name": "White Belt", "order": 1, "min_trainings": 0},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "White Belt"
        assert data["order"] == 1

    def test_list_grades_api(self, club, owner_user):
        gs = create_grade_system(club_id=club.id, discipline="BJJ")
        add_grade(club_id=club.id, grade_system_id=gs.id, name="White", order=1)
        add_grade(club_id=club.id, grade_system_id=gs.id, name="Blue", order=2, min_trainings=100)
        response = client.get(
            f"/grades/systems/{gs.id}/grades/",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        assert len(response.json()) == 2


@pytest.mark.django_db
class TestAssignStudentGradeAPI:
    def test_assign_student_grade_api(self, club, owner_user):
        student = StudentFactory(club=club)
        gs = create_grade_system(club_id=club.id, discipline="BJJ")
        response = client.post(
            "/grades/student-grades/",
            json={"student_id": student.id, "grade_system_id": gs.id},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 201
        data = response.json()
        assert data["student_id"] == student.id
        assert data["trainings_since_last_grade"] == 0

    def test_trainer_cannot_assign_unscoped_student_grade(self, club, trainer_user):
        TrainerFactory(club=club, user=trainer_user)
        unassigned = StudentFactory(club=club)
        gs = create_grade_system(club_id=club.id, discipline="BJJ")

        response = client.post(
            "/grades/student-grades/",
            json={"student_id": unassigned.id, "grade_system_id": gs.id},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403
        assert not StudentGrade.objects.for_club(club).filter(student=unassigned).exists()

    def test_trainer_cannot_assign_checkin_only_student_grade(self, club, trainer_user):
        trainer = TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club, trainer=trainer)
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            trainer=trainer,
            training_type=schedule.training_type,
        )
        gs = create_grade_system(club_id=club.id, discipline="BJJ")

        response = client.post(
            "/grades/student-grades/",
            json={"student_id": student.id, "grade_system_id": gs.id},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403
        assert not StudentGrade.objects.for_club(club).filter(student=student).exists()

    def test_trainer_package_owner_can_assign_student_grade(self, club, trainer_user):
        trainer = TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(club=club)
        _create_active_package_allocation(club=club, student=student, owner_trainer=trainer)
        gs = create_grade_system(club_id=club.id, discipline="BJJ")

        response = client.post(
            "/grades/student-grades/",
            json={"student_id": student.id, "grade_system_id": gs.id},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 201
        assert response.json()["student_id"] == student.id
        assert StudentGrade.objects.for_club(club).filter(student=student).count() == 1

    @pytest.mark.parametrize(
        ("_case", "status", "expires_delta_days", "trainings_left", "deleted"),
        NON_CURRENT_PACKAGE_CASES,
    )
    def test_trainer_package_owner_without_current_subscription_cannot_assign_student_grade(
        self,
        club,
        trainer_user,
        _case,
        status,
        expires_delta_days,
        trainings_left,
        deleted,
    ):
        trainer = TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(club=club)
        _create_active_package_allocation(
            club=club,
            student=student,
            owner_trainer=trainer,
            status=status,
            expires_at=timezone.now() + timedelta(days=expires_delta_days),
            trainings_left=trainings_left,
            deleted=deleted,
        )
        gs = create_grade_system(club_id=club.id, discipline="BJJ")

        response = client.post(
            "/grades/student-grades/",
            json={"student_id": student.id, "grade_system_id": gs.id},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403
        assert not StudentGrade.objects.for_club(club).filter(student=student).exists()


@pytest.mark.django_db
class TestPromoteStudentAPI:
    def test_promote_student_api(self, club, owner_user):
        student = StudentFactory(club=club)
        gs = create_grade_system(club_id=club.id, discipline="BJJ")
        white = add_grade(club_id=club.id, grade_system_id=gs.id, name="White", order=1)
        sg = assign_student_grade(club_id=club.id, student_id=student.id, grade_system_id=gs.id)
        response = client.post(
            f"/grades/student-grades/{sg.id}/promote/",
            json={"new_grade_id": white.id},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["current_grade"]["name"] == "White"
        assert data["trainings_since_last_grade"] == 0

    def test_trainer_cannot_promote_unscoped_student_grade(self, club, trainer_user):
        TrainerFactory(club=club, user=trainer_user)
        unassigned = StudentFactory(club=club)
        gs = create_grade_system(club_id=club.id, discipline="BJJ")
        white = add_grade(club_id=club.id, grade_system_id=gs.id, name="White", order=1)
        sg = assign_student_grade(club_id=club.id, student_id=unassigned.id, grade_system_id=gs.id)

        response = client.post(
            f"/grades/student-grades/{sg.id}/promote/",
            json={"new_grade_id": white.id},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403
        sg.refresh_from_db()
        assert sg.current_grade_id is None

    def test_trainer_package_owner_can_promote_student_grade(self, club, trainer_user):
        trainer = TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(club=club)
        _create_active_package_allocation(club=club, student=student, owner_trainer=trainer)
        gs = create_grade_system(club_id=club.id, discipline="BJJ")
        white = add_grade(club_id=club.id, grade_system_id=gs.id, name="White", order=1)
        sg = assign_student_grade(club_id=club.id, student_id=student.id, grade_system_id=gs.id)

        response = client.post(
            f"/grades/student-grades/{sg.id}/promote/",
            json={"new_grade_id": white.id},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        assert response.json()["current_grade"]["name"] == "White"

    @pytest.mark.parametrize(
        ("_case", "status", "expires_delta_days", "trainings_left", "deleted"),
        NON_CURRENT_PACKAGE_CASES,
    )
    def test_trainer_package_owner_without_current_subscription_cannot_promote_student_grade(
        self,
        club,
        trainer_user,
        _case,
        status,
        expires_delta_days,
        trainings_left,
        deleted,
    ):
        trainer = TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(club=club)
        _create_active_package_allocation(
            club=club,
            student=student,
            owner_trainer=trainer,
            status=status,
            expires_at=timezone.now() + timedelta(days=expires_delta_days),
            trainings_left=trainings_left,
            deleted=deleted,
        )
        gs = create_grade_system(club_id=club.id, discipline="BJJ")
        white = add_grade(club_id=club.id, grade_system_id=gs.id, name="White", order=1)
        sg = assign_student_grade(club_id=club.id, student_id=student.id, grade_system_id=gs.id)

        response = client.post(
            f"/grades/student-grades/{sg.id}/promote/",
            json={"new_grade_id": white.id},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403
        sg.refresh_from_db()
        assert sg.current_grade_id is None


@pytest.mark.django_db
class TestUnassignStudentGradeAPI:
    def test_trainer_cannot_unassign_unscoped_student_grade(self, club, trainer_user):
        TrainerFactory(club=club, user=trainer_user)
        unassigned = StudentFactory(club=club)
        gs = create_grade_system(club_id=club.id, discipline="BJJ")
        sg = assign_student_grade(club_id=club.id, student_id=unassigned.id, grade_system_id=gs.id)

        response = client.delete(
            f"/grades/student-grades/{sg.id}/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403
        assert StudentGrade.objects.for_club(club).filter(id=sg.id).exists()

    def test_trainer_package_owner_can_unassign_student_grade(self, club, trainer_user):
        trainer = TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(club=club)
        _create_active_package_allocation(club=club, student=student, owner_trainer=trainer)
        gs = create_grade_system(club_id=club.id, discipline="BJJ")
        sg = assign_student_grade(club_id=club.id, student_id=student.id, grade_system_id=gs.id)

        response = client.delete(
            f"/grades/student-grades/{sg.id}/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 204
        assert not StudentGrade.objects.for_club(club).filter(id=sg.id).exists()


@pytest.mark.django_db
class TestStudentProgressAPI:
    def test_student_progress_api(self, club, owner_user):
        student = StudentFactory(club=club)
        gs = create_grade_system(club_id=club.id, discipline="BJJ")
        white = add_grade(club_id=club.id, grade_system_id=gs.id, name="White", order=1)
        add_grade(club_id=club.id, grade_system_id=gs.id, name="Blue", order=2, min_trainings=100)
        sg = assign_student_grade(club_id=club.id, student_id=student.id, grade_system_id=gs.id)
        from apps.grades.services import promote_student

        promote_student(club_id=club.id, student_grade_id=sg.id, new_grade_id=white.id)

        response = client.get(
            f"/grades/students/{student.id}/progress/",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["current_grade"]["name"] == "White"
        assert data[0]["next_grade"]["name"] == "Blue"
        assert data[0]["trainings_to_next"] == 100

    def test_trainer_cannot_read_unscoped_student_progress(self, club, trainer_user):
        TrainerFactory(club=club, user=trainer_user)
        unassigned = StudentFactory(club=club)
        gs = create_grade_system(club_id=club.id, discipline="BJJ")
        assign_student_grade(club_id=club.id, student_id=unassigned.id, grade_system_id=gs.id)

        response = client.get(
            f"/grades/students/{unassigned.id}/progress/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403

    def test_trainer_with_actual_checkin_can_read_student_progress(self, club, trainer_user):
        trainer = TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(club=club)
        schedule = ScheduleFactory(club=club, trainer=trainer)
        CheckinFactory(
            club=club,
            student=student,
            schedule=schedule,
            trainer=trainer,
            training_type=schedule.training_type,
        )
        gs = create_grade_system(club_id=club.id, discipline="BJJ")
        assign_student_grade(club_id=club.id, student_id=student.id, grade_system_id=gs.id)

        response = client.get(
            f"/grades/students/{student.id}/progress/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        assert len(response.json()) == 1

    def test_trainer_package_owner_can_read_student_progress(self, club, trainer_user):
        trainer = TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(club=club)
        _create_active_package_allocation(club=club, student=student, owner_trainer=trainer)
        gs = create_grade_system(club_id=club.id, discipline="BJJ")
        white = add_grade(club_id=club.id, grade_system_id=gs.id, name="White", order=1)
        add_grade(club_id=club.id, grade_system_id=gs.id, name="Blue", order=2, min_trainings=100)
        sg = assign_student_grade(club_id=club.id, student_id=student.id, grade_system_id=gs.id)
        from apps.grades.services import promote_student

        promote_student(club_id=club.id, student_grade_id=sg.id, new_grade_id=white.id)

        response = client.get(
            f"/grades/students/{student.id}/progress/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["student_grade_id"] == sg.id
        assert data[0]["current_grade"]["name"] == "White"


@pytest.mark.django_db
class TestReadyForPromotionAPI:
    def test_ready_for_promotion_api(self, club, owner_user):
        student = StudentFactory(club=club)
        gs = create_grade_system(club_id=club.id, discipline="BJJ")
        white = add_grade(club_id=club.id, grade_system_id=gs.id, name="White", order=1, min_trainings=0)
        add_grade(club_id=club.id, grade_system_id=gs.id, name="Blue", order=2, min_trainings=100)
        sg = assign_student_grade(club_id=club.id, student_id=student.id, grade_system_id=gs.id)
        from apps.grades.services import promote_student

        promote_student(club_id=club.id, student_grade_id=sg.id, new_grade_id=white.id)
        # Set trainings to >= 100 to be ready for Blue
        from apps.grades.models import StudentGrade

        StudentGrade.objects.filter(id=sg.id).update(trainings_since_last_grade=100)

        response = client.get(
            f"/grades/systems/{gs.id}/ready-for-promotion/",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["student_id"] == student.id

    def test_trainer_ready_for_promotion_only_returns_scoped_students(self, club, trainer_user):
        trainer = TrainerFactory(club=club, user=trainer_user)
        scoped = StudentFactory(club=club, assigned_trainer=trainer)
        unassigned = StudentFactory(club=club)
        gs = create_grade_system(club_id=club.id, discipline="BJJ")
        white = add_grade(club_id=club.id, grade_system_id=gs.id, name="White", order=1, min_trainings=0)
        add_grade(club_id=club.id, grade_system_id=gs.id, name="Blue", order=2, min_trainings=100)
        from apps.grades.services import promote_student

        scoped_grade = assign_student_grade(club_id=club.id, student_id=scoped.id, grade_system_id=gs.id)
        unassigned_grade = assign_student_grade(club_id=club.id, student_id=unassigned.id, grade_system_id=gs.id)
        promote_student(club_id=club.id, student_grade_id=scoped_grade.id, new_grade_id=white.id)
        promote_student(club_id=club.id, student_grade_id=unassigned_grade.id, new_grade_id=white.id)
        StudentGrade.objects.filter(id__in=[scoped_grade.id, unassigned_grade.id]).update(
            trainings_since_last_grade=100
        )

        response = client.get(
            f"/grades/systems/{gs.id}/ready-for-promotion/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        student_ids = {item["student_id"] for item in response.json()}
        assert scoped.id in student_ids
        assert unassigned.id not in student_ids

    def test_trainer_ready_for_promotion_includes_package_owned_students(self, club, trainer_user):
        trainer = TrainerFactory(club=club, user=trainer_user)
        package_student = StudentFactory(club=club)
        unassigned = StudentFactory(club=club)
        _create_active_package_allocation(club=club, student=package_student, owner_trainer=trainer)
        gs = create_grade_system(club_id=club.id, discipline="BJJ")
        white = add_grade(club_id=club.id, grade_system_id=gs.id, name="White", order=1, min_trainings=0)
        add_grade(club_id=club.id, grade_system_id=gs.id, name="Blue", order=2, min_trainings=100)
        from apps.grades.services import promote_student

        package_grade = assign_student_grade(club_id=club.id, student_id=package_student.id, grade_system_id=gs.id)
        unassigned_grade = assign_student_grade(club_id=club.id, student_id=unassigned.id, grade_system_id=gs.id)
        promote_student(club_id=club.id, student_grade_id=package_grade.id, new_grade_id=white.id)
        promote_student(club_id=club.id, student_grade_id=unassigned_grade.id, new_grade_id=white.id)
        StudentGrade.objects.filter(id__in=[package_grade.id, unassigned_grade.id]).update(
            trainings_since_last_grade=100
        )

        response = client.get(
            f"/grades/systems/{gs.id}/ready-for-promotion/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        student_ids = {item["student_id"] for item in response.json()}
        assert package_student.id in student_ids
        assert unassigned.id not in student_ids

    @pytest.mark.parametrize(
        ("_case", "status", "expires_delta_days", "trainings_left", "deleted"),
        NON_CURRENT_PACKAGE_CASES,
    )
    def test_trainer_ready_for_promotion_excludes_non_current_package_owned_students(
        self,
        club,
        trainer_user,
        _case,
        status,
        expires_delta_days,
        trainings_left,
        deleted,
    ):
        trainer = TrainerFactory(club=club, user=trainer_user)
        assigned = StudentFactory(club=club, assigned_trainer=trainer)
        stale_package_student = StudentFactory(club=club)
        _create_active_package_allocation(
            club=club,
            student=stale_package_student,
            owner_trainer=trainer,
            status=status,
            expires_at=timezone.now() + timedelta(days=expires_delta_days),
            trainings_left=trainings_left,
            deleted=deleted,
        )
        gs = create_grade_system(club_id=club.id, discipline="BJJ")
        white = add_grade(club_id=club.id, grade_system_id=gs.id, name="White", order=1, min_trainings=0)
        add_grade(club_id=club.id, grade_system_id=gs.id, name="Blue", order=2, min_trainings=100)
        from apps.grades.services import promote_student

        assigned_grade = assign_student_grade(club_id=club.id, student_id=assigned.id, grade_system_id=gs.id)
        package_grade = assign_student_grade(
            club_id=club.id,
            student_id=stale_package_student.id,
            grade_system_id=gs.id,
        )
        promote_student(club_id=club.id, student_grade_id=assigned_grade.id, new_grade_id=white.id)
        promote_student(club_id=club.id, student_grade_id=package_grade.id, new_grade_id=white.id)
        StudentGrade.objects.filter(id__in=[assigned_grade.id, package_grade.id]).update(
            trainings_since_last_grade=100
        )

        response = client.get(
            f"/grades/systems/{gs.id}/ready-for-promotion/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        student_ids = {item["student_id"] for item in response.json()}
        assert assigned.id in student_ids
        assert stale_package_student.id not in student_ids


@pytest.mark.django_db
class TestSeedTemplatesAPI:
    def test_seed_templates_api(self, club, owner_user):
        response = client.post(
            "/grades/seed-templates/",
            json={"disciplines": ["BJJ"]},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 201
        data = response.json()
        assert len(data) == 1
        assert data[0]["discipline"] == "BJJ"

    def test_seed_templates_idempotent(self, club, owner_user):
        client.post(
            "/grades/seed-templates/",
            json={"disciplines": ["BJJ"]},
            **_auth_params(owner_user, club),
        )
        response = client.post(
            "/grades/seed-templates/",
            json={"disciplines": ["BJJ"]},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 201
        assert len(response.json()) == 0


@pytest.mark.django_db
class TestMyProgressAPI:
    def test_student_can_view_own_progress(self, club, student_user):
        student = StudentFactory(club=club, user=student_user)
        gs = create_grade_system(club_id=club.id, discipline="BJJ")
        white = add_grade(club_id=club.id, grade_system_id=gs.id, name="White", order=1)
        sg = assign_student_grade(club_id=club.id, student_id=student.id, grade_system_id=gs.id)
        from apps.grades.services import promote_student

        promote_student(club_id=club.id, student_grade_id=sg.id, new_grade_id=white.id)

        response = client.get(
            "/grades/my-progress/",
            **_auth_params(student_user, club, role="student"),
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["current_grade"]["name"] == "White"
        assert data[0]["grade_system_name"] == "BJJ"

    def test_student_progress_is_sorted_deterministically(self, club, student_user):
        student = StudentFactory(club=club, user=student_user)

        karate = create_grade_system(club_id=club.id, discipline="Karate")
        bjj = create_grade_system(club_id=club.id, discipline="BJJ")

        add_grade(club_id=club.id, grade_system_id=karate.id, name="10 kyu", order=1)
        add_grade(club_id=club.id, grade_system_id=bjj.id, name="White", order=1)

        assign_student_grade(club_id=club.id, student_id=student.id, grade_system_id=karate.id)
        assign_student_grade(club_id=club.id, student_id=student.id, grade_system_id=bjj.id)

        response = client.get(
            "/grades/my-progress/",
            **_auth_params(student_user, club, role="student"),
        )
        assert response.status_code == 200
        data = response.json()
        assert [item["grade_system_name"] for item in data] == ["BJJ", "Karate"]

    def test_non_student_role_denied(self, club, parent_user):
        response = client.get(
            "/grades/my-progress/",
            **_auth_params(parent_user, club, role="parent"),
        )
        assert response.status_code == 403


@pytest.mark.django_db
class TestStudentProgressAccessControl:
    def test_student_cannot_view_other_student_grades(self, club, student_user):
        StudentFactory(club=club, user=student_user)
        student_b = StudentFactory(club=club)
        gs = create_grade_system(club_id=club.id, discipline="BJJ")
        assign_student_grade(club_id=club.id, student_id=student_b.id, grade_system_id=gs.id)

        response = client.get(
            f"/grades/students/{student_b.id}/progress/",
            **_auth_params(student_user, club, role="student"),
        )
        assert response.status_code == 403

    def test_student_can_view_own_grades(self, club, student_user):
        student_a = StudentFactory(club=club, user=student_user)
        gs = create_grade_system(club_id=club.id, discipline="BJJ")
        white = add_grade(club_id=club.id, grade_system_id=gs.id, name="White", order=1)
        sg = assign_student_grade(club_id=club.id, student_id=student_a.id, grade_system_id=gs.id)
        from apps.grades.services import promote_student

        promote_student(club_id=club.id, student_grade_id=sg.id, new_grade_id=white.id)

        response = client.get(
            f"/grades/students/{student_a.id}/progress/",
            **_auth_params(student_user, club, role="student"),
        )
        assert response.status_code == 200
        assert len(response.json()) == 1

    def test_owner_can_view_any_student_grades(self, club, owner_user):
        student = StudentFactory(club=club)
        gs = create_grade_system(club_id=club.id, discipline="BJJ")
        assign_student_grade(club_id=club.id, student_id=student.id, grade_system_id=gs.id)

        response = client.get(
            f"/grades/students/{student.id}/progress/",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200


@pytest.mark.django_db
class TestGradesTenantIsolation:
    def test_grades_tenant_isolation_api(self, club, other_club, owner_user):
        create_grade_system(club_id=club.id, discipline="BJJ")
        create_grade_system(club_id=other_club.id, discipline="BJJ")

        response = client.get(
            "/grades/systems/",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        assert len(response.json()) == 1
        assert response.json()[0]["discipline"] == "BJJ"
