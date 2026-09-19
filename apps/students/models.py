import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q

from apps.clubs.models import ClubMembership
from apps.common.managers import TenantManager, TenantQuerySet
from apps.common.models import SoftDeleteMixin, TenantMixin

phone_validator = RegexValidator(
    regex=r"^\+?\d{10,15}$",
    message="Введите корректный номер телефона (10-15 цифр, можно с +)",
)


class OpeningImportBatch(TenantMixin):
    """Private mutable draft; accepted evidence lives in separate append-only rows."""

    revision = models.PositiveIntegerField(default=1)
    source_namespace = models.CharField(max_length=120)
    status = models.CharField(max_length=24, default="draft")
    source_file = models.CharField(max_length=80, blank=True)
    prepared_file = models.CharField(max_length=80, blank=True)
    expires_at = models.DateTimeField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    accepted_preview = models.ForeignKey(
        "OpeningImportPreview", null=True, blank=True, on_delete=models.PROTECT, related_name="+",
    )
    apply_actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
    )
    apply_channel = models.CharField(max_length=24, blank=True)
    expected_effects = models.JSONField(default=dict)
    heartbeat_at = models.DateTimeField(null=True, blank=True)


class OpeningImportItem(TenantMixin):
    class Kind(models.TextChoices):
        ENTITLEMENT = "entitlement", "Абонемент"
        SETTLEMENT_OPENING = "settlement_opening", "Начальный расчёт"
        SETTLEMENT_PAYOUT = "settlement_payout", "Выплата"

    batch = models.ForeignKey(OpeningImportBatch, on_delete=models.PROTECT, related_name="items")
    kind = models.CharField(max_length=24, choices=Kind.choices)
    source_key = models.CharField(max_length=120)
    source_row = models.PositiveIntegerField()
    source_sheet = models.CharField(max_length=40)
    source_data = models.JSONField(default=dict)
    normalized_data = models.JSONField(default=dict)
    status = models.CharField(max_length=24, default="draft")
    errors = models.JSONField(default=list)
    result_receipt = models.ForeignKey(
        "OpeningImportItemReceipt", null=True, blank=True, on_delete=models.PROTECT, related_name="result_items",
    )


class _OpeningImportEvidenceQuerySet(TenantQuerySet):
    def update(self, **kwargs):
        raise ValidationError("Accepted import evidence is immutable.")

    def delete(self):
        raise ValidationError("Accepted import evidence is immutable.")


class _OpeningImportEvidence(TenantMixin):
    objects = TenantManager.from_queryset(_OpeningImportEvidenceQuerySet)()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Accepted import evidence is immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Accepted import evidence is immutable.")


class OpeningImportPreview(_OpeningImportEvidence):
    batch = models.ForeignKey(OpeningImportBatch, on_delete=models.PROTECT, related_name="previews")
    revision = models.PositiveIntegerField()
    selection_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    selection = models.JSONField(default=list)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    expires_at = models.DateTimeField()


class OpeningImportItemReceipt(_OpeningImportEvidence):
    item = models.OneToOneField(OpeningImportItem, on_delete=models.PROTECT, related_name="receipt")
    source_namespace = models.CharField(max_length=120)
    kind = models.CharField(max_length=24, choices=OpeningImportItem.Kind.choices)
    source_key = models.CharField(max_length=120)
    payload_fingerprint = models.CharField(max_length=64)
    domain_result = models.JSONField(default=dict)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    channel = models.CharField(max_length=24)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["club", "source_namespace", "kind", "source_key"],
            name="unique_opening_import_source_receipt",
        )]


