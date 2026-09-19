import logging

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.common.exceptions import BusinessLogicError
from apps.grades.models import Grade, GradeSystem, StudentGrade
from apps.students.models import Student

logger = logging.getLogger(__name__)

GRADE_TEMPLATES: dict[str, list[tuple[str, int, int]]] = {
    "Тайский бокс": [
        ("Котёнок", 0, 0),
        ("Дикий кот", 1, 20),
        ("Охотник", 2, 50),
        ("Боец", 3, 100),
        ("Хищник", 4, 170),
        ("Тигр", 5, 250),
        ("Ягуар", 6, 350),
        ("Чёрный Ягуар", 7, 500),
    ],
    "BJJ": [
        ("Белый пояс", 0, 0),
        ("Синий пояс", 1, 100),
        ("Фиолетовый пояс", 2, 200),
        ("Коричневый пояс", 3, 300),
        ("Чёрный пояс", 4, 500),
    ],
    "Каратэ": [
        ("10 кю", 0, 0),
        ("9 кю", 1, 30),
        ("8 кю", 2, 60),
        ("7 кю", 3, 90),
        ("6 кю", 4, 120),
        ("5 кю", 5, 150),
        ("4 кю", 6, 180),
        ("3 кю", 7, 210),
        ("2 кю", 8, 240),
        ("1 кю", 9, 270),
        ("1 дан", 10, 400),
    ],
    "Бокс": [
        ("Новичок", 0, 0),
        ("Начинающий", 1, 50),
        ("Любитель", 2, 150),
        ("Продвинутый", 3, 300),
    ],
}


def create_grade_system(*, club_id: int, discipline: str) -> GradeSystem:
    gs = GradeSystem.objects.create(club_id=club_id, discipline=discipline)
    logger.info("grade_system_created", extra={"grade_system_id": gs.id, "club_id": club_id})
    return gs


def add_grade(
    *,
    club_id: int,
    grade_system_id: int,
    name: str,
    order: int,
    min_trainings: int = 0,
) -> Grade:
    gs = GradeSystem.objects.for_club(club_id).get(id=grade_system_id)
    grade = Grade.objects.create(
        club_id=club_id,
        grade_system=gs,
        name=name,
        order=order,
        min_trainings=min_trainings,
    )
    logger.info("grade_added", extra={"grade_id": grade.id, "club_id": club_id})
    return grade


def assign_student_grade(
    *,
    club_id: int,
    student_id: int,
    grade_system_id: int,
    initial_grade_id: int | None = None,
) -> StudentGrade:
    gs = GradeSystem.objects.for_club(club_id).get(id=grade_system_id)
    Student.objects.for_club(club_id).get(id=student_id)
    if initial_grade_id is not None:
        if not Grade.objects.for_club(club_id).filter(id=initial_grade_id, grade_system=gs).exists():
            raise BusinessLogicError("Grade does not belong to the grade system", code="grade_system_mismatch")
    sg = StudentGrade.objects.create(
        club_id=club_id,
        student_id=student_id,
        grade_system=gs,
        current_grade_id=initial_grade_id,
    )
    logger.info(
        "student_grade_assigned",
        extra={"student_grade_id": sg.id, "student_id": student_id, "club_id": club_id},
    )
    return sg


def promote_student(*, club_id: int, student_grade_id: int, new_grade_id: int) -> StudentGrade:
    sg = (
        StudentGrade.objects.for_club(club_id)
        .select_related("club", "grade_system", "student")
        .get(id=student_grade_id)
    )
    new_grade = Grade.objects.for_club(club_id).get(id=new_grade_id)

    if new_grade.grade_system_id != sg.grade_system_id:
        raise BusinessLogicError(
            "Grade does not belong to the same grade system",
            code="grade_system_mismatch",
        )

    sg.current_grade = new_grade
    sg.trainings_since_last_grade = 0
    sg.promoted_at = timezone.now()
    sg.save(update_fields=["current_grade", "trainings_since_last_grade", "promoted_at", "updated_at"])

    logger.info(
        "student_promoted",
        extra={
            "student_grade_id": sg.id,
            "new_grade_id": new_grade_id,
            "club_id": club_id,
        },
    )

    # Send parent push if child has parent
    student = sg.student
    if student.is_child and student.parent_user_id:
        try:
            from apps.notifications.routes import parent_child_url
            from apps.notifications.services import send_parent_notification

            send_parent_notification(
                club=sg.club,
                student=student,
                notification_type="parent_grade_up",
                context={"name": student.first_name, "grade": new_grade.name},
                fallback_title=f"{student.first_name} — новый грейд!",
                fallback_body=f"{student.first_name} получил(а) {new_grade.name}!",
                url=parent_child_url(student.id),
            )
        except Exception:
            logger.warning("grade_promotion_push_failed", extra={"student_id": student.id})

    return sg


