from django.db.models import QuerySet

from apps.grades.models import Grade, GradeSystem, StudentGrade


def get_grade_systems(*, club) -> QuerySet[GradeSystem]:
    return GradeSystem.objects.for_club(club).filter(is_active=True)


def get_grades_for_system(*, club, grade_system_id: int) -> QuerySet[Grade]:
    return Grade.objects.for_club(club).filter(grade_system_id=grade_system_id).order_by("order")


def _get_grades_by_system(*, club, grade_system_id: int) -> list[Grade]:
    """Load all grades for a system in one query. Typically 5-10 belts."""
    return list(Grade.objects.for_club(club).filter(grade_system_id=grade_system_id).order_by("order"))


def _find_next_grade(*, grades: list[Grade], current_order: int | None) -> Grade | None:
    """Find next grade after current_order from pre-loaded grades list."""
    for g in grades:
        if current_order is None or g.order > current_order:
            return g
    return None


def get_student_grade_progress(*, club_id: int, student_id: int, grade_system_id: int) -> dict:
    sg = (
        StudentGrade.objects.for_club(club_id)
        .select_related("current_grade", "grade_system")
        .get(student_id=student_id, grade_system_id=grade_system_id)
    )

    grades = _get_grades_by_system(club=club_id, grade_system_id=grade_system_id)
    return _build_grade_progress(sg=sg, grades=grades)


def _build_grade_progress(*, sg: StudentGrade, grades: list[Grade]) -> dict:
    """Build grade progress dict from StudentGrade and pre-loaded grades."""
    current = sg.current_grade
    current_data = None
    next_data = None
    trainings_to_next = None

    current_order = current.order if current else None
    next_grade = _find_next_grade(grades=grades, current_order=current_order)

    if current:
        current_data = {
            "id": current.id,
            "name": current.name,
            "order": current.order,
            "min_trainings": current.min_trainings,
        }

    if next_grade:
        next_data = {
            "id": next_grade.id,
            "name": next_grade.name,
            "order": next_grade.order,
            "min_trainings": next_grade.min_trainings,
        }
        trainings_to_next = next_grade.min_trainings - sg.trainings_since_last_grade

    return {
        "student_grade_id": sg.id,
        "grade_system_id": sg.grade_system_id,
        "grade_system_name": sg.grade_system.discipline if sg.grade_system else None,
        "current_grade": current_data,
        "trainings_since_last_grade": sg.trainings_since_last_grade,
        "next_grade": next_data,
        "trainings_to_next": trainings_to_next,
    }


def get_student_all_grades(*, club_id: int, student_id: int) -> list[dict]:
    student_grades = list(
        StudentGrade.objects.for_club(club_id)
        .filter(student_id=student_id)
        .order_by("grade_system__discipline", "grade_system_id", "id")
        .select_related("current_grade", "grade_system")
    )
    # Pre-load grades for all relevant grade systems in bulk
    gs_ids = {sg.grade_system_id for sg in student_grades}
    grades_by_system: dict[int, list[Grade]] = {}
    for gs_id in gs_ids:
        grades_by_system[gs_id] = _get_grades_by_system(club=club_id, grade_system_id=gs_id)

    return [_build_grade_progress(sg=sg, grades=grades_by_system[sg.grade_system_id]) for sg in student_grades]


def get_ready_for_promotion(*, club, grade_system_id: int) -> QuerySet[StudentGrade]:
    """Students where trainings_since_last_grade >= next grade's min_trainings."""
    student_grades = (
        StudentGrade.objects.for_club(club).filter(grade_system_id=grade_system_id).select_related("current_grade")
    )

    # Pre-load all grades for this system (typically 5-10 belts)
    grades = _get_grades_by_system(club=club, grade_system_id=grade_system_id)

    ready_ids = []
    for sg in student_grades:
        current_order = sg.current_grade.order if sg.current_grade else None
        next_grade = _find_next_grade(grades=grades, current_order=current_order)
        if next_grade and sg.trainings_since_last_grade >= next_grade.min_trainings:
            ready_ids.append(sg.id)

    return (
        StudentGrade.objects.for_club(club)
        .filter(id__in=ready_ids)
        .select_related("student", "current_grade", "grade_system")
    )
