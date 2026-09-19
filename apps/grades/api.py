from ninja import Router

from apps.clubs.models import ClubMembership
from apps.common.permissions import role_required
from apps.grades.schemas import (
    AssignGradeIn,
    GradeIn,
    GradeOut,
    GradeProgressOut,
    GradeSystemIn,
    GradeSystemOut,
    PromoteIn,
    SeedGradeTemplatesIn,
    StudentGradeOut,
)
from apps.grades.selectors import (
    get_grade_systems,
    get_grades_for_system,
    get_ready_for_promotion,
    get_student_all_grades,
)
from apps.grades.services import (
    add_grade,
    assign_student_grade,
    create_grade_system,
    promote_student,
    seed_grade_templates,
)
from apps.students.models import Student
from apps.students.scopes import (
    actor_can_manage_student_sensitive_actions,
    assert_actor_is_scoped_to_student,
    get_current_trainer_id_for_user,
    trainer_student_scope_filter,
)

router = Router(tags=["grades"])


def _assert_trainer_grade_read_student_scope(request, *, student_id: int) -> None:
    assert_actor_is_scoped_to_student(
        club=request.club,
        membership_role=request._membership.role,
        user=request.user,
        student_id=student_id,
    )


def _assert_trainer_grade_management_student_scope(request, *, student_id: int) -> None:
    if not actor_can_manage_student_sensitive_actions(
        club=request.club,
        membership_role=request._membership.role,
        user=request.user,
        student_id=student_id,
    ):
        from ninja.errors import HttpError

        raise HttpError(403, "Access denied: not your student")


@router.post("/systems/", response={201: GradeSystemOut})
@role_required("owner", "admin")
def create_grade_system_endpoint(request, payload: GradeSystemIn):
    gs = create_grade_system(club_id=request.club.id, discipline=payload.discipline)
    return 201, gs


@router.get("/systems/", response=list[GradeSystemOut])
@role_required("owner", "admin", "trainer")
def list_grade_systems(request):
    return list(get_grade_systems(club=request.club))


@router.post("/systems/{system_id}/grades/", response={201: GradeOut})
@role_required("owner", "admin")
def add_grade_endpoint(request, system_id: int, payload: GradeIn):
    grade = add_grade(
        club_id=request.club.id,
        grade_system_id=system_id,
        name=payload.name,
        order=payload.order,
        min_trainings=payload.min_trainings,
    )
    return 201, grade


@router.get("/systems/{system_id}/grades/", response=list[GradeOut])
@role_required("owner", "admin", "trainer")
def list_grades(request, system_id: int):
    return list(get_grades_for_system(club=request.club, grade_system_id=system_id))


@router.post("/student-grades/", response={201: StudentGradeOut})
@role_required("owner", "admin", "trainer")
def assign_student_grade_endpoint(request, payload: AssignGradeIn):
    _assert_trainer_grade_management_student_scope(request, student_id=payload.student_id)
    sg = assign_student_grade(
        club_id=request.club.id,
        student_id=payload.student_id,
        grade_system_id=payload.grade_system_id,
        initial_grade_id=payload.initial_grade_id,
    )
    return 201, sg


@router.delete("/student-grades/{student_grade_id}/", response={204: None})
@role_required("owner", "admin", "trainer")
def unassign_student_grade(request, student_grade_id: int):
    from apps.grades.models import StudentGrade
    sg = StudentGrade.objects.for_club(request.club).get(id=student_grade_id)
    _assert_trainer_grade_management_student_scope(request, student_id=sg.student_id)
    sg.delete()
    return 204, None


@router.post("/student-grades/{student_grade_id}/promote/", response=StudentGradeOut)
@role_required("owner", "admin", "trainer")
def promote_student_endpoint(request, student_grade_id: int, payload: PromoteIn):
    from apps.grades.models import StudentGrade

    student_grade = StudentGrade.objects.for_club(request.club).get(id=student_grade_id)
    _assert_trainer_grade_management_student_scope(request, student_id=student_grade.student_id)
    sg = promote_student(
        club_id=request.club.id,
        student_grade_id=student_grade_id,
        new_grade_id=payload.new_grade_id,
    )
    return sg


@router.get("/my-progress/", response=list[GradeProgressOut])
@role_required("student")
def my_progress(request):
    from apps.students.selectors import get_student_by_user

    student = get_student_by_user(club=request.club, user_id=request.user.id)
    return get_student_all_grades(club_id=request.club.id, student_id=student.id)


@router.get("/students/{student_id}/progress/", response=list[GradeProgressOut])
@role_required("owner", "admin", "trainer", "student")
def student_progress(request, student_id: int):
    if request._membership.role == "student":
        from apps.students.selectors import get_student_by_user

        my_student = get_student_by_user(club=request.club, user_id=request.user.id)
        if my_student.id != student_id:
            from ninja.errors import HttpError

            raise HttpError(403, "Access denied")
    else:
        _assert_trainer_grade_read_student_scope(request, student_id=student_id)
    return get_student_all_grades(club_id=request.club.id, student_id=student_id)


@router.get("/systems/{system_id}/ready-for-promotion/", response=list[StudentGradeOut])
@role_required("owner", "admin", "trainer")
def ready_for_promotion(request, system_id: int):
    ready = get_ready_for_promotion(club=request.club, grade_system_id=system_id)
    if request._membership.role == ClubMembership.Role.TRAINER:
        trainer_id = get_current_trainer_id_for_user(club=request.club, user=request.user)
        scoped_student_ids = (
            Student.objects.for_club(request.club)
            .filter(trainer_student_scope_filter(trainer_id), deleted_at__isnull=True)
            .values("id")
        )
        ready = ready.filter(student_id__in=scoped_student_ids).distinct()
    return list(ready)


@router.post("/seed-templates/", response={201: list[GradeSystemOut]})
@role_required("owner", "admin")
def seed_templates_endpoint(request, payload: SeedGradeTemplatesIn):
    systems = seed_grade_templates(club_id=request.club.id, disciplines=payload.disciplines)
    return 201, systems