def increment_grade_progress(*, club_id: int, student_id: int, grade_system_id: int) -> None:
    StudentGrade.objects.for_club(club_id).filter(student_id=student_id, grade_system_id=grade_system_id).update(
        trainings_since_last_grade=F("trainings_since_last_grade") + 1
    )


def decrement_grade_progress(*, club_id: int, student_id: int, grade_system_id: int) -> None:
    # Only decrement if > 0
    StudentGrade.objects.for_club(club_id).filter(
        student_id=student_id,
        grade_system_id=grade_system_id,
        trainings_since_last_grade__gt=0,
    ).update(trainings_since_last_grade=F("trainings_since_last_grade") - 1)


def update_grade(*, grade_id: int, club_id: int, name: str, order: int, min_trainings: int = 0) -> Grade:
    grade = Grade.objects.for_club(club_id).filter(id=grade_id).first()
    if not grade:
        raise BusinessLogicError("Грейд не найден", code="grade_not_found")
    grade.name = name
    grade.order = order
    grade.min_trainings = min_trainings
    grade.save(update_fields=["name", "order", "min_trainings", "updated_at"])
    logger.info("grade_updated", extra={"grade_id": grade.id, "club_id": club_id})
    return grade


def delete_grade(*, grade_id: int, club_id: int) -> None:
    grade = Grade.objects.for_club(club_id).filter(id=grade_id).first()
    if not grade:
        raise BusinessLogicError("Грейд не найден", code="grade_not_found")
    student_count = StudentGrade.objects.for_club(club_id).filter(current_grade=grade).count()
    if student_count > 0:
        raise BusinessLogicError(
            f"Нельзя удалить: {student_count} учеников на этом грейде",
            code="grade_in_use",
        )
    grade.delete()
    logger.info("grade_deleted", extra={"grade_id": grade_id, "club_id": club_id})


def delete_grade_system(*, grade_system_id: int, club_id: int) -> None:
    gs = GradeSystem.objects.for_club(club_id).filter(id=grade_system_id).first()
    if not gs:
        raise BusinessLogicError("Система аттестации не найдена", code="grade_system_not_found")
    student_count = StudentGrade.objects.for_club(club_id).filter(grade_system=gs).count()
    if student_count > 0:
        raise BusinessLogicError(
            f"Нельзя удалить: {student_count} учеников привязаны к этой системе",
            code="grade_system_in_use",
        )
    with transaction.atomic():
        # Explicit tenant filter on bulk grades delete (defense in depth)
        Grade.objects.for_club(club_id).filter(grade_system=gs).delete()
        gs.delete()
    logger.info("grade_system_deleted", extra={"grade_system_id": grade_system_id, "club_id": club_id})


def seed_grade_templates(*, club_id: int, disciplines: list[str]) -> list[GradeSystem]:
    created_systems: list[GradeSystem] = []
    for discipline in disciplines:
        template = GRADE_TEMPLATES.get(discipline)
        if template is None:
            raise BusinessLogicError(
                f"Unknown discipline template: {discipline}",
                code="unknown_discipline",
            )
        if GradeSystem.objects.for_club(club_id).filter(discipline=discipline).exists():
            continue
        gs = GradeSystem.objects.create(club_id=club_id, discipline=discipline)
        grades = [
            Grade(
                club_id=club_id,
                grade_system=gs,
                name=name,
                order=order,
                min_trainings=min_trainings,
            )
            for name, order, min_trainings in template
        ]
        Grade.objects.bulk_create(grades)
        created_systems.append(gs)
        logger.info(
            "grade_template_seeded",
            extra={
                "discipline": discipline,
                "grades_count": len(grades),
                "club_id": club_id,
            },
        )
    return created_systems
