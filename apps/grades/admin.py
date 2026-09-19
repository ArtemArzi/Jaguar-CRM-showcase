from django.contrib import admin

from apps.grades.models import Grade, GradeSystem, StudentGrade


@admin.register(GradeSystem)
class GradeSystemAdmin(admin.ModelAdmin):
    list_display = ("discipline", "club", "is_active")
    list_filter = ("is_active",)


@admin.register(Grade)
class GradeAdmin(admin.ModelAdmin):
    list_display = ("name", "grade_system", "order", "min_trainings")
    list_filter = ("grade_system",)


@admin.register(StudentGrade)
class StudentGradeAdmin(admin.ModelAdmin):
    list_display = ("student", "grade_system", "current_grade", "trainings_since_last_grade")