class Student(TenantMixin, SoftDeleteMixin):
    class CrmEntryKind(models.TextChoices):
        LEGACY_UNKNOWN = "legacy_unknown", "Legacy unknown"
        LEAD_INTAKE = "lead_intake", "Lead intake"
        EXISTING_STUDENT = "existing_student", "Existing student"

    class Status(models.TextChoices):
        LEAD = "lead", "Лид"
        TRIAL = "trial", "Пробное"
        ACTIVE = "active", "Активен"
        AT_RISK = "at_risk", "В риске"
        CHURNED = "churned", "Ушёл"
        LOST = "lost", "Потерян"

    class Source(models.TextChoices):
        RECOMMENDATION = "recommendation", "Рекомендация"
        INSTAGRAM = "instagram", "Instagram"
        VK = "vk", "ВКонтакте"
        SIGNBOARD = "signboard", "Вывеска"
        WEBSITE = "website", "Сайт"
        OTHER = "other", "Другое"

    class LeadStatus(models.TextChoices):
        NEW = "new", "Новый"
        CONTACTED = "contacted", "Связались"
        TRIAL_BOOKED = "trial_booked", "Пробное назначено"
        TRIAL_DONE = "trial_done", "Пробное проведено"
        THINKING = "thinking", "Думает"

    class LossReason(models.TextChoices):
        EXPENSIVE = "expensive", "Дорого"
        DIDNT_LIKE_TRAINING = "didnt_like_training", "Не понравились тренировки"
        DIDNT_LIKE_TRAINER = "didnt_like_trainer", "Не понравился тренер"
        TOO_FAR = "too_far", "Далеко"
        NO_TIME = "no_time", "Нет времени"
        CHOSE_OTHER_CLUB = "chose_other_club", "Выбрал другой клуб"
        INJURY_HEALTH = "injury_health", "Травма / здоровье"
        CHILD_DIDNT_LIKE = "child_didnt_like", "Ребёнку не понравилось"
        CHANGED_MIND = "changed_mind", "Передумал"
        OTHER = "other", "Другое"

    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100, blank=True)
    phone = models.CharField(max_length=20, blank=True, default="", validators=[phone_validator])
    guardian_phone = models.CharField(max_length=20, blank=True, default="", validators=[phone_validator])
    email = models.EmailField(blank=True, default="")
    date_of_birth = models.DateField(null=True, blank=True)
    is_child = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.LEAD)
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.OTHER)
    contraindications = models.TextField(blank=True, default="")
    last_visit_date = models.DateField(null=True, blank=True)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="student_profile",
    )
    parent_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="children_students",
    )
    lead_status = models.CharField(
        max_length=20, choices=LeadStatus.choices, null=True, blank=True, db_index=True
    )
    loss_reason = models.CharField(
        max_length=30, choices=LossReason.choices, null=True, blank=True
    )
    assigned_trainer = models.ForeignKey(
        "trainers.Trainer",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_leads",
    )
    trial_date = models.DateTimeField(null=True, blank=True)
    crm_entry_kind = models.CharField(
        max_length=32,
        choices=CrmEntryKind.choices,
        default=CrmEntryKind.LEGACY_UNKNOWN,
        db_index=True,
    )
    crm_entered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="crm_entered_students",
    )
    became_student_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["club", "status"]),
            models.Index(fields=["club", "phone"]),
            models.Index(fields=["club", "guardian_phone"]),
            models.Index(fields=["club", "lead_status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["club", "phone"],
                condition=Q(deleted_at__isnull=True) & ~Q(phone=""),
                name="unique_student_phone_per_club",
            ),
            models.CheckConstraint(
                condition=Q(is_child=True) | ~Q(phone=""),
                name="student_adult_requires_phone",
            ),
            models.CheckConstraint(
                condition=Q(is_child=False) | ~Q(phone="") | ~Q(guardian_phone=""),
                name="student_child_requires_contact",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(status="lead") | Q(lead_status__isnull=False) | Q(became_student_at__isnull=False)
                ),
                name="student_lead_requires_lead_status",
            ),
        ]

    def __str__(self):
        return f"{self.first_name} {self.last_name}"


class _AppendOnlyStudentIntakeCommandQuerySet(TenantQuerySet):
    def update(self, **kwargs):
        raise ValidationError("Student intake commands are append-only.")

    def delete(self):
        raise ValidationError("Student intake commands are append-only.")


class _AppendOnlyStudentIntakeCommandManager(TenantManager):
    def get_queryset(self):
        return _AppendOnlyStudentIntakeCommandQuerySet(self.model, using=self._db)

    def unscoped(self):
        return self.get_queryset()


