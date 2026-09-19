from django.db import models

from apps.common.models import TenantMixin


class GradeSystem(TenantMixin):
    discipline = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["club", "discipline"],
                name="unique_grade_system_per_club_discipline",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.discipline} ({self.club})"


class Grade(TenantMixin):
    grade_system = models.ForeignKey(GradeSystem, on_delete=models.PROTECT, related_name="grades")
    name = models.CharField(max_length=100)
    order = models.PositiveIntegerField()
    min_trainings = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order"]
        constraints = [
            models.UniqueConstraint(
                fields=["grade_system", "order"],
                name="unique_grade_order_per_system",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} (#{self.order})"


class StudentGrade(TenantMixin):
    student = models.ForeignKey("students.Student", on_delete=models.PROTECT, related_name="student_grades")
    grade_system = models.ForeignKey(GradeSystem, on_delete=models.PROTECT, related_name="student_grades")
    current_grade = models.ForeignKey(Grade, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    trainings_since_last_grade = models.PositiveIntegerField(default=0)
    last_counted_checkin_id = models.PositiveIntegerField(null=True, blank=True)
    promoted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["club", "student"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["student", "grade_system"],
                name="unique_student_per_grade_system",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.student} - {self.grade_system}"


class GradeProgressEvent(TenantMixin):
    student_grade = models.ForeignKey(
        StudentGrade,
        on_delete=models.PROTECT,
        related_name="progress_events",
    )
    checkin = models.ForeignKey(
        "attendance.Checkin",
        on_delete=models.PROTECT,
        related_name="grade_progress_events",
    )

    class Meta:
        indexes = [
            models.Index(fields=["club", "checkin"]),
            models.Index(fields=["club", "student_grade"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["club", "student_grade", "checkin"],
                name="unique_grade_progress_event_per_checkin",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.student_grade_id}:{self.checkin_id}"
