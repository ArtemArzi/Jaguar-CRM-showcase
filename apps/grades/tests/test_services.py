from unittest.mock import patch

import pytest
from django.db import IntegrityError

from apps.clubs.tests.factories import ClubFactory, UserFactory
from apps.grades.models import Grade, GradeSystem, StudentGrade
from apps.grades.selectors import get_student_grade_progress
from apps.grades.services import (
    add_grade,
    assign_student_grade,
    create_grade_system,
    decrement_grade_progress,
    increment_grade_progress,
    promote_student,
    seed_grade_templates,
)
from apps.notifications.models import NotificationPreference, SentNotification
from apps.students.tests.factories import StudentFactory


@pytest.mark.django_db
class TestCreateGradeSystem:
    def test_create_grade_system(self):
        club = ClubFactory()
        gs = create_grade_system(club_id=club.id, discipline="BJJ")
        assert gs.discipline == "BJJ"
        assert gs.club == club
        assert gs.is_active is True

    def test_grade_system_unique_per_club_discipline(self):
        club = ClubFactory()
        create_grade_system(club_id=club.id, discipline="BJJ")
        with pytest.raises(IntegrityError):
            create_grade_system(club_id=club.id, discipline="BJJ")


@pytest.mark.django_db
class TestAddGrade:
    def test_add_grade(self):
        club = ClubFactory()
        gs = create_grade_system(club_id=club.id, discipline="BJJ")
        grade = add_grade(
            club_id=club.id,
            grade_system_id=gs.id,
            name="White Belt",
            order=1,
            min_trainings=0,
        )
        assert grade.name == "White Belt"
        assert grade.order == 1
        assert grade.grade_system == gs

    def test_grade_order_unique_per_system(self):
        club = ClubFactory()
        gs = create_grade_system(club_id=club.id, discipline="BJJ")
        add_grade(club_id=club.id, grade_system_id=gs.id, name="White", order=1)
        with pytest.raises(IntegrityError):
            add_grade(club_id=club.id, grade_system_id=gs.id, name="Blue", order=1)


@pytest.mark.django_db
class TestAssignStudentGrade:
    def test_assign_student_grade(self):
        club = ClubFactory()
        student = StudentFactory(club=club)
        gs = create_grade_system(club_id=club.id, discipline="BJJ")
        sg = assign_student_grade(club_id=club.id, student_id=student.id, grade_system_id=gs.id)
        assert sg.student == student
        assert sg.grade_system == gs
        assert sg.current_grade is None
        assert sg.trainings_since_last_grade == 0

    def test_student_unique_per_grade_system(self):
        club = ClubFactory()
        student = StudentFactory(club=club)
        gs = create_grade_system(club_id=club.id, discipline="BJJ")
        assign_student_grade(club_id=club.id, student_id=student.id, grade_system_id=gs.id)
        with pytest.raises(IntegrityError):
            assign_student_grade(club_id=club.id, student_id=student.id, grade_system_id=gs.id)


@pytest.mark.django_db
class TestPromoteStudent:
    def test_promote_student(self):
        club = ClubFactory()
        student = StudentFactory(club=club)
        gs = create_grade_system(club_id=club.id, discipline="BJJ")
        white = add_grade(club_id=club.id, grade_system_id=gs.id, name="White", order=1)
        blue = add_grade(club_id=club.id, grade_system_id=gs.id, name="Blue", order=2, min_trainings=100)
        sg = assign_student_grade(club_id=club.id, student_id=student.id, grade_system_id=gs.id)
        sg = promote_student(club_id=club.id, student_grade_id=sg.id, new_grade_id=white.id)
        assert sg.current_grade == white
        assert sg.trainings_since_last_grade == 0
        assert sg.promoted_at is not None

        # Promote again to blue
        sg.trainings_since_last_grade = 100
        sg.save()
        sg = promote_student(club_id=club.id, student_grade_id=sg.id, new_grade_id=blue.id)
        assert sg.current_grade == blue
        assert sg.trainings_since_last_grade == 0

    def test_promote_validates_grade_belongs_to_system(self):
        from apps.common.exceptions import BusinessLogicError

        club = ClubFactory()
        student = StudentFactory(club=club)
        gs1 = create_grade_system(club_id=club.id, discipline="BJJ")
        gs2 = create_grade_system(club_id=club.id, discipline="Karate")
        add_grade(club_id=club.id, grade_system_id=gs1.id, name="White", order=1)
        karate_grade = add_grade(club_id=club.id, grade_system_id=gs2.id, name="10 kyu", order=1)
        sg = assign_student_grade(club_id=club.id, student_id=student.id, grade_system_id=gs1.id)
        with pytest.raises(BusinessLogicError):
            promote_student(
                club_id=club.id,
                student_grade_id=sg.id,
                new_grade_id=karate_grade.id,
            )

    @patch("apps.notifications.services.send_push_to_user")
    def test_parent_grade_up_respects_child_checkin_opt_out(self, mock_push):
        club = ClubFactory()
        parent_user = UserFactory()
        NotificationPreference.objects.create(
            user=parent_user,
            disabled_categories=["child_checkin"],
        )
        student = StudentFactory(
            club=club,
            is_child=True,
            parent_user=parent_user,
            first_name="Masha",
        )
        gs = create_grade_system(club_id=club.id, discipline="BJJ")
        white = add_grade(club_id=club.id, grade_system_id=gs.id, name="White", order=1)
        sg = assign_student_grade(club_id=club.id, student_id=student.id, grade_system_id=gs.id)

        promote_student(club_id=club.id, student_grade_id=sg.id, new_grade_id=white.id)

        mock_push.assert_not_called()
        assert not SentNotification.objects.filter(
            club=club,
            student=student,
            notification_type="parent_grade_up",
        ).exists()

    @patch("apps.notifications.services.send_push_to_user")
    def test_parent_grade_up_records_sent_notification(self, mock_push):
        club = ClubFactory()
        parent_user = UserFactory()
        student = StudentFactory(
            club=club,
            is_child=True,
            parent_user=parent_user,
            first_name="Masha",
        )
        gs = create_grade_system(club_id=club.id, discipline="BJJ")
        white = add_grade(club_id=club.id, grade_system_id=gs.id, name="White", order=1)
        sg = assign_student_grade(club_id=club.id, student_id=student.id, grade_system_id=gs.id)

        promote_student(club_id=club.id, student_grade_id=sg.id, new_grade_id=white.id)

        mock_push.assert_called_once()
        assert SentNotification.objects.filter(
            club=club,
            student=student,
            notification_type="parent_grade_up",
        ).exists()


