from django.db import models

from apps.common.models import SoftDeleteMixin, TenantMixin


class DocumentType(TenantMixin):
    class Scope(models.TextChoices):
        CHILDREN = "children", "Children only"
        ADULTS = "adults", "Adults only"
        ALL = "all", "All students"

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    is_required = models.BooleanField(default=True)
    scope = models.CharField(max_length=20, choices=Scope.choices, default=Scope.ALL)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["club", "name"],
                condition=models.Q(is_active=True),
                name="unique_active_doctype_name_per_club",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.scope})"


class StudentDocument(TenantMixin, SoftDeleteMixin):
    student = models.ForeignKey("students.Student", on_delete=models.PROTECT, related_name="documents")
    document_type = models.ForeignKey(DocumentType, on_delete=models.PROTECT, related_name="student_documents")
    is_provided = models.BooleanField(default=False)
    file = models.FileField(upload_to="documents/%Y/%m/", blank=True, default="")
    is_private = models.BooleanField(default=True)
    uploaded_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["club", "student", "document_type"],
                condition=models.Q(deleted_at__isnull=True),
                name="unique_student_document_per_type",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.student} - {self.document_type.name}"