class StudentIntakeCommand(TenantMixin):
    """Append-only receipt for one staff intake request.

    The immutable receipt deliberately contains only API-safe outcome fields.
    Identity inputs are represented by a salted fingerprint rather than raw
    contact values.
    """

    class IntakeKind(models.TextChoices):
        NEW_CONTACT = "new_contact", "New contact"
        EXISTING_STUDENT = "existing_student", "Existing student"

    idempotency_key = models.UUIDField()
    request_fingerprint = models.CharField(max_length=64)
    student = models.ForeignKey(
        Student,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="intake_commands",
    )
    intake_kind = models.CharField(max_length=32, choices=IntakeKind.choices)
    result_kind = models.CharField(max_length=64)
    result_receipt = models.JSONField(default=dict)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="student_intake_commands",
    )
    objects = _AppendOnlyStudentIntakeCommandManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["club", "idempotency_key"],
                name="unique_student_intake_command_key_per_club",
            ),
        ]
        indexes = [
            models.Index(
                fields=["club", "created_at"],
                name="students_si_club_id_3e302f_idx",
            ),
            models.Index(
                fields=["club", "student", "created_at"],
                name="students_si_club_id_6969d5_idx",
            ),
        ]
        ordering = ["created_at", "id"]

    def __str__(self) -> str:
        return f"StudentIntakeCommand(id={self.id}, club_id={self.club_id})"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Student intake commands are append-only.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Student intake commands are append-only.")


class StudentProvenanceBackfillReceipt(TenantMixin):
    """Append-only, PII-free evidence selected by the D1 expansion migration."""

    class EvidenceType(models.TextChoices):
        CONFIRMED_PAYMENT = "confirmed_payment", "Confirmed payment"
        SUBSCRIPTION = "subscription", "Subscription"
        CANONICAL_MEMBERSHIP = "canonical_membership", "Canonical membership"
        QUALIFYING_CHECKIN = "qualifying_checkin", "Qualifying check-in"

    student = models.OneToOneField(
        Student,
        on_delete=models.PROTECT,
        related_name="provenance_backfill_receipt",
    )
    evidence_type = models.CharField(max_length=32, choices=EvidenceType.choices)
    evidence_id = models.PositiveBigIntegerField()
    became_student_at = models.DateTimeField()
    objects = _AppendOnlyStudentIntakeCommandManager()

    class Meta:
        indexes = [
            models.Index(
                fields=["club", "evidence_type"],
                name="students_sp_club_id_a3c3c8_idx",
            ),
            models.Index(
                fields=["club", "created_at"],
                name="students_sp_club_id_35c97f_idx",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Student provenance backfill receipts are append-only.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Student provenance backfill receipts are append-only.")


class StudentNote(TenantMixin):
    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="notes")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="student_notes")
    text = models.TextField()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Note for {self.student} by {self.author}"


class ParentInvite(TenantMixin):
    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="parent_invites")
    token = models.UUIDField(default=uuid.uuid4, unique=True, db_index=True)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="accepted_invites",
    )

    def __str__(self):
        return f"Invite for {self.student} (id={self.id})"


class AccountAccess(TenantMixin):
    class Role(models.TextChoices):
        STUDENT = ClubMembership.Role.STUDENT, "Student"
        PARENT = ClubMembership.Role.PARENT, "Parent"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        RESET = "reset", "Reset"

    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="account_accesses")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="student_account_accesses",
    )
    role = models.CharField(max_length=20, choices=Role.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="issued_student_account_accesses",
    )
    issued_at = models.DateTimeField(auto_now_add=True)
    reset_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reset_student_account_accesses",
    )
    reset_at = models.DateTimeField(null=True, blank=True)
    must_change_password = models.BooleanField(default=True)
    temporary_credential_revealed_at = models.DateTimeField(null=True, blank=True)
    setup_token_hash = models.CharField(max_length=128, blank=True, default="")

    class Meta:
        indexes = [
            models.Index(fields=["club", "user", "role"]),
            models.Index(fields=["club", "student", "role"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["club", "student", "role"],
                name="unique_account_access_student_role_per_club",
            ),
        ]

    def __str__(self):
        return f"AccountAccess student_id={self.student_id} user_id={self.user_id} role={self.role}"