@pytest.mark.django_db
class TestGradeProgress:
    def test_get_student_grade_progress(self):
        club = ClubFactory()
        student = StudentFactory(club=club)
        gs = create_grade_system(club_id=club.id, discipline="BJJ")
        white = add_grade(club_id=club.id, grade_system_id=gs.id, name="White", order=1, min_trainings=0)
        add_grade(club_id=club.id, grade_system_id=gs.id, name="Blue", order=2, min_trainings=100)
        sg = assign_student_grade(club_id=club.id, student_id=student.id, grade_system_id=gs.id)
        promote_student(club_id=club.id, student_grade_id=sg.id, new_grade_id=white.id)

        progress = get_student_grade_progress(club_id=club.id, student_id=student.id, grade_system_id=gs.id)
        assert progress["current_grade"]["name"] == "White"
        assert progress["trainings_since_last_grade"] == 0
        assert progress["next_grade"]["name"] == "Blue"
        assert progress["trainings_to_next"] == 100

    def test_increment_grade_progress(self):
        club = ClubFactory()
        student = StudentFactory(club=club)
        gs = create_grade_system(club_id=club.id, discipline="BJJ")
        assign_student_grade(club_id=club.id, student_id=student.id, grade_system_id=gs.id)
        increment_grade_progress(club_id=club.id, student_id=student.id, grade_system_id=gs.id)
        sg = StudentGrade.objects.get(student=student, grade_system=gs)
        assert sg.trainings_since_last_grade == 1

    def test_decrement_grade_progress(self):
        club = ClubFactory()
        student = StudentFactory(club=club)
        gs = create_grade_system(club_id=club.id, discipline="BJJ")
        sg = assign_student_grade(club_id=club.id, student_id=student.id, grade_system_id=gs.id)
        increment_grade_progress(club_id=club.id, student_id=student.id, grade_system_id=gs.id)
        decrement_grade_progress(club_id=club.id, student_id=student.id, grade_system_id=gs.id)
        sg.refresh_from_db()
        assert sg.trainings_since_last_grade == 0

        # Decrement below 0 should stay at 0
        decrement_grade_progress(club_id=club.id, student_id=student.id, grade_system_id=gs.id)
        sg.refresh_from_db()
        assert sg.trainings_since_last_grade == 0


@pytest.mark.django_db
class TestTenantIsolation:
    def test_grade_system_tenant_isolation(self):
        club_a = ClubFactory()
        club_b = ClubFactory()
        create_grade_system(club_id=club_a.id, discipline="BJJ")
        create_grade_system(club_id=club_b.id, discipline="BJJ")

        assert GradeSystem.objects.for_club(club_a).count() == 1
        assert GradeSystem.objects.for_club(club_b).count() == 1


@pytest.mark.django_db
class TestSeedGradeTemplates:
    def test_seed_grade_templates(self):
        club = ClubFactory()
        systems = seed_grade_templates(club_id=club.id, disciplines=["BJJ"])
        assert len(systems) == 1
        assert systems[0].discipline == "BJJ"
        grades = Grade.objects.filter(grade_system=systems[0]).order_by("order")
        assert grades.count() == 5
        assert grades.first().name == "Белый пояс"
        assert grades.last().name == "Чёрный пояс"
        assert grades.last().min_trainings == 500

    def test_seed_grade_templates_idempotent(self):
        club = ClubFactory()
        seed_grade_templates(club_id=club.id, disciplines=["BJJ"])
        systems = seed_grade_templates(club_id=club.id, disciplines=["BJJ"])
        assert len(systems) == 0
        assert GradeSystem.objects.for_club(club).count() == 1
